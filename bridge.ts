import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export default function (pi: ExtensionAPI) {
  const pane = process.env.TMUX_PANE;
  if (!process.env.TMUX || !pane) return;
  const script = fileURLToPath(new URL("./sidebar.py", import.meta.url));
  const birth = readFileSync("/proc/self/stat", "utf8").split(") ").at(-1)!.split(" ")[19];
  let busy = false;

  async function report(ctx: ExtensionContext) {
    await pi.exec("tmux", ["set-option", "-p", "-t", pane!, "@pi_sidebar", JSON.stringify({
      pid: process.pid, birth, file: ctx.sessionManager.getSessionFile(),
      name: ctx.sessionManager.getSessionName(), cwd: ctx.cwd, busy,
    })], { timeout: 2000 });
  }

  pi.on("session_start", async (_event, ctx) => { busy = !ctx.isIdle(); await report(ctx); });
  pi.on("session_info_changed", async (_event, ctx) => { await report(ctx); });
  pi.on("agent_start", async (_event, ctx) => { busy = true; await report(ctx); });
  pi.on("agent_settled", async (_event, ctx) => { busy = !ctx.isIdle(); await report(ctx); });
  pi.on("session_shutdown", async () => {
    await pi.exec("tmux", ["set-option", "-p", "-u", "-t", pane!, "@pi_sidebar"], { timeout: 2000 });
  });
  pi.registerCommand("session-sidebar", {
    description: "展开可折叠、可置顶的会话左栏",
    handler: async (_args, ctx) => {
      const result = await pi.exec("python3", [script, "open", "--source", pane!], { timeout: 10000 });
      if (result.code !== 0) ctx.ui.notify(result.stderr || "会话栏启动失败", "error");
    },
  });
}
