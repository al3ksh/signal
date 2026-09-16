FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
COPY public ./public
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SIGNAL_DATA_DIR=/data
RUN groupadd --gid 10001 signal && useradd --uid 10001 --gid signal --no-create-home signal \
    && mkdir /data && chown signal:signal /data
WORKDIR /app
COPY server.py storage.py alerts.py setup_access.py ./
COPY --from=frontend /build/dist ./dist
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('SIGNAL_PORT','8091')+'/healthz',timeout=3)"
CMD ["python", "server.py"]
