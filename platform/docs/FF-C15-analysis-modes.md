# FF-C15 分析类型与原生验证

## CalculiX 模态

通过 `POST /cases/{case_id}/calculix:solve` 提交包含 `*FREQUENCY` 的原生 `.inp`。用 `GET /cases/{case_id}/calculix/modal-results?input_path=Ref/beam.inp` 从当前已提交的发布读取原生 DAT 频率表。附加 `publication_id` 可读取指定历史发布。响应记录求解步、模态编号、特征值、实频率、实角频率、虚部角频率和 DAT 的路径与摘要。频率采用每模型时间单位的周数。模型使用秒时读数即 Hz。FRD 继续保存原生振型。 [CalculiX 2.23 手册](https://www.dhondt.de/ccx_2.23.pdf) · [接口实现](../server/app/routers/calculix.py)

用 80 个 C3D8I 单元、189 个节点模拟长 1 m、截面 0.04 × 0.02 m 的钢悬臂梁。前四阶由弱轴和强轴各两阶组成。真实 CalculiX 2.23 得到 16.88654、33.72081、105.9803 和 210.5199 Hz，与独立梁方程的最大偏差为 0.879%。六阶的 DAT 与 FRD 频率标记在原生输出精度内一致。 [输入](../../tmp/FF-C15-modal-20261123/cantilever.inp) · [独立求解与解析值](../../tmp/FF-C15-modal-20261123/native-modal.json)

SQLite 文件系统及 PostgreSQL 加认证 SeaweedFS 均通过原生提交、退出和频率回读。将密度加倍并重跑，六阶频率均按 `1/√2` 缩放，原发布及其 DAT 摘要保持。跨账户和错误输入路径读取返回 404。文件系统归档 DAT 被改时返回 409，恢复原始字节后按同一版本读取。真实模型 0 次，模拟 0 次。 [文件系统](../../tmp/ff-c15-calculix-modal-2ol9sd_p/modal.json) · [认证对象存储](../../tmp/ff-c15-calculix-modal-g3mx1pks/modal.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-78t6cvya/cleanup.json)

## CalculiX 谐响应

对包含 `*FREQUENCY,STORAGE=YES` 与 `*STEADY STATE DYNAMICS` 的原生模型，通过同一个求解入口提交。`GET /cases/{case_id}/calculix/harmonic-results` 接受 `input_path`、`node_set`、`node` 和可选的 `publication_id`。由相同归档 DAT 读取每个频点的实部与虚部，返回三分量振幅和弧度相位。频率和位移沿用模型时间和长度单位。缺少配对数据或频点时拒绝返回部分数值。 [CalculiX 谐响应](https://web.mit.edu/calculix_v2.7/CalculiX/ccx_2.7/doc/ccx/node244.html) · [Rayleigh 阻尼](https://web.mit.edu/calculix_v2.7/CalculiX/ccx_2.7/doc/ccx/node217.html)

实算 2 kg 质量、1000 N/m 主方向弹簧、1 N 谐力，分别采用无阻尼和 4 N·s/m 阻尼。0.5 到 2.5 Hz 五个频点的复位移与独立解析式对照，最大相对误差为 `4.10 × 10⁻⁷`。2.5 Hz 有阻尼的主方向振幅为 `1.9592401 mm`，相位 `−0.12341579 rad`。两套存储的 DAT 摘要与全部十个样本一致。旧版读取、跨账户拒绝、指定不存在的节点返回 404，原生模态回读一并复核。 [独立计算](../../tmp/FF-C15-harmonic-20261123/native-harmonic.json) · [文件系统](../../tmp/ff-c15-calculix-harmonic-uzd8uwrr/harmonic.json) · [认证对象存储](../../tmp/ff-c15-calculix-harmonic-o4x05jfd/harmonic.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-pexun0s4/cleanup.json)

## 参数化、优化和不确定度研究

通过 `POST /workflow-batches` 将 `study` 与 `members` 一起提交。各成员引用独立算例和其当前修订，使用 `postprocess` 或 `report` 目标。参数取自提交时读到的实际 `caseDict`，参数值与算例修订、求解计划及批次成员一起封存。

```json
{
  "mode": "optimization",
  "parameter_path": "boundariesLoadsInitials.boundarySets.xMax",
  "parameter_unit": "K",
  "metric": "mean.T@1",
  "metric_unit": "K",
  "goal": "minimize"
}
```

`parameter_path` 可以直接选有 `value` 和 `unit` 的字典。`parametric` 报告已提交候选的量值范围。`optimization` 按 `minimize` 或 `maximize` 返回实际候选中的最优值及所有并列工况。`UQ` 返回加权均值和总体标准差，`weights` 以成员键设置，相对权重会归一化。省略 `weights` 时采用等权。

所有成员完成工程确认后，`GET /workflow-batches/{batch_id}/study-results` 从各成员自己的原生后处理发布读取指定指标，并回传发布 ID、指标文件及其摘要。单位必须相符。旧批次继续使用提交时的参数和结果版本。 [提交接口](../server/app/routers/workflows.py) · [指标核验](../server/app/services/workflow_studies.py)

SQLite 文件系统上三种研究各实际运行三个导热工况。壁温 380、400、420 K 对应平均温度 340、350、360 K，至原生浮点精度内一致。离散优化选出 380 K 壁温工况。按 1∶2∶1 加权得到均值 350 K、总体标准差 `7.071067811865477 K`。实际 SQLite 写锁短时竞争后成功提交，错误参数单位原子回退，同键重放和跨账户拒绝均通过。 [实际批次与指标摘要](../../tmp/ff-c15-workflow-studies-t4mrhg6q/study.json)

认证 SeaweedFS 与 PostgreSQL 的复验分别用 380 K 与 420 K 两个工况完成 B–E。两个独立原生发布读回 340 K 与 360 K，和文件系统的同名工况一致。此前三个工况已经发布，但读取第二个归档指标时 SQL 会话释放了先前的 ORM 实例，接口返回 500。现于读取前提取绑定字段，并在读取每个算例前重新取出账户。复验包含两次真实归档读取、同键重放、错单位原子回退和跨账户拒绝。真实模型 0 次，模拟 0 次。 [首轮错误日志](../../tmp/ff-c15-workflow-studies-eptiof43/api.log) · [复验](../../tmp/ff-c15-workflow-studies-63_fm8zw/study.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-cz41b6cb/cleanup.json)

T04 验收汇总覆盖实际 CalculiX 模态与复谐响应，以及原生 OpenFOAM 参数扫描、离散优化和加权不确定度。 [验收汇总](../../tmp/FF-C15-studies-20261123/validation.json)

单独冷启动 API 并修改当前算例描述后，三个历史批次仍可读。九份实际指标及其发布 ID、指标文件摘要和汇总值逐项一致。 [冷启动复核](../../tmp/ff-c15-study-cold-read-k49dfljw/cold-study.json)
