# Build the frontend
FROM node:20-bookworm-slim AS build-frontend

WORKDIR /src

COPY . .

RUN npm install --omit=dev && \
  npm install tailwindcss && \
  npm run build-frontend

# Main app build
FROM python:3.11-bookworm

WORKDIR /app

COPY ./requirements.txt .
COPY --from=build-frontend /src/public public

RUN pip install -r requirements.txt

CMD ["python", "meshchat.py"]
