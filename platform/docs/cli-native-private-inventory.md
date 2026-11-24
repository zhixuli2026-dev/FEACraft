# WSL 原生私有目录计量与真实模型验收

2026-11-07。接续 [私有写前用户准入](cli-private-user-admission.md)。用户再次明确禁止模拟 LLM；本次所有模型回复、流式输出、工具选择和续答均调用本地已授权配置的真实 DeepSeek，失败算例没有模拟补过。

## 实现

显式配置的 `codex_wsl_state_root` 通过选定发行版的 WSL 路径加入节点总量；其中 `users/<id>` 加入该用户的用量。未配置时不猜测路径；已配置但不可访问时拒绝计量，不能算零。根身份、路径重定向、共享截止时间及累计条目上限保持检查。私有写入还检查原生目标卷剩余空间。

实际 Linux CLI 初始化会在 `home/tmp/arg0-linux` 创建 `apply_patch`、`applypatch` 和 `codex-linux-sandbox` 链接。Windows Python 的 WSL 路径将它们报告为普通文件 mode 加 reparse tag `0xA000001D`（LX_SYMLINK），因此只检查 `S_ISLNK` 不足。原生扫描现在只计链接自身的逻辑字节，不打开、解析或递归访问目标；其他 reparse 类型仍拒绝。配置根及用户命名空间目录仍必须是普通目录。通用宿主扫描规则没有放宽。

节点失败诊断可记录有限的 mode、attributes、reparse_tag 和字节数，不记录文件内容或密钥。此次定位的是原生目录的链接拒绝；以前宿主扫描偶发耗时约 20 秒的问题仍未定位。

## 实际检查

显式入口 `platform/server/live_llm_cli_native_usage_acceptance.py --source-profile-id 26 [--postgres --s3]`。FS/SQLite 与真实 SeaweedFS/PostgreSQL 各 12 项，共 **24 项实际基础设施检查**：真实 Linux CLI 初始化/关闭、两个用户的原生字节归属、节点增量、节点容量拒绝、用户套餐拒绝、真实加密缓存更新、外部/悬空链接只计自身字节、配置根失联拒绝、配置根链接拒绝、实际条目上限、真实容量超限触发运行期关闭并等待清理记录落盘。文件系统故障通过真实改名和真实链接触发，纯协议模拟 0 项。

证据：[SQLite](../../tmp/live-cli-startup-h045pj2f/cli-native-usage-checks.json)、[S3/PostgreSQL](../../tmp/live-cli-startup-21cg837j/cli-native-usage-checks.json)。CLI 根退出并核验 native state 为 exited；API 均退出；临时 PG 已删除；对象存储已停止。只改动隔离用户、数据库和本轮唯一测试目录，开发数据库只读配置，保留测试目录作为证据。

## 真实模型

独立平台验收 [回执](../../tmp/live-deepseek-txgzsg7o/live-result.json) 覆盖模型列表、真实非流式翻译、流式回答及持久化、模型调用 `read_text_file` 后续答：**4 次真实推理，11,879 tokens**，另有一次模型列表请求。

两套原生计量验收后，平台 Agent 真实调用工具读取该套检查摘要的哈希，并验证流式及持久回答一致：[SQLite 模型回执](../../tmp/live-cli-startup-h045pj2f/live-result.json)、[S3/PG 模型回执](../../tmp/live-cli-startup-21cg837j/live-result.json)，最终两套另 **4 次真实推理**；此前两套 11 项矩阵的模型读回也保留，共 4 次，不重复计入最终基础设施检查。

本阶段合计 **12 次 `api.deepseek.com / deepseek-flash` 推理，全部 HTTP 200，46,447 tokens；模拟 LLM 0**。回执保留实际 completion ID、服务日期、model 与 usage。平台 Agent 模型测试和真实 CLI 初始化分别验收；**CLI 内部推理仍未验证**。

## 失败记录与后续

四次原生前置失败保留：`6jhomgeg` 首次缺少诊断；`seb56ssh` 运行期 observer 因链接关闭会话；`p697g53s` 只处理普通符号链接仍被拒绝；`32gntiyk` 记录 LX_SYMLINK 的实际 tag。均在模型阶段前失败，真实调用 0，不能计为模型通过。各次真实 CLI 已退出。其中 `seb56ssh` 的测试 finally 曾因 session.stopping 跳过等待，隔离 API 结束时数据库仍为 active；事后按已存进程身份只读探针确认宿主和原生进程均 exited，原数据库记录保留，不伪造成功回执。已改成始终等待 session.close；新增真实容量超限后的关闭、持久化和保留文件检查，最终双环境各 12 项通过。首次归档脚本也因此在只读前置核验中停止，未改文档或创建归档；使用独立收尾脚本完成。

尚未实现原生初始化及 CLI 自行增长的用户运行期硬限制、OS 硬磁盘配额、旧配置根/未知归属目录对账、跨用户图片和猝死交错矩阵，以及 CLI 内部真实模型/审批/turn/fork 联调。链接目标不属于本根库存；本扫描不能证明进程所有外部写入都受限。WSL 卷剩余空间也不等于 Windows VHD 后备盘的可用容量保证。

主线 **33/53 完成，剩 20（进行中 3、待办 17）**；资源 **11/17、剩 6**；295 项全功能交付目标保持。N4-05 整项继续进行中，本次子检查不提前销项。
