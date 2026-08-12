import assert from "node:assert/strict";
import { test } from "node:test";

import {
  authorized,
  buildChildEnv,
  buildCodexArgs,
  createBrokerServer,
} from "./broker.mjs";

test("authorization requires the exact bearer token", () => {
  assert.equal(authorized("Bearer private-token", "private-token"), true);
  assert.equal(authorized("Bearer wrong-token", "private-token"), false);
  assert.equal(authorized(undefined, "private-token"), false);
});

test("Codex child environment omits broker and unrelated secrets", () => {
  const environment = buildChildEnv({
    HOME: "/home/node",
    PATH: "/usr/bin",
    CODEX_HOME: "/home/node/.codex",
    DOTTY_CODEX_BROKER_TOKEN: "must-not-leak",
    OPENAI_API_KEY: "must-not-leak-either",
  });
  assert.deepEqual(environment, {
    CODEX_HOME: "/home/node/.codex",
    HOME: "/home/node",
    PATH: "/usr/bin",
  });
});

test("Codex invocation is ephemeral, strict, and rooted in the empty workspace", () => {
  const args = buildCodexArgs("What changed today?");
  assert.equal(args[0], "exec");
  assert.ok(args.includes("--ephemeral"));
  assert.ok(args.includes("--strict-config"));
  assert.ok(args.includes("--ignore-rules"));
  assert.equal(args[args.indexOf("--cd") + 1], "/workspace");
  assert.match(args.at(-1), /use live web search/i);
  assert.match(args.at(-1), /What changed today\?/);
});

test("HTTP interface is authenticated and only returns broker results", async (context) => {
  const queries = [];
  const server = createBrokerServer({
    token: "private-token",
    search: async (query) => {
      queries.push(query);
      return "Current answer with sources.";
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const { port } = server.address();
  const base = `http://127.0.0.1:${port}`;

  const health = await fetch(`${base}/healthz`);
  assert.equal(health.status, 200);

  const denied = await fetch(`${base}/search`, {
    method: "POST",
    body: JSON.stringify({ query: "private" }),
  });
  assert.equal(denied.status, 401);

  const allowed = await fetch(`${base}/search`, {
    method: "POST",
    headers: {
      authorization: "Bearer private-token",
      "content-type": "application/json",
    },
    body: JSON.stringify({ query: "What changed today?" }),
  });
  assert.equal(allowed.status, 200);
  assert.deepEqual(await allowed.json(), { result: "Current answer with sources." });
  assert.deepEqual(queries, ["What changed today?"]);
});
