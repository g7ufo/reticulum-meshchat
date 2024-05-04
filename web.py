#!/usr/bin/env python

import argparse
import json
import os
import signal
import time
from datetime import datetime, timezone
from typing import Callable, List

import RNS
import LXMF
from aiohttp import web, WSMessage, WSMsgType, WSCloseCode
import asyncio
import base64

from peewee import SqliteDatabase

import database


class ReticulumWebChat:

    def __init__(self, reticulum_config_dir, identity: RNS.Identity):

        # default values before loading config
        self.display_name = "Anonymous Peer"

        # create storage path based on identity being used
        # ./storage/identities/<identity_hex>/
        # ./storage/identities/<identity_hex>/config.json
        # ./storage/identities/<identity_hex>/database.db
        # ./storage/identities/<identity_hex>/lxmf
        storage_path = os.path.join("storage", "identities", identity.hash.hex())
        print("Using Storage Path: {}".format(storage_path))
        os.makedirs(storage_path, exist_ok=True)

        # define path to files based on storage path
        config_path = os.path.join(storage_path, "config.json")
        database_path = os.path.join(storage_path, "database.db")
        lxmf_router_path = os.path.join(storage_path, "lxmf_router")

        # load config
        self.config_file = config_path
        self.load_config()

        # init database
        database.database.initialize(SqliteDatabase(database_path))
        self.db = database.database
        self.db.connect()
        self.db.create_tables([
            database.LxmfMessage,
        ])

        # init reticulum
        self.reticulum = RNS.Reticulum(reticulum_config_dir)
        self.identity = identity

        # init lxmf router
        self.message_router = LXMF.LXMRouter(identity=self.identity, storagepath=lxmf_router_path)

        # register lxmf identity
        self.local_lxmf_destination = self.message_router.register_delivery_identity(self.identity)

        # set a callback for when an lxmf message is received
        self.message_router.register_delivery_callback(self.on_lxmf_delivery)

        # set a callback for when an lxmf announce is received
        RNS.Transport.register_announce_handler(LXMFAnnounceHandler(self.on_lxmf_announce_received))

        # remember websocket clients
        self.websocket_clients: List[web.WebSocketResponse] = []

    def load_config(self):

        # default config
        config = {

        }

        # attempt to load config and override default values
        try:
            with open(self.config_file, 'r') as f:
                custom_config = json.load(f)
                config |= custom_config

        # config is broken, fallback to defaults
        except:
            print("failed to load config, defaults will be used")

        # update display name from config
        if "display_name" in config:
            self.display_name = config["display_name"]

        # return loaded config
        return config

    def save_config(self):

        # build config
        config = {
            "display_name": self.display_name,
        }

        # attempt to save config
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)

        # config is broken, fallback to defaults
        except:
            print("failed to save config")

    # web server has shutdown, likely ctrl+c, but if we don't do the following, the script never exits
    async def shutdown(self, app):

        # force close websocket clients
        for websocket_client in self.websocket_clients:
            print("force closing websocket for shutdown")
            await websocket_client.close(code=WSCloseCode.GOING_AWAY)
            print("force closed websocket")

        # stop reticulum
        print("stopping reticulum")
        RNS.Transport.detach_interfaces()
        self.reticulum.exit_handler()
        RNS.exit()


    def run(self, host, port):

        # create route table
        routes = web.RouteTableDef()

        # serve index.html
        @routes.get("/")
        async def index(request):
            return web.FileResponse(path="public/index.html")

        # handle websocket clients
        @routes.get("/ws")
        async def ws(request):

            # prepare websocket response
            websocket_response = web.WebSocketResponse()
            await websocket_response.prepare(request)

            # add client to connected clients list
            self.websocket_clients.append(websocket_response)

            # send config to all clients
            await self.send_config_to_websocket_clients()

            # send known peers to all clients
            await self.send_known_peers_to_websocket_clients()

            # handle websocket messages until disconnected
            async for msg in websocket_response:
                msg: WSMessage = msg
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self.on_websocket_data_received(websocket_response, data)
                    except Exception as e:
                        # ignore errors while handling message
                        print("failed to process client message")
                        print(e)
                elif msg.type == WSMsgType.ERROR:
                    # ignore errors while handling message
                    print('ws connection error %s' % websocket_response.exception())

            # websocket closed
            self.websocket_clients.remove(websocket_response)

            return websocket_response

        # serve lxmf messages
        @routes.get("/api/v1/lxmf-messages")
        async def index(request):

            # get query params
            source_hash = request.query.get("source_hash", None)
            destination_hash = request.query.get("destination_hash", None)

            # source_hash is required
            if source_hash is None:
                return web.json_response({
                    "message": "source_hash is required",
                }, status=422)

            # destination_hash is required
            if destination_hash is None:
                return web.json_response({
                    "message": "destination_hash is required",
                }, status=422)

            # get lxmf messages from db where "source to destination" or "destination to source" and ordered by oldest to newest
            db_lxmf_messages = (database.LxmfMessage.select()
                                .where((database.LxmfMessage.source_hash == source_hash) & (database.LxmfMessage.destination_hash == destination_hash))
                                .orwhere((database.LxmfMessage.destination_hash == source_hash) & (database.LxmfMessage.source_hash == destination_hash))
                                .order_by(database.LxmfMessage.id.asc())
                                )

            # convert to response json
            lxmf_messages = []
            for db_lxmf_message in db_lxmf_messages:
                lxmf_messages.append({
                    "id": db_lxmf_message.id,
                    "hash": db_lxmf_message.hash,
                    "source_hash": db_lxmf_message.source_hash,
                    "destination_hash": db_lxmf_message.destination_hash,
                    "is_incoming": db_lxmf_message.is_incoming,
                    "state": db_lxmf_message.state,
                    "progress": db_lxmf_message.progress,
                    "title": db_lxmf_message.title,
                    "content": db_lxmf_message.content,
                    "fields": json.loads(db_lxmf_message.fields),
                    "timestamp": db_lxmf_message.timestamp,
                    "created_at": db_lxmf_message.created_at.replace(tzinfo=timezone.utc).isoformat(),
                    "updated_at": db_lxmf_message.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                })

            return web.json_response({
                "lxmf_messages": lxmf_messages,
            })

        asyncio.get_event_loop().add_signal_handler(signal.SIGINT, lambda: exit(-1))
        asyncio.get_event_loop().add_signal_handler(signal.SIGTERM, lambda: exit(-1))

        # create and run web app
        app = web.Application()
        app.add_routes(routes)
        app.add_routes([web.static('/', "public")])  # serve anything in public folder
        app.on_shutdown.append(self.shutdown)  # need to force close websockets and stop reticulum now
        web.run_app(app, host=host, port=port)

    # handle data received from websocket client
    async def on_websocket_data_received(self, client, data):

        # get type from client data
        _type = data["type"]

        # handle updating config
        if _type == "config.set":

            # send lxmf message to destination
            config = data["config"]

            # update display name in state
            if "display_name" in config and config["display_name"] != "":
                self.display_name = config["display_name"]
                print("updated display name to: " + self.display_name)

            # save config
            self.save_config()

            # send config to websocket clients
            await self.send_config_to_websocket_clients()

        # handle sending an lxmf message
        elif _type == "lxmf.delivery":

            # send lxmf message to destination
            destination_hash = data["destination_hash"]
            message = data["message"]
            await self.send_message(destination_hash, message)

            # # TODO: send response to client when marked as delivered?
            # await client.send(json.dumps({
            #     "type": "lxmf.sent",
            # }))

        # handle sending an announce
        elif _type == "announce":

            # send announce for lxmf
            self.local_lxmf_destination.announce(app_data=self.display_name.encode("utf-8"))

        # handle downloading a file from a nomadnet node
        elif _type == "nomadnet.file.download":

            # get data from websocket client
            destination_hash = data["nomadnet_file_download"]["destination_hash"]
            file_path = data["nomadnet_file_download"]["file_path"]

            # convert destination hash to bytes
            destination_hash = bytes.fromhex(destination_hash)

            # handle successful file download
            def on_file_download_success(file_name, file_bytes):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.file.download",
                    "nomadnet_file_download": {
                        "status": "success",
                        "destination_hash": destination_hash.hex(),
                        "file_path": file_path,
                        "file_name": file_name,
                        "file_bytes": base64.b64encode(file_bytes).decode("utf-8"),
                    },
                })))

            # handle file download failure
            def on_file_download_failure(failure_reason):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.file.download",
                    "nomadnet_file_download": {
                        "status": "error",
                        "failure_reason": failure_reason,
                        "destination_hash": destination_hash.hex(),
                        "file_path": file_path,
                    },
                })))

            # handle file download progress
            def on_file_download_progress(progress):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.file.download",
                    "nomadnet_file_download": {
                        "status": "progress",
                        "progress": progress,
                        "destination_hash": destination_hash.hex(),
                        "file_path": file_path,
                    },
                })))

            # todo: handle file download progress

            # download the file
            NomadnetFileDownloader(destination_hash, file_path, on_file_download_success, on_file_download_failure, on_file_download_progress)

        # handle downloading a page from a nomadnet node
        elif _type == "nomadnet.page.download":

            # get data from websocket client
            destination_hash = data["nomadnet_page_download"]["destination_hash"]
            page_path = data["nomadnet_page_download"]["page_path"]

            # convert destination hash to bytes
            destination_hash = bytes.fromhex(destination_hash)

            # handle successful page download
            def on_page_download_success(page_content):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.page.download",
                    "nomadnet_page_download": {
                        "status": "success",
                        "destination_hash": destination_hash.hex(),
                        "page_path": page_path,
                        "page_content": page_content,
                    },
                })))

            # handle page download failure
            def on_page_download_failure(failure_reason):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.page.download",
                    "nomadnet_page_download": {
                        "status": "error",
                        "failure_reason": failure_reason,
                        "destination_hash": destination_hash.hex(),
                        "page_path": page_path,
                    },
                })))

            # handle page download progress
            def on_page_download_progress(progress):
                asyncio.run(client.send(json.dumps({
                    "type": "nomadnet.page.download",
                    "nomadnet_page_download": {
                        "status": "progress",
                        "progress": progress,
                        "destination_hash": destination_hash.hex(),
                        "page_path": page_path,
                    },
                })))

            # todo: handle page download progress

            # download the page
            NomadnetPageDownloader(destination_hash, page_path, on_page_download_success, on_page_download_failure, on_page_download_progress)

        # unhandled type
        else:
            print("unhandled client message type: " + _type)

    # broadcast provided data to all connected websocket clients
    async def websocket_broadcast(self, data):
        for websocket_client in self.websocket_clients:
            await websocket_client.send_str(data)

    # broadcasts config to all websocket clients
    async def send_config_to_websocket_clients(self):
        await self.websocket_broadcast(json.dumps({
            "type": "config",
            "config": {
                "display_name": self.display_name,
                "identity_hash": self.identity.hexhash,
                "lxmf_address_hash": self.local_lxmf_destination.hexhash,
            },
        }))

    # broadcasts known peers to all websocket clients
    async def send_known_peers_to_websocket_clients(self):

        # process known peers
        known_peers = []
        for destination_hash in RNS.Identity.known_destinations:
            known_destination = RNS.Identity.known_destinations[destination_hash]
            last_announce_timestamp = known_destination[0]
            known_peers.append({
                "destination_hash": destination_hash.hex(),
                "app_data": self.convert_app_data_to_string(RNS.Identity.recall_app_data(destination_hash)),
                "last_announce_timestamp": last_announce_timestamp,
            })

        # send known peers to websocket clients
        await self.websocket_broadcast(json.dumps({
            "type": "known_peers",
            "known_peers": known_peers,
        }))

    # convert app data to string, or return none unable to do so
    def convert_app_data_to_string(self, app_data):

        # attempt to convert to utf-8 string
        if app_data is not None:
            try:
                return app_data.decode("utf-8")
            except:
                # ignore failure to convert to string
                pass

        # unable to convert to string
        return None

    # convert an lxmf message to a dictionary, for sending over websocket
    def convert_lxmf_message_to_dict(self, lxmf_message: LXMF.LXMessage):

        # handle fields
        fields = {}
        message_fields = lxmf_message.get_fields()
        for field_type in message_fields:

            value = message_fields[field_type]

            # handle file attachments field
            if field_type == LXMF.FIELD_FILE_ATTACHMENTS:

                # process file attachments
                file_attachments = []
                for file_attachment in value:
                    file_name = file_attachment[0]
                    file_bytes = base64.b64encode(file_attachment[1]).decode("utf-8")
                    file_attachments.append({
                        "file_name": file_name,
                        "file_bytes": file_bytes,
                    })

                # add to fields
                fields["file_attachments"] = file_attachments

            # handle image field
            if field_type == LXMF.FIELD_IMAGE:
                image_type = value[0]
                image_bytes = base64.b64encode(value[1]).decode("utf-8")
                fields["image"] = {
                    "image_type": image_type,
                    "image_bytes": image_bytes,
                }

        # convert 0.0-1.0 progress to 0.00-100 percentage
        progress_percentage = round(lxmf_message.progress * 100, 2)

        return {
            "hash": lxmf_message.hash.hex(),
            "source_hash": lxmf_message.source_hash.hex(),
            "destination_hash": lxmf_message.destination_hash.hex(),
            "is_incoming": lxmf_message.incoming,
            "state": self.convert_lxmf_state_to_string(lxmf_message),
            "progress": progress_percentage,
            "title": lxmf_message.title.decode('utf-8'),
            "content": lxmf_message.content.decode('utf-8'),
            "fields": fields,
            "timestamp": lxmf_message.timestamp,
        }

    # convert lxmf state to a human friendly string
    def convert_lxmf_state_to_string(self, lxmf_message: LXMF.LXMessage):

        # convert state to string
        lxmf_message_state = "unknown"
        if lxmf_message.state == LXMF.LXMessage.DRAFT:
            lxmf_message_state = "draft"
        elif lxmf_message.state == LXMF.LXMessage.OUTBOUND:
            lxmf_message_state = "outbound"
        elif lxmf_message.state == LXMF.LXMessage.SENDING:
            lxmf_message_state = "sending"
        elif lxmf_message.state == LXMF.LXMessage.SENT:
            lxmf_message_state = "sent"
        elif lxmf_message.state == LXMF.LXMessage.DELIVERED:
            lxmf_message_state = "delivered"
        elif lxmf_message.state == LXMF.LXMessage.FAILED:
            lxmf_message_state = "failed"
        
        return lxmf_message_state

    # handle an lxmf delivery from reticulum
    # NOTE: cant be async, as Reticulum doesn't await it
    def on_lxmf_delivery(self, message):
        try:

            # convert lxmf message to dict
            lxmf_message_dict = self.convert_lxmf_message_to_dict(message)

            # save to database
            lxmf_message_db = database.LxmfMessage(
                hash=lxmf_message_dict["hash"],
                source_hash=lxmf_message_dict["source_hash"],
                destination_hash=lxmf_message_dict["destination_hash"],
                is_incoming=lxmf_message_dict["is_incoming"],
                state=lxmf_message_dict["state"],
                progress=lxmf_message_dict["progress"],
                title=lxmf_message_dict["title"],
                content=lxmf_message_dict["content"],
                fields=json.dumps(lxmf_message_dict["fields"]),
                timestamp=lxmf_message_dict["timestamp"],
            )
            lxmf_message_db.save()

            # send received lxmf message data to all websocket clients
            asyncio.run(self.websocket_broadcast(json.dumps({
                "type": "lxmf.delivery",
                "lxmf_message": self.convert_lxmf_message_to_dict(message),
            })))

        except Exception as e:
            # do nothing on error
            print("lxmf_delivery error: {}".format(e))

    # handle delivery status update for an outbound lxmf message
    def on_lxmf_sending_state_updated(self, lxmf_message):

        # upsert lxmf message to database
        self.db_upsert_lxmf_message(lxmf_message)

        # send lxmf message state to all websocket clients
        asyncio.run(self.websocket_broadcast(json.dumps({
            "type": "lxmf_message_state_updated",
            "lxmf_message": self.convert_lxmf_message_to_dict(lxmf_message),
        })))

    # handle delivery failed for an outbound lxmf message
    def on_lxmf_sending_failed(self, lxmf_message):
        # just pass this on, we don't need to do anything special
        self.on_lxmf_sending_state_updated(lxmf_message)

    # upserts the provided lxmf message to the database
    def db_upsert_lxmf_message(self, lxmf_message: LXMF.LXMessage):

        # convert lxmf message to dict
        lxmf_message_dict = self.convert_lxmf_message_to_dict(lxmf_message)

        # prepare data to insert or update
        data = {
            "hash": lxmf_message_dict["hash"],
            "source_hash": lxmf_message_dict["source_hash"],
            "destination_hash": lxmf_message_dict["destination_hash"],
            "is_incoming": lxmf_message_dict["is_incoming"],
            "state": lxmf_message_dict["state"],
            "progress": lxmf_message_dict["progress"],
            "title": lxmf_message_dict["title"],
            "content": lxmf_message_dict["content"],
            "fields": json.dumps(lxmf_message_dict["fields"]),
            "timestamp": lxmf_message_dict["timestamp"],
            "updated_at": datetime.now(),
        }

        # upsert to database
        query = database.LxmfMessage.insert(data)
        query = query.on_conflict(conflict_target=[database.LxmfMessage.hash], update=data)
        query.execute()

    # handle sending an lxmf message to reticulum
    async def send_message(self, destination_hash, message_content):

        try:

            # convert destination hash to bytes
            destination_hash = bytes.fromhex(destination_hash)

            # FIXME: can this be removed, and just rely on the router to check paths?
            # find destination identity from hash
            destination_identity = RNS.Identity.recall(destination_hash)
            if destination_identity is None:

                # we don't know the path/identity for this destination hash, we will request it
                RNS.Transport.request_path(destination_hash)

                # we have to bail out of sending, since we don't have the path yet
                return

            # create destination for recipients lxmf delivery address
            lxmf_destination = RNS.Destination(destination_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")

            # create lxmf message
            lxmf_message = LXMF.LXMessage(lxmf_destination, self.local_lxmf_destination, message_content, desired_method=LXMF.LXMessage.DIRECT)
            lxmf_message.try_propagation_on_fail = True
            lxmf_message.register_delivery_callback(self.on_lxmf_sending_state_updated)
            lxmf_message.register_failed_callback(self.on_lxmf_sending_failed)

            # send lxmf message to be routed to destination
            self.message_router.handle_outbound(lxmf_message)

            # upsert lxmf message to database
            self.db_upsert_lxmf_message(lxmf_message)

            # send outbound lxmf message to websocket (after passing to router so hash is available)
            await self.websocket_broadcast(json.dumps({
                "type": "lxmf_outbound_message_created",
                "lxmf_message": self.convert_lxmf_message_to_dict(lxmf_message),
            }))

        except:
            # FIXME send error to websocket?
            print("failed to send lxmf message")

    # handle an announce received from reticulum, for an lxmf address
    # NOTE: cant be async, as Reticulum doesn't await it
    def on_lxmf_announce_received(self, destination_hash, announced_identity, app_data):

        # log received announce
        RNS.log("Received an announce from " + RNS.prettyhexrep(destination_hash))

        # parse app data
        parsed_app_data = None
        if app_data is not None:
            parsed_app_data = app_data.decode("utf-8")

        # send received lxmf announce to all websocket clients
        asyncio.run(self.websocket_broadcast(json.dumps({
            "type": "announce",
            "announce": {
                "destination_hash": destination_hash.hex(),
                "app_data": parsed_app_data,
                "last_announce_timestamp": time.time(),
            },
        })))


# an announce handler for lxmf.delivery aspect that just forwards to a provided callback
class LXMFAnnounceHandler:

    def __init__(self, received_announce_callback):
        self.aspect_filter = "lxmf.delivery"
        self.received_announce_callback = received_announce_callback

    # we will just pass the received announce back to the provided callback
    def received_announce(self, destination_hash, announced_identity, app_data):
        try:
            # handle received announce
            self.received_announce_callback(destination_hash, announced_identity, app_data)
        except:
            # ignore failure to handle received announce
            pass


class NomadnetDownloader:

    def __init__(self, destination_hash: bytes, path: str, on_download_success: Callable[[bytes], None], on_download_failure: Callable[[str], None], on_progress_update: Callable[[float], None], timeout: int|None = None, auto_download=True):
        self.app_name = "nomadnetwork"
        self.aspects = "node"
        self.destination_hash = destination_hash
        self.path = path
        self.timeout = timeout
        self.on_download_success = on_download_success
        self.on_download_failure = on_download_failure
        self.on_progress_update = on_progress_update
        if auto_download:
            self.download()

    # setup link to destination and request download
    def download(self):

        # request path to destination
        RNS.Transport.request_path(self.destination_hash)

        # find existing identity
        identity = RNS.Identity.recall(self.destination_hash)
        if identity is None:
            self.on_download_failure("identity not found")
            return

        # create destination to nomadnet node
        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            self.app_name,
            self.aspects,
        )

        # create link to destination
        RNS.Link(destination, established_callback=self.link_established)

    # link to destination was established, we should now request the download
    def link_established(self, link):

        # request download over link
        link.request(
            self.path,
            data=None,
            response_callback=self.on_response,
            failed_callback=self.on_failed,
            progress_callback=self.on_progress,
            timeout=self.timeout,
        )

    # handle successful download
    def on_response(self, request_receipt):
        print("file_received")
        self.on_download_success(request_receipt.response)

    # handle failure
    def on_failed(self, request_receipt=None):
        self.on_download_failure("request_failed")

    # handle download progress
    def on_progress(self, request_receipt):
        self.on_progress_update(request_receipt.progress)


class NomadnetPageDownloader(NomadnetDownloader):

    def __init__(self, destination_hash: bytes, page_path: str, on_page_download_success: Callable[[str], None], on_page_download_failure: Callable[[str], None], on_progress_update: Callable[[float], None], timeout: int|None = None, auto_download=True):
        self.on_page_download_success = on_page_download_success
        self.on_page_download_failure = on_page_download_failure
        super().__init__(destination_hash, page_path, self.on_download_success, self.on_download_failure, on_progress_update, timeout, auto_download)

    # page download was successful, decode the response and send to provided callback
    def on_download_success(self, response_bytes):
        micron_markup_response = response_bytes.decode("utf-8")
        self.on_page_download_success(micron_markup_response)

    # page download failed, send error to provided callback
    def on_download_failure(self, failure_reason):
        self.on_page_download_failure(failure_reason)


class NomadnetFileDownloader(NomadnetDownloader):

    def __init__(self, destination_hash: bytes, page_path: str, on_file_download_success: Callable[[str, bytes], None], on_file_download_failure: Callable[[str], None], on_progress_update: Callable[[float], None], timeout: int|None = None, auto_download=True):
        self.on_file_download_success = on_file_download_success
        self.on_file_download_failure = on_file_download_failure
        super().__init__(destination_hash, page_path, self.on_download_success, self.on_download_failure, on_progress_update, timeout, auto_download)

    # file download was successful, decode the response and send to provided callback
    def on_download_success(self, response):
        file_name: str = response[0]
        file_data: bytes = response[1]
        self.on_file_download_success(file_name, file_data)

    # page download failed, send error to provided callback
    def on_download_failure(self, failure_reason):
        self.on_file_download_failure(failure_reason)


def main():

    # parse command line args
    parser = argparse.ArgumentParser(description="ReticulumWebChat")
    parser.add_argument("--host", nargs='?', default="0.0.0.0", type=str, help="The address the web server should listen on.")
    parser.add_argument("--port", nargs='?', default="8000", type=int, help="The port the web server should listen on.")
    parser.add_argument("--identity-file", type=str, help="Path to a Reticulum Identity file to use as your LXMF address.")
    parser.add_argument("--identity-base64", type=str, help="A base64 encoded Reticulum Identity to use as your LXMF address.")
    parser.add_argument("--generate-identity-file", type=str, help="Generates and saves a new Reticulum Identity to the provided file path and then exits.")
    parser.add_argument("--generate-identity-base64", action='store_true', help="Outputs a randomly generated Reticulum Identity as base64 and then exits.")
    parser.add_argument("--reticulum-config-dir", type=str, help="Path to a Reticulum config directory for the RNS stack to use (e.g: ~/.reticulum)")
    args = parser.parse_args()

    # util to generate reticulum identity and save to file without using rnid
    if args.generate_identity_file is not None:

        # do not overwrite existing files, otherwise user could lose existing keys
        if os.path.exists(args.generate_identity_file):
            print("DANGER: the provided identity file path already exists, not overwriting!")
            return

        # generate a new identity and save to provided file path
        identity = RNS.Identity(create_keys=True)
        with open(args.generate_identity_file, "wb") as file:
            file.write(identity.get_private_key())

        print("A new Reticulum Identity has been saved to: {}".format(args.generate_identity_file))
        return

    # util to generate reticulum identity as base64 without using rnid
    if args.generate_identity_base64 is True:
        identity = RNS.Identity(create_keys=True)
        print(base64.b64encode(identity.get_private_key()).decode("utf-8"))
        return

    # use provided identity, or fallback to a random one
    if args.identity_file is not None:
        identity = RNS.Identity(create_keys=False)
        identity.load(args.identity_file)
        print("Reticulum Identity has been loaded from file.")
        print(identity)
    elif args.identity_base64 is not None:
        identity = RNS.Identity(create_keys=False)
        identity.load_private_key(base64.b64decode(args.identity_base64))
        print("Reticulum Identity has been loaded from base64.")
        print(identity)
    else:
        identity = RNS.Identity(create_keys=True)
        print("Reticulum Identity has been randomly generated.")
        print(identity)

    # init app
    reticulum_webchat = ReticulumWebChat(args.reticulum_config_dir, identity)
    reticulum_webchat.run(args.host, args.port)
    
    
if __name__ == "__main__":
    main()
