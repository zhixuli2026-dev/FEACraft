# FF-F50 Codex 原生目标回合

2026-11-19。原生目标回合在模型结束并核验收尾后显示「等待继续」。工作台重新打开或刷新时读取已持久的回合状态。点击「继续目标」才提交一轮，可以先补充当前草稿。后台通过 Codex App Server 的 `thread/resume` 恢复原 thread ID，活动回合的停止使用原有取消入口。平台 API Agent 另有自己的有界自动续轮入口。[原合同 F50](feature-gap-audit.md) · [Codex App Server](https://developers.openai.com/codex/app-server/) · [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)

显式续聊请求绑定页面观察到的原生目标回合 ID。准入在既有会话写入协调内校验它仍是最近的成功目标回合，而且文件同步和进程收尾已核验。旧页面请求在真实模型启动前得到 `409 input_stale`，同一个客户端消息 ID 仍可读回原受理结果。普通原生发送与已支持的手动线程恢复保留原入口。[原生工作台](../web/src/panels/NativeChatPanel.tsx) · [准入](../server/app/services/codex_turns.py)

## 实际验收

真实 Edge 使用 Windows Codex `0.169.0-alpha.11.1` 和授权 DeepSeek `deepseek-flash`。首轮通过 PowerShell 读取隔离算例中未放入提示的随机字符串，模型回复后停在「等待继续」。刷新后仍显示同一个回合，实际模型请求数和已登记的回合数均未增加。补充任务并点击「继续目标」后，模型在同一原生线程返回上轮的准确字符串，期间没有再次读取文件。浏览器全程只发起了三次原生提交，`/goal:run` 请求为 0。[等待截图](../../tmp/live-native-goal-0fjbxnm3/native-goal-waiting.png) · [浏览器回执](../../tmp/live-native-goal-0fjbxnm3/browser-result.json)

旧目标回合的显式续聊请求返回真实 409，之后用最新回合作为目标，第三轮进入真实模型请求后点击「停止执行」。平台记录 `cancelled`，原生回合记录 `interrupted`，会话恢复就绪。共计四次官方 DeepSeek Responses HTTP 200，其中包含被停止回合的上游回执。三份会话停止证明有效。隔离 SQLite、文件存储和测试算例保留原始证据。[真实 HTTP 回执](../../tmp/live-native-goal-0fjbxnm3/upstream.json) · [完整原始回执](../../tmp/live-native-goal-0fjbxnm3/native-goal-result.json)

原始入口中，Windows API 停止后立即查询进程时仍获得同一身份，外层留下 `passed: false`。随后独立用 Uvicorn 进程号和 SQLite 持久启动身份核验原 API 确已退出，端口关闭。三个 Codex 根进程、三个 bridge 和八个运行环境探测范围均已退出。夹具将单次探测改为最多十秒的有界等待，并用实际 Windows 子进程验证其从 `match` 转为 `exited`。补验的模型调用为 0。C 盘构建前后均为 22.18 GiB，Docker 构建缓存为 0。[独立退出核验](../../tmp/live-native-goal-0fjbxnm3/cleanup-independent.json) · [等待逻辑补验](../../tmp/live-native-goal-0fjbxnm3/process-wait-supplement.json)

凭据审计扫描了隔离证据目录的 156 个文件，官方 API 密钥明文命中数为 0。自动命令执行策略拒绝移除 D 盘试验目录的单个 vault key。隔离解密文件和一条加密配置仍在，监听端口及对应进程已停止。[明文审计](../../tmp/live-native-goal-0fjbxnm3/credential-plaintext-audit.json) · [去活化回执](../../tmp/live-native-goal-0fjbxnm3/credential-retirement.json)

## 复验入口

从 `platform/server` 使用已审查的 `live_llm_*` 入口，复用已授权的模型配置和已安装的 Windows Codex。启动前核对 `--review` 清单中的源码和 Codex 二进制摘要。浏览器、API、SQLite、存储和凭据副本均在独立试验环境中。[实际验收](../server/live_llm_native_goal_browser_acceptance.py)

```powershell
& .venv/Scripts/python.exe -B -X utf8 live_llm_native_goal_browser_acceptance.py `
  --review D:/vibeflow_reverse/tmp/f50-native-goal-review-exit-fix-20261119.json `
  --source-profile-id 26 --dist D:/vibeflow_reverse/tmp/f50-native-goal-dist-20261119 `
  --browser-python C:/ProgramData/anaconda3/python.exe --channel msedge
```
