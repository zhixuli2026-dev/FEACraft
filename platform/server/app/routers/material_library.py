"""Read and revise the material catalog of an accessible workspace."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import material_library as library
from ..services.case_materials import ENUMS, UNITS
from ..services.case_write import CaseWriteConflict
from ..services.workspace_write import coordinate_workspaces
from .workspaces import _accessible_workspace


router = APIRouter(prefix='/api/v1/workspaces/{workspace_id}/material-library', tags=['material-library'])
MaterialKey = Annotated[str, Path(pattern=library.KEY)]


class MaterialRevisionIn(BaseModel):
    expected_revision: int = Field(ge=0, strict=True)


class MaterialIn(MaterialRevisionIn):
    material: dict


@router.get('')
async def list_materials(workspace_id: int, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db), after: str = Query(default='', max_length=64),
                         limit: int = Query(default=50, ge=1, le=100), include_archived: bool = False):
    await _accessible_workspace(workspace_id, user, db)
    entries = await library.list_entries(db, workspace_id, after=after, limit=limit, include_archived=include_archived)
    return {**entries, 'schema': {'enums': {key: sorted(values) for key, values in ENUMS.items()},
                                 'units': {key: list(values) for key, values in UNITS.items()}}}


@router.get('/{key}')
async def read_material(workspace_id: int, key: MaterialKey, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db), revision: int | None = Query(default=None, ge=1)):
    await _accessible_workspace(workspace_id, user, db)
    row = await library.get(db, workspace_id, key, revision)
    if row is None:
        raise HTTPException(404, '材料库版本不存在')
    return library.serialize(row, full=True)


async def _append(workspace_id, key, user, db, expected_revision, body):
    await _accessible_workspace(workspace_id, user, db, write=True)
    try:
        async with coordinate_workspaces(db, [workspace_id]):
            row = await library.append(db, workspace_id, key, expected_revision, body)
            result = library.serialize(row, full=True)
        await db.commit()
        return result
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except FileNotFoundError as error:
        raise HTTPException(404, '材料库条目不存在') from error


@router.put('/{key}')
async def save_material(workspace_id: int, key: MaterialKey, body: MaterialIn,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        payload = library.encode(key, body.material)
    except (ValueError, RecursionError) as error:
        raise HTTPException(422, str(error)) from error
    return await _append(workspace_id, key, user, db, body.expected_revision, payload)


@router.post('/{key}:archive')
async def archive_material(workspace_id: int, key: MaterialKey, body: MaterialRevisionIn,
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _append(workspace_id, key, user, db, body.expected_revision, None)
