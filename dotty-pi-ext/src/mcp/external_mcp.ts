import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createMcpAdapter } from "pi-mcp-adapter";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { McpConfig, ServerEntry } from "pi-mcp-adapter/types";

export const DEFAULT_MCP_CONFIG_PATH = "/root/.pi/agent/mcp.json";

const EXPECTED_SETTINGS = {
  toolPrefix: "server",
  hostConfigDiscovery: "off",
  requestTimeoutMs: 5000,
  directTools: false,
  disableProxyTool: true,
  autoAuth: false,
  sampling: false,
  samplingAutoApprove: false,
  elicitation: false,
} as const;

const EXPECTED_OUTPUT_GUARD = {
  maxBytes: 8192,
  maxLines: 100,
  detailsMaxBytes: 2048,
} as const;

const ALLOWED_TOOLS: Record<string, readonly string[]> = {
  pilot: ["pilot_lookup"],
  home_assistant: ["GetLiveContext", "HassTurnOn", "HassTurnOff"],
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function sameStrings(left: unknown, right: readonly string[]): boolean {
  return Array.isArray(left)
    && left.length === right.length
    && left.every((value, index) => value === right[index]);
}

function assertServer(name: string, value: unknown): asserts value is ServerEntry {
  if (!isRecord(value)) throw new Error(`MCP server ${name} must be an object`);
  const allowed = ALLOWED_TOOLS[name];
  if (!allowed) throw new Error(`MCP server ${name} is not approved`);
  if (!sameStrings(value.includeTools, allowed)) {
    throw new Error(`MCP server ${name} includeTools must match the approved allowlist`);
  }
  if (!sameStrings(value.directTools, allowed)) {
    throw new Error(`MCP server ${name} directTools must match includeTools`);
  }
  if (value.exposeResources !== false) {
    throw new Error(`MCP server ${name} must disable resources`);
  }
  const hasCommand = typeof value.command === "string" && value.command.length > 0;
  const hasUrl = typeof value.url === "string" && value.url.length > 0;
  if (hasCommand === hasUrl) {
    throw new Error(`MCP server ${name} must define exactly one transport`);
  }
  if ("bearerToken" in value || "oauth" in value) {
    throw new Error(`MCP server ${name} contains inline authentication material`);
  }
  if (value.headers !== undefined) {
    if (!isRecord(value.headers)) throw new Error(`MCP server ${name} headers are invalid`);
    for (const header of Object.values(value.headers)) {
      if (typeof header !== "string" || !/^\$\{[A-Z][A-Z0-9_]*\}$/.test(header)) {
        throw new Error(`MCP server ${name} headers must be environment references`);
      }
    }
  }
  if (value.env !== undefined) {
    if (!isRecord(value.env)) throw new Error(`MCP server ${name} env is invalid`);
    for (const envValue of Object.values(value.env)) {
      if (typeof envValue !== "string" || !/^\$\{[A-Z][A-Z0-9_]*\}$/.test(envValue)) {
        throw new Error(`MCP server ${name} env values must be environment references`);
      }
    }
  }
  if (name === "pilot" && !hasCommand) throw new Error("Pilot MCP must use stdio");
  if (name === "pilot" && value.cwd !== "${DOTTY_MCP_EXTENSION_DIR}") {
    throw new Error("Pilot MCP cwd must use the extension directory environment reference");
  }
  if (name === "home_assistant") {
    if (!hasUrl || value.auth !== "bearer" || value.bearerTokenEnv !== "HOMEASSISTANT_TOKEN") {
      throw new Error("Home Assistant MCP must use the approved bearer environment token");
    }
    if (value.url !== "${HOMEASSISTANT_MCP_URL}/api/mcp") {
      throw new Error("Home Assistant MCP URL must remain LAN environment-configured");
    }
  }
}

export function parseExternalMcpConfig(text: string): McpConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("MCP config is not valid JSON");
  }
  if (!isRecord(parsed) || !isRecord(parsed.settings) || !isRecord(parsed.mcpServers)) {
    throw new Error("MCP config must contain settings and mcpServers objects");
  }
  if ("imports" in parsed) throw new Error("MCP config imports are forbidden");
  for (const [key, expected] of Object.entries(EXPECTED_SETTINGS)) {
    if (parsed.settings[key] !== expected) {
      throw new Error(`MCP setting ${key} must remain fail-closed`);
    }
  }
  if (!isRecord(parsed.settings.outputGuard)) throw new Error("MCP outputGuard is required");
  for (const [key, expected] of Object.entries(EXPECTED_OUTPUT_GUARD)) {
    if (parsed.settings.outputGuard[key] !== expected) {
      throw new Error(`MCP outputGuard.${key} has an unsafe value`);
    }
  }
  const names = Object.keys(parsed.mcpServers);
  if (names.length === 0) throw new Error("MCP config must contain an approved server");
  for (const [name, server] of Object.entries(parsed.mcpServers)) assertServer(name, server);
  return structuredClone(parsed) as unknown as McpConfig;
}

function enabled(value: string | undefined, defaultValue: boolean): boolean {
  if (value === undefined || value.trim() === "") return defaultValue;
  return !["0", "false", "no", "off"].includes(value.trim().toLowerCase());
}

export function isPrivateHomeAssistantUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    if (!url.hostname || !["http:", "https:"].includes(url.protocol)) return false;
    if (url.username || url.password || url.search || url.hash) return false;
    if (url.pathname !== "/" && url.pathname !== "") return false;
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (host === "localhost" || host.endsWith(".local") || host.endsWith(".home.arpa")) return true;
    if (!host.includes(".") && !host.includes(":")) return true;
    const parts = host.split(".").map(Number);
    if (parts.length === 4 && parts.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
      return parts[0] === 10
        || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
        || (parts[0] === 192 && parts[1] === 168)
        || parts[0] === 127;
    }
    return host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe8") || host.startsWith("fe9") || host.startsWith("fea") || host.startsWith("feb");
  } catch {
    return false;
  }
}

export function loadExternalMcpConfig(
  path = process.env.DOTTY_MCP_CONFIG_PATH || DEFAULT_MCP_CONFIG_PATH,
): McpConfig {
  const config = parseExternalMcpConfig(readFileSync(path, "utf8"));
  config.mcpServers.pilot.disabled = !enabled(process.env.DOTTY_MCP_PILOT_ENABLED, true);
  const homeAssistantEnabled = enabled(
    process.env.DOTTY_MCP_HOME_ASSISTANT_ENABLED,
    false,
  );
  if (homeAssistantEnabled) {
    if (!isPrivateHomeAssistantUrl(process.env.HOMEASSISTANT_MCP_URL || "")) {
      throw new Error("HOMEASSISTANT_MCP_URL must be a private LAN URL");
    }
    if (!(process.env.HOMEASSISTANT_TOKEN || "").trim()) {
      throw new Error("HOMEASSISTANT_TOKEN is required when Home Assistant MCP is enabled");
    }
  }
  config.mcpServers.home_assistant.disabled = !homeAssistantEnabled;
  return config;
}

export function registerExternalMcp(pi: ExtensionAPI): boolean {
  // Missing configuration is disabled: an operator must opt into even the
  // hermetic pilot after the deployment and baseline gates have passed.
  if (!enabled(process.env.DOTTY_MCP_ENABLED, false)) return false;
  try {
    process.env.DOTTY_MCP_EXTENSION_DIR ||= fileURLToPath(new URL("../..", import.meta.url));
    const config = loadExternalMcpConfig();
    if (Object.values(config.mcpServers).every((server) => server.disabled === true)) return false;
    // MCP prompt templates are intentionally not part of Dotty's voice
    // surface. Suppress only the adapter's generated mcp__ prompt commands;
    // management/status commands remain available to operators.
    const promptSafePi = new Proxy(pi, {
      get(target, property, receiver) {
        if (property !== "registerCommand") return Reflect.get(target, property, receiver);
        return (name: string, command: unknown) => {
          if (name.startsWith("mcp__")) return;
          target.registerCommand(name, command as Parameters<typeof target.registerCommand>[1]);
        };
      },
    });
    createMcpAdapter({ config })(promptSafePi);
    pi.registerCommand("dotty-mcp-status", {
      description: "Show Dotty's active external MCP tool allowlist",
      handler: async (_args, ctx) => {
        const tools = pi.getActiveTools()
          .filter((name) => name.startsWith("pilot_") || name.startsWith("home_assistant_"))
          .sort();
        ctx.ui.notify(`Dotty MCP tools: ${tools.join(", ") || "none"}`, "info");
      },
    });
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : "unknown configuration error";
    console.error(`Dotty external MCP disabled: ${message}`);
    return false;
  }
}
