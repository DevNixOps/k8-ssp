FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG HELM_VERSION=v3.16.2
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz | tar xz -C /tmp \
    && mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm \
    && apt-get purge -y curl && rm -rf /var/lib/apt/lists/* /tmp/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
RUN useradd -u 10001 -m ssp
USER 10001
ENV SSP_MOCK_MODE=false
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
