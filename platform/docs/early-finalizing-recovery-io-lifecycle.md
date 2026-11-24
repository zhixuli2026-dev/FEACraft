# 独立恢复请求的取消、退出与提交边界

2026-11-10。主线仍为37/53完成、剩16项，本段新增完成整项0。N5-04尚未完成，53项及295项全功能目标不变。

## 实现与结果

`finalizing_stage_recovery.recover`复用输出恢复的两个并发请求槽；达到上限返回`early_finalization_busy`，在请求真正退出后释放。实际文件线程未返回时，取消不能提前结算IO归属。API确切退出后，新API才能接续原文件计划；捕获事务已提交、IO归属尚未关闭时，只回收对应遗留归属，不再生成另一份捕获或重跑原求解器。

|实际场景|基础设施|保留报告|
|---|---|---|
|TCP断连，等待实际部分文件线程返回，再重复请求|FS、SQLite|`../../tmp/early-finalizing-j60ryb_t/early-result.json`|
|实际写入7字节后恢复API退出，其他API接续|S3、临时PostgreSQL|`../../tmp/early-finalizing-ti98j5b5/early-result.json`|
|捕获事务提交后、IO归属关闭前退出，再接续原发布|FS、SQLite|`../../tmp/early-finalizing-axzw4htt/early-result.json`|
|部分写入后API退出，发布后由真实模型读取原文件|S3、临时PostgreSQL|`../../tmp/early-finalizing-xuu5j52w/early-result.json`|

四组均通过。各原程序根仅启动一次，共4次；保留原Job、attempt、输入、期限、物理停止证明和publication。每组两条IO操作最终均为settled，只有一次捕获事件。取消场景两次实际返回，无伪造退出；API退出场景分别具有一次确切退出和一次实际返回。存活恢复API持有IO期间，另一个API返回409，不能按租约时间接管。实际请求占用一个并发槽，退出后恢复两个可用槽。

真实模型与基础设施证据分开统计：本段仅最后一组调用`api.deepseek.com / deepseek-flash`，2次HTTP 200、2份usage、8556 tokens；实际`read_text_file`一次，读回仅存在于源文件中的完整随机验证串。模拟LLM调用0。前三组为真实程序、存储和HTTP故障验收，不计为模型测试。独立复核是只读证据与实际基础设施查询，不计为新增业务功能。

15个实际API出生身份均有保留创建句柄的正向退出记录；独立检查端口已关闭。2个自建S3 owner经WSL `/proc`确认退出；2个临时PostgreSQL库经只读系统目录查询确认已删除；开发数据库未写入。97个已审查源码哈希与四组报告一致。

证据：[独立复核](../../tmp/N5-early-recovery-IO-verified-evidence-20261110.json)，[源码审查](../../tmp/N5-early-IO-review-v1-20261110.json)，[只读复核审查](../../tmp/N5-early-IO-auxiliary-review-20261110.json)。本段通过不覆盖其他历史失败，也不补造缺失的原生停止证明。

## 收口与后续

上述四个命名场景已完成，本子阶段结束。下一步逐条核对N5-04原四条remaining，复用已封存的准备、上传、缓存、取消、期限与发布证据，先形成覆盖和缺口清单，再只实现或验收缺口。保留原四条条件，不增加53项分母，也不把检查数量换算成交付进度。历史LaTeX缺失停止证明必须恢复真实证据或给出明确人工处置；旧staging与不支持的回调不能自动重算或伪造成功。

主线11月23～30日仍仅为低置信度粗估，尚无逐项剩余工时支撑；295项全功能没有可信完工日期。耗时须区分产品缺陷、验收返工和阶段收口安排，不将全部耗时归为任务复杂。
