# 文件准备残留诊断与生成文件事务

2026-11-06。针对 [N2-06 原条件复核](N2-06-acceptance-audit.md) 中确定的缺口，补齐只读残留查询，并将 UDF 模板和批次清单接入文件事务。主线任务总范围仍为 53 项，全功能仍为 295 项；本批不提前关闭 N2-06。

## 残留查询

已有 `GET /api/v1/cases/{id}/file-transactions` 新增可选参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `include_preparations` | `false` | 附带准备版本清单；原默认响应保持兼容 |
| `preparation_limit` | `100` | 每页 1–200 项 |
| `preparation_after` | 空 | 上一页的 `preparations.next_cursor` |

查询保留原账户授权、算例可用性和写入占用检查。`status` 仍描述当前 pending/占用状态；`idle` 不代表没有历史或准备残留。请求详细信息时，`preparations.entries` 返回位置、事务 ID（能识别时）、类型和保留原因；`has_more` / `next_cursor` 支持继续查看。

本地只枚举事务目录下的 `preparing-*`，不读取备份正文，不跟随链接或 Windows 目录连接。部分 JSON、空目录、未知名称和链接均保留。S3 只对固定的私有 `objects/` 子树做提供方分页和回执 HEAD，不扫描整个工作区或下载全部备份；不依赖本机执行缓存。

| 状态 | 说明 |
| --- | --- |
| `preparation_retained` | 发现准备目录/对象前缀；没有因此判定提交或授权删除 |
| `referenced_by_pending` | 与当前已诊断的 pending 事务 ID 对应 |
| `receipt_retained` | 存在回执对象；只确认存在，不把未经校验的回执当 SQL 提交证明 |
| `unverified_retained` | 名称、类型或观察不能按标准准备目录解释 |

每项均为 `commit_state: not_determined`、`decision: retain`。已有 pending 诊断和正式恢复协议继续依据完整清单、SQL 事件和版本证明作出提交/回滚决定。准备清单不是清理 API，也不能证明写者已经退出。清单读取失败单独返回 `preparations.status: unavailable`，不会伪装成没有残留或掩盖原 pending 诊断。

分页是实时观察，不是冻结快照；外部进程改动目录时可能需要重新从首页读取。S3 回执与目录均未被本接口修改。

## 两个生成入口

UDF `/udf/scaffold` 的九个目标文件现在作为一个文件事务保存，保留原“所有目标必须不存在”的并发条件。批次 `/jobs:batch` 的批次 manifest 与 index 也作为一个事务保存，与 Job/attempt 和投递意图同一次 SQL 提交；异常后依据持久日志恢复，去掉仅在进程仍存活时才有机会运行的手工删改补偿。

成功提交后，两入口通过原 `finish_committed` 收尾。实际崩溃发生在首文件后或 SQL commit 前时，新 API 回滚全部已应用目标；发生在 SQL commit 后时保留已提交版本。批次已提交但响应丢失时，相同幂等键仍指向原批次，不能新增 Job。未提交的批次不留下 manifest/index 指向不存在的 Job。

## 验证范围

诊断复用原本地 `before_journal` 和 S3 `before_pending` 真正 API 终止夹具，四种存储/数据库组合验证重新启动后的残留定位、旧内容保留、只读重复查询和单项分页。本地另外检查部分清单、旧文件和真实目录符号链接；S3 检查缺 manifest 的前缀和损坏回执不能被标为已提交。匿名请求及超限页大小被拒绝。

`generated_file_transactions_smoke.py` 针对真实 UDF/批次 HTTP 入口，在首文件后、commit 前、commit 后三个位置终止 API，由新 API 恢复。测试仅禁止批次执行派发以固定文件/SQL提交边界，不把它当作真实求解器恢复测试；原写入并发套件另验证实际批次执行与 UDF 并发编辑/复制。具体终态回执、结果和源码绑定以本批 `tmp/mainline-N2-preparation-diagnosis-evidence/` 为准。

诊断和两个写入口的测试是分阶段增量证据，不声称全部历史处理器在最终源码一次重跑。临时数据库、端口和桶与开发环境隔离。当前剩余的直接写入口见 [逐入口覆盖表](legacy-file-write-coverage.md)，后续资源预算、人工恢复界面、索引和部署责任仍按原验收复核表执行。
