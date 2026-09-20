FROM node:22-alpine@sha256:b6f26b36c8ff49624cfdac716b8ea1138d606df02586a77d364bb5536a634f85 AS web
WORKDIR /web
ENV NODE_OPTIONS=--max-old-space-size=768
RUN npm install --global pnpm@11.9.0
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.13-slim@sha256:59d365aafe9c497e90af2caf4affe3e57f677328b251945b0327807887ed3772
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CFSPEED_WEB_ROOT=/app/web/dist
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.lock
RUN groupadd --gid 10001 cfspeed && useradd --uid 10001 --gid 10001 --no-create-home cfspeed && mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 cfspeed/ /app/cfspeed/
COPY --from=web --chown=10001:10001 /web/dist /app/web/dist
USER 10001:10001
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=3).read()"]
ENTRYPOINT ["python", "-m", "cfspeed"]
CMD ["serve", "--config", "/app/config.toml"]
