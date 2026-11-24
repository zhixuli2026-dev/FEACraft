# 创建和打开 StandardCase 算例

工作台创建工作区后会打开该工作区。创建算例成功后会选中新增算例，并加载标准目录。创建中切换工作区时，服务器仍在原工作区提交，迟到的应答不会覆盖用户的新选择。如果重名或校验失败，表单保留已输入的名称。工作区共享给其他组织成员后，成员可以打开算例并读取文件树，创建和导入由工作区所有者执行。

重新打开工作台时，浏览器用当前账户的记录恢复工作区和算例。算例列表加载失败后，从项目栏点击“重新加载”可以接续原选择。文件树加载失败时，同一个按钮只重试文件树。创建应答到达后表单立即确认成功，S3 上的文件树读取可以独立完成。浏览器仍可处理旧版只返回标识、名称和阶段的创建应答。

工作区和算例名称沿用重命名时的校验规则，去除首尾空白，拒绝空白名称及带路径分隔符或空字符的名称。算例创建在写入前等待账户存储额度。等待使用现有的存储测量期限，超时返回 HTTP 409。创建时按工作区核对同名算例，提交时写入初始 caseDict、脚手架和创建事件。

## 验收记录

| 数据库和存储 | 实际 API 与浏览器 | 真实模型回执 |
|---|---|---|
| SQLite 与文件系统 | [验收结果](../../tmp/live-cli-startup-0r8cpvc4/case-creation.json) | [DeepSeek](../../tmp/live-cli-startup-0r8cpvc4/live-result.json) |
| PostgreSQL 与文件系统 | [验收结果](../../tmp/live-cli-startup-un5hi9_q/case-creation.json) | [DeepSeek](../../tmp/live-cli-startup-un5hi9_q/live-result.json) |
| SQLite 与 S3 | [验收结果](../../tmp/live-cli-startup-yt4xle7h/case-creation.json) | [DeepSeek](../../tmp/live-cli-startup-yt4xle7h/live-result.json) |
| PostgreSQL 与 S3 | [验收结果](../../tmp/live-cli-startup-d1cusqt5/case-creation.json) | [DeepSeek](../../tmp/live-cli-startup-d1cusqt5/live-result.json) |

- 四组均通过注册、创建工作区、并发提交同名算例、12 个无效输入，以及外部账号、共享成员、只读成员和撤销共享后的访问检查。测试实际持有账户存储额度。缩短测试期限时返回 409，释放额度后同名重试恰好创建一次。
- 每组检查两个算例的 13 个文件，包括初始阶段 A 的 caseDict、网格与求解占位、Python 脚本和创建事件。终止隔离 API 并重启后，文件摘要逐个一致，创建恢复报告没有待人工处理项。
- Chromium 跑过键盘创建与打开、重名保留输入、创建成功后的迟到应答、旧版应答、断网后恢复列表与文件树、损坏 ZIP 后使用同名文件重新导入，以及 420 px 窄屏创建。创建和导入调用真实 API。浏览器故障测试截断指定请求或暂存真实服务器应答。[桌面截图](../../tmp/live-cli-startup-d1cusqt5/case-create-desktop.png) · [窄屏截图](../../tmp/live-cli-startup-d1cusqt5/case-create-narrow.png)
- 四个最终运行各有两次真实 `deepseek-flash` HTTP 200 请求，模型通过工具读取算例内的验收摘要。本轮较早的两组文件系统复测另有四次 HTTP 200。所有最终测试 API 已退出、两份临时 PostgreSQL 库已删除、两套隔离 S3 已停止。

首轮导入测试使用的手写 caseDict 缺少必需的 caseMeta，后改用系统新建算例的实际字典。S3 复测也发现表单等待文件树读取才确认成功，最终版本将创建回执和文件树读取分别处理。[手写字典失败记录](../../tmp/live-cli-startup-nk40bjps/live-result.json) · [S3 复测记录](../../tmp/live-cli-startup-ewtlz46d/live-result.json)

## 复验

用服务器虚拟环境运行 `live_llm_case_creation_acceptance.py --source-profile-id 26`。添加 `--postgres` 或 `--s3` 选择数据库和存储。 `VIBEFLOW_TEST_WEB_DIST` 指向本次生产构建的目录，`VIBEFLOW_TEST_BROWSER_PYTHON` 指向装有 Playwright 的 Python。本次变更复用现有数据库表。[API 与浏览器验收入口](../server/live_llm_case_creation_acceptance.py) · [浏览器场景](../server/case_creation_browser.py)
