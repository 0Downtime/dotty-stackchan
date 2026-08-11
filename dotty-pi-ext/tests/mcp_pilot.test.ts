import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import readline from "node:readline";

const server = spawn(process.execPath, [
  new URL("../src/mcp/pilot_server.mjs", import.meta.url).pathname,
], { stdio: ["pipe", "pipe", "pipe"] });

const lines = readline.createInterface({ input: server.stdout });
const pending = new Map<number, (message: any) => void>();
lines.on("line", (line) => {
  const message = JSON.parse(line);
  pending.get(message.id)?.(message);
  pending.delete(message.id);
});

let nextId = 1;
function request(method: string, params: Record<string, unknown> = {}): Promise<any> {
  const id = nextId++;
  const promise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout waiting for ${method}`)), 2000);
    pending.set(id, (message) => {
      clearTimeout(timer);
      resolve(message);
    });
  });
  server.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  return promise;
}

try {
  const initialized = await request("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "dotty-test", version: "1" },
  });
  assert.equal(initialized.result.serverInfo.name, "dotty-hermetic-pilot");
  server.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);

  const listed = await request("tools/list");
  assert.deepEqual(
    listed.result.tools.map((tool: any) => tool.name),
    ["pilot_lookup", "test_blocked_write", "test_slow", "test_oversized"],
  );
  const lookup = await request("tools/call", {
    name: "pilot_lookup",
    arguments: { topic: "weather" },
  });
  assert.equal(
    lookup.result.content[0].text,
    "Pilot MCP result for weather: connectivity is healthy.",
  );
  const invalid = await request("tools/call", {
    name: "pilot_lookup",
    arguments: { topic: "" },
  });
  assert.equal(invalid.result.isError, true);
} finally {
  server.stdin.end();
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => { server.kill(); resolve(); }, 1000);
    server.once("exit", () => { clearTimeout(timer); resolve(); });
  });
}

assert.equal(server.exitCode, 0);
console.log("mcp_pilot: 5/5 pass");
