# dotty-pi

Production Docker image for the **pi coding agent** running as Dotty's
voice-tool brain on Unraid. Replaces the RPi-hosted `zeroclaw-bridge`
per [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36).

## What this is

A pinned `node:25.9-alpine3.23` image with `@earendil-works/pi-coding-agent`
installed globally. Idles via `sleep infinity`; voice turns invoke pi on
demand via `docker exec -i` from the Unraid-local `PiClient` (lives in
[`../custom-providers/pi_voice/`](../custom-providers/pi_voice/)).

The runtime contract is:

- **xiaozhi-server** routes voice-LLM calls to the `PiVoiceLLM` provider.
- **PiVoiceLLM / PiClient** translates each turn into a pi RPC request.
- **pi** (this container) runs the prompt against the oMLX-compatible
  inference endpoint (`http://100.64.0.1:18000/v1`) using the
  `Qwen3.5-4B-MLX-4bit` voice model, with
  the [`dotty-pi-ext`](../dotty-pi-ext/) extension loaded for the seven
  voice tools (`memory_lookup`, `remember`, `recall_person`,
  `remember_person`, `think_hard`, `take_photo`, `play_song`).

## Build + run on Unraid

Use the deploy script — it ships the build context, config, and extension
source, builds the pinned image, recreates the container, and healthchecks:

```bash
DOTTY_PI_HOST=root@<UNRAID_HOST> bash scripts/deploy-dotty-pi.sh
```

The script is the repeatable replacement for the old hand-run
`docker build … && docker compose up -d`. It writes only the build context,
`agent/models.json`, and `extensions/dotty-pi-ext/` — it never touches the
live `memory/brain.db` or `persona/`, and it preserves the extension's
hand-compiled `node_modules` (deps are unchanged; see
[`scripts/deploy-dotty-pi.sh`](../scripts/deploy-dotty-pi.sh) for the full
contract). A functional voice-tool smoke test is a manual post-deploy step
(the script prints the reminder) — keep the agent loop on `qwen3.5:4b`.

On-box layout (build context and live state are **separate** directories):

```
/mnt/user/appdata/
├── dotty-pi-src/                # build context (SRC_DIR)
│   ├── Dockerfile
│   └── docker-compose.yml
└── dotty-pi/                    # bind-mount → /root/.pi (STATE_DIR)
    ├── agent/
    │   ├── models.json          # provider config (deployed)
    │   ├── auth.json            # live — never touched by deploy
    │   └── sessions/            # live — never touched by deploy
    ├── persona/                 # Dotty persona — migrated from RPi (live)
    ├── memory/
    │   └── brain.db             # FTS5 store — migrated from RPi (live)
    ├── sessions/                # pi session state (unused for now)
    └── extensions/
        └── dotty-pi-ext/        # voice-tool extension source (deployed)
            └── node_modules/    # hand-compiled better-sqlite3 (preserved)
```

## Model selection — use the registered oMLX voice model

The live deployment exposes the voice model through an OpenAI-compatible
oMLX endpoint. Keep the configured provider/model pair stable; an
unregistered alias makes the Pi RPC process exit before a voice turn.

The cutover model split (validated 2026-05-17 end-to-end):

| Loop | Model | Why |
|---|---|---|
| Outer agent (`pi --model …`) | `Qwen3.5-4B-MLX-4bit` | Fast oMLX voice model for Dotty's flat tool surface. |
| `think_hard` escalation | `qwen3.6:27b-think` | The 8K-context 27B, in voice set, resident alongside 4B. Direct llama-swap POST inside the extension; no agent overhead. |

Do not call Pi with an unregistered provider or model alias. Validate the
live inventory with `pi --list-models`; the voice path uses provider `omlx`
and model `Qwen3.5-4B-MLX-4bit`.

Measured wall-clock for the 4B + 27B-think split:

- `memory_lookup` (no LLM escalation): ~5.8 s total (4B turn + tool + reply)
- `think_hard` ("reply with `pong`"): ~45 s total warm (4B turn + tool fires inner 27B-think call + reply)

The shipped `models.json` registers the single voice model used by the
agent loop: `Qwen3.5-4B-MLX-4bit`.

## Versioning

| Tag | Pi version | Notes |
|---|---|---|
| `dotty-pi:0.1.0` | `0.74.0` | Production-grade promotion of the 2026-05-15 spike. |
| `dotty-pi:spike` | `0.74.0` | The original day-0 spike (`audits/pi-rpc-spike-report.md`). Keep until production is soaked. |

Bump the image tag deliberately when pi or node moves; do not use floating
tags. Cutover testing depends on a known-good image.

## See also

- [`../dotty-pi-ext/README.md`](../dotty-pi-ext/README.md) — voice-tool extension contract.
- [`../custom-providers/pi_voice/README.md`](../custom-providers/pi_voice/README.md) — xiaozhi-side glue.
- [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36) — the cutover plan + soak rule.
