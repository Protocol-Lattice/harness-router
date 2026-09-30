/** ohmypi decision-loop hooks backed by the shared Harness Router bridge. */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const bridge = fileURLToPath(new URL("../../hooks/pre_decision.py", import.meta.url));

type RouterState = {
  goal: string;
  nextTool: string;
  redirected: boolean;
  baselineTools: string[];
  enforcing: boolean;
  preparedPrompt: string;
};

function isRouter(name: string): boolean {
  return name.toLowerCase().replaceAll("-", "_").includes("harness_router");
}

function latestUser(ctx: ExtensionContext): { id: string; text: string } {
  const branch = ctx.sessionManager.getBranch();
  for (let i = branch.length - 1; i >= 0; i--) {
    const entry = branch[i];
    if (entry.type !== "message" || entry.message.role !== "user") continue;
    if (entry.message.attribution === "agent") continue;
    const content = entry.message.content;
    const text = typeof content === "string"
      ? content
      : content.filter((block) => block.type === "text").map((block) => block.text).join(" ");
    return { id: entry.id, text: text.slice(0, 1600) };
  }
  return { id: "", text: "" };
}

function liveTools(pi: ExtensionAPI, baselineTools: string[]) {
  const active = new Set(baselineTools.length ? baselineTools : pi.getActiveTools());
  return pi.getAllTools().filter((tool) => active.has(tool.name) && !isRouter(tool.name));
}

function baselineToolNames(pi: ExtensionAPI, state: RouterState): string[] {
  return state.baselineTools.length ? [...state.baselineTools] : pi.getActiveTools();
}

async function restoreBaseline(pi: ExtensionAPI, state: RouterState): Promise<void> {
  if (!state.baselineTools.length) return;
  await pi.setActiveTools([...state.baselineTools]);
  state.enforcing = false;
}

async function atomicJSON(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${Date.now()}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

async function publishCatalog(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  state: RouterState,
): Promise<void> {
  try {
    const sessionId = ctx.sessionManager.getSessionId();
    const key = createHash("sha256").update(sessionId).digest("hex");
    const tools = liveTools(pi, baselineToolNames(pi, state));
    const catalog = { provider: "ohmypi", session_id: sessionId,
      generated_at: new Date().toISOString(), tools };
    const root = join(ctx.cwd, ".omp");
    await atomicJSON(join(root, "harness-router", "sessions", `${key}.json`), catalog);
    await atomicJSON(join(root, "harness-router-tools.json"), catalog);
  } catch {
    // Catalog persistence is diagnostic; liveTools remains authoritative.
  }
}

function runRouter(
  payload: Record<string, unknown>,
  cwd: string,
): Promise<Record<string, unknown>> {
  const seconds = Number(process.env.HARNESS_ROUTER_PREDECISION_TIMEOUT ?? "4");
  if (!Number.isFinite(seconds) || seconds <= 0) return Promise.resolve({});
  return new Promise((resolve) => {
    const child = spawn(process.env.HARNESS_ROUTER_PYTHON_BIN || "python3", [bridge], {
      cwd, detached: process.platform !== "win32", stdio: ["pipe", "pipe", "ignore"],
    });
    let output = "";
    let settled = false;
    const finish = (value: Record<string, unknown>) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch {}
      child.stdin.destroy();
      child.stdout.destroy();
      resolve(value);
    };
    const timer = setTimeout(() => finish({}), (seconds + 1) * 1000);
    child.on("error", () => finish({}));
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      output += chunk;
      if (output.length > 64_000) finish({});
    });
    child.on("close", (code) => {
      if (code !== 0) return finish({});
      try {
        const value = JSON.parse(output.trim().split("\n", 1)[0]);
        finish(value && typeof value === "object" && !Array.isArray(value) ? value : {});
      } catch {
        finish({});
      }
    });
    try {
      child.stdin.end(JSON.stringify(payload));
    } catch {
      finish({});
    }
  });
}

async function precompute(
  state: RouterState,
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  observation: unknown,
  lastAction: string | null,
): Promise<void> {
  const tools = liveTools(pi, baselineToolNames(pi, state));
  if (!state.goal || tools.length < 2) return;
  const result = await runRouter({
    harness: "ohmypi",
    hook: "tool_result",
    cwd: ctx.cwd,
    session_id: ctx.sessionManager.getSessionId(),
    goal: state.goal,
    observation: typeof observation === "string"
      ? observation.slice(0, 4000)
      : JSON.stringify(observation).slice(0, 4000),
    last_action: lastAction,
    tools,
  }, ctx.cwd);
  const selected = typeof result.tool === "string" ? result.tool.trim() : "";
  const confidence = Number(result.confidence);
  const threshold = Number(
    process.env.HARNESS_ROUTER_PREDECISION_THRESHOLD ?? "0.85",
  );

  if (
    selected &&
    !result.fallback &&
    baselineToolNames(pi, state).includes(selected) &&
    Number.isFinite(confidence) &&
    confidence >= threshold
  ) {
    state.nextTool = selected;
    await pi.setActiveTools([selected]);
    state.enforcing = true;
  } else {
    state.nextTool = "";
    await restoreBaseline(pi, state);
  }
}

export default function harnessRouter(pi: ExtensionAPI): void {
  const state: RouterState = {
    goal: "",
    nextTool: "",
    redirected: false,
    baselineTools: pi.getActiveTools(),
    enforcing: false,
    preparedPrompt: "",
  };

  pi.on("session_start", async (_event, ctx) => {
    state.goal = latestUser(ctx).text;
    state.nextTool = "";
    state.redirected = false;
    state.enforcing = false;
    state.preparedPrompt = "";
    state.baselineTools = pi.getActiveTools();
    await publishCatalog(pi, ctx, state);
  });

  pi.on("session_switch", async (_event, ctx) => {
    state.goal = latestUser(ctx).text;
    state.nextTool = "";
    state.redirected = false;
    await publishCatalog(pi, ctx);
  });

  pi.on("session_branch", async (_event, ctx) => {
    state.goal = latestUser(ctx).text;
    state.nextTool = "";
    state.redirected = false;
    await publishCatalog(pi, ctx);
  });

  pi.on("before_agent_start", async (event, ctx) => {
    const prompt = event.prompt.slice(0, 1600);

    // setActiveTools() can cause policy preparation to repeat. Preserve the
    // original active set and avoid routing twice for the same prompt.
    if (state.preparedPrompt === prompt && state.enforcing && state.nextTool) {
      return;
    }

    if (state.enforcing) {
      try {
        await restoreBaseline(pi, state);
      } catch {
        state.enforcing = false;
      }
    }

    state.goal = prompt;
    state.nextTool = "";
    state.redirected = false;
    state.enforcing = false;
    state.preparedPrompt = prompt;
    state.baselineTools = pi.getActiveTools();
    await publishCatalog(pi, ctx, state);
    await precompute(state, pi, ctx, "Initial decision state", null);
  });

  pi.on("tool_result", async (event, ctx) => {
    try {
      await precompute(
        state,
        pi,
        ctx,
        {
          content: (event as any).content,
          details: (event as any).details,
          isError: (event as any).isError,
        },
        event.toolName,
      );
    } catch {
      // Precomputation is fail-open and must never break tool_result handling.
    }
  });

  pi.on("tool_call", async (event) => {
    try {
      if (isRouter(event.toolName)) return;
      const expected = state.nextTool;
      if (!expected || expected === event.toolName) return;

      const reason = (
        `Harness Router selected ${expected} as the next tool and restricted the active ` +
        `tool set to that choice, but the runtime requested ${event.toolName}. Refresh the ` +
        `current router decision before executing another tool.`
      );
      return { block: true, reason };
    } catch {
      return;
    }
  });
}
