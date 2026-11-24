# 根启动意图与守护登记交接的失联恢复

2026-11-16，继续 N4-07 原有失联恢复条件，复用已有准备恢复器与 v4 检查点。

## 问题与实现

启动意图已保存，而 guardian 尚未建立或实际已建立但登记未提交时，旧分配恢复拒绝已有启动意图，执行范围恢复又缺少原 guardian 登记。这一交接空窗无法完成原 attempt 的恢复收尾。

Windows owner 的新启动意图现在保存相关实现摘要及“原守护和启动登记完成后才允许业务执行”的约定。旧意图格式仍可读取和使用已有范围证明，但不能自动升级为这条未释放执行证明。

现有准备恢复器增加一个明确的交接凭据格式：只接受恰好一条有效原启动意图，拒绝已登记的 guardian、观察、prepared、admitted 或业务执行身份。Windows 要求封存私有执行；WSL 另须对应原已分配范围，核对启动 nonce 与分配身份并实际撤销。原 worker 和已知分配控制器必须实际退出。

未提交的 guardian 出生身份保持未知，回执不包含虚构的 guardian 退出或停止证明。原启动历史与分配历史共同绑定到独立恢复结果，锁内复核快照、完整历史及取消时间后，原 attempt 收尾为 interrupted 或 cancelled。原公开成果、输入、缺失业务身份和 stop 不补写，不重放原命令。原已登记范围仍走原恢复路径。

## 实际验收

`live_llm_guardian_handoff_acceptance.py` 使用真实 PostgreSQL、S3、Redis、Windows API、Windows/Linux worker：

- Windows 与 WSL 各在原启动意图提交后退出 worker。
- Windows 与 WSL 各在实际 guardian 返回后、原 SQL 登记前退出 worker。
- WSL 未提交 guardian 的退出后取消，再由新 worker 恢复。
- 同版源码复验原未确认分配恢复和根首条观察缺失恢复。

钩子先执行原函数，使用 ContextVar 识别原根 owner，在 SQL 事务之外等待实际退出（17）。已建立但未提交的 guardian 信息仅保存在独立测试证据中；测试另行核验它的实际出生、退出和完整关闭回执，生产恢复没有收到这份未提交登记。原业务命令未执行，旧文件保留，每个场景随后执行新命令并发布随机文件，普通 WSL 实测 UID65534。最后由真实 DeepSeek 文件工具读取新文件。

Windows/Linux 凭据修改拒绝检查单列，不冒充模型验收；没有模拟 LLM 回归。

## 最终结果与归档

2026-11-16 N4-07 根启动意图与guardian登记交接恢复已补齐：新意图保存原守护/启动登记先于业务释放的实现约定，既有准备恢复器接入Windows无原生分配及WSL原范围撤销，未登记guardian保持未知、不伪造退出。Windows/WSL意图提交、实际guardian返回但未提交、WSL取消五场景通过，三个未提交guardian由测试独立核验实际退出/关闭；同版原未确认分配及首条观察缺失恢复兼容通过。旧产物保留，新命令发布，普通WSL UID65534。共18个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；292项纯凭据检查另列，83个已记录原进程身份消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 guardian-handoff-loss-recovery.md。主线48/53、剩5，295交付状态保持；专业child更早准备、其他外部owner及N4-05/N7原范围继续。

实际证据 `tmp/remote-worker-hr8sp8t8`；数据库清理 `tmp/remote-worker-postgres-36e5ffa7ba3440799da7db20e2fe160e`。源码归档 `platform/backups/mainline-N4-07-guardian-handoff-20261116.zip`，校验记录 `tmp/mainline-N4-07-guardian-handoff-evidence/validation.json`。所有执行源码与本轮审查散列一致。
