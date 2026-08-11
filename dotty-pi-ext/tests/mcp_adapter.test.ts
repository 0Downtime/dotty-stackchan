import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import readline from "node:readline";
import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { guardMcpOutput } from "../node_modules/pi-mcp-adapter/mcp-output-guard.ts";

const projectDir = resolve(new URL("..", import.meta.url).pathname);
const piBinary = join(projectDir, "node_modules", ".bin", "pi");
const extensionPath = join(projectDir, "src", "index.ts");
const configPath = join(projectDir, "config", "mcp.json");
const pilotPath = join(projectDir, "src", "mcp", "pilot_server.mjs");

const versionDir = mkdtempSync(join(tmpdir(), "dotty-pi-version-"));
const version = spawnSync(piBinary, ["--version"], {
  encoding: "utf8",
  env: { ...process.env, PI_CODING_AGENT_DIR: versionDir },
});
rmSync(versionDir, { recursive: true, force: true });
assert.equal(version.status, 0);
assert.match(`${version.stdout}${version.stderr}`.trim(), /0\.74\.0$/);

const agentDir = mkdtempSync(join(tmpdir(), "dotty-pi-rpc-agent-"));
const poisonCwd = mkdtempSync(join(tmpdir(), "dotty-pi-rpc-cwd-"));
writeFileSync(join(poisonCwd, ".mcp.json"), JSON.stringify({
  mcpServers: {
    ambient_poison: {
      command: "definitely-not-a-command",
      directTools: true,
    },
  },
}));

const pi = spawn(piBinary, [
  "--mode", "rpc",
  "--no-builtin-tools",
  "--no-session",
  "--no-context-files",
  "--offline",
  "--no-skills",
  "--no-prompt-templates",
  "--no-themes",
  "--thinking", "off",
  "--extension", extensionPath,
], {
  cwd: poisonCwd,
  env: {
    ...process.env,
    PI_CODING_AGENT_DIR: agentDir,
    DOTTY_MCP_CONFIG_PATH: configPath,
    DOTTY_MCP_ENABLED: "true",
    DOTTY_MCP_HOME_ASSISTANT_ENABLED: "false",
    MCP_UI_VIEWER: "none",
  },
  stdio: ["pipe", "pipe", "pipe"],
});

const frames: any[] = [];
const lines = readline.createInterface({ input: pi.stdout });
lines.on("line", (line) => {
  try { frames.push(JSON.parse(line)); } catch { /* stdout must be JSONL; asserted below */ }
});

async function waitFor(predicate: () => boolean, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    if (pi.exitCode !== null) throw new Error(`Pi exited early with ${pi.exitCode}`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("Pi RPC compatibility smoke timed out");
}

try {
  await waitFor(() => frames.some((frame) =>
    frame.type === "extension_ui_request"
    && frame.method === "notify"
    && frame.message === "MCP: 1 servers connected (1 tools)"
  ));
  await waitFor(() => frames.some((frame) =>
    frame.type === "extension_ui_request"
    && frame.method === "notify"
    && String(frame.message).includes("direct tools refreshed (+1")
  ));
  pi.stdin.write(`${JSON.stringify({
    id: "status-1",
    type: "prompt",
    message: "/dotty-mcp-status",
  })}\n`);
  await waitFor(() => frames.some((frame) =>
    frame.type === "extension_ui_request"
    && frame.method === "notify"
    && frame.message === "Dotty MCP tools: pilot_pilot_lookup"
  ));
  assert.equal(frames.some((frame) => JSON.stringify(frame).includes("ambient_poison")), false);
  assert.equal(frames.some((frame) => JSON.stringify(frame).includes("test_blocked_write")), false);
  assert.equal(frames.some((frame) => frame.type === "extension_error"), false);
} finally {
  pi.kill("SIGTERM");
  await new Promise<void>((resolveExit) => {
    const timer = setTimeout(() => { pi.kill("SIGKILL"); resolveExit(); }, 2000);
    pi.once("exit", () => { clearTimeout(timer); resolveExit(); });
  });
  rmSync(agentDir, { recursive: true, force: true });
  rmSync(poisonCwd, { recursive: true, force: true });
}

const guarded = await guardMcpOutput(
  [{ type: "text", text: "X".repeat(20000) }],
  { maxBytes: 1024, maxLines: 100, detailsMaxBytes: 2048 },
);
assert.ok(guarded.outputGuard?.truncated);
assert.ok((guarded.content[0] as { text: string }).text.length < 4000);
if (guarded.outputGuard?.fullOutputPath) rmSync(guarded.outputGuard.fullOutputPath, { force: true });

const transport = new StdioClientTransport({ command: process.execPath, args: [pilotPath] });
const client = new Client({ name: "dotty-timeout-test", version: "1" });
await client.connect(transport);
const started = Date.now();
await assert.rejects(
  client.callTool(
    { name: "test_slow", arguments: { delay_ms: 1000 } },
    { timeout: 100 },
  ),
  /timed? out|timeout/i,
);
assert.ok(Date.now() - started < 1000);
const recovered = await client.callTool({
  name: "pilot_lookup",
  arguments: { topic: "after-timeout" },
});
assert.equal(
  (recovered.content[0] as { text: string }).text,
  "Pilot MCP result for after-timeout: connectivity is healthy.",
);
await client.close();

console.log("mcp_adapter: 14/14 pass");
