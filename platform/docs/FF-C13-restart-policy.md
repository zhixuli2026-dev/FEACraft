# FF-C13 串行 OpenFOAM 续算策略

在 `caseDict` 的 `execution` 块设置 `restartPolicy`。平台在准备原串行求解时将该策略绑定到不可变的尝试描述。恢复时按原尝试的策略核验，并沿用现有的停止、输出清单、求解器、网格和字段校验。已有尝试缺少此字段时，按默认的 `explicit` 处理。 [算例校验](../server/app/services/case_execution_settings.py) · [续算检查点](openfoam-checkpoint-resume.md)

| `restartPolicy` | 续算行为 |
| --- | --- |
| `explicit` | 提供原尝试实际写出的正时刻，例如 `0.005`。 |
| `latestWritten` | 可以指定写出时刻，也可以选择原尝试最近写出的时刻。 |
| `disabled` | 检查点续算入口禁用。 |

```text
execution
{
    restartPolicy latestWritten;
}
```

`POST /api/v1/jobs/{id}:repeat` 在 `action=resume` 时接受 `parameters.time`。工作流 `POST /api/v1/workflow-runs/{id}:resume` 在 `mode=checkpoint` 时接受 `checkpoint_time`。策略为 `latestWritten` 时，客户端可发送 `latest`。服务器核对原输出清单，从原起止时刻之间选取最近的完整时刻。检查点及其保留的较早场都通过原生网格和字段校验。选定时刻及摘要随新尝试和恢复流程记录。用户从作业面板确认后才启动，原作业与尝试可按编号查看。 [策略许可](../server/app/services/solve_restart_policy.py) · [完整时刻核验](../server/app/services/solve_resume_artifacts.py) · [正式工作流恢复](workflow-resume-atomic.md)

文件系统实际 `icoFoam` 续算选择 `0.015 s`，第一步 `0.0155 s`，最终到 `0.5 s` 并正式发布。此轮无中断对照在实际计算完毕后达到 120 秒整次尝试期限，完整对照另行复跑。PostgreSQL 的 `disabled` 策略用真实取消作业验证，指定时刻和 `latest` 均返回 `checkpoint_disabled`，过程停止已核验。禁用结果经真实 DeepSeek `deepseek-flash` 读回，两次 HTTP 200，模拟 0。 [首轮续算与原生时刻](../../tmp/live-cli-startup-4blkwj9a/solve-resume-progress.json) · [禁用策略](../../tmp/live-cli-startup-__oz5gt9/solve-resume.json) · [真实模型回执](../../tmp/live-cli-startup-__oz5gt9/live-result.json)

Edge 从真实作业的「恢复与重新执行」切换到指定时刻，再切回最近写出时刻并明确确认。公开 API 接收 `latest`，后端返回实际选定时刻，界面在新任务上回显。新尝试完成原生求解、发布和停止核验。1440、900、640 px 实际工作台视窗检查表单宽度和至少 44 px 的控件，焦点边框为 2 px。 [Edge 请求和新尝试回执](../../tmp/ff-c13-restart-browser-vrrabus8/restart-browser-acceptance.json) · [窄窗口截图](../../tmp/ff-c13-restart-browser-vrrabus8/restart-controls-640.png)

实际再次取消时，`0.03/phi` 已创建且长度为零。保留该原始封存输出，在隔离 API 重新发起 `latest`，实际选中 `0.025 s` 并从 `0.0255 s` 算至 `0.5 s`。原中断记录及空文件摘要保持，重复提交返回同一新尝试。旧版验证器生成的检查点也经新版本重新核验通过。 [真实半写记录](../../tmp/live-cli-startup-4ywqt8sn/live-result.json) · [半写恢复并发布](../../tmp/ff-c13-native-partial-replay-6y3wo1ng/partial-checkpoint.json) · [旧版检查点](../../tmp/FF-C13-restart-20261123/legacy-validator.json)

重启隔离 API 后，用同一续算发布读取产物事件、作业谱系和检查点摘要，查到对应 `caseDict` 修订 3 的原历史事件以及引用的已确认网格。其他账户返回 404。适用的策略值和旧尝试的显式默认方式另有协议验证。 [重启后的产物追溯](../../tmp/ff-c13-artifact-trace-0x_w0xtu/restart-artifact-trace.json) · [策略校验](../../tmp/FF-C13-restart-20261123/schema-policy.json)

本地 240 秒作业预算的完整对照已通过。实际 `icoFoam` 选中 `0.02 s`，从 `0.0205 s` 推进至 `0.5 s`。与同一算例从零运行相比，最终 `U` 与 `p` 最大绝对差均为 0。真实 `deepseek-flash` 回读通过，两次 HTTP 200，模拟 0。认证对象存储曾在 100 组写出时刻的收尾阶段触发 120 秒和 240 秒期限，保留实际超时记录。当前对象存储对照保持计算时步与终点，每 50 步写出一次，共 20 组时刻。 [文件系统完整对照](../../tmp/live-cli-startup-dclqllsw/solve-resume.json) · [真实模型回执](../../tmp/live-cli-startup-dclqllsw/live-result.json) · [认证对象存储超时](../../tmp/live-cli-startup-hhno3bsw/native-resume-budget-5.json)

PostgreSQL 和认证 SeaweedFS 的工作流续算从 `0.025 s` 继续到 `0.5 s`，到达 D 人工确认。测试先移走原尝试的本地缓存，从认证归档重新核验。重复请求返回原尝试，完整从零求解的最终 U、p 与续算比较，最大绝对差均为 0。真实 DeepSeek 本次返回两次 HTTP 200。 [认证对象存储完整对照](../../tmp/live-cli-startup-34f5dllp/solve-resume.json) · [真实模型回执](../../tmp/live-cli-startup-34f5dllp/live-result.json)

FF-C13 按 T04 验收。8 组回执覆盖算例策略、实际取消、半写检查点、原文件、重启后的产物追溯、页面与两套存储。专项真实 DeepSeek `deepseek-flash` 共 6 次 HTTP 200，模拟 0。主线 51/53，全功能 194/295，分别剩 2 项和 101 项。 [验收汇总](../../tmp/FF-C13-restart-20261123/validation.json) · [台账核对](../../tmp/stage-ledger-evidence-20261123-ff-c13.json)

续算界面构建落在 D 盘。构建前和结束时 C 盘分别可用 14,123,380,736 B、14,122,393,600 B。前后观察 Docker 构建缓存均为 22.67 MB，这段编译没有回收 Docker 对象。
