import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

export default function harnessRouter(pi: ExtensionAPI): void {
  pi.on("turn_start", async (_event, ctx) => {
    // turn_start is before the model call. The current user turn is available
    // through the session context; the command bridge extracts it and performs
    // a cached/Jev pre-decision without blocking tool execution.
    const result = await pi.exec("python3", [
      ".omp/hooks/pre/harness-router-predecision.py",
    ]);
    if (result.code !== 0) return;
  });
}
