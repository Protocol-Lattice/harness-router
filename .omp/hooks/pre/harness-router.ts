import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { spawn } from "node:child_process";

export default function harnessRouter(pi: ExtensionAPI): void {
  pi.on("context", async (event) => {
    const messages = event.messages as Array<{ role?: string; content?: unknown }>;
    const user = [...messages].reverse().find((m) => m?.role === "user");
    if (!user) return;
    const text = typeof user.content === "string"
      ? user.content
      : JSON.stringify(user.content ?? "");
    const goal = text.slice(0, 1600);
    const root = process.cwd();
    const catalog = `${root}/.omp/harness-router-tools.json`;
    const child = spawn("python3", [`${root}/hooks/pre_decision.py`], {
      cwd: root,
      env: {
        ...process.env,
        HARNESS_ROUTER_GOAL: goal,
        HARNESS_ROUTER_TOOLS_FILE: catalog,
        HARNESS_ROUTER_HARNESS: "ohmypi",
      },
      stdio: ["ignore", "ignore", "ignore"],
      detached: true,
    });
    child.unref();
  });
}
