# 文档重试与 S3 连续续算验收

2026-11-08。本批是 N5-05 的部分验收，主线仍为 34/53，剩余 19 项；全部 295 项功能交付范围保持。

## PDF 与 PPT

入口为 `live_llm_document_repeat_acceptance.py --source-profile-id 26 --postgres`。使用独立 API、临时 PostgreSQL 和私有文件目录。测试在实际工作进程获准执行前，于该尝试自己的输出位置创建真实冲突文件，再调用原有进程放行函数。实际 PDF/PPT worker 因 FileExistsError 失败，随后移除后续尝试的故障条件，走公开操作接口重试和重跑；没有替换进程结果或模型输出。

`tmp/live-cli-startup-_4vfrk1q/document-repeat.json` 记录两个处理器、六个实际文档作业：

- `pdf.number`：原两页 PDF 失败后，以原封存输入、同根 attempt 2 重试；生成文件每页的实际页码通过解析验证。将当前输入改为三页后，在新的输出路径和新根重跑，实际页数和每页页码正确。
- `ppt.build`：原橙色图片资源失败后，以原封存输入、同根 attempt 2 重试；生成的一页 PPT 含实际文字和原图片字节。当前图片改为蓝色后，新根重跑读取新资源，实际 PPT 图片字节正确。
- 两者均验证正式发布、已占用输出拒绝、原不可变版本和失败历史保留，以及同 key 重试重放不重复创建作业。

这批使用 FS/PG；S3、其他专业模式、工具链变更与其余处理器继续原范围，不能将命名场景记为全部模式验收。

## S3 下两次取消后的第三次续算

入口为 `live_llm_repeated_solve_resume_acceptance.py --source-profile-id 26 --s3 --postgres`。`tmp/live-cli-startup-d95kjyca/solve-resume.json` 记录实际 400 单元 icoFoam：首次在 0.005 检查点取消，第一次续算在 0.01 检查点再次取消并排空，第三次同根尝试从 0.0105 计算到 0.5，完成正式发布。源尝试整份本地缓存实际移走，从核验过的 S3 归档读取检查点。再以 fresh 新根计算，与续算最终 U、p 场的最大绝对差均为 0。前两次取消历史保留，九类归档/参数拒绝检查通过。

本批补齐 S3 连续续算的命名场景；崩溃交错、部分输入缓存的独立归档视图恢复、其他处理器及旧固定 argv 策略继续。

## 模型与环境证据

每个场景成功后，真实 `api.deepseek.com / deepseek-flash` Agent 通过读文件工具回读绑定场景报告的哈希。共 4 次真实 HTTP 请求、17,112 reported tokens，模拟模型调用 0。模型回读与实际文档生成、原生数值比较分别验收，不能将回读当作模型选择了这些业务操作或验证了物理结果。

两个临时 PostgreSQL 数据库已删除，API 已退出，独立 S3 服务已停止；开发数据库未修改。验收清单与源码归档信息见 `tmp/mainline-N5-document-and-s3-chain-evidence/validation.json`。处理器覆盖表更新为 26 类中 10 类有部分命名重试/续算证据，16 类尚缺这类证据；没有据此将任何处理器或主线整项标为完整交付。
