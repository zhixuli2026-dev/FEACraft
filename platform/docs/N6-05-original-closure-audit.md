# N6-05原条件退出审计

状态：原N6-05按原条件关闭。主线42/53，剩11；全部53/295目标继续，未全部完成。有效截图上的独立完整审阅为ship，见.impeccable/review/case-workflow/final-review.md。

## 原范围

原任务是“CasePanel整合阶段确认、输入过期、重算范围和产物证据”。退出条件是用户能理解为何被阻止及下一步修复，不能从UI绕过阶段接口。保留原两条remaining：

1. 保留N2-04原remaining中的完整前端失效解释、目标重算操作、复用/新计算区别和新旧流程导航；已有依赖图与重算API不能替代CasePanel交付。
2. 承接N2-06：明确显示恢复冲突、已保留版本与可选修复方式，不绕过后端证明。

## 原条件与权威证据

| 原条件 | 实现与实际证据 | 证明边界 |
|---|---|---|
| 绑定算例、流程、阶段步骤和输入/步骤版本 | CaseWorkflowControls发送workflow_step_id、expected_step_version和expected_revision；`tmp/case-stage-confirmation-makrtyrd/stage-result.json` | 实际CadQuery、实际B确认、重复导航和原SQL回执；不把独立阶段当作流程确认。 |
| 丢失响应不重复执行 | 原请求先保存，精确匹配原流程确认回执；独立请求保留未知并明确读取服务器当前状态 | 绑定回执见stage-result；独立请求、切换算例及显式保留核对记录见`tmp/case-workflow-controls-gin87ok_/browser-finished.json`的stage_unknown。该整轮仍失败，不能当作整体通过。 |
| 确认草稿期间输入或步骤变化 | 草稿戳、确认修订和步骤版本；旧确认禁用，记账修订只重核不代确认 | controls初轮stage_concurrency；实际几何lx乘1.2后的重核409/input_stale、阶段未变和旧发布拒绝见publication_conflict。 |
| 当前有效、缺失、过期、无法核验与历史发布 | 依赖图、来源字段、原始诊断、固定发布和当前活动发布分别说明 | stage最终实际缺失/有效；完整链实际改网格/STL损坏/精确恢复；controls初轮新活动发布替换旧发布。 |
| 按目标核对与明确重置 | 五个目标的草稿、预算/模式/目录和A重置后果；服务端重新核验 | `tmp/case-workflow-controls-fcbmxlkm/browser-finished.json`的all_target_drafts只证明UI核对且不发送变更，非实际计算；实际创建/幂等/重算见完整链。 |
| 真实完整链与旧结果保留 | `tmp/case-workflow-chain-jg4a48bl/chain-result.json`通过 | 10个实际原生Job，两轮B/C/D/E，320→384单元且均值350K；实际仅复用几何，重建其余四段，新报告第二代，原HTML/DOCX和原四类业务行保持。 |
| 取消/失败后重新进入 | 初轮controls的cancel_reentry/failure_reentry是实际原生失败、修复和关联流程 | 初轮整轮因两项界面问题保持false；这些具名子证据不冒充整体通过。对应界面问题由controls第二轮修正并通过。 |
| 计划与实际执行分开 | 计划范围仅标为计划；实际状态来自步骤/尝试，完成复用还匹配原发布ID；没有步骤记录不推定没执行 | `tmp/case-workflow-controls-ou5zf5so/controls-result.json`通过：已成功的历史源、等待时4段无尝试、真实网格Job11成功后3段取消且无尝试、实际失败CAD Job13；保留来源导航。 |
| 恢复冲突不能绕过 | 缺失归属记录时没有新流程表单，强制请求409/recovery_required且E/11/两流程十作业保持 | 明确的单列SQL故障；`tmp/case-workflow-controls-jqv6az23/controls-result.json`与最终controls通过精确恢复及原四表哈希；不冒充真实中断或制造停止证明。 |
| 解释下一步修复与保留版本 | 阻止原因旁说明原作业“恢复与重新执行”、原主机负责人核验归属/停止/排空，指向原“文件版本与人工恢复”；明确文件动作不替代执行归属证明 | 最终controls的ownership_guidance；原作业7和文件版本区域导航见第三轮具名浏览器证据。隔离数据没有待处理版本时如实为空；不将空列表称为真实保留版本恢复测试。 |
| 沿用真实人工版本恢复 | 原ManualFileRecovery组件、CSS、manual_file_recovery服务和manual_files路由与N6-04封存逐文件哈希相同 | 实际保留/隔离/恢复的原N5证据见manual-file-recovery.md、manual-file-recovery相关实际验收记录；本段验证整合路径，不替代完整历史源码/版本阅读器。 |
| 权限与算例边界 | 已知viewer只读提示与变更禁用；读取/导航可用；后端仍作最后校验 | controls初轮permissions_API：外账户404、共享viewer读200，阶段/取消/重算/回读写权限及跨算例步骤均404且版本不变；第二轮viewer界面通过。 |
| 旧上下文、编辑保留与界面 | 首次展开读取；后续输入/流程/事件变化只刷新上下文，不替换参数、草稿或未决请求 | `tmp/case-workflow-controls-3kd_3rsu/controls-result.json`通过实际CAD活动→结束自动恢复、375px无溢出、44px控件、2px键盘焦点、深浅主题；最小文字对比度6.42/6.40。完整手机工作台不在本项验收内。 |

## 失败与证据有效性

- stage最初两轮分别保留旧确认/错误和错误流程选择，原失败文件仍保留；最终具名stage结果通过。
- controls第一轮因旧上下文与viewer操作入口明确保持失败；第二轮具体修复通过。
- controls第三轮故障脚本通过ORM同时改动updated_at，精确历史检查失败；第四轮修正为带原值条件的单列SQL故障/恢复并通过。没有改写第三轮为通过。
- 第一次独立审阅为fix，指出计划误标实际及缺失修复说明。最终controls真实验证了两项修正。后续两次审阅仅返回recapture，未给功能通过；先后修正过早的总览和取消态恢复读取中的截图，旧失败审阅保留。
- 只读补图服务第一次启动因已有同名算例而拒绝；另一次补图复现PTY收尾SQLite锁超时，结果仍失败。实际OS只读核验确认该服务的API、PTY根/桥进程均退出、唯一Windows作业组不存在且无成员，未修改数据库终端行或补造回执。其持久终端记录仍待恢复，继续原N6-07，不宣称已修复。
- 所有本段模型计数为真实LLM0、模拟LLM0；使用真实原生、浏览器/HTTP/SQL证据，未运行历史混合模拟推理回归。故障协议、真实原生失败和真实模型验收分别记录。

## 继续保留的原工作

N6-02的outbox/文件树/索引/发布对账；N6-06的刷新后流程、日志游标、选中尝试、固定报告版本及完整历史源码/版本界面；N6-07的统一入口/全工作台/PTY数据库收尾；原N4资源与统一停机、全部N7部署/故障/性能/最终验收继续。高输出MPI SIGKILL原因未明，没有以低输出成功替代修复。商业网站登录的注册配置按用户决定暂不联调。

全部295条全功能清单不因本项增量批量签收；107条待完整回归、89条待实施、其余99条为实施/补证等标签，completed仍全部false。53项计数不是全产品完成率。原11月23～30日仅是11月9日基准的低置信度粗估，295全交付仍无可信日期。


封存验收：`tmp/mainline-N6-05-closure-evidence/validation.json`；源代码包`platform/backups/mainline-N6-05-closed-20261112.zip`。独立审阅只覆盖原N6-05，不代表全工作台/模型/295全功能通过。两次recapture均没有被当作功能通过。
