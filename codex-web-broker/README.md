# Dotty Codex web broker

This optional private sidecar gives the OpenAI Realtime voice bridge one narrow
Internet-research function backed by a Codex CLI login. It is not published to
the host network. The Xiaozhi container reaches `/search` only over the private
Compose network with a separate bearer token.

The broker runs `codex exec` with an ephemeral session, live web search, an
empty temporary workspace, and a read-only permission profile that denies
model-run commands access to the Codex credential directory. The subprocess
environment excludes the broker token and unrelated deployment secrets.

Authenticate the named volume interactively after building the image:

```bash
docker compose run --rm --entrypoint codex codex-web-broker login --device-auth
```

Treat the resulting auth volume like a password. Do not mount it into Xiaozhi,
copy it into the repository, or expose the broker port outside the private
Docker network.
