import assert from "node:assert/strict";

process.env.DOTTY_ADMIN_TOKEN = "test-token";
const { runDeviceStatus } = await import("../src/tools/device_status.ts");

const original = globalThis.fetch;
let seenUrl = "";
let seenToken: string | null = null;

try {
  globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
    seenUrl = String(url);
    seenToken = new Headers(init?.headers).get("X-Admin-Token");
    return new Response(JSON.stringify({
      ok: true,
      status: { audio_speaker: { volume: 70 }, battery: { level: 88 } },
    }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;

  const got = JSON.parse(await runDeviceStatus());
  assert.equal(got.audio_speaker.volume, 70);
  assert.equal(got.battery.level, 88);
  assert.ok(seenUrl.endsWith("/xiaozhi/admin/device-status"));
  assert.equal(seenToken, "test-token");

  globalThis.fetch = (async () => new Response(null, { status: 503 })) as typeof fetch;
  assert.equal(await runDeviceStatus(), "(device status unavailable)");
} finally {
  globalThis.fetch = original;
}

console.log("device_status: 2/2 pass");
