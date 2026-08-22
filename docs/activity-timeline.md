---
title: Activity timeline
description: Opt-in voice-turn and perception telemetry on the dashboard.
---

# Activity timeline

The dashboard at `http://<DASHBOARD_HOST>:8081/ui/` has one unified activity
stream for voice turns and hardware/perception events. It uses the dashboard's
same-origin `GET /ui/activity` SSE endpoint; the browser never connects to the
voice or behaviour containers directly.

The feature is server-side only. It does not require a firmware flash or a
robot reboot.

## Enable it

Deploy the bridge first. Set the same non-empty `DOTTY_ADMIN_TOKEN` in the
bridge, xiaozhi-server, and dotty-behaviour environments, then enable the two
producers:

```dotenv
DOTTY_ACTIVITY_ENABLED=true
DOTTY_ACTIVITY_URL=http://<DASHBOARD_HOST>:8081/admin/activity
```

`DOTTY_ACTIVITY_ENABLED` defaults to `false`. Producers use bounded,
best-effort delivery: a full queue, timeout, or stopped bridge drops telemetry
without delaying a voice turn or a perception consumer.

The ingest endpoint accepts loopback requests when no shared token is
configured. Container peers must present `X-Admin-Token`; this accommodates
the bridge's host networking and xiaozhi-server's bridge networking without
opening the other `/admin/*` routes.

## Rollout check

Restart only the bridge, dotty-behaviour, and xiaozhi-server containers, in
that order. Before a hardware turn, send one synthetic event from the Docker
host:

```bash
curl --fail-with-body \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${DOTTY_ADMIN_TOKEN}" \
  --data '{"schema_version":1,"event_id":"00000000-0000-4000-8000-000000000001","ts":1,"source":"behaviour","kind":"event","phase":"perception","turn_id":null,"session_id":null,"device_id":"rollout","payload":{"name":"rollout_check","data":{"ok":true}}}' \
  http://127.0.0.1:8081/admin/activity
```

Use a fresh `event_id` if repeating the check. The event should appear under
All and Events, and a normal voice request should progress through Heard,
Thinking, Reply, Speaking, and Done. Tool turns show only tool name, duration,
and success state.

## Retention and privacy

The bridge retains the latest 100 grouped items in memory. Completed, failed,
and aborted turn summaries are appended once to the existing daily
`convo-YYYY-MM-DD.ndjson`; intermediate lifecycle stages are ephemeral.

Ingestion caps user text at 500 characters, response text at 1,000, and errors
at 300. Tool arguments/results, nested event payloads, raw audio, images,
frames, tokens, and other blob-like fields are discarded.

## Rollback

Set `DOTTY_ACTIVITY_ENABLED=false` (or remove the activity variables) in both
producer containers and restore the prior images if needed. The dashboard
controls and robot voice path do not depend on telemetry.
