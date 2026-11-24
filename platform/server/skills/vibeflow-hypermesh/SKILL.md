---
name: vibeflow-hypermesh
description: HyperMesh（前处理） 的接口预留契约（商业求解器，组织自配 license）。本平台不捆绑商业求解器——组织部署后按本契约接入。（自配环境参考）
---

# HyperMesh（前处理） 接口预留（自有）

**支持范围**　自配环境参考。执行前在选定运行时确认HyperMesh 客户端、批处理工具和有效许可证，并以相应版本的手册和实际命令结果为准。

## 定位

商业求解器**不随平台捆绑**：由组织自备 license server 或走 caseDict `executionProfile: remote|HPC` 外发。本技能只定**接口形状**。

## 接入形态

- 输入/运行形态：hm 脚本 Tcl + `hmbatch -tcl xxx.tcl`。
- 后处理：网格导出各求解器格式；统一走 `postprocess.py`（数值曲线）与可视化（能出 VTK/VTU 的转后进 trame 面板）。
- 目录放 `C-run/hypermesh/case-N/`；license 不可用时 agent 不得静默失败——必须明确报 license/环境问题并回写 caseDict `errors`。

## 平台侧现状

- 作业系统可承载其命令行（WSL/宿主按实际安装）；模板/技能的领域精排由组织自行扩充（vibeflow-skill-creator 契约）。
