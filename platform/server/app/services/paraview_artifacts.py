"""Whole-case Python dependencies and file deltas from an owned ParaView run."""
import json

from . import attempt_workspace as workspace
from .case_file_delta import snapshot,changes,current_source,file_records

CONFIG='.vibeflow/prepared/paraview.json'
PIPELINE='.vibeflow/prepared/paraview-pipeline.py'
RECEIPT='.vibeflow/paraview/receipt.json'
LOG='.vibeflow/paraview/native.log'


def script_path(value):
    value=workspace.canonical_path(value)
    if value.split('/')[0].casefold()=='.vibeflow' or not value.lower().endswith('.py'):
        raise ValueError('ParaView requires a user .py script')
    return value


def expected_script(config):
    path = config['script']
    if path == PIPELINE:
        return config['pipeline']['script']
    source = config['source']['files'][script_path(path)]
    return {key: source[key] for key in ('size', 'sha256')}


def verify_pipeline(run, config, records, deleted):
    pipeline = config['pipeline']
    folder = pipeline['folder']
    report_path = f'{folder}/pipeline.json'
    state = f'{folder}/pipeline.pvsm'
    if report_path not in records or state not in records or deleted:
        raise ValueError('ParaView pipeline did not return its dataset and state')
    report = json.loads(workspace.checked_path(run, report_path).read_bytes())
    result = report.get('result')
    if (report.get('schema') != 'vibeflow.paraview.pipeline.v1' or report.get('recipe') != pipeline['nodes']
            or report.get('state') != state or not isinstance(result, str) or result not in records
            or not result.startswith(f'{folder}/result.') or
            any(not path.startswith(folder+'/') or path in config['source']['files'] for path in records)):
        raise ValueError('ParaView pipeline output does not match its frozen recipe')
    plugins = pipeline.get('plugins')
    if plugins is not None:
        measured = report.get('plugins',[])
        if ([item.get('name') for item in measured] != plugins or not all(item.get('loaded') is True for item in measured)):
            raise ValueError('ParaView pipeline did not load the frozen plugin list')
    if 'chart' in report:
        image = f'{folder}/bag-plot.png'
        if pipeline['nodes'][-1].get('name') != 'ExtractBagPlots' or report['chart'] != image or image not in records:
            raise ValueError('ParaView chart differs from the frozen pipeline')
        with workspace.checked_path(run,image).open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                raise ValueError('ParaView Bag Plot did not produce a native PNG')
    if pipeline.get('volume_format'):
        kind = pipeline['volume_format']
        suffixes = ('.img','.hdr') if kind == 'Analyze' else ('.nii',)
        paths = [f'{folder}/volume{suffix}' for suffix in suffixes]
        volume = report.get('volume')
        if (not isinstance(volume,dict) or volume.get('format') != kind or volume.get('files') != paths
                or any(path not in records or not records[path]['size'] for path in paths)):
            raise ValueError('ParaView volume differs from the frozen export format')
        with workspace.checked_path(run,paths[-1]).open('rb') as stream:
            if stream.read(4) not in (b'\x5c\x01\x00\x00',b'\x00\x00\x01\x5c'):
                raise ValueError('ParaView did not produce a valid volume header')
    animation = pipeline.get('animation')
    if animation is not None:
        measured = report.get('animation')
        frames = measured.get('frames', []) if isinstance(measured,dict) else []
        streaming = animation.get('streaming')
        times = [index/animation['fps'] for index in range(len(frames))] if streaming else animation['times']
        if (not isinstance(measured,dict) or measured.get('fps') != animation['fps'] or len(frames) != len(times)
                or measured.get('tracks') != animation['tracks'] or measured.get('camera') != animation['camera']
                or measured.get('flipbook') != animation.get('flipbook') or measured.get('streaming') != streaming
                or streaming and not 1 <= len(frames) <= streaming):
            raise ValueError('ParaView animation frame count does not match the frozen timeline')
        received_blocks = set()
        points = 0
        for index, (frame, moment) in enumerate(zip(frames,times)):
            path = f'{folder}/frames/frame-{index:04d}.png'
            if frame.get('path') != path or frame.get('t') != moment or path not in records:
                raise ValueError('ParaView animation frame does not match the frozen timeline')
            if animation.get('flipbook') and frame.get('node') != animation['flipbook'][index]:
                raise ValueError('ParaView Flip Book did not render the selected node')
            if streaming:
                blocks = frame.get('blocks')
                if (not isinstance(blocks,list) or not blocks or any(not isinstance(part,list) or len(part)!=2
                        or any(type(value) is not int or value < 0 for value in part) for part in blocks)
                        or type(frame.get('points')) is not int or frame['points'] <= points):
                    raise ValueError('ParaView particle frame lacks new native data')
                keys = {tuple(part) for part in blocks}
                if len(keys) != len(blocks) or received_blocks & keys:
                    raise ValueError('ParaView particle stream contains a duplicate block')
                received_blocks.update(keys)
                points = frame['points']
            with workspace.checked_path(run,path).open('rb') as stream:
                if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                    raise ValueError('ParaView animation did not produce a native PNG')
    if pipeline.get('extractors'):
        verify_extractors(folder, pipeline['extractors'], report.get('extractors'), records)
    if pipeline.get('inspection'):
        settings = pipeline['inspection']
        inspection = report.get('inspection')
        image = f'{folder}/inspection.png'
        if (not isinstance(inspection,dict) or inspection.get('settings') != settings or inspection.get('image') != image
                or image not in records):
            raise ValueError('ParaView inspection differs from its frozen configuration')
        columns, rows = settings['comparison']['dimensions'] if settings['comparison'] else (1,1)
        if len(inspection.get('tiles',[])) != columns*rows or not inspection.get('memory',{}).get('process_kib',0) > 0:
            raise ValueError('ParaView inspection is missing its tiles or native memory reading')
        if settings.get('representation'):
            representation = settings['representation']
            if inspection.get('representation') != {key:representation[key] for key in ('mode','array')}:
                raise ValueError('ParaView did not render the selected native representation')
        if settings.get('projection') and inspection.get('projection') != settings['projection']:
            raise ValueError('ParaView did not render the selected panoramic projection')
        with workspace.checked_path(run,image).open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                raise ValueError('ParaView inspection did not produce a native PNG')
        if settings.get('raytrace'):
            ray = inspection.get('raytrace')
            path = f'{folder}/materials.json'
            if (not isinstance(ray,dict) or ray.get('name') != settings['raytrace']['name']
                    or ray.get('backend') != 'OSPRay pathtracer' or ray.get('library') != path or path not in records):
                raise ValueError('ParaView did not render and publish the selected OSPRay material')
            expected_library = {'family':'OSPRay','version':'0.0','materials':settings['raytrace']['materials'],
                                'vibeflow_texture_base':'case'}
            if json.loads(workspace.checked_path(run,path).read_bytes()) != expected_library:
                raise ValueError('OSPRay output library differs from its frozen material definitions')


def verify_extractors(folder, rules, extracted, records):
    if not isinstance(extracted,list) or len(extracted) != len(rules):
        raise ValueError('ParaView did not return all frozen extractor rules')
    for rule, result in zip(rules,extracted):
        if not isinstance(result,dict):
            raise ValueError('Invalid ParaView extractor result')
        prefix = f'{folder}/extracts/{rule["id"]}/'
        files = {path for path in records if path.startswith(prefix)}
        frames = result.get('frames',[])
        kind = result.get('native_format','')
        vtk_formats = {'VTP','VTU','VTI','VTR','VTS','VTM','VTT'}
        allowed = vtk_formats if rule['format'] == 'VTK' else {rule['format']}
        if result.get('rule') != rule or kind not in allowed or not frames or files != set(result.get('files',[])):
            raise ValueError('ParaView extractor does not match its frozen configuration')
        if rule['end'] is not None and len(frames) != 1 + (rule['end']-rule['start'])//rule['every']:
            raise ValueError('ParaView extract is missing selected steps')
        suffix = ('p' if kind in vtk_formats-{'VTM','VTT'} else '')+kind.lower()
        for index, frame in enumerate(frames):
            step = rule['start']+index*rule['every']
            path = f'{prefix}step_{step:06d}.{suffix}'
            if (frame.get('step') != step or frame.get('path') != path or path not in files
                    or (rule['end'] is not None and step > rule['end'])):
                raise ValueError('ParaView did not publish the selected extractor step')


def catalyst_result(config, records, deleted):
    catalyst = config['catalyst']
    folder = catalyst['folder']
    if (not records or deleted or any(not path.startswith(folder+'/') or path in config['source']['files']
                                      for path in records)):
        raise ValueError('Catalyst must generate extracts in its new output folder and preserve case inputs')
    return {'schema':'vibeflow.paraview.catalyst.v1','version':catalyst['version'],
            'driver':config['script'],'state':catalyst['state_path'],
            'files':[{'path':path,**records[path]} for path in sorted(records)]}


def artifacts(run,descriptor,output,frozen):
    from .postprocess_artifacts import context
    identity=context(run)
    config=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config!=descriptor['configuration']:raise ValueError('ParaView private configuration changed')
    receipt=json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))
    source=descriptor['source'];script=descriptor['script']
    if workspace.digest_file(workspace.checked_path(run,script))!=expected_script(config):
        raise ValueError('ParaView modified its executing script')
    user_files={p:r for p,r in output['files'].items() if p.split('/')[0].casefold()!='.vibeflow'}
    observed=snapshot(run)
    if observed['files']!=user_files:raise ValueError('ParaView files changed after capture')
    records={p:user_files[p] for p in receipt['outputs']}
    deleted={p:r for p,r in source['files'].items() if p not in user_files}
    # Every changed/new file must be described, and every omitted file must
    # still have its sealed bytes. Unchanged rewrites are bound by the worker.
    for path,record in user_files.items():
        if path not in records and source['files'].get(path)!=record:
            raise ValueError('ParaView receipt omitted a changed file')
    staged = {CONFIG}
    if script == PIPELINE:
        verify_pipeline(run, config, records, deleted)
        generated = frozen[PIPELINE]
        if {key: generated[key] for key in ('size', 'sha256')} != expected_script(config):
            raise ValueError('ParaView pipeline script differed from its frozen input')
        staged.add(PIPELINE)
    if config.get('catalyst'):
        path = config['catalyst']['folder']+'/catalyst.json'
        if path not in records:
            raise ValueError('Catalyst invocation did not publish its extract manifest')
        expected = catalyst_result(config,{key:record for key,record in records.items() if key != path},deleted)
        if json.loads(workspace.checked_path(run,path).read_bytes()) != expected:
            raise ValueError('Catalyst manifest differs from its sealed invocation and extract files')
    frozen_user={p:r for p,r in frozen.items() if p not in staged}
    if frozen_user!=source['files']:raise ValueError('ParaView sealed selection differs from preparation')
    expected={'schema':'vibeflow.paraview-result.v1','case_id':descriptor['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],
        'execution_id':descriptor['execution_id'],'script':script,'backend':descriptor['backend'],'returncode':0,
        'executable_sha256':descriptor['executable_sha256'],'worker_sha256':descriptor['worker_sha256'],
        'inputs':source,'outputs':records,'deleted':deleted,'log':workspace.digest_file(workspace.checked_path(run,LOG))}
    if receipt!=expected:raise ValueError('ParaView has no matching native invocation receipt')
    descriptor['output_files']=records;descriptor['deleted_files']=deleted;descriptor['receipt']=receipt
    return records
