FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.147.0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && mkdir -p /home/node/.codex /workspace \
    && chown -R node:node /home/node/.codex /workspace

COPY --chown=node:node broker.mjs /app/broker.mjs

USER node
ENV HOME=/home/node \
    CODEX_HOME=/home/node/.codex \
    DOTTY_CODEX_BROKER_HOST=0.0.0.0 \
    DOTTY_CODEX_BROKER_PORT=8092
WORKDIR /workspace

EXPOSE 8092
ENTRYPOINT ["node", "/app/broker.mjs"]
