# Private remote inference

Dotty can keep ASR and TTS on its Docker host while sending only the LLM and
vision requests to an OpenAI-compatible server elsewhere on a private network.
This is useful when the Docker host is small and an Apple Silicon Mac runs the
models.

## Environment contract

`OpenAICompat` accepts these runtime overrides:

- `DOTTY_INFERENCE_URL` — base URL ending at `/v1`
- `DOTTY_INFERENCE_API_KEY` — optional bearer token; keep it outside Git
- `DOTTY_VOICE_MODEL` — fast model used for direct voice turns
- `DOTTY_VOICE_ENABLE_THINKING` — optional boolean passed to Qwen/oMLX as
  `chat_template_kwargs.enable_thinking`; set it to `false` for voice so model
  reasoning cannot be streamed into TTS, and omit it for backends that reject
  extra request fields
- `DOTTY_OFFLINE_REPLY` — short local response used for timeout, connection,
  and HTTP failures

`dotty-behaviour` also accepts:

- `DOTTY_REASONING_URL` and `DOTTY_REASONING_MODEL`
- `DOTTY_VISION_URL`, `DOTTY_VISION_MODEL`, and `DOTTY_VISION_API_KEY`

The older `NARRATIVE_*` and `VLM_*` names remain supported. No provider is
selected automatically, so configure `selected_module.LLM: OpenAICompat` and
do not configure a cloud fallback if local-only behavior is required.

## Tailscale or Headscale example

Keep the inference process bound to loopback on the Mac and publish only the
two required TCP ports to the tailnet:

```sh
tailscale serve --bg --tcp=18000 tcp://127.0.0.1:18000
tailscale serve --bg --tcp=18002 tcp://127.0.0.1:18002
```

Do not enable Funnel. Restrict the Dotty node to these ports in the tailnet
policy, and verify that the same services remain unreachable through the Mac's
ordinary LAN address.

With Headscale, point each Tailscale client at the self-hosted control server
before enrollment:

```sh
tailscale login --login-server=https://headscale.example.com
```

Raw TCP forwarding is the relevant Serve mode here; it does not depend on the
HTTPS certificate flow. Native HTTPS Serve remains a Headscale feature gap as
of v0.29, so keep TLS termination outside this inference path. Apply the
least-privilege port grant in Headscale's policy file and test it before reload.

For the baseline stage, omit `dotty-pi` and both `/var/run/docker.sock` and the
Docker CLI mount from xiaozhi-server. Reintroduce them only when enabling the
PiVoiceLLM `docker exec` transport.
