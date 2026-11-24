"""从 Flotherm IR 生成 OpenFOAM 热 case 与预检证据。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from .flotherm_coverage import build_coverage_report, source_trace
from .flotherm_ir import validate_ir
from .flotherm_output import MARKER, prepare_output, write_manifest
from .flotherm_refinement import refinement_level, region_levels
from .flotherm_settings import conversion_modes, mesh_skewness, parallel_subdomains

_HEADER = "FoamFile\n{\n version 2.0; format ascii; class dictionary; object %s;\n}\n"
_WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}')


def _bbox(obj: dict):
    shape = obj.get("shape", {})
    if shape.get("kind") != "box":
        return None
    dims = shape.get("dimensions")
    if not isinstance(dims, list) or len(dims) != 3 or any(v is None for v in dims):
        return None
    origin = shape.get("origin", [0, 0, 0])
    return [list(origin), [origin[i] + dims[i] for i in range(3)]]


def _imported_conditions(ir):
    raw = ir.get('source_conditions')
    if not isinstance(raw, dict):
        raise ValueError('FloTHERM IR 缺少原始计算域和环境温度')
    domain = ir.get('settings', {}).get('domain', raw.get('domain'))
    temperature = raw.get('ambient_temperature_k')
    if (not isinstance(domain, list) or len(domain) != 6 or
            any(type(value) not in (float, int) or not math.isfinite(value) for value in (*domain, temperature)) or
            any(domain[i] >= domain[i+3] for i in range(3)) or temperature <= 0):
        raise ValueError('FloTHERM IR 的计算域或环境温度无效')
    return domain, temperature


def generate_case(ir: dict, output: Path) -> dict:
    validate_ir(ir)
    if any(not _WORD.fullmatch(obj['id']) for obj in ir['objects']):
        raise ValueError('FloTHERM 对象 ID 必须是有效的 OpenFOAM 名称')
    settings = ir.get('settings', {})
    if 'boundaries' in settings or 'openfoam' in settings:
        from .flotherm_channel_case import generate_channel_case
        domain, temperature = _imported_conditions(ir)
        return generate_channel_case(ir, output, domain, temperature, _bbox)
    prepare_output(output, force=False)
    start_from, merge = conversion_modes(settings)
    boxes = {obj["id"]: _bbox(obj) for obj in ir["objects"]}
    imported = ir['source'].get('format') in {'ECXML', 'PDML'}
    domain, temperature = _imported_conditions(ir) if imported else (ir.get('settings', {}).get('domain'), 300)
    overrides = []
    levels = region_levels(settings,ir.get('regions',()))
    if any(levels.values()) or any(refinement_level(obj.get('refinement',0)) for obj in ir['objects']):
        overrides.append({'object':'global','reason':'局部网格细化需要共轭传热配置，纯导热采用原固定正交网格'})
    if mesh_skewness(settings) is not None:
        overrides.append({'object':'global','reason':'maxSkewness 网格门禁需要共轭传热配置'})
    if merge:
        overrides.append({'object':'global','reason':'并盒适用于共轭传热的多区域生成'})
    sources = []
    conductivities = set()
    for obj in ir["objects"]:
        if 'flow_device' in obj:
            continue
        properties = ir["materials"][obj["material"]]
        conductivity = properties["conductivity_w_mk"]
        if isinstance(conductivity, list):
            overrides.append({"object": obj["id"], "reason": "正交各向异性需要张量扩散求解器，laplacianFoam 不支持"})
        elif imported:
            capacity = properties.get('rho_cp')
            if type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
                overrides.append({'object': obj['id'], 'reason': 'FloTHERM 纯导热需要有效的材料体积热容'})
            else:
                diffusivity = conductivity/capacity
                if not math.isfinite(diffusivity) or diffusivity <= 0:
                    raise ValueError('FloTHERM 材料热扩散率无效')
                conductivities.add(diffusivity)
        else:
            conductivities.add(float(conductivity))
    if len(conductivities) > 1:
        overrides.append({"object": "global", "reason": "多种扩散率需要分区域 DT/专用求解器，不能压成统一标量"})
    diffusivity = next(iter(conductivities)) if len(conductivities) == 1 else 0.001
    for obj in ir["objects"]:
        power = obj.get("power_w", 0)
        if 'flow_device' in obj:
            overrides.append({'object':obj['id'],'reason':'风扇和流阻设备需要共轭传热流体求解配置'})
            continue
        if obj.get('type') == 'twoResistorModel':
            overrides.append({'object':obj['id'],'reason':'2R 热阻网络需要流体、壳体和 PCB 的多区域求解配置'})
            continue
        if imported and obj['mapping'] not in {'converted', 'equivalent'}:
            overrides.append({'object': obj['id'], 'reason': 'FloTHERM 对象未映射到当前算例'})
            continue
        if boxes[obj["id"]] is None and obj["mapping"] in {"converted", "equivalent"}:
            overrides.append({"object": obj["id"], "reason": "当前生成器仅支持 box 热源区域"})
            continue
        if power:
            material = ir["materials"][obj["material"]]
            rho_cp = material.get("rho_cp")
            if not isinstance(rho_cp, (int, float)) or rho_cp <= 0:
                overrides.append({"object": obj["id"], "reason": "材料缺 rho_cp，不能换算体积热源"})
                continue
            low, high = boxes[obj["id"]]
            volume = (high[0] - low[0]) * (high[1] - low[1]) * (high[2] - low[2])
            sources.append({"id": obj["id"], "zone": obj["id"] + "Cells",
                            "su": power/rho_cp if imported else power/(rho_cp*volume), "power_w": power})
    if imported:
        geometry = next(iter(boxes.values()))
        if len(boxes) != 1 or geometry is None or not all(
                math.isclose(value, bound, abs_tol=1e-9, rel_tol=1e-9)
                for value, bound in zip(geometry[0]+geometry[1], domain)):
            overrides.append({'object': 'global', 'reason': '当前原始模型可求解范围是单个填满计算域的固体。含空气的计算域及多个区域需要共轭传热。'})
    if domain is None:
        valid = [box for box in boxes.values() if box]
        if not valid:
            domain = [0, 0, 0, 1, 1, 1]
        else:
            domain = [min(box[0][i] for box in valid) for i in range(3)] + [max(box[1][i] for box in valid) for i in range(3)]
    x0, y0, z0, x1, y1, z1 = domain
    vertices = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    (output / "system").mkdir()
    (output / "constant").mkdir()
    (output / "0").mkdir()
    block = _HEADER % "blockMeshDict" + "convertToMeters 1;\nvertices (\n" + "".join(
        f" ({x} {y} {z})\n" for x, y, z in vertices) + ");\nblocks (hex (0 1 2 3 4 5 6 7) (10 10 10) simpleGrading (1 1 1));\nboundary ( walls { type wall; faces ((0 3 2 1) (4 5 6 7) (0 1 5 4) (1 2 6 5) (2 3 7 6) (3 0 4 7)); } );\n"
    (output / "system" / "blockMeshDict").write_text(block, encoding="utf-8")
    topo = _HEADER % "topoSetDict" + "actions (\n" + "".join(
        f" {{ name {obj['id']}Cells; type cellZoneSet; action new; source boxToCell; box ({' '.join(map(str, boxes[obj['id']][0]))}) ({' '.join(map(str, boxes[obj['id']][1]))}); }}\n"
        for obj in ir["objects"] if boxes[obj["id"]] is not None) + ");\n"
    (output / "system" / "topoSetDict").write_text(topo, encoding="utf-8")
    function_blocks = []
    thermal_balance = imported and len(boxes) == 1 and not overrides
    mapped_solids = {obj['id'] for obj in ir['objects'] if obj['mapping'] in {'converted', 'equivalent'}}
    monitor_results = []
    monitor_coverage = []
    for index, monitor in enumerate(ir.get("monitors", [])):
        kind = monitor["type"]
        name = f"monitor_{index}_{kind.replace('-', '_')}"
        if kind == "region-temperature":
            for position, target in enumerate(dict.fromkeys(monitor['objects'])):
                zone = target + 'Cells'
                function = name if position == 0 else f'{name}_{position}'
                if boxes[target] is None:
                    overrides.append({'object': function, 'reason': '温度监视器的目标区域没有可生成的网格选区'})
                    continue
                function_blocks.append(f" {function} {{ type volFieldValue; libs (fieldFunctionObjects); regionType cellZone; name {zone}; operation volAverage; fields (T); writeFields false; executeControl timeStep; executeInterval 20; writeControl timeStep; writeInterval 20; }}\n")
                monitor_results.append({'monitor': index, 'object': target, 'method': 'volFieldValue', 'object_name': function})
        elif kind == "wall-heat-flux":
            if thermal_balance:
                monitor_results.append({'monitor': index, 'objects': monitor['objects'],
                                        'method': 'solved-temperature-wall-gradient', 'command': 'balance'})
            else:
                overrides.append({'object': name, 'reason': '此纯导热算例缺少经核验的壁面热通量计算条件'})
        elif kind == "probe":
            locations = monitor.get("locations", [[(domain[axis]+domain[axis+3])/2 for axis in range(3)]])
            if (not isinstance(locations, list) or not 1 <= len(locations) <= 1000 or
                    any(not isinstance(point, list) or len(point) != 3 or
                        any(type(value) not in (float, int) or not math.isfinite(value) for value in point)
                        for point in locations)):
                raise ValueError('FloTHERM 探针需要有限的三维坐标')
            source_fields = {key: monitor[key] for key in ('id', 'name', 'path', 'sources')} if 'sources' in monitor else {}
            reason = monitor.get('reason') if not monitor['objects'] else None
            if source_fields and set(monitor['objects']) - mapped_solids:
                reason = '探针所属的固体尚未映射'
            if reason is None and any(not all(domain[axis] < point[axis] < domain[axis+3] for axis in range(3))
                                      for point in locations):
                reason = '温度探针必须位于计算域内部'
            if reason is not None:
                overrides.append({'object': name, 'reason': reason, **({'monitor_id':monitor['id']} if source_fields else {})})
                if source_fields:
                    monitor_coverage.append({**source_fields, 'mapping':'unsupported', 'reason':reason})
                continue
            points = " ".join(f"({' '.join(map(str, point))})" for point in locations)
            function_blocks.append(f" {name} {{ type probes; libs (sampling); fields (T); probeLocations ({points}); executeControl timeStep; executeInterval 20; writeControl timeStep; writeInterval 20; }}\n")
            monitor_results.append({'monitor': index, 'objects': monitor['objects'], 'method': 'probes',
                                    'object_name': name, 'locations': locations, **source_fields})
            if source_fields:
                monitor_coverage.append({**source_fields, 'mapping':'converted', 'function':name})
        else:
            overrides.append({"object": name, "reason": "laplacianFoam 纯热 case 没有 channel 流量场 phi"})
    functions = "functions\n{\n" + "".join(function_blocks) + "}\n" if function_blocks else ""
    write_format = ir.get('settings', {}).get('writeFormat')
    format_entry = f"writeFormat {write_format};\n" if write_format is not None else ""
    (output / "system" / "controlDict").write_text(_HEADER % "controlDict" +
        f"application laplacianFoam; startFrom {start_from}; startTime 0; stopAt endTime; endTime 100; deltaT 1; writeControl timeStep; writeInterval 20;\n"
        + format_entry + functions, encoding="utf-8")
    (output / "system" / "fvSchemes").write_text(_HEADER % "fvSchemes" +
        "ddtSchemes { default steadyState; } gradSchemes { default Gauss linear; } divSchemes { default none; } laplacianSchemes { default Gauss linear corrected; } interpolationSchemes { default linear; } snGradSchemes { default corrected; }\n", encoding="utf-8")
    (output / "system" / "fvSolution").write_text(_HEADER % "fvSolution" +
        "solvers { T { solver PCG; preconditioner DIC; tolerance 1e-6; relTol 0.01; } }\n", encoding="utf-8")
    (output / "constant" / "transportProperties").write_text(_HEADER % "transportProperties" +
        f"DT [0 2 -1 0 0 0 0] {diffusivity};\n", encoding="utf-8")
    fv = _HEADER % "fvOptions" + "".join(
        f"heatSource_{src['id']} {{ type scalarSemiImplicitSource; active true; scalarSemiImplicitSourceCoeffs {{ selectionMode cellZone; cellZone {src['zone']}; volumeMode absolute; injectionRateSuSp {{ T ({src['su']} 0); }} }} }}\n"
        for src in sources)
    (output / "constant" / "fvOptions").write_text(fv, encoding="utf-8")
    temperature_header = (_HEADER % "T").replace("class dictionary", "class volScalarField")
    (output / "0" / "T").write_text(temperature_header +
        f"dimensions [0 0 0 1 0 0 0]; internalField uniform {temperature}; "
        f"boundaryField {{ walls {{ type fixedValue; value uniform {temperature}; }} }}\n", encoding="utf-8")
    coverage = build_coverage_report(ir)
    manifest = {"schema": "vibeflow.flotherm2foam.case-manifest.v1", "source": ir["source"],
                "application": "laplacianFoam", "objects": [obj["id"] for obj in ir["objects"]],
                "heat_sources": sources, "required_overrides": overrides}
    if monitor_results:
        manifest['monitor_results'] = monitor_results
    if write_format is not None:
        manifest['write_format'] = write_format
    if monitor_coverage:
        manifest['monitor_coverage'] = monitor_coverage
    if ir.get('regions'):
        manifest['regions'] = [{**row, 'mapping':'domain' if row['type'] == 'domain' else 'metadata-only'}
                               for row in ir['regions']]
    count = parallel_subdomains(settings)
    if type(settings.get('scotch',True)) is not bool:
        raise ValueError('scotch 需要布尔值')
    method = 'scotch' if settings.get('scotch',True) else 'simple'
    (output/'system/decomposeParDict').write_text(_HEADER % 'decomposeParDict' +
        f'numberOfSubdomains {count}; method {method}; simpleCoeffs {{ n ({count} 1 1); }}\n',encoding='utf8')
    manifest['parallel_subdomains'] = count
    if start_from=='latestTime':
        manifest['start_from'] = start_from
    trace = source_trace(ir, manifest)
    if trace:
        manifest['source_trace'] = trace
    (output / "case-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "required-overrides.json").write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
    (output / "power-balance.json").write_text(json.dumps(coverage["summary"], indent=2) + "\n", encoding="utf-8")
    preflight = {"ok": not overrides, "blockers": overrides,
                 "required_files": ["system/blockMeshDict", "system/topoSetDict", "constant/fvOptions", "0/T"]}
    (output / "preflight.json").write_text(json.dumps(preflight, indent=2) + "\n", encoding="utf-8")
    (output / "mesh_gate.json").write_text(json.dumps({"ok": False, "reason": "等待 blockMesh+topoSet 证据"}, indent=2) + "\n", encoding="utf-8")
    files = [path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file() and path.name != MARKER]
    write_manifest(output, files)
    return manifest


# All generated entry points share the existing output marker's native lock.
# Nested bash invocations inherit the open description, so they can take it again.
RUN_LOCK_SCRIPT = f'''if [[ ! -f {MARKER} || -L {MARKER} ]]; then
    printf '%s\\n' 'FloTHERM generation marker is missing or invalid' >&2
    exit 2
fi
if [[ ! {MARKER} -ef /proc/$$/fd/9 ]]; then
    exec 9< {MARKER}
fi
flock -n 9 || {{ printf '%s\\n' 'FloTHERM case is already running or being cleaned' >&2; exit 75; }}
'''
CONDUCTION_MESH = 'python3 system/flotherm_conduction_gate.py'


def write_run_scripts(output: Path, manifest: dict | None = None):
    manifest = manifest or {}
    cht = manifest.get('application') == 'chtMultiRegionSimpleFoam'
    latest = manifest.get('start_from') == 'latestTime'
    count = manifest.get('parallel_subdomains',2)
    header = 'cd -- "$(dirname -- "$0")"\n'
    if cht:
        scripts = {
            'Allrun.pre': header+'python3 system/channel_mesh.py\n',
            'Allrun.solve': header+'python3 system/channel_mesh.py --require-ready\nchtMultiRegionSimpleFoam\n',
            'Allrun.serial': header+'bash Allrun.pre\nbash Allrun.solve\n',
            'Allrun.parallel': header+'bash Allrun.pre\n'
                               f'decomposePar -allRegions\nmpirun -np {count} chtMultiRegionSimpleFoam -parallel\n'
                               'reconstructPar -allRegions\n',
        }
        if count == 1:
            scripts['Allrun.parallel'] = scripts['Allrun.serial']
    else:
        runtime = Path(__file__).with_name('flotherm_conduction_gate.py')
        (output/'system'/runtime.name).write_bytes(runtime.read_bytes())
        scripts = {
            'Allrun.pre':header+CONDUCTION_MESH+'\n',
            'Allrun.solve':header+CONDUCTION_MESH+' --require-ready\nlaplacianFoam\n',
            'Allrun.serial':header+'bash Allrun.pre\nbash Allrun.solve\n',
        }
        scripts['Allrun.parallel'] = scripts['Allrun.serial'] if count==1 else (
            header+'bash Allrun.pre\n'
            f'decomposePar -time 0\nmpirun -np {count} laplacianFoam -parallel\nreconstructPar\n')
    if latest:
        source = Path(__file__).with_name('flotherm_restart_runtime.py')
        (output/'system/flotherm_restart.py').write_bytes(source.read_bytes())
        app = manifest['application']
        regions = ' -allRegions' if cht else ''
        mesh_steps = scripts['Allrun.pre'].removeprefix(header)
        scripts['Allrun.pre'] = header+'python3 system/flotherm_restart.py before-mesh\n'+mesh_steps
        scripts['Allrun.solve'] = header+f'python3 system/flotherm_restart.py serial\n{app}\n'
        scripts['Allrun.serial'] = header+'bash Allrun.solve\n'
        scripts['Allrun.parallel'] = scripts['Allrun.serial'] if count==1 else (
            header+'python3 system/flotherm_restart.py parallel\n'
            f'mpirun -np {count} {app} -parallel\nreconstructPar{regions} -newTimes\n')
    if cht:
        from .flotherm_flow_devices import FIXED_LIBRARY, FIXED_MODEL, FIXED_SOURCE_DIR
        from .flotherm_two_resistance import LIBRARY, LIBRARY_DIR, SOURCE_DIR

        two_r = manifest.get('two_resistance_networks')
        fixed = any(row['model'] == FIXED_MODEL for row in manifest.get('flow_devices', ()))
        if (two_r or fixed) and not manifest.get('required_overrides'):
            builds = []
            if two_r:
                builds.append((SOURCE_DIR, LIBRARY))
            if fixed:
                builds.append((FIXED_SOURCE_DIR, FIXED_LIBRARY))
            scripts['Allrun.build'] = ('cd "$(dirname "$0")"\n'
                f'mkdir -p -- {LIBRARY_DIR}\n'
                f'export FOAM_USER_LIBBIN="$PWD/{LIBRARY_DIR}"\n'+''.join(
                    f'wmake libso {directory}\ntest -s "$FOAM_USER_LIBBIN/{library}"\n'
                    for directory, library in builds))
            scripts['Allrun.pre'] = scripts['Allrun.pre'].replace('python3 system/channel_mesh.py',
                                                                 'bash Allrun.build\npython3 system/channel_mesh.py')
            if latest:
                for script in ('Allrun.solve','Allrun.parallel'):
                    scripts[script] = scripts[script].replace('python3 system/flotherm_restart.py ',
                                                               'bash Allrun.build\npython3 system/flotherm_restart.py ')
            elif not two_r:
                scripts['Allrun.solve'] = scripts['Allrun.solve'].replace(
                    'chtMultiRegionSimpleFoam\n', 'bash Allrun.build\nchtMultiRegionSimpleFoam\n')
        if two_r and not manifest.get('required_overrides'):
            for script in ('Allrun.solve','Allrun.parallel'):
                if script == 'Allrun.parallel' and count == 1:
                    continue
                # Keep the native branch rows compact even when solver stdout is long.
                command = 'chtMultiRegionSimpleFoam' if script == 'Allrun.solve' else (
                    f'mpirun -np {count} chtMultiRegionSimpleFoam -parallel')
                build = 'bash Allrun.build\n' if script == 'Allrun.solve' and not latest else ''
                log = 'touch log.twoResistance\n' if latest else ': > log.twoResistance\n'
                redirect = '>>' if latest else '>'
                steps = (build+log+command+
                         ' 2>&1 | awk \'{ print; if (index($0,"twoResistanceNetworkSource device=")==1) '
                         f'{{ print {redirect} "log.twoResistance"; fflush("log.twoResistance"); }} }}\'')
                scripts[script] = scripts[script].replace(command,steps,1)
    for name, body in scripts.items():
        change_directory, commands=body.split('\n',1)
        (output / name).write_text("#!/bin/bash\nset -euo pipefail\n" + change_directory+'\n'+RUN_LOCK_SCRIPT+commands,
                                   encoding="utf-8", newline="\n")
    runtime = Path(__file__).with_name('flotherm_clean_runtime.py')
    (output/'system/flotherm_clean.py').write_bytes(runtime.read_bytes())
    clean = '#!/bin/bash\nset -euo pipefail\n'+header+RUN_LOCK_SCRIPT+'python3 system/flotherm_clean.py\n'
    (output / "Allclean").write_text(clean, encoding="utf-8", newline="\n")


def record_mesh_gate(output: Path, ok: bool, evidence: dict):
    (output / "mesh_gate.json").write_text(json.dumps({"ok": ok, "evidence": evidence}, indent=2) + "\n", encoding="utf-8")
    files = [path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file() and path.name != MARKER]
    write_manifest(output, files)
