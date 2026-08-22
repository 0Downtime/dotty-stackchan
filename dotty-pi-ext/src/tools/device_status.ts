import { Type } from "typebox";
import { fetchDeviceStatus, type AdminOptions } from "../lib/xiaozhi_admin.ts";

export async function runDeviceStatus(opts: AdminOptions = {}): Promise<string> {
  return await fetchDeviceStatus(opts);
}

export const deviceStatusTool = {
  name: "device_status",
  label: "Device Status",
  description:
    "Read Dotty's current device status from the robot, including speaker " +
    "volume, battery, screen, and network information. Use for questions " +
    "about Dotty's condition right now; never guess current values.",
  promptSnippet: "Read Dotty's current firmware device status.",
  promptGuidelines: [
    "Call device_status for current speaker volume, battery, screen, or " +
      "network questions. Base the answer on the returned status.",
  ],
  parameters: Type.Object({}),
  async execute(
    _toolCallId: string,
    _params: Record<string, never>,
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = await runDeviceStatus();
    return { content: [{ type: "text", text }] };
  },
};
