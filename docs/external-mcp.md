---
title: External MCP
description: Private, allowlisted MCP servers used by Dotty's Pi voice brain.
---

# Dotty external MCP

> **AI-assistance note:** this implementation and document were drafted with
> OpenAI Codex and require maintainer review before deployment.

Dotty's long-lived Pi voice process can consume external MCP servers without a
firmware change and without opening a public listener. Pi remains pinned at
`0.74.0`; `pi-mcp-adapter` is pinned at `2.13.0`.

## Kanban status

| Card | Worker | Status | Evidence / next gate |
|---|---|---|---|
| F0 Preserve current work | Lead | Done | Dirty checkout preserved; integration worktree is independent. |
| F1 Production routing baseline | Policy | Done | Session, status, tool telemetry, and routing regressions pass. |
| C1 Pi adapter foundation | MCP Client | Done | Exact Pi/adapter RPC compatibility passes on macOS and pinned Node Alpine. |
| C2 Hermetic MCP pilot | QA | Done | Allowlist, ambient-config isolation, timeout/output guards, and post-timeout recovery pass. |
| C3 Deployment packaging | MCP Client | Done | Locked Alpine build/test and atomic extension/config rollback implemented. |
| C4 Voice pilot integration | Policy | Human/bench gate | Code complete; live 19/20 routing evidence still required. |
| U1 Safe pilot UAT | QA + Human | Human/bench gate | Requires approved deployment, 20 calls, and 24-hour soak. |
| H1 Home Assistant preparation | Human | Blocked by U1 | Requires dedicated user/token and entity exposure review. |
| H2 Home Assistant read path | MCP Client | Ready, blocked by H1 | `GetLiveContext` allowlisted; live selected-sensor check pending. |
| H3 Confirmed light controls | Policy | Ready, blocked by H2 | Pre-transport, same-session, single-use gate passes hermetic tests. |
| H4 Security and live UAT | QA + Human | Blocked by H3 | One-light physical test and latency/audit evidence pending. |
| R1 Documentation and rollback | Lead | In review | Operator and rollback docs complete; live evidence awaits H4. |

Implementation WIP is closed. The remaining cards are deliberately held at
the Human Gate; none requires a firmware flash or robot/host reboot.

## Security boundary

The extension reads `/root/.pi/agent/mcp.json`, validates it, then passes the
parsed object to `createMcpAdapter({config})`. The adapter never discovers or
merges Codex, Claude, project, or user MCP files. Imports, wildcard tools,
resources, generated MCP prompt commands, sampling, elicitation, automatic
OAuth, the proxy tool, inline credentials, and public Home Assistant URLs are
blocked. Requests time out after 5 seconds and results are capped at 8 KiB,
100 lines, and 2 KiB of details.

Pi `0.74.0` remains an explicit project pin and has published local/shared-host
advisories that require a later Pi upgrade to fully clear. This pilot runs in a
dedicated container with root-owned extension/config paths, no untrusted
project checkout, no coding built-ins, and no session export. Patched
transitive dependency overrides clear the extension's production dependency
audit; upgrading Pi itself remains a separate compatibility project.

External MCP is disabled when `DOTTY_MCP_ENABLED` is absent or false; set it
to true only after the Human Gate. The pilot is enabled
separately with `DOTTY_MCP_PILOT_ENABLED`; Home Assistant requires the explicit
`DOTTY_MCP_HOME_ASSISTANT_ENABLED=true` gate, a private LAN base URL, and a
token supplied only through the container environment.

## Pilot

The tracked stdio server advertises four test tools, but the production config
exposes only `pilot_lookup(topic)`, model-visible as `pilot_pilot_lookup`.
The blocked-write, slow, and oversized tools are QA oracles and cannot pass the
production config validator or direct-tool allowlist.

Safe rollout:

1. Keep Home Assistant disabled.
2. Deploy only after operator approval.
3. Ask twenty explicit pilot questions and verify every spoken answer contains
   the returned topic/result rather than a claimed success.
4. Run a 24-hour ordinary-voice soak before enabling Home Assistant.

## Home Assistant

Enable Home Assistant's official **Model Context Protocol Server** integration.
Its endpoint is the private `${HOMEASSISTANT_MCP_URL}/api/mcp`; create a
dedicated non-admin user and long-lived token. In Home Assistant's exposed
entities page, expose at most five approved sensors and three lights. Do not
expose switches, locks, covers, climate, alarms, cameras, scripts, scenes,
media devices, or any other entity domain.

Dotty allows only `GetLiveContext`, `HassTurnOn`, and `HassTurnOff`. The latter
two are intercepted before transport by `ha_confirmation.ts`:

- The first call must name one light and is blocked before Home Assistant.
- Dotty says `Turn <on|off> <name>? Say "Confirm <on|off> <name>" within 15 seconds.`
- The entire second utterance must match case- and punctuation-insensitively,
  in the same nonempty Xiaozhi session.
- The tool name and canonical full arguments must be identical.
- Expiry, session change, missing context, changed arguments, a second action,
  or replay blocks execution and consumes the candidate.

The trusted voice-session marker is appended by PiVoiceLLM, consumed and
removed by the extension's RPC input hook, and cannot be replaced by a marker
inside user speech.

Home Assistant's own exposed-entity list is the second authorization boundary.
See the [official MCP Server integration documentation](https://www.home-assistant.io/integrations/mcp_server).

## Deployment and rollback

`scripts/deploy-dotty-pi.sh` extracts a stage directory, runs Alpine `npm ci`
and all extension tests, checks the exact Pi/adapter versions, builds the image,
then renames the complete extension into place. It atomically replaces the two
repo-owned agent configs and has a remote error trap that restores the previous
extension/config before restarting the prior service state. It never modifies
`memory/brain.db`, persona, sessions, or credentials.

Rollback does not require a host or StackChan reboot:

1. Set `DOTTY_MCP_ENABLED=false` in the protected dotty-pi deploy environment.
2. Recreate only `dotty-pi` using the prior pinned image or let the deployment
   script's failed-swap trap restore the prior extension.
3. Verify one ordinary no-tool voice turn.

No firmware flash, robot reboot, public URL, or public MCP listener is part of
this design.

## Automated evidence

Run:

```bash
cd dotty-pi-ext && npm ci && npm test
python3 -m unittest custom-providers/pi_voice/tests/test_pi_client.py \
  custom-providers/pi_voice/tests/test_pi_voice.py -v
bash -n scripts/deploy-dotty-pi.sh
```

The suite covers isolated config rejection, a poisoned ambient MCP file, exact
Pi 0.74.0 RPC startup, the single production-visible pilot tool, protocol
framing, timeout and output guards, exact/same-session confirmation, mismatch,
expiry, replay, multi-action cancellation, and result-grounded confirmation
speech.
