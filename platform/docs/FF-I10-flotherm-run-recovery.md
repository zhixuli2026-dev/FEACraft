# FF-I10 FloTHERM 求解取消、续跑与长程对拍

2026-11-23。生成的 Allrun 和 Allclean 在 Linux 使用同一生成标记的文件锁。串行和 MPI 各实际取消两次，核验本次求解进程全部退出。求解运行中，重复启动和清理退出 75，CLI 强制覆盖退出 2，生成标记保持完整。停止后从原检查点继续，最终温度场与对应不间断运行的 SHA-256 完全一致。一次串行停止时遇到未写完的时刻 60，先把该目录移到算例外保存，再从完整时刻 40 续算。清理成功后仍可读取原始初始场和用户记录。[串并行续跑](../../tmp/FF-I10-run-recovery-20261123/retry1/cancellation.json) · [强制覆盖与取消复验](../../tmp/FF-I10-run-recovery-20261123/force-recovery/cancellation.json)

对 7 种离线输入组合，分别用提取版和平台版的实际 OpenFOAM v2406 从 200 继续至 2000，共完成 14 次非特权 WSL 求解。每种组合均有 0.4 W 转换输入、约 0.064 m³ 空气和 0.008 m³ 固体。各版本内部的 7 种输入得到相同界面热量。平台与提取版仍有网格、离散格式、热源实现和收敛设置差异。[逐组合配置与文件摘要](../../tmp/flotherm-g6-reference-20261120/matrix/native-matrix.json) · [14 次长程求解](../../tmp/FF-I10-run-recovery-20261123/long/long-matrix.json)

| 求解设置 | 迭代 2000 的界面净热 |
| --- | --- |
| 平台版原配置 | 0.323254 W |
| 提取版提高流体能量松弛和求解精度，固体能量松弛 0.70 | 0.028331 W |
| 同一 ECXML 和提取版检查点再单独将固体能量松弛改为 0.95 | 0.168328 W |

三组在时刻 2000 均有稳态热平衡残差。单变量复验量化了固体松弛设置的影响。另一个两热源流道算例用收紧能量设置的提取版和原平台版都趋近 0.4 W，界面净热相差约 0.0000103 W。[固体松弛复验](../../tmp/FF-I10-run-recovery-20261123/solid-relaxation/long-matrix.json) · [两热源流道](../../tmp/flotherm-g6-reference-20261120/reference-tuning.json)

Windows CLI `check` 通过既有算例及当前生成算例。转换作业与原生求解分别保留恢复证据。实际 Edge 取消和 SQLite、PostgreSQL、认证 S3 的输出发布、重启回收及产物校验见 FF-I10D。此次真实模型调用 0，模拟调用 0。[旧算例检查](../../tmp/FF-I10-run-recovery-20261123/legacy-check/existing-check.json) · [发布保护](FF-I10D-flotherm-output-protection.md) · [T09 核对](../../tmp/FF-I10-run-recovery-20261123/t09-validation.json)
