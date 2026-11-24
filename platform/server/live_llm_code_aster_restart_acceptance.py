"""Verify Code_Aster resumes a historical base through the public solve API."""
import asyncio
from functools import partial
import hashlib
from io import BytesIO
import json
import re
import subprocess
import uuid
from zipfile import ZipFile

from live_llm_code_aster_acceptance import FOLDER, H5DUMP, FIELD, PROBE, raw
from live_llm_job_repeat_acceptance import main, put
from s3testsupport import ObjectStore, SERVER


NATIVE_MEMBERS = {'glob.1','pick.code_aster.infos','pick.code_aster.objects','pick.code_aster.sha'}


def state_summary(data):
    with ZipFile(BytesIO(data)) as archive:
        assert set(archive.namelist())=={'base.json',*NATIVE_MEMBERS}
        record = json.loads(archive.read('base.json'))
        assert record['schema']=='vibeflow.code-aster-base.v1' and len(record['runtime_package_sha256'])==64
        size = sum(archive.getinfo(name).file_size for name in NATIVE_MEMBERS)
        assert 0<size<128*1024*1024
        return {'encoded_bytes':len(data),'native_bytes':size,'sha256':hashlib.sha256(data).hexdigest(),
                'runtime_package_sha256':record['runtime_package_sha256']}


def displacement(api, filename, dataset):
    path = api.root/filename
    path.write_bytes(raw(api,FOLDER+filename))
    command = [H5DUMP]
    native = str(path)
    if path.drive:
        command = ['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',H5DUMP]
        native = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    output = subprocess.run([*command,'-y','-m','%.17g','-d',dataset,native],capture_output=True,text=True,
                            check=True,encoding='utf8',timeout=40).stdout
    values = [float(item) for item in re.search(r'\bDATA\s*\{([^}]+)\}',output).group(1).split(',')]
    assert len(values)==6 and values[:1]+values[2:]==[0]*5
    return values


def study(api,name,restored=None):
    route = api.prefix+'/code-aster:solve'
    body = {'input_path':FOLDER+name,**(restored or {})}
    headers = {'Idempotency-Key':uuid.uuid4().hex}
    identifier = api.call('POST',route,body,extra_headers=headers)['job_id']
    attempt = api.wait_job(identifier,timeout=160)['attempt']
    child, = attempt['child_processes']['children'].values()
    assert attempt['handler']['key']=='code-aster.solve' and attempt['process_stop']['verified']
    assert child['purpose']=='code_aster_solve' and child['stop']['verified']
    assert child['identity']['native']['process']['uid']==65534
    assert attempt['outputs']['publication']['status']=='committed'
    assert api.call('POST',route,body,extra_headers=headers)['job_id']==identifier
    return identifier,attempt


def scenario(api, *, store=None, **_):
    fixture = SERVER/'examples/code_aster/axial'
    export = (fixture/'axial.export').read_text(encoding='ascii')
    deck = PROBE+(fixture/'axial.comm').read_text(encoding='ascii')
    assert put(api,FOLDER+'axial.mail',(fixture/'axial.mail').read_text(encoding='ascii'))['ok']
    continuous_export = export.replace('axial.comm','continuous.comm').replace('axial.rmed','continuous.rmed').replace('axial.mess','continuous.mess')
    assert put(api,FOLDER+'continuous.export',continuous_export)['ok']
    assert put(api,FOLDER+'continuous.comm',deck.replace('FX=2.1e6','FX=4.2e6'))['ok']
    full,_ = study(api,'continuous.export')
    direct = displacement(api,'continuous.rmed',FIELD)
    assert abs(direct[1]-.002)<1e-12

    assert put(api,FOLDER+'axial.export',export+'\nR base state R 0\n')['ok']
    assert put(api,FOLDER+'axial.comm',deck)['ok']
    base,original = study(api,'axial.export')
    archive = FOLDER+'state_base.zip'
    original_state = state_summary(raw(api,archive))
    original_pub = original['outputs']['publication']['id']
    assert put(api,FOLDER+'axial.comm',deck.replace('E=210e9','E=105e9'))['ok']
    latest,newer = study(api,'axial.export')
    assert newer['outputs']['publication']['id']!=original_pub
    latest_state = state_summary(raw(api,archive))
    assert latest_state['sha256']!=original_state['sha256']
    assert abs(displacement(api,'axial.rmed',FIELD)[1]-.002)<1e-12

    assert put(api,FOLDER+'continue.export',(fixture/'continue.export').read_text(encoding='ascii'))['ok']
    assert put(api,FOLDER+'continue.comm',PROBE+(fixture/'continue.comm').read_text(encoding='ascii'))['ok']
    missing = api.call('POST',api.prefix+'/code-aster:solve',{'input_path':FOLDER+'continue.export'},expect=400)
    assert 'published base reference' in missing['detail']
    restart = {'restart':{'publication_id':original_pub,'checkpoint_path':archive}}
    resumed,attempt = study(api,'continue.export',restart)
    source, = attempt['workspace']['publications']
    assert source['publication_id']==original_pub and not source['require_current']
    assert attempt['workspace']['files'][archive]['sha256']==original_state['sha256']
    restored = displacement(api,'restored.rmed',FIELD.replace('solutionDEPL','resumed_DEPL'))
    assert restored==direct
    message = raw(api,FOLDER+'restored.mess').decode('utf8')
    assert re.search(r'^VIBEFLOW_CODE_ASTER_CONFINED=1$',message,re.M)
    assert 'VIBEFLOW_ASTER_RESTORED_TIP_M=' in message
    continued = state_summary(raw(api,FOLDER+'continued-state_base.zip'))
    assert continued['sha256'] not in {original_state['sha256'],latest_state['sha256']}
    assert state_summary(raw(api,archive))['sha256']==latest_state['sha256']
    result = {'passed':True,'storage':'s3' if store else 'fs','jobs':[full,base,latest,resumed],
              'original_publication':original_pub,'original_state':original_state,'newer_state':latest_state,
              'resumed_state':continued,'notes':{'live_alias_unchanged':True,'historical_state_selected':True,
              'missing_reference_rejected':True,'parent_child_stops_verified':True},
              'continuous_tip_m':direct[1],'restored_tip_m':restored[1],'full_field_error_m':max(abs(a-b) for a,b in zip(restored,direct,strict=True))}
    (api.root/'code-aster-restart.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


if __name__=='__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
