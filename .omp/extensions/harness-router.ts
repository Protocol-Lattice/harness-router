/** ohmypi tool_call hooks backed by the portable Harness Router MCP bridge. */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const bridge = fileURLToPath(new URL("../hooks/pre_tool_use.py", import.meta.url));
const redirectType = "harness-router.ohmypi.redirect";

function isRouter(name: string): boolean {
  return name.toLowerCase().replaceAll("-", "_").includes("harness_router");
}

function latestUser(ctx: ExtensionContext): { id: string; text: string; redirected: boolean } {
  const branch = ctx.sessionManager.getBranch();
  for (let i = branch.length - 1; i >= 0; i--) {
    const entry = branch[i];
    if (entry.type !== "message" || entry.message.role !== "user") continue;
    if (entry.message.attribution === "agent") continue;
    const content = entry.message.content;
    const text = typeof content === "string" ? content : content
      .filter((block) => block.type === "text").map((block) => block.text).join(" ");
    const redirected = branch.slice(i + 1).some((item) => item.type === "custom"
      && item.customType === redirectType
      && (item.data as { userEntryId?: string } | undefined)?.userEntryId === entry.id);
    return { id: entry.id, text: text.slice(0, 1600), redirected };
  }
  return { id: "", text: "", redirected: false };
}

function liveTools(pi: ExtensionAPI) {
  const active = new Set(pi.getActiveTools());
  return pi.getAllTools().filter((tool) => active.has(tool.name) && !isRouter(tool.name));
}

async function atomicJSON(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

async function publishCatalog(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  try {
    const sessionId = ctx.sessionManager.getSessionId();
    const key = createHash("sha256").update(sessionId).digest("hex");
    const tools = liveTools(pi);
    const catalog = { provider: "ohmypi", session_id: sessionId,
      generated_at: new Date().toISOString(), tools };
    const root = join(ctx.cwd, ".omp");
    await atomicJSON(join(root, "harness-router", "sessions", `${key}.json`), catalog);
    await atomicJSON(join(root, "harness-router-tools.json"), catalog);
  } catch {
    // Catalog files are for inspection; routing reads the live runtime registry.
  }
}

function runBridge(payload: unknown, cwd: string): Promise<Record<string, unknown>> {
  const seconds = Number(process.env.HARNESS_ROUTER_PRETOOL_TIMEOUT ?? "4");
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 3600) return Promise.resolve({});
  return new Promise((resolve) => {
    // A process group lets the outer deadline also stop the MCP/graph children on POSIX.
    const child = spawn(process.env.HARNESS_ROUTER_PYTHON_BIN || "python3", [bridge], {
      cwd, detached: process.platform !== "win32", stdio: ["pipe", "pipe", "ignore"],
    });
    let output = "";
    let settled = false;
    const finish = (value: Record<string, unknown> = {}) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch { /* The process may already have exited. */ }
      child.stdin.destroy();
      child.stdout.destroy();
      resolve(value);
    };
    // Leave time for Python startup and its bounded MCP cleanup.
    const timer = setTimeout(() => finish(), (seconds + 1) * 1000);
    child.on("error", () => finish());
    child.stdin.on("error", () => finish());
    child.stdout.on("error", () => finish());
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      output += chunk;
      if (output.length > 64_000) finish();
    });
    child.on("close", (code) => {
      if (code !== 0) return finish();
      try {
        const result = JSON.parse(output);
        finish(result && typeof result === "object" && !Array.isArray(result) ? result : {});
      } catch { finish(); }
    });
    try { child.stdin.end(JSON.stringify(payload)); } catch { finish(); }
  });
}

export default function harnessRouter(pi: ExtensionAPI): void {
  let state = { goal: "", redirected: false };

  const restore = async (_event: unknown, ctx: ExtensionContext) => {
    state = { goal: "", redirected: false };
    try {
      const user = latestUser(ctx);
      state = { goal: user.text, redirected: user.redirected };
    } catch { /* The session may not have any branch history yet. */ }
    await publishCatalog(pi, ctx);
  };
  pi.on("session_start", restore);
  pi.on("session_switch", restore);
  pi.on("session_branch", restore);
  pi.on("session_tree", restore);
  pi.on("session_shutdown", () => { state = { goal: "", redirected: true }; });
  pi.on("before_agent_start", async (event, ctx) => {
    state = { goal: event.prompt.slice(0, 1600), redirected: false };
    await publishCatalog(pi, ctx);
  });

  pi.on("tool_call", async (event, ctx) => {
    // ohmypi blocks tool calls if an extension throws. Catch all bridge failures.
    try {
      const turn = state;
      if (turn.redirected || isRouter(event.toolName)) return;
      const user = latestUser(ctx);
      if (user.redirected) return;
      const tools = liveTools(pi);
      if (tools.length < 2 || !tools.some((tool) => tool.name === event.toolName)) return;
      const sessionId = ctx.sessionManager.getSessionId();
      const result = await runBridge({ hook_event_name: "tool_call", cwd: ctx.cwd,
        session_id: sessionId, tool_name: event.toolName, tool_input: event.input,
        goal: turn.goal || user.text, tools }, ctx.cwd);
      if (state !== turn || turn.redirected || ctx.sessionManager.getSessionId() !== sessionId) return;
      if (result.block !== true || typeof result.reason !== "string" || !result.reason
        || typeof result.tool !== "string" || result.tool === event.toolName
        || !tools.some((tool) => tool.name === result.tool)
        || !pi.getActiveTools().includes(result.tool)) return;
      // No await between checking and claiming: parallel calls can redirect only once.
      turn.redirected = true;
      pi.appendEntry(redirectType, { userEntryId: user.id, tool: result.tool });
      return { block: true, reason: result.reason };
    } catch {
      return;
    }
  });
}
