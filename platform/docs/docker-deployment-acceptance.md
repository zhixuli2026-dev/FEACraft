# Docker 实际部署与恢复记录

2026-11-17：Docker Desktop 已恢复运行。独立项目 `vf-review-20261117` 的五个容器正常运行，PostgreSQL、Redis、API 健康检查通过，Web 与 `/api/health` 返回 200。此次完成 N7-05 的整栈业务验收部分；主线仍 49/53、剩 4 项，资源预算 11/17、剩 6 项，295 项全功能目标保持。

## 实际环境和结果

使用 Docker Desktop 4.7.1 / Engine 20.10.14、WSL2 Linux 内核，独立数据库、命名卷及项目网络。入口仅绑定本机 `http://127.0.0.1:62883`。没有替换现有 Windows API 数据库和算例目录。

从前一份可审查包实际构建 Linux API 和前端镜像，执行 Alembic 到 `d31f98a254ca`。API 使用 Python 3.11，PostgreSQL 16 Alpine，Redis 7 Alpine。精确镜像 ID、基础镜像摘要、实际依赖版本分别保存于下方证据；未把无版本锁定的 requirements 描述为可重复解析的依赖锁。

一次通过的 8 项验收：

1. 前端静态资源、CSP 和 Nginx 到 API 的健康请求。
2. 真实 PostgreSQL 账户注册、工作区与算例创建。
3. 经 Nginx 写入 2,097,186 字节文件，读回 SHA-256 相同。
4. Redis 投递到真实容器 worker，执行原生 Python 作业，并核验实际 worker 身份和进程停止证明。
5. 等待作业输出运行标记后实际取消，确认进程停止证明。
6. 经 Nginx 导出工作区 ZIP 并通过公开接口恢复。
7. 实际重启 API/worker 后重新登录，原文件逐字节一致、原成功作业仍存在。
8. 使用本地授权的 DeepSeek profile 26，在容器中真实调用模型；模型调用 `read_text_file` 读取随机验证字符串，流式回复原值，工具结果和会话事件持久保存。

真实模型应用请求 1 次；工具调用后的上游 HTTP 回合数未单独计量，不把应用请求数当上游调用数或 token 数。模拟 LLM 0，Claude 调用 0。浏览器另确认登录表单正常渲染；未登录 `/auth/me` 返回 401 符合预期。本阶段未完成完整的登录后浏览器操作验收。

## 本次修正

- Docker 默认 seccomp 阻止执行门创建 namespace。实际默认配置的 `unshare` 失败、指定 `seccomp=unconfined` 成功后，仅对 API/worker 加入该配置。随后真实分布式作业执行与取消通过。
- Nginx 默认 1 MiB 请求正文限制让实际大文件写入返回 413。上限改为 101 MiB，API 请求正文关闭预缓冲，保留 API 自身限额；实际大文件、备份和恢复通过。
- 验收程序原先在取消请求被接受时立即读取停止证明，得到尚未产生的空值。保留失败证据，改为在原 100 秒期限内等待实际停止证明；没有降低停止要求或修改生产取消逻辑。

配置检查共 14 项通过，属于配置验证，不算模型验收。

## Docker 停止提示及代理恢复

此前官方镜像拉取超时，Desktop 的 system proxy 未正确使用本机代理。修复时重启了 Docker Desktop，期间界面出现 `Docker Desktop stopped...`；之后引擎恢复，镜像拉取与两份镜像构建完成。实际启动后的五容器和业务验收证明服务已经恢复。无需因旧提示再次重启引擎。

此机器的代理入口为 `127.0.0.1:4780`。当前通过只绑定 WSL 虚拟网卡 `192.168.176.1:14780` 的 Windows portproxy 转发到该入口，Desktop 手动 HTTP/HTTPS 代理指向该虚拟网卡地址。该地址属于本机环境观察，不能复制作为其他机器固定地址；WSL 网卡重建后需要重新核对。原设置保留在私有本地备份，不进入发布包。

本机 BuildKit 客户端认证使用宿主进程代理 `http://127.0.0.1:4780`；构建阶段和容器需要可从 Linux 到达的代理地址。若再次失败，分别检查 Windows 本机代理、WSL 可达性、Desktop 代理和构建客户端环境，避免只反复重启。生产环境应使用目标网络的有效出口，不依赖本机临时转发。

维护只针对明确的项目容器。先结束活动业务任务，再重启 API/worker；不要执行 `down -v` 删除命名卷。数据库、文件卷、运行配置、原 provider 加密密钥需联合备份，工作区 ZIP 不替代整库灾备。完整步骤见 [部署与运维](deployment.md)。

## 可核对证据及剩余范围

- [部署汇总](../../tmp/docker-deployment-20261117/deployment-validation.json)：五容器、镜像 ID、迁移、模型和浏览器范围。
- [业务验收](../../tmp/docker-deployment-20261117/acceptance-cfddde3b16/deployment-result.json)、[真实模型事件](../../tmp/docker-deployment-20261117/acceptance-cfddde3b16/live-model-events.json)。
- [657 份已审查源码归档](../../tmp/docker-deployment-20261117/acceptance-cfddde3b16/reviewed-sources.zip)、[审查登记](../../tmp/N7-05-docker-deployment-review-v3-20261117.json)。
- [Linux 实际依赖](../../tmp/docker-deployment-20261117/linux-pip-freeze.txt)、[基础设施结果](../../tmp/docker-deployment-20261117/infrastructure-result.json)、[namespace 实际探测](../../tmp/docker-deployment-20261117/namespace-probe.json)。
- 失败保留：`acceptance-f30bf38739` 的 Nginx 413；`acceptance-2de5fd7e32` 的取消轮询错误及稍后已停止观察；首次前端 BuildKit DNS 失败保留在 `build-web.log`。后续实际成功日志不覆盖失败记录。

证据目录中的 `private/`、实际生成的 `compose.json`、provider 密钥和账户文件属于私有运行材料，禁止进入发布包。对外分享证据前仍需按路径挑选。

尚未完成：Linux 完整 CAE/CLI 外部工具链和编译缓存、目标系统部署与恢复手册的剩余条件、物理多主机拓扑及最终全功能验收。现有 Windows/WSL 的工具链证据继续有效；此次容器 Python 作业不代表全部专业工具可用。`Dockerfile.sandbox` 也未因此变成已接入后端。正式 Sign in with ChatGPT 联调按用户决定暂缓，Claude 按用户要求停止推进。

2026-11-18：S3工程部署的真实模型调用捕获本机代理TLS连接失败，直连对比通过后为该部署API/worker加入 `NO_PROXY=... ,api.deepseek.com`（实际值无空格），并通过真实模型接续和全新算例。完整范围与失败证据见[真实模型Linux工程链](live-linux-engineering-acceptance.md)。
