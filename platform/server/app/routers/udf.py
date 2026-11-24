"""case 内 UDF D1–D5：模板、wmake/CDLL 与真实 OpenFOAM 集成。"""
from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import legacy_job_admission as admission
from ..services.case_write import coordinate
from ..services import file_transactions, owner_storage_budget, udf_profiles
from ..services.durable_events import json_text
from ..services.async_ops import finish_before_cancel
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}/udf", tags=["udf"])
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "udf"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
PRELOAD = ("/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libOpenFOAM.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libfiniteVolume.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libmeshTools.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libfileFormats.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libsurfMesh.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/sys-openmpi/libPstream.so")


class UdfScaffoldIn(BaseModel):
    name: str = "myFunctionObject"
    objective: str | None = Field(default=None, max_length=1000)
    kind: Literal['functionObject','boundaryCondition','sourceTerm','turbulenceModel','solver'] = 'functionObject'


class UdfBuildIn(BaseModel):
    name: str = "myFunctionObject"


class UdfIntegrateIn(UdfBuildIn):
    run_dir: str


def _name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise HTTPException(400, "UDF 名称必须是字母开头的 1–40 位字母/数字/下划线")
    return value


@router.post("/scaffold")
async def scaffold(case_id: int, body: UdfScaffoldIn, user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    name = _name(body.name)
    root = f"UDF/{name}"
    objectives = {udf_profiles.LEGACY:'首次执行输出 P0_UDF_OK',udf_profiles.BOUNDARY:'施加声明的速度边界条件',
                  udf_profiles.SOURCE:'为动量方程施加声明的均匀加速度',
                  udf_profiles.TURBULENCE:'在 RAS 模型中使用指定系数计算湍流黏度',
                  udf_profiles.SOLVER:'用自定义求解器计算瞬态热扩散'}
    objective = (body.objective if body.objective is not None else objectives[body.kind]).strip()
    if not objective:
        raise HTTPException(400, "D1 需求目标不能为空")
    files = {
        "myFunctionObject.H": (EXAMPLE / "myFunctionObject.H").read_text(encoding="utf-8"),
        "myFunctionObject.C": (EXAMPLE / "myFunctionObject.C").read_text(encoding="utf-8"),
        "Make/files": (EXAMPLE / "Make" / "files").read_text(encoding="utf-8"),
        "Make/options": (EXAMPLE / "Make" / "options").read_text(encoding="utf-8"),
        "D1_REQUIREMENTS.md": f"# D1 需求\n\n目标：{objective}\n\n验收：D4 wmake+CDLL，D5 真实 OpenFOAM 日志出现 P0_UDF_OK。\n",
        "D2_DESIGN.md": "# D2 设计\n\n继承 functionObject，TypeName 注册，execute 首次输出，write 不写文件。\n",
        "D3_IMPLEMENTATION.md": "# D3 实现\n\nmyFunctionObject.H/C 与 Make/files/options 组成最小动态库。\n",
        "D4_BUILD.md": "# D4 构建验证\n\nwmake libso 后使用 ctypes.CDLL 验证动态加载。\n",
        "D5_INTEGRATION.md": "# D5 集成\n\ncontrolDict 注册 type myFunctionObject 和 libs，运行真实 OpenFOAM 检查 P0_UDF_OK。\n",
    }
    if body.kind==udf_profiles.LEGACY and 'kind' in body.model_fields_set:
        files['README.md'] = ('# OpenFOAM 函数对象\n\n在算例的 UDF 面板完成 D4，再选择 `icoFoam` 运行目录执行 D5。'
                              '平台在私有算例写入函数对象注册，原生求解日志包含 `P0_UDF_OK`。'
                              '需求、设计、源码、构建和集成步骤见同目录的 D1–D5 文件。\n')
    if body.kind==udf_profiles.BOUNDARY:
        example=EXAMPLE/'boundary'
        names=('VibeflowVelocityBoundary.C','Make/files','Make/options')
        files={name:(example/name).read_text(encoding='utf-8') for name in names}
        files.update({
            udf_profiles.MANIFEST:json_text(udf_profiles.declaration(body.kind))+'\n',
            'README.md':'# 自定义速度边界\n\n在算例的 `0/U` 给选定 patch 声明 `type vibeflowFixedVelocity`、`velocity (1 0 0)` 和 `value uniform (0 0 0)`。构建后选同一算例目录进行集成。\n',
            'D1_REQUIREMENTS.md':f'# D1 需求\n\n目标：{objective}\n\n验收：末态边界速度等于声明向量。\n',
            'D2_DESIGN.md':'# D2 设计\n\n在原生有限体积边界系数更新中施加给定速度。\n',
            'D3_IMPLEMENTATION.md':'# D3 实现\n\n`VibeflowVelocityBoundary.C` 注册 `vibeflowFixedVelocity` 边界类型。\n',
            'D4_BUILD.md':'# D4 编译\n\n使用 `wmake libso` 编译，再实际动态加载 `libvibeflowVelocityBoundary.so`。\n',
            'D5_INTEGRATION.md':'# D5 集成\n\n确认 `0/U` 已选择原生自定义边界。系统将库加入顶层 `controlDict.libs`，运行真实 `icoFoam`，并保留新时刻的速度与压力。\n',
        })
    if body.kind==udf_profiles.SOURCE:
        example=EXAMPLE/'source'
        names=('VibeflowAcceleration.C','Make/files','Make/options')
        files={name:(example/name).read_text(encoding='utf-8') for name in names}
        files.update({
            udf_profiles.MANIFEST:json_text(udf_profiles.declaration(body.kind))+'\n',
            'README.md':'# 自定义动量源项\n\n在 `constant/fvOptions` 中声明 `type vibeflowUniformAcceleration`，在其 `vibeflowUniformAccelerationCoeffs` 中指定 `acceleration (5 0 0)`，单位 m/s²。选择串行 `pimpleFoam`。\n',
            'D1_REQUIREMENTS.md':f'# D1 需求\n\n目标：{objective}\n\n验收：周期管道的速度变化符合所施加的加速度。\n',
            'D2_DESIGN.md':'# D2 设计\n\n注册原生 `fvOption`，向速度方程的各单元动量源施加体积加权的恒定加速度。\n',
            'D3_IMPLEMENTATION.md':'# D3 实现\n\n`VibeflowAcceleration.C` 注册 `vibeflowUniformAcceleration`。\n',
            'D4_BUILD.md':'# D4 编译\n\n使用 `wmake libso` 编译并动态加载 `libvibeflowUniformAcceleration.so`。\n',
            'D5_INTEGRATION.md':'# D5 集成\n\n系统将库加入顶层 `controlDict.libs`，运行 `pimpleFoam` 并保留实际场。\n',
        })
    if body.kind==udf_profiles.TURBULENCE:
        example=EXAMPLE/'turbulence'
        names=('VibeflowKEpsilon.C','Make/files','Make/options')
        files={name:(example/name).read_text(encoding='utf-8') for name in names}
        files.update({
            udf_profiles.MANIFEST:json_text(udf_profiles.declaration(body.kind))+'\n',
            'README.md':'# 自定义湍流模型\n\n使用串行 `pimpleFoam`，准备 `0/k`、`0/epsilon` 和 `0/nut`。在 `constant/turbulenceProperties` 设置 `simulationType RAS`，然后在 `RAS` 块声明 `RASModel vibeflowKEpsilon`、`turbulence on` 和 `vibeflowKEpsilonCoeffs { effectiveCmu 0.06; }`。\n',
            'D1_REQUIREMENTS.md':f'# D1 需求\n\n目标：{objective}\n\n验收时核对原生湍动能、耗散率和湍流黏度场。\n',
            'D2_DESIGN.md':'# D2 设计\n\n复用 OpenFOAM k–ε 输运方程，将模型系数 `effectiveCmu` 用于 `nut = effectiveCmu * k² / epsilon`。\n',
            'D3_IMPLEMENTATION.md':'# D3 实现\n\n`VibeflowKEpsilon.C` 将 `vibeflowKEpsilon` 注册到不可压缩 RAS 运行时选择表。\n',
            'D4_BUILD.md':'# D4 编译\n\n用 `wmake libso` 编译并动态加载 `libvibeflowKEpsilon.so`，保留编译及加载日志。\n',
            'D5_INTEGRATION.md':'# D5 集成\n\n平台将动态库加入私有 `controlDict.libs`，运行原生 `pimpleFoam` 并发布新时刻结果。\n',
        })
    if body.kind==udf_profiles.SOLVER:
        example=EXAMPLE/'solver'
        names=('vibeflowDiffusionFoam.C','Make/files','Make/options')
        files={name:(example/name).read_text(encoding='utf-8') for name in names}
        files.update({
            udf_profiles.MANIFEST:json_text(udf_profiles.declaration(body.kind))+'\n',
            'README.md':'# 自定义热扩散求解器\n\n在 `controlDict` 设置 `application vibeflowDiffusionFoam`。提供 `0/T` 和原生热扩散率 `constant/transportProperties.DT`。\n',
            'D1_REQUIREMENTS.md':f'# D1 需求\n\n目标：{objective}\n\n验收：实际温度场与独立原生求解器一致。\n',
            'D2_DESIGN.md':'# D2 设计\n\n求解瞬态扩散方程，采用算例指定的时间步、扩散系数及有限体积边界。\n',
            'D3_IMPLEMENTATION.md':'# D3 实现\n\n`vibeflowDiffusionFoam.C` 使用原生有限体积矩阵并写出每个选定时刻的温度。\n',
            'D4_BUILD.md':'# D4 编译\n\n用 `wmake` 编译可执行文件，并运行该文件的 `-help` 入口验证安装版本能够启动。\n',
            'D5_INTEGRATION.md':'# D5 集成\n\n工作流使用封存的实际 `vibeflowDiffusionFoam` 可执行文件求解并发布温度场。\n',
        })
    # Every generated target must be absent, including partially-created trees.
    async with owner_storage_budget.hold_case(db, case):
        async with coordinate(db, case, expected_files={f"{root}/{relative}": None for relative in files}):
            await file_transactions.write(db, case,
                {f"{root}/{relative}": content.encode('utf8') for relative, content in files.items()})
        await finish_before_cancel(db.commit())
        await finish_before_cancel(file_transactions.finish_committed(db, case))
    return {"ok": True, "name": name, "files": sorted(files), "stages": ["D1", "D2", "D3"]}


@router.post(":build")
async def build(case_id: int, body: UdfBuildIn, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_udf(db,case,user,'build',body,idempotency_key,request)


@router.post(":integrate")
async def integrate(case_id: int, body: UdfIntegrateIn, request: Request, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db),
                    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_udf(db,case,user,'integrate',body,idempotency_key,request)


async def _registered_udf(db,case,user,mode,body,key,request):
    from ..services import registered_input_seal,step_handlers
    from ..services.case_write import CaseWriteConflict
    from ..services.workflow_state import ExecutionConflict
    try:
        prepare=partial(step_handlers.get_handler('udf.'+mode).prepare,
            parameters={'name':body.name,'run_dir':body.run_dir if mode=='integrate' else ''},
            budget={'timeout_seconds':86400,'max_log_bytes':16*1024*1024})
        job,created=await registered_input_seal.submit(db,case,user,key,'udf.'+mode,body.model_dump(),prepare,
                                                       request=request)
        if created:
            try:return await admission.launch(db,job)
            except ExecutionConflict as error:
                raise HTTPException(409,{'code':error.code,'message':error.detail,'job_id':job.id}) from error
        return await admission.response(db,job)
    except FileExistsError as error:raise HTTPException(409,str(error)) from error
    except FileNotFoundError as error:raise HTTPException(404,'UDF source, library or target controlDict is missing') from error
    except ExecutionConflict as error:raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except CaseWriteConflict as error:raise HTTPException(409,error.detail()) from error
    except ValueError as error:raise HTTPException(400,str(error)) from error
