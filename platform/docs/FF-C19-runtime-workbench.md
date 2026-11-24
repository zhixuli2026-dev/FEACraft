# FF-C19 作业面板运行环境

「作业 → 新建任务 → 单作业」可选择 Windows cmd、WSL vibeflow-linux 或 WSL 隔离沙箱。核对页显示实际将提交的运行环境和命令。会话保存原提交，遇到可重试的存储忙响应时沿用原请求。普通 WSL 和隔离沙箱均以非特权身份运行。

2026-11-20，Edge 连接独立 SQLite 和文件系统 API，实际提交 Windows `echo`、WSL `id -u; pwd`、沙箱 `id -u; pwd`。Windows 输出预期标记。WSL 返回 UID 65534 和 `/mnt/` 下工作目录，沙箱返回 UID 65534 和 `/case`。另一个 WSL 作业实际运行后由浏览器取消，持久尝试为已取消，退出核验通过。每个算例只有一个工作台提交。浏览器无脚本异常，真实模型 0 次，模拟 0 次。[浏览器与持久结果](../../tmp/ff-c19-runtime-browser-9g1cr623/runtime-browser.json)

实际重启隔离 API 后，四个作业保持原 ID、终态及退出证明。独立用户不能提交，组织只读成员可以查看原作业，不能提交。本项使用当前运行环境入口和原有持久数据结构，交付无需数据库迁移。[隔离 API 结束记录](../../tmp/ff-c19-runtime-browser-9g1cr623/result.json) · [浏览器截图](../../tmp/ff-c19-runtime-browser-9g1cr623/runtime-browser.png)
