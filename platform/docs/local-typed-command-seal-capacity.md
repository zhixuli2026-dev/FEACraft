# 本地类型化作业的输入封存

2026-11-22，N4-05 的 RB-12 和 RB-14 补齐本地文件系统单作业的默认输入封存。适用 `POST /cases/{id}/jobs` 中的 `mesh`、`solve`、`geometry`、`pvpython` 和 `convert`，包括从作业面板运行 `D-post/postprocess.py`。类型化作业保留原执行参数、显式选择的封存清单和幂等键。[通用类型命令](typed-command-publication.md) · [后处理脚本](FF-D14-script-report.md)

## 提交顺序

1. 校验权限、作业额度和命令参数，结束查询事务。
2. 在文件复制名额内枚举本地输入，计算摘要与私有双份工作区的峰值。先核对工作区所有者的存储额度，再按峰值取得节点预留。
3. 在数据库写事务外复制、复核源摘要并生成封存目录。读取与复制共用 300 秒请求期限；请求断开或超时后，等已运行的线程结束再清理本次暂存并释放节点预留。
4. 在提交事务中再次核对权限、算例版本、源与暂存文件身份及额度。采用已有同卷原子安装和提交见证。输入发生变化时拒绝旧请求。同一幂等键可在拒绝后提交当前版本，成功后重放返回原作业。

## 实测

Windows 实际复制 41,943,040 字节。旧版暂停在第一份数据副本后时，独立 SQLite 写事务无法开始，同算例保存返回 409。新版暂停在同一位置时，同算例保存返回 200，独立事务可以写入，源选择变化后旧提交返回 `409 input_stale`，无作业记录。同键重试运行了真实 WSL Python，输出的文件摘要与当前源一致，再次提交返回同一作业。[旧版对照](../../tmp/n4-local-cmd-baseline-mex0xtuy/seal-result.json) · [SQLite 验收](../../tmp/n4-local-cmd-fixed-b0waiyxu/seal-result.json)

完成副本后，从隔离测试进程绕开文件 API 改写源文件一个字节，并恢复原大小和修改时间。提交仍返回 409，同键重跑读到了改变后的摘要。临时 PostgreSQL 同样通过独立锁、同案保存、旧提交拒绝及实际 WSL 重跑，临时数据库已删除。[源身份验收](../../tmp/n4-local-cmd-external-7dqimhs1/seal-result.json) · [PostgreSQL](../../tmp/n4-local-cmd-fixed-u4n02rkz/seal-result.json) · [临时库清理](../../tmp/terminal-agent-postgres-3kkj97u5/cleanup.json)

三秒的隔离期限耗尽后，API 等实际复制排空才返回 504。真实 HTTP 客户端断开也终止后续提交。两组无新作业，暂存已清理。90 MiB 工作区所有者额度在第一次复制前返回 429。模型调用为 0。[期限](../../tmp/n4-local-cmd-timeout-hi3cvibo/seal-result.json) · [断线](../../tmp/n4-local-cmd-disconnect-jquqv_cs/seal-result.json) · [所有者额度](../../tmp/n4-local-cmd-quota-y4dyvk_l/seal-result.json)

复制暂停时实测节点预留 83,912,046 字节，覆盖两份副本及元数据。80 MiB 节点额度时，第一次复制前即返回 `409 node_storage_full`。所有新版夹具的临时目录和节点预留均已排空。使用认证 SeaweedFS 的真实 WSL 类型化作业，以及 Windows 普通 Shell 的默认输入与显式输入，均通过兼容检查。[节点峰值](../../tmp/n4-local-cmd-fixed-5n6n5sb2/seal-result.json) · [节点拒绝](../../tmp/n4-local-cmd-node-7smreajz/seal-result.json) · [兼容实测](../../tmp/N4-05-local-command-seal-20261122/compatibility.json) · [验收汇总](../../tmp/N4-05-local-command-seal-20261122/validation.json)

如需回退，先等在途请求排空，并依据已有封存见证处理未决目录，再撤回类型化作业在提交路由中的本地暂存接线。
