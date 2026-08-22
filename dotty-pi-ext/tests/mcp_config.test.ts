import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  parseExternalMcpConfig,
  registerExternalMcp,
  isPrivateHomeAssistantUrl,
} from "../src/mcp/external_mcp.ts";

const configText = readFileSync(new URL("../config/mcp.json", import.meta.url), "utf8");
const valid = JSON.parse(configText);

const parsed = parseExternalMcpConfig(configText);
assert.deepEqual(parsed.mcpServers.pilot.includeTools, ["pilot_lookup"]);
assert.deepEqual(parsed.mcpServers.pilot.directTools, ["pilot_lookup"]);
assert.equal(parsed.mcpServers.pilot.exposeResources, false);
assert.equal(parsed.mcpServers.home_assistant.bearerTokenEnv, "HOMEASSISTANT_TOKEN");

function rejects(mutator: (copy: any) => void, pattern: RegExp) {
  const copy = structuredClone(valid);
  mutator(copy);
  assert.throws(() => parseExternalMcpConfig(JSON.stringify(copy)), pattern);
}

rejects((copy) => { copy.imports = ["codex"]; }, /imports are forbidden/);
rejects((copy) => { copy.settings.hostConfigDiscovery = "on"; }, /fail-closed/);
rejects((copy) => { copy.settings.disableProxyTool = false; }, /fail-closed/);
rejects((copy) => { copy.settings.outputGuard.maxBytes = 99999; }, /unsafe value/);
rejects((copy) => { copy.mcpServers.pilot.includeTools = ["*"]; }, /approved allowlist/);
rejects((copy) => { copy.mcpServers.pilot.directTools = true; }, /match includeTools/);
rejects((copy) => { copy.mcpServers.pilot.exposeResources = true; }, /disable resources/);
rejects((copy) => { copy.mcpServers.pilot.url = "https://example.invalid/mcp"; }, /one transport/);
rejects((copy) => { copy.mcpServers.home_assistant.bearerToken = "secret"; }, /inline authentication/);
rejects((copy) => { copy.mcpServers.home_assistant.url = "https://public.example/mcp"; }, /LAN environment/);
rejects((copy) => { copy.mcpServers.ambient = copy.mcpServers.pilot; }, /not approved/);

assert.equal(isPrivateHomeAssistantUrl("http://192.168.1.10:8123"), true);
assert.equal(isPrivateHomeAssistantUrl("http://homeassistant.local:8123"), true);
assert.equal(isPrivateHomeAssistantUrl("http://ha.home.arpa:8123"), true);
assert.equal(isPrivateHomeAssistantUrl("https://home.example.com"), false);
assert.equal(isPrivateHomeAssistantUrl("http://user:secret@homeassistant.local:8123"), false);
assert.equal(isPrivateHomeAssistantUrl("http://homeassistant.local:8123/api"), false);
assert.equal(isPrivateHomeAssistantUrl("http://homeassistant.local:8123?token=secret"), false);

const oldEnabled = process.env.DOTTY_MCP_ENABLED;
let registerCalls = 0;
const fakePi = { registerTool: () => { registerCalls += 1; } } as any;
delete process.env.DOTTY_MCP_ENABLED;
assert.equal(registerExternalMcp(fakePi), false, "missing master switch is disabled");
process.env.DOTTY_MCP_ENABLED = "false";
assert.equal(registerExternalMcp(fakePi), false);
assert.equal(registerCalls, 0);
if (oldEnabled === undefined) delete process.env.DOTTY_MCP_ENABLED;
else process.env.DOTTY_MCP_ENABLED = oldEnabled;

console.log("mcp_config: 23/23 pass");
