---
title: OpenAI Realtime Voice
description: Opt-in speech-to-speech bridge using gpt-realtime-2.1-mini, with local fallback and Kid Mode gating.
---

# OpenAI Realtime voice (experimental)

Dotty can optionally route an adult/general-purpose voice session through
OpenAI's Realtime API. The StackChan firmware does not connect to OpenAI and
does not store an API key: it continues speaking the Xiaozhi WebSocket
protocol to the self-hosted server. The server bridge translates 16 kHz Opus
microphone frames to 24 kHz PCM input and encodes the model's 24 kHz PCM output
back to the robot's 24 kHz Opus stream.

The default remains the fully local pipeline. Realtime is opt-in, and any
configuration, connection, codec, or session failure leaves the local route
available for the next turn.

Official references:

- [Realtime and audio](https://developers.openai.com/api/docs/guides/realtime)
- [Realtime WebSocket connections](https://developers.openai.com/api/docs/guides/realtime-websocket)
- [Realtime conversations and interruption](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)

## Safety and privacy boundary

Realtime is **always bypassed while Kid Mode is active**. The local Kid Mode
filter buffers the complete response text and decides whether to allow or
replace it before TTS starts. Direct model audio arrives incrementally, so it
cannot provide that same pre-speech guarantee. Enabling Kid Mode during a
Realtime response stops queued audio at the next 60 ms frame boundary and
returns subsequent turns to the local path.

When Realtime is active, microphone audio and the session instructions leave
the LAN for OpenAI. Tool calls may send a bounded text request to Dotty's local
pi agent and return its text result to the Realtime session. The bridge hashes
the device ID before using it as OpenAI's safety identifier; the raw device ID
is not sent in that header.

## Enable it

Keep the key only in the deployment's untracked, mode-600 `.env` file:

```dotenv
DOTTY_REALTIME_ENABLED=true
OPENAI_API_KEY=your-deployment-secret

# Optional tuning
DOTTY_REALTIME_MODEL=gpt-realtime-2.1-mini
DOTTY_REALTIME_VOICE=marin
DOTTY_REALTIME_TRANSCRIPTION_MODEL=gpt-live-transcribe
DOTTY_REALTIME_REASONING_EFFORT=low
```

Input transcription is optional and is separate from the model's ability to
understand microphone audio. Set `DOTTY_REALTIME_TRANSCRIPTION_MODEL=` to omit
the transcript stream when that model is unavailable or has a lower rate
limit; speech-to-speech conversation continues normally.

Then rebuild/recreate the Xiaozhi service so the new bind mount and environment
are applied:

```bash
docker compose up -d --build xiaozhi-esp32-server
```

Turn Kid Mode off in the dashboard before testing. Realtime is deliberately
not exposed as a replacement `selected_module.LLM`: it owns live audio,
transcription, response audio, and interruption as one session, above the
separate ASR/LLM/TTS provider stages.

Never paste an API key into chat, commit it, put it in `.config.yaml`, or enter
it on the StackChan device.

## Runtime flow

```text
StackChan microphone (Opus 16 kHz)
  -> Xiaozhi WebSocket
  -> Dotty Realtime bridge (decode + resample)
  -> OpenAI Realtime WebSocket (PCM 24 kHz)
  -> response audio (PCM 24 kHz)
  -> Dotty Realtime bridge (Opus encode + 60 ms pacing)
  -> StackChan speaker
```

For firmware `mode: "auto"` / `"realtime"`, OpenAI server VAD detects the end
of speech and creates the response automatically; these modes do not reliably
emit a device-side `listen stop`. Manual listening keeps explicit push-to-talk
boundaries: the bridge clears the input buffer on start, commits it on stop,
then sends `response.create`.

The bridge runs half-duplex during response playback: it consumes but drops
microphone frames while Dotty is speaking. This prevents the robot's speaker
from retriggering server VAD and creating an acoustic response loop.

When the user interrupts playback, the bridge:

1. stops queued device audio;
2. sends `response.cancel`;
3. reports the played duration with `conversation.item.truncate`; and
4. lets Xiaozhi perform its normal queue and speaking-state cleanup.

The Realtime session exposes one function,
`consult_dotty_local_agent`, when the configured Xiaozhi LLM has a response
interface. It delegates remembered facts, device status/control, camera,
songs, and deeper reasoning to the existing local pi agent. Ordinary
conversation stays in the Realtime model.

## Roll back

Set:

```dotenv
DOTTY_REALTIME_ENABLED=false
```

and recreate the Xiaozhi service. The default local pipeline was never
removed or reconfigured. Enabling Kid Mode also bypasses Realtime immediately
without a container restart.

## Validation checklist

After deployment, validate behavior rather than configuration text alone:

1. With Kid Mode on, confirm logs show no Realtime connection and a normal
   local Piper response succeeds.
2. Turn Kid Mode off and start a voice turn. Confirm one
   `OpenAI Realtime ready model=...` line without any key or transcript in the
   log.
3. Confirm the robot displays a transcript, begins speaking, and returns to
   listening after the response.
4. Interrupt a response and confirm audio stops promptly and the next question
   retains sensible conversation context.
5. Ask for current device status or a remembered fact and confirm the local
   agent tool runs before the spoken answer.
6. Turn Kid Mode on during a response and confirm audio stops, followed by a
   successful local-path turn.
7. Temporarily remove or invalidate the deployment key, recreate the service,
   and confirm a local response still succeeds.

The repository test suite uses fake WebSockets and codecs; it does not call
OpenAI and cannot prove microphone quality, acoustic echo cancellation,
speaker playback, account access, quota, or end-to-end latency.
