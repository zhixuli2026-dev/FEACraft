"""OpenFOAM 补全路由（自建索引；对标原版 Ctrl+Space 补全，Monaco 侧走 provider）"""
from fastapi import APIRouter, Depends, Query

from ..deps import get_current_user
from ..models import User
from ..services.completion import search

router = APIRouter(prefix="/api/v1/completion", tags=["completion"])


@router.get("/openfoam")
def complete_openfoam(q: str = Query("", max_length=64),
                            kind: str = Query("", max_length=30),
                            parent: str = Query("", max_length=64),
                            context: str = Query("", max_length=64),
                            limit: int = Query(30, ge=1, le=200),
                            user: User = Depends(get_current_user)):
    """前缀补全。索引未构建时 ready=false（前端静默回退到内置核心集）"""
    return search(q, kind, limit, parent, context)
