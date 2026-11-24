// 终端 WS 冒烟：连接 /ws/terminal，发 echo 命令，读输出
import WebSocket from "../web/node_modules/ws/index.js";

const ws = new WebSocket("ws://localhost:8000/ws/terminal?cols=80&rows=24");
let buf = "";
const timer = setTimeout(() => { console.log("TIMEOUT，已收数据长度:", buf.length); process.exit(1); }, 15000);

ws.on("open", () => {
  console.log("ws open");
});
ws.on("message", (data, isBinary) => {
  if (isBinary) {
    buf += data.toString("utf8");
  } else {
    const s = data.toString();
    if (s.startsWith("{")) {
      const m = JSON.parse(s);
      console.log("ctrl:", m);
      if (m.type === "ready") {
        // 打一条有明确标记的命令
        ws.send(JSON.stringify({ type: "input", data: "echo VIBEFLOW_PTY_OK_$PID\r" }));
      }
    } else {
      buf += s;
    }
  }
  if (buf.includes("VIBEFLOW_PTY_OK_")) {
    console.log("PTY 回显验证通过 ✓");
    console.log("输出片段:", JSON.stringify(buf.slice(-120)));
    clearTimeout(timer);
    ws.close();
    process.exit(0);
  }
});
ws.on("error", (e) => { console.error("ws error:", e.message); process.exit(1); });
