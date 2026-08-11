import readline from "node:readline";

function result(id, value) {
  process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result: value })}\n`);
}

function error(id, code, message) {
  process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } })}\n`);
}

const tools = [
  {
    name: "pilot_lookup",
    description: "Read a deterministic topic from Dotty's hermetic MCP connectivity pilot.",
    inputSchema: {
      type: "object",
      properties: { topic: { type: "string", minLength: 1, maxLength: 120 } },
      required: ["topic"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "test_blocked_write",
    description: "QA-only mutation oracle. This tool must never be exposed by Dotty.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "test_slow",
    description: "QA-only timeout oracle.",
    inputSchema: {
      type: "object",
      properties: { delay_ms: { type: "integer", minimum: 0, maximum: 30000 } },
      required: ["delay_ms"],
      additionalProperties: false,
    },
  },
  {
    name: "test_oversized",
    description: "QA-only output guard oracle.",
    inputSchema: {
      type: "object",
      properties: { bytes: { type: "integer", minimum: 1, maximum: 1000000 } },
      required: ["bytes"],
      additionalProperties: false,
    },
  },
];

async function callTool(name, args) {
  if (name === "pilot_lookup") {
    const topic = typeof args?.topic === "string" ? args.topic.trim() : "";
    if (!topic || topic.length > 120) throw new Error("topic must be 1-120 characters");
    return `Pilot MCP result for ${topic}: connectivity is healthy.`;
  }
  if (name === "test_slow") {
    const delay = Number(args?.delay_ms || 0);
    await new Promise((resolve) => setTimeout(resolve, delay));
    return `waited ${delay}ms`;
  }
  if (name === "test_oversized") return "X".repeat(Number(args?.bytes || 1));
  if (name === "test_blocked_write") {
    if (process.env.PILOT_WRITE_MARKER) {
      const { writeFile } = await import("node:fs/promises");
      await writeFile(process.env.PILOT_WRITE_MARKER, "unexpected write\n", { flag: "a" });
    }
    return "write marker created";
  }
  throw new Error("unknown tool");
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", async (line) => {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return;
  }
  if (request.method === "notifications/initialized" || request.method === "notifications/cancelled") return;
  if (request.id === undefined) return;
  if (request.method === "initialize") {
    result(request.id, {
      protocolVersion: request.params?.protocolVersion || "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: "dotty-hermetic-pilot", version: "0.1.0" },
    });
    return;
  }
  if (request.method === "tools/list") {
    result(request.id, { tools });
    return;
  }
  if (request.method === "tools/call") {
    try {
      const text = await callTool(request.params?.name, request.params?.arguments);
      result(request.id, { content: [{ type: "text", text }], isError: false });
    } catch (cause) {
      result(request.id, {
        content: [{ type: "text", text: cause instanceof Error ? cause.message : "tool failed" }],
        isError: true,
      });
    }
    return;
  }
  if (request.method === "ping") {
    result(request.id, {});
    return;
  }
  error(request.id, -32601, "method not found");
});
