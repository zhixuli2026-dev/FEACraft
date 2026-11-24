"""Legacy OpenCode status; global mutations retired in favor of case contracts."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])

OPENCODE_CFG = Path.home() / ".config" / "opencode"

# 自有工作流契约（写进 opencode 全局 AGENTS.md 的文本；语义对齐平台，不复制原厂文字）
CONTRACT = """# VibeFlow 工作流契约（平台注入，供 opencode 在本平台算例目录内工作时遵循）

- caseDict（0-caseDict/caseDict）是唯一参数源；每次任务执行后回写 workflowState 字母（A 几何/B 网格/C 求解/D 后处理/E 报告/V 开发）。
- 目录布局：0-caseDict/ A-geo/ B-mesh/ C-run/case-*/ D-post/ E-report/ UDF/ Ref/。
- 危险操作（删除/覆写/跑命令）先给计划再执行；聚焦编辑用精确替换，别整文件重写。
- 复杂任务先列计划（todo）；回答末尾给：1. 状态更新 2. 待办 3. 下一步。
- 技能库在平台服务端 server/skills/<name>/SKILL.md（只读参考，需要时按名读取）。
- 经验沉淀写进 0-caseDict/memory.md（用户纠错/踩坑）。
"""


@router.post("/opencode:apply-prompts")
async def apply_opencode_prompts(user: User = Depends(get_current_user)):
    raise HTTPException(410, 'Global prompt writes retired; use the case CLI contract settings')


@router.get("/opencode:status")
async def opencode_status(user: User = Depends(get_current_user)):
    target = OPENCODE_CFG / "AGENTS.md"
    return {"config_dir": str(OPENCODE_CFG), "exists": OPENCODE_CFG.is_dir(),
            "applied": target.is_file() and "VibeFlow 工作流契约" in
                     target.read_text(encoding="utf-8", errors="replace")}
