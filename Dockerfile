FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY apps/webui/package.json apps/webui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/webui/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/
WORKDIR /MaiMBot
COPY apps/core/pyproject.toml apps/core/uv.lock ./
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/MaiMBot/.venv/bin:${PATH}"
COPY deploy/requirements.txt /tmp/wemaa-requirements.txt
RUN uv pip install --python /MaiMBot/.venv/bin/python -r /tmp/wemaa-requirements.txt
RUN python -m playwright install-deps chromium && rm -rf /var/lib/apt/lists/*
COPY apps/core/ ./
COPY wemaa/ /opt/wemaa/wemaa/
COPY plugins/website-agent/ /opt/wemaa/builtin-plugins/website-agent/
COPY deploy/entrypoint.sh /opt/wemaa/entrypoint.sh
COPY --from=web /build/dist /opt/wemaa/webui/
ENV PYTHONPATH=/opt/wemaa:/MaiMBot WEMAA_WEB_DIST=/opt/wemaa/webui WEMAA_DATA_DIR=/MaiMBot/data/wemaa
EXPOSE 8001
ENTRYPOINT ["/bin/sh","/opt/wemaa/entrypoint.sh"]
