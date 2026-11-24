---
name: vibeflow-jobs
description: 当需要执行耗时命令（求解器、网格、脚本、转换）、跟踪作业进度、查看日志或取消作业时使用。覆盖作业类型（shell/solve/mesh/pvpython/geometry/convert）、实时日志流、取消语义与工作区沙箱约定。
---

# 作业执行技能

**支持范围**　平台已实测注册作业与通用命令的日志、取消、重试。命令在所选运行时执行，平台通过所属进程范围核验停止。

## 何时用作业（而不是终端）

- 耗时 > 30 秒的任务（求解、网格、编译、转换）→ 作业（可取消、日志持久化、SSE 实时跟踪）
- 交互式/探索性命令 → 终端面板

## API 面

- `POST /cases/{id}/jobs` {type, command} → 作业 id
- `GET /jobs/{id}` → 状态+日志尾部；`GET /jobs/{id}/log?offset=` 增量轮询
- `GET /jobs/{id}/log-stream` SSE 实时；`POST /jobs/{id}:cancel` 取消
- 日志文件：`.vibeflow/jobs/job-N.log`（算例内，随工作区持久化）

## 约定

- 作业在算例工作区目录下执行（cwd=算例根），`VIBEFLOW_CASE_ROOT` 环境变量已注入
- 求解器作业先把 OpenFOAM 字典写到 `C-run/case-*/`，再调求解器命令
- 危险命令模式（rm -rf / mkfs / format）会被平台二次拦截
