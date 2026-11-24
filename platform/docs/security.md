# 安全边界与加固说明

2026-11-03 WSL CLI已验证非root原生出生与进程树清理、凭据不进入工具环境、Linux私有目录权限和一次实际workspace-write文件边界。共享worker UID仍不等于租户间OS隔离；未验完整网络/资源策略，full_access权限语义未改变。Windows readiness不代表MXC支持，Web组织管理员不能触发主机UAC初始化。见[WSL与sandbox边界](owned-cli-wsl.md)。

2026-11-03 网站登录增量：[Sign in with ChatGPT](website-sign-in.md) 已采用持久一次性事务、PKCE/nonce/签名验证、显式身份关联、可撤销第一方 Cookie 会话、Cookie 写请求与 WebSocket Origin 校验；模型 API 凭据仍独立。退出撤销后续请求/建连权限，不宣称即时中断全部既有长连接或已受理作业。真实注册 client、提供方安全通知、旧 JWT 全局撤销、限流和完整多租户隔离仍待后续；本增量不改变下文公网交付边界。

> 当前核对：2026-10-27。本文描述实现中的安全边界，不把“有 JWT/配额”当作多租户安全证明。

## 1. 当前结论

平台适合受信任开发用户的单机环境。它是数值仿真工作台，允许用户提交作业和 Agent 工具命令，因此**命令执行隔离是第一安全边界**。

当前仍有以下本质限制：

- Windows 宿主或普通 WSL 可执行命令；`cwd` 位于 case 目录不等于阻止访问其他路径。
- 显式 `wsl-sandbox` 已使用 pivot_root/user/mount/net/PID namespace：nobody、宿主路径不可见、默认无公网、case staging 回收。它还不是 cgroup 限额或全执行入口统一策略；并发 staging 与审计完整性仍需验收。
- Job已逐步接入持久attempt/claim/outbox；PTY/viz活动登记、端口及配额仍主要在单进程内，不支持安全的多副本共享。
- Organization/Membership 主要用于套餐与配额，不是完整的组织资源 ACL。
- S3 与本地文件路径混用，不能假设所有执行链都具有同一隔离语义；prod 已默认持久化 fs，未显式允许受限模式的 S3 会在 DB 初始化前拒绝启动。

不可信用户或公网部署前，必须完成硬沙箱、组织授权、审计、限流和部署验收。

2026-11-02本轮补充：算例派生/PTY/viz启动协调与权限复核已通过双库各93项；真实会话及Windows/Linux目录发布证据见 [case-write-coordination.md](case-write-coordination.md)。该验证未覆盖跨API持久会话所有权或完整进程树，不构成整份安全审计的重新验收。

## 2. 身份与资源归属

- 密码：PBKDF2-HMAC-SHA256、随机 salt、20 万轮。
- JWT：HS256，包含 `sub/iat/exp`；默认 TTL 72 小时，生产应按风险缩短。
- HTTP：Bearer 解码后重新读取用户，并要求 `status=active`。
- workspace/case：直接所有权由 `workspaces.user_id` 校验，授权组织共享按viewer/member控制读写；未授权资源404。删除/恢复和跨工作区移动仅允许直接owner，member可写共享文件但不能删除或转移owner算例。排队后的协调写入和Job启动重新核验回收站状态。

2026-10-24 加固后的会话规则：

| 资源 | 规则 |
|---|---|
| PTY session | 创建/重连/每次输入核验active用户；case必须owner且未删除，不能无权后回退cwd；REST列表/关闭仅本人；断线释放订阅、保留进程，权限失效输入关闭自身进程 |
| viz session | 创建时记录 `user_id`；HTTP 和 wslink proxy 同时校验 token/按 sid 路径隔离的 HttpOnly cookie 与所有者；`last_activity` 续期，超时回收进程 |
| provider profile | 新建会话、fork、压缩、消息、approve 和 run loop 都必须归属当前用户 |
| 历史异常 profile | 在使用点重新校验，不因 conversation 已存在而信任 |

`security_smoke.py` 使用双用户覆盖这些路径；`viz_smoke.py` 同时验证所有者 200 和跨用户 404。每个 PTY 的阻塞读取使用独立单线程执行器，避免会话数量占满 asyncio 默认池并饿死文件、索引和 LaTeX 任务；`terminal_executor_smoke.py` 只清理测试自己创建的 session id。

## 3. Agent 与命令执行

危险工具：

- `run_shell_command`
- `propose_file_update`
- `propose_text_replacement`

权限模式：

- `approve_writes`：危险工具挂起，等待批准。
- `full_access`：危险工具可直接执行；仅适合明确授权的可信场景。
- `ask`：不向模型提供工具；provider 违规返回的结构化 tool_calls 同样拒绝执行，正文/reasoning 的伪 XML/DSML/sentinel 在流式、落库和重放前统一清洗。
- `plan`：仅提供只读工具，违规危险 tool_calls 也拒绝；只有资源所有者显式批准后才能切回 approve_writes，活动/待审批时禁止修改权限。

用户图片仅允许 PNG/JPEG/GIF/WEBP，最多 3 张/每张 4 MiB，严格 Base64 与 magic bytes 校验，不接收 SVG。原图只写当前算例 `.vibeflow/attachments`，事件不存 Base64；模型请求按双协议编码，浏览器 Blob 同样需要所有者 token。

模型原始流、正文加推理、单 SSE 行与事件、单工具调用及非流式响应分别限制为 64、8、8、2、10 MiB。错误预览限 16 KiB，记录前脱敏 Bearer、API key、token、secret 和常见 sk-* 密钥。客户端断开可取消 provider 流及所属原生命令，并通知同步扫描线程尽早停止。两个 API 共享数据库时，跨实例停止按算例权限、当前租约及运行 ID 核验。 [F34 验收](FF-F34-request-isolation-and-limits.md) · [原生命令验收](FF-F24-F25-agent-shell.md)

高危正则拦截是补充防线，不是沙箱。`shell=True`、PowerShell、WSL、子进程和用户脚本都应按可执行任意代码评估。

`web_search` 当前默认使用本地 mock provider，不发起真实搜索请求，结果必须保留 mock/非实时标识。runId 隔离和取消不替代未来真实 provider 的出口网络安全、查询隐私与限流审查。

`web_fetch` 仅允许无凭据的绝对 http/https URL。直接 IP 和 DNS 的每个结果都必须是公网地址；aiohttp 只连接 resolver 返回的固定 IP，同时保留 URL 原 host 用于 Host、SNI 和证书校验。禁用环境代理/Cookie，重定向逐次重新校验，TLS 不可关闭，响应限 8 MiB 并有总超时。当前本地正负例通过；公网 example.com 因证书校验失败为 LIVE_LIMITED，未通过关闭验证规避。

viz 的 VF Python 控制台是另一层受限接口：仅白名单函数和字面量参数，禁止 import、属性、下标和推导式；它不是通用 Python 控制台，也不构成宿主硬沙箱。scenes.py 仍是受信任脚本执行路径，不能向不可信用户开放。

LaTeX 使用独立源快照、`-no-shell-escape -norc` 和 120 秒 timeout，最新成功作业才发布产物；这些参数缩小 TeX 执行能力，但 latexmk/TeX 仍是受信任原生工具链，不是硬沙箱。PDF 页码和 PPTX worker 使用 argv 而非 shell 命令拼接，输入资源限于算例内，输出独占创建且不覆盖源文件/模板。SyncTeX 以 argv 执行，查询输入映射必须回到当前算例内存在的 `.tex`，不返回服务器绝对路径；缺 sidecar 的旧 PDF 明确返回 409。作业状态仍是单进程注册表，不能据此声称多副本安全。

**approve 的顺序不可改变**：先验证会话、case、tool_call 和续跑 profile 归属，再执行工具。否则即使请求最终因 profile 越权失败，文件或命令也可能已经产生副作用。

文件/文本提案登记时记录 baseline_sha256，批准执行前再次比较当前文件，外部修改必须重新提案。普通文件采用同目录临时文件、flush+fsync 和 os.replace，失败只清理本次临时文件；caseDict 继续走统一字典写入，不能绕过 DB 镜像。基线校验不是符号链接沙箱，链接与高级只读语义仍按 F31/F32 单列。

生产要求：

- 每用户低权限容器/沙箱，限制 CPU、内存、PID、文件系统和系统调用。
- 默认无公网；网络访问经独立受控代理。
- 不暴露 Docker socket，不以 root 执行，不挂其他用户目录。
- 全量审计命令、审批、文件变更和出口访问。

## 4. 文件与 caseDict

显式输入封存与产物版本分别位于`.vibeflow/attempts/{uuid}`和`.vibeflow/publications/{uuid}`；普通storage/Agent文本/上传/删除禁止覆盖，返回input_sealed/409。工作副本仍可编辑后重新提交。独立cwd不限制任意原生命令访问宿主文件，直接写盘专业适配器仍需逐项检查，完整沙箱/进程树/版本恢复待续。证据见 [attempt-workspaces.md](attempt-workspaces.md)。

- 存储层拒绝 `..` 路径穿越，并检查解析后的路径仍位于存储根。
- 普通写入、上传、删除和 Agent 提案统一 canonicalize 路径，避免 `./`、重复分隔符、反斜杠变体绕过。
- caseDict 只能经统一函数执行 parse → 文件写入 → DB 镜像；普通文件 API 不得直接写或删除它。
- `/raw` 需要案例归属和 Bearer；支持单段 Range/206 与 HEAD，多段 Range 返回 416。无 Range 的 GET 仍一次读取全部字节，超大文件必须使用 Range 并补性能与大小限制。PDF.js 阅读器使用 HEAD 获取长度，只接受 206 并校验区间长度，禁用自动整文件回退；目录外链仅允许明确 http(s)。
- 当前 multipart 上传先完整读入内存再检查配额，超大文件场景需改为流式限制。

- 外部 StandardCase 使用 ZIP staging 导入，不开放任意服务器路径；拒绝 ZIP slip、绝对路径、符号链接、加密/重复路径和异常压缩比，限制上传/单文件/总大小，失败只清理本次新建前缀。

技能 ZIP 仅写入当前用户或 admin 组织目录，最多 100 个文件/解压 1 MiB，拒绝路径穿越、绝对路径和符号链接，默认不覆盖；覆盖先备份再替换。内置 < 组织 < 用户覆盖只影响技能解析，不授予其他组织写权限。

`_fs_path` 以 `Path.is_relative_to` 同时约束 storage root 和当前 prefix；resolve 会检查最近存在父目录的符号链接，算例内部链接可用，跨 case、外部/相似根、broken link 和链接下未创建目标均拒绝。路径检查不能替代命令沙箱：获准执行 shell 的用户仍可通过命令访问工作区之外的路径。

## 5. Token 与浏览器边界

- token 存在 `localStorage.vf_token`；XSS、恶意扩展和同源脚本都可能读取。
- WS/iframe 首页使用 `?token=<jwt>`，token 可能进入 URL 日志、历史、代理和错误报告；首页成功后设置 `Path=/viz-proxy/{sid}/`、HttpOnly、SameSite=Strict 的会话 cookie，供 trame 子资源和 wslink 使用，每次仍校验用户与会话归属。HTTPS 下 cookie 标记 Secure。
- 生产应使用短期 token、窄 scope 和严格 TLS；一次性握手票据仍未实现。2026-10-29 nginx 已改为只记录 `$uri` 的 `vibeflow_noquery` 日志格式，不记录 `$request`/`$query_string`，生产 uvicorn 使用 `--no-access-log`；这只降低日志暴露，不消除浏览器历史、其他代理或错误报告中的 URL token 风险。
- Markdown/聊天使用 DOMPurify，但不能因此忽略依赖更新、iframe 同源资产和 CSP。HTML/DOCX 阅读器统一使用根级 DOMPurify 3.4.17，去除脚本/表单/iframe/嵌入物、事件属性和内联样式，再在 Shadow DOM 显示；Mammoth 只解析 DOCX，不执行宏。算例图片经 Bearer Blob 加载，外部图片不自动请求，危险链接由解析器拒绝。
- 2026-10-29 根级 npm override 已生效：Monaco 嵌套 DOMPurify 统一为 3.4.17，`npm ls dompurify` 显示 deduped，`npm audit --omit=dev` 为 0，build 和主题 E2E 通过。后续升级仍必须复查嵌套版本，不能只检查根级 DOMPurify。
- API 已实际验证 nosniff、SAMEORIGIN、no-referrer、Permissions-Policy、窄 CSP 和 CORS 恶意/null Origin 拒绝；nginx 前端 CSP 有离线配置检查。TLS、nginx 实际 CSP 运行和完整浏览器兼容验收仍未完成。

- J10 默认 OIDC 使用 authorization code+PKCE S256 和单次 10 分钟 state，身份取自直连 token/userinfo 响应，要求 verified email；access token/client secret 不写日志或返回前端。当前 state 为单进程内存，未做 nonce/ID Token 校验、多 provider 或真实外部 IdP 验收，多实例部署前必须共享 state 并补这些检查。


- E20 屏幕捕获只能由用户点击“屏幕框选”启动，结束/取消/组件卸载都会停止 MediaStreamTrack；自动化只使用 canvas.captureStream 假源，未捕获真实显示器，也不自动上传裁剪图。


## 6. 密钥与敏感数据

- LLM key 使用 Fernet 加密存于 `provider_profiles.key_ciphertext`，API 不回显明文。`/llm/models` 与 `:test` 仅在服务端解密后用于对应 provider 请求；失败只返回脱敏错误和当前 default_model。base_url 必须是 HTTPS，或 `localhost/127.0.0.1/::1` 回环 HTTP；拒绝 URL 内嵌用户名/密码和非回环 HTTP。
- 主密钥位于 `server/app/.vault_key`；这是本地 Fernet 文件方案，**不是 KMS**。
- J09 组织 profile 分享只保存 org/profile/enabled 链接；成员使用权限由 active membership 决定，API 不返回密文或明文，owner 才能更新/删除/分享。共享不等于把密钥安全迁移到组织 KMS。
- `.vault_key` 丢失会导致已有密文无法解密；必须限权、备份并考虑轮换流程。
- `.env`、JWT secret、数据库口令、S3 密钥和真实 LLM key 不进入公开文档或普通源码包。
- `server/.env.example` 只含占位符。
- 文档不记录本机数据库口令；本地测试账号与 mock key 不得用于生产。

## 7. 配额不是完整安全边界

代码中有 viz 并发、作业并发、Agent 日日消息和存储 MB 四类检查点，本轮 viz/job/LLM/storage 429 与还原均已通过；设置页仍只有作业/LLM/存储三条用量条。覆盖同样不完整：

- 通用 job 提交有检查，几何/报告等直接建 Job 的路径需要单独复核。
- multipart 上传有存储检查，普通文本写、Agent 写和作业产物不统一计费。
- Agent 用户消息有 LLM 日配额，翻译/压缩等内部调用需要统一策略。
- 上传先读入内存再查配额，大文件可能先耗尽内存。

生产需要统一的入口计费、流式上传限制和出口代理。J19 已有用户/IP×API/LLM 类别的单进程 token bucket，低限额 429/Retry-After/用户隔离实测通过；精确 provider profile、Redis 跨副本和转发 IP 信任策略仍未完成。

- C16 SSH executionProfile 仅组织 admin 可用，主机必须显式列入 `VIBEFLOW_REMOTE_HOSTS`；强制 client_key/known_hosts、禁用 SSH agent，不自动信任未知主机。临时密钥仅用于本地测试，真实 HPC 凭据/文件同步/调度器尚未验收。


## 8. 组织审计链边界

- `server/data/audit/org-<id>.jsonl` 由服务端私有 API 追加，记录 actor/action/resource/时间、previous_hash/event_hash；成员、Setup 和作业事件已接入，命令仅保存 SHA-256，不保存明文、邀请邮箱或配置值。
- admin 查询会重新校验整条链；隔离测试修改事件后 `chain_valid=false`。当前实现是单进程文件锁和防篡改证据，不是多副本 WORM；文件管理员可重写整条链，尾部删除也缺少外部锚定。
- 审计写入发生在业务提交之后，极端写入失败仍可能形成缺口；审批/取消等来源尚未全覆盖，不能作为完整合规审计或不可否认性证明。


## 9. 上线前安全验收

- [ ] 跨用户 workspace/case/PTY/viz/profile 全部有负向测试。
- [ ] shell/几何脚本/报告/UDF 等所有执行入口都进入同一硬沙箱策略。
- [x] 组织 seat/admin、邀请接受/拒绝/撤销、成员角色和 disabled 行为有多用户 API 与双账号浏览器验证；disabled 是组织成员状态，不等于全局禁用账号。
- [ ] 完整组织级资源 ACL 矩阵；J07 已验证共享列表/case/tree/file/raw、viewer 只读、member 写入和撤销，其他端点与 admin 代理管理仍需逐项负例。
- [ ] TLS、短期窄 scope token 和请求大小限制完成验收；安全 header、CORS 负例与 query 日志策略已有部分实测/离线检查，nginx CSP 运行仍待验证。
- [ ] 全量配额、上传内存限制和出口代理通过测试；单进程 API/LLM token bucket 已验证，Redis/provider 级限流仍待完成。
- [ ] 路径穿越、命令注入、SSRF、XSS、 vault 回显和暴力访问有专项测试。
- [ ] 备份不包含公开明文秘密；恢复后密钥和权限仍正确。
- [ ] 第三方资产及许可证已核查，见 `third-party.md`。
