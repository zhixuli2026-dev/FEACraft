# 同算例准备批次：运行中取消与明确重跑

本阶段承接 N3-07 的同算例 MPI、UDF、串行注册成员。主线仍为 36/53，剩 17 项；本页描述的子阶段通过也不能单独关闭 N3-07，原四条 remaining 保留。

## 验收范围

显式入口 `platform/server/live_llm_same_case_batch_cancel_acceptance.py` 使用隔离 PostgreSQL、自建 Redis、两个实际 arq worker、正式 CadQuery/网格准备和已安装 OpenFOAM。辅助代码及实际导入夹具审查记录为 `tmp/same-case-batch-cancel-live-review-20261109.json`。模型和原生执行结果均不替换。

同一个 Case 派生三个独立目录，分别提交 MPI、UDF 和串行注册操作。先完成三个真实 10000 单元、10 步、终止时间 0.0001 的短求解，记录各自真实发布通道、当前指针和速度场摘要。该短求解用于取消前后成果核验；原 400 单元算例及 N7 的物理基准要求继续保留。

第一阶段将终止时间改为长求解，记录输入修改引起的真实 `artifact.invalidated` 事件及修改后的指针。UDF 将集成后的 controlDict 作为生成输出发布，明确编辑该文件会使当前 UDF 指针成为草稿；旧不可变成果与速度场仍保留。取消前后比较使用输入准备完成后的实际指针，另核验全部旧不可变成果。

随后实际观察两 rank MPI、已加载指定 ELF 的 UDF icoFoam 和 strace，同时核对串行成员从未领取。两个独立 API 并发取消整个批次，检查实际出生身份退出、全部成员排空、排队成员从未启动、单次持久取消事件，以及取消前的发布指针和旧速度场保持。

第二阶段限制批次并发为 1，实际观察串行 icoFoam 和 strace，再取消该批次并核对排队 MPI 未领取。此阶段补齐串行的实际运行取消。

随后按实际出生身份终止源 API；恢复 API 读取原批次、保持原幂等键且不生成新 Job，恢复短求解输入，明确提交一个新三模式批次并核对新 Job、新成果、正确新指针及旧历史。S3 验收要求恢复 API 使用此前不存在的独立缓存。

工程场景及所属 worker 正常停止均成功后，公共 live 包装器才请求 `api.deepseek.com` 上本地已配置的真实模型进行两回合工具读取与证据摘要回读。模型回读不作为自主工程规划的证明。

## 当前执行

- FS/PG 首轮：`tmp/live-cli-startup-s693kcxe` 失败，真实三模式基线通过，UDF 观察在 dlopen 完成前过早断言。清理又将从未领取的成员误作需要已启动进程停止凭据，阻止继续清理其他成员，清理报告如实保留错误。所属两个 worker 和两个原生入口之后独立观测退出，临时 PG 删除、自建 Redis 停止；真实模型 0。原运行源码保留在证据目录。
- 修正了 UDF 实际加载观察时点，以实际 SQL 领取/启动/进程字段证明排队任务从未启动；清理先对所有所属活动成员请求取消，再分别等待并记录每项失败。修正审查记录为 `tmp/same-case-batch-cancel-live-review-corrective-20261109.json`。
- FS/PG 第二轮：`tmp/live-cli-startup-lbfde6xx` 失败，实际 MPI/UDF 运行取消、串行排队不领取、所有观察出生身份退出及单次取消事件通过；错误在于验收把输入修改前的 UDF 指针作为取消后的预期。实际检查三个旧速度场摘要均保持，原发布仍为 committed；按生成别名编辑规则，输入准备本身会清除 UDF 当前指针。清理错误为空、临时 PG 删除、Redis 停止，真实模型 0。
- FS/PG 第三轮通过：`tmp/live-cli-startup-1pj9jh8w`，临时 PG 目录 `tmp/job-repeat-postgres-ysodxt6a`，自建 Redis 目录 `tmp/same-case-batch-cancel-redis-271qk739`。两种实际运行取消、排队从未领取、单次事件、输入修改与取消的分别核验、旧不可变成果/速度场、API 实际终止和明确新三模式批次均通过。4个实际 icoFoam 出生身份（MPI两 rank、UDF一个、串行一个）所属观察成员全部退出，两个 worker 正常停止；真实 DeepSeek 2次、8562 tokens，模拟0。审查记录为 `tmp/same-case-batch-cancel-live-review-input-edit-20261109.json`。
- S3/PG 首轮：`tmp/live-cli-startup-24tf1v8b` 失败。真实基线及输入准备完成，下一次完整批次提交遇到通用客户端 30 秒观察超时，未留下新 Job；服务端具体耗时未被该轮记录，不作精确归因。清理错误为空、PG 删除、对象服务和 Redis 停止，真实模型 0。
- S3/PG 后续运行通过：证据目录 `tmp/live-cli-startup-c_j5bjvy`，临时 PG 目录 `tmp/job-repeat-postgres-6dztdtac`，对象服务目录 `tmp/s3-attempt-store-3m6yeyrb`，自建 Redis 目录 `tmp/same-case-batch-cancel-redis-p4qiyfz9`。仅 S3 客户端提交观察改为 180 秒，并增加实际路径、状态和耗时；服务端校验、请求内容、键及原生预算保留。其余工程流程 AST 与通过 FS 的源码一致，FS 分支仍调用原客户端；具体审查为 `tmp/same-case-batch-cancel-live-review-S3-observation-20261109.json`。实际取消、独立空缓存恢复、新三模式批次及真实模型回读通过。

## 保留边界

worker 运行中猝死的 MPI/UDF 已有独立证据，见 [worker 恢复](professional-worker-death-recovery.md)。本入口不替代直接原生根进程猝死、不完整启动登记、finalizing、远端 owner/跨主机接管及原 S3 UDF 单次 409 归因。没有观测到的故障不得记为通过。

## 双后端归档

2026-11-09（同算例准备批次取消与重跑）：FS/PG和S3/PG两个真实Redis worker、MPI/UDF同时运行取消及串行排队、串行实际运行取消及MPI排队通过；所有观察出生身份退出、单次取消事件、旧不可变成果/字段、取消前指针、实际API终止后的历史/幂等和明确新三模式10000单元短求解通过，S3独立空缓存。明确修改UDF生成控制文件引起的正常失效与取消分别核验。新增4次真实DeepSeek、17132tokens，仅独立回读，模拟0；两轮验收代码失败、一轮S3观察超时及首轮清理错误保留。S3客户端观察与实际提交耗时单列，服务端预算/完整来源不变。主线36/53、剩17；剩余跟踪UDF范围、原409和原条件审计继续，N3-07未关闭。

源码及实际退出、对象服务、PG、Redis、逐文件哈希/AST和原remaining对照见 tmp/mainline-N3-07-same-case-batch-cancel-evidence/validation.json。
