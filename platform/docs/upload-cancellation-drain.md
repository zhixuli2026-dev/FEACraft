# 分块上传取消后的写入排空

上传取消的线程越界已修复：创建/写块/完成/取消四个操作在重复取消后仍先排空后台线程，再释放算例锁，防止覆盖已确认的新编辑或重建已回收目录。实际产品双库各4项、完整并发写入SQLite93项、上传兼容1套通过。仅保证排空顺序，不声称文件系统/数据库原子提交或自动残留恢复。head4e2da9f3c865/35表、主线11完成/18进行中/24待开始（剩42）及全功能295项范围不变；正式SIWC联调按用户决定暂缓。 详见[上传取消排空](upload-cancellation-drain.md)。

2026-11-03，N5-01／N4-04 的写入边界补充。承接[发布校验](publication-verify-lease.md)的剩余来源锁审计；在引入新的读取占用协议前，先保证已经进入算例写事务的文件线程不会在请求取消后逃出保护。修复已合入并通过正式源码回归；不增加主线或全功能任务的完成数。

## 已复现问题

`complete_upload_session` 原先在 `coordinate` 内直接等待 `asyncio.to_thread(complete_session, ...)`。ASGI 路由任务被取消时，等待者退出，算例事务随之释放，但线程不能被 asyncio 取消。它仍可能完成已经通过目标哈希检查的文件替换。

修复前双 API 证据：真实分块上传在 `os.replace(data.part, target)` 前暂停，取消路由任务后，另一 API 成功写入 `NEWER`；恢复原线程，目标被覆盖为 `UPLOAD`。证据 `tmp/upload-cancel-before-b4ojelbc/before-result.json` 保存修复前路由与上传服务哈希；复现脚本 `tmp/check-upload-cancel-before.py` 锁定该源码版本，修复后不能重跑代替原证据。这是注入 ASGI 任务取消，不声称每一次客户端断线都会取消服务器处理任务。

## 实现

文件路由的创建上传、写入分块、完成上传和取消上传四个 `to_thread` 调用，改用既有 `finish_before_cancel` 等待后台操作。重复取消会被记住，线程结束后才向上层传播取消，因此数据库协调锁一直保留到文件工作结束。只读上传状态查询没有引入写入排空处理。

文件哈希前置条件、连续 offset、整文件 SHA-256、目标保护和上传目录规则均沿用原逻辑。这里修复取消与文件线程的先后关系；文件系统修改与数据库提交仍不是一个原子事务，取消时已经写出的私有上传数据也不保证自动回滚或回收。残留、未知提交和别名／索引对账继续属于 N5-03／N5-04。

## 验证

新增 `upload_coordination_smoke.py --postgres` 使用两个真实 API、共享隔离存储和数据库，四个操作分别在后台线程窗口暂停；两次取消均不得结束路由，竞争编辑或回收站请求必须等待。释放线程后：完成上传场景中新编辑保持为最终内容；其他场景中算例不被迟到线程重建，恢复后原文件保留，分块数据／清理结果可核对。

正式源码改动前，临时模块覆盖候选已在双库各通过4项（`tmp/upload-drain-evidence-vkbawl6o/result.json`）；这只是候选证据。其初次失败是夹具未通过当前路由结构捕获实际处理任务，随后改为在文件路由的协调入口捕获，失败日志 `tmp/upload-drain-candidate-dual.log` 保留。正式源码必须重新运行专项，不拿候选通过代替。

实际源码结果：专项双库各4项通过（`tmp/upload-drain-evidence-zsfke2fr/result.json`），临时 PostgreSQL 库已删除；完整 `case_write_smoke`（SQLite）93项通过（`tmp/case-write-evidence-p149cwin/result.json`）；隔离 `upload_advanced_smoke` 通过（`tmp/upload-coordination-upload-compat/summary.json`）。源码没有迁移；开发数据库与端口8000未操作，正式 SIWC 注册／公网联调按用户决定暂缓。

直接词法审计 `tmp/unshielded-case-threads-audit.json` 原有5处受协调的裸 `to_thread`，4处写操作修复后剩1处只读状态查询（`tmp/unshielded-case-threads-after.json`）。这不是完整动态调用图，不代表所有线程或原生写入都已覆盖。下一步继续可编辑来源／依赖长事务，以及终端／CLI／Agent 等已知原生写者的准入边界。
