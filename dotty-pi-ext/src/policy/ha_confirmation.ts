import { createHash } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MARKER = "[DOTTY_TURN_CONTEXT_V1]";
const WRITE_TOOL = /^(?:home_assistant_)?HassTurn(On|Off)$/i;

type TurnContext = {
  session: string;
  turn: number;
  utterance: string;
};

type PendingAction = {
  session: string;
  turn: number;
  toolName: string;
  argsHash: string;
  action: "on" | "off";
  friendlyName: string;
  expectedConfirmation: string;
  deadline: number;
};

export type PolicyResult = { block: true; reason: string } | undefined;

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, canonicalize(child)]),
    );
  }
  return value;
}

export function canonicalArgsHash(args: Record<string, unknown>): string {
  return createHash("sha256").update(JSON.stringify(canonicalize(args))).digest("hex");
}

export function normalizeConfirmation(text: string): string {
  return text
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replace(/[\p{P}]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function actionForTool(toolName: string): "on" | "off" | null {
  const match = toolName.match(WRITE_TOOL);
  if (!match) return null;
  return match[1].toLowerCase() === "on" ? "on" : "off";
}

function validateTarget(args: Record<string, unknown>): string | null {
  const allowed = new Set(["name", "domain"]);
  if (Object.keys(args).some((key) => !allowed.has(key))) return null;
  if (args.domain !== undefined && args.domain !== "light") return null;
  if (typeof args.name !== "string") return null;
  const name = args.name.trim().replace(/\s+/g, " ");
  if (!name || name.length > 100) return null;
  return name;
}

function parseFinalContext(text: string): { cleanText: string; context: TurnContext } | null {
  const index = text.lastIndexOf(`\n${MARKER}`);
  if (index < 0) return null;
  const encoded = text.slice(index + MARKER.length + 1).trim();
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) return null;
  try {
    const raw = Buffer.from(encoded, "base64url").toString("utf8");
    const context = JSON.parse(raw) as Partial<TurnContext>;
    if (
      typeof context.session !== "string"
      || typeof context.turn !== "number"
      || !Number.isSafeInteger(context.turn)
      || typeof context.utterance !== "string"
    ) return null;
    return {
      cleanText: text.slice(0, index),
      context: context as TurnContext,
    };
  } catch {
    return null;
  }
}

export class HomeAssistantConfirmationPolicy {
  private current: TurnContext | null = null;
  private pending: PendingAction | null = null;
  private approval: PendingAction | null = null;
  private cancelledThisTurn = false;
  private approvalConsumedThisTurn = false;
  private writeAttemptedThisTurn = false;

  constructor(private readonly now: () => number = Date.now) {}

  handleInput(text: string, source: string): { action: "transform"; text: string } | undefined {
    this.current = null;
    this.approval = null;
    this.cancelledThisTurn = false;
    this.approvalConsumedThisTurn = false;
    this.writeAttemptedThisTurn = false;
    if (source !== "rpc") {
      this.pending = null;
      return undefined;
    }
    const parsed = parseFinalContext(text);
    if (!parsed) {
      this.pending = null;
      return undefined;
    }
    this.current = parsed.context;
    if (this.pending) {
      const exact = normalizeConfirmation(parsed.context.utterance)
        === normalizeConfirmation(this.pending.expectedConfirmation);
      if (
        parsed.context.session
        && parsed.context.session === this.pending.session
        && this.now() < this.pending.deadline
        && exact
      ) {
        this.approval = this.pending;
        this.pending = null;
      } else {
        this.pending = null;
        this.cancelledThisTurn = true;
      }
    }
    return { action: "transform", text: parsed.cleanText };
  }

  handleToolCall(toolName: string, input: Record<string, unknown>): PolicyResult {
    const action = actionForTool(toolName);
    if (!action) return undefined;

    if (!this.current?.session) {
      this.clear();
      return { block: true, reason: "DOTTY_CONFIRMATION_DENIED: missing trusted voice session" };
    }

    if (this.approval) {
      const approval = this.approval;
      this.approval = null;
      this.approvalConsumedThisTurn = true;
      const matches = this.current.session === approval.session
        && this.now() < approval.deadline
        && toolName === approval.toolName
        && canonicalArgsHash(input) === approval.argsHash;
      if (matches) return undefined;
      return { block: true, reason: "DOTTY_CONFIRMATION_MISMATCH: action changed or expired" };
    }

    if (this.approvalConsumedThisTurn) {
      return { block: true, reason: "DOTTY_CONFIRMATION_CANCELLED: approval already consumed" };
    }

    if (this.cancelledThisTurn) {
      return { block: true, reason: "DOTTY_CONFIRMATION_CANCELLED: pending action cancelled" };
    }

    if (this.writeAttemptedThisTurn || this.pending?.turn === this.current.turn) {
      this.pending = null;
      this.cancelledThisTurn = true;
      return { block: true, reason: "DOTTY_CONFIRMATION_CANCELLED: multiple actions requested" };
    }
    this.writeAttemptedThisTurn = true;

    const friendlyName = validateTarget(input);
    if (!friendlyName) {
      this.pending = null;
      return {
        block: true,
        reason: "DOTTY_CONFIRMATION_DENIED: choose one named light only",
      };
    }

    const expectedConfirmation = `Confirm ${action} ${friendlyName}`;
    this.pending = {
      session: this.current.session,
      turn: this.current.turn,
      toolName,
      argsHash: canonicalArgsHash(input),
      action,
      friendlyName,
      expectedConfirmation,
      deadline: this.now() + 15_000,
    };
    return {
      block: true,
      reason: `DOTTY_CONFIRMATION_REQUIRED: ${expectedConfirmation}`,
    };
  }

  finishTurn(): void {
    this.approval = null;
    this.current = null;
    this.cancelledThisTurn = false;
    this.approvalConsumedThisTurn = false;
    this.writeAttemptedThisTurn = false;
  }

  clear(): void {
    this.current = null;
    this.pending = null;
    this.approval = null;
    this.cancelledThisTurn = false;
    this.approvalConsumedThisTurn = false;
    this.writeAttemptedThisTurn = false;
  }

  pendingForTest(): Readonly<PendingAction> | null {
    return this.pending;
  }
}

export function installHomeAssistantConfirmationPolicy(pi: ExtensionAPI): void {
  const policy = new HomeAssistantConfirmationPolicy();
  pi.on("input", async (event) => policy.handleInput(event.text, event.source));
  pi.on("tool_call", async (event) => policy.handleToolCall(event.toolName, event.input));
  pi.on("agent_end", async () => policy.finishTurn());
  pi.on("session_shutdown", async () => policy.clear());
}
