"""Compare published MOOSE checkpoint continuation with real uninterrupted diffusion."""
import asyncio
import csv
from functools import partial
import hashlib
from io import BytesIO
import json
import re
import subprocess
from urllib.parse import urlencode
import uuid
from zipfile import ZipFile

from live_llm_job_repeat_acceptance import main, put
from s3testsupport import ObjectStore, SERVER


FOLDER = 'C-run/moose/'
DUMP = '/opt/vibeflow-ff-c08/moose/bin/ncdump'


def raw(api,path):
    return api.call('GET',api.prefix+'/raw?'+urlencode({'path':path}),raw=True)


def fields(api,case):
    data = raw(api,FOLDER+case+'_out.e')
    path = api.root/(case+'.e')
    path.write_bytes(data)
    command = [DUMP]
    native = str(path)
    if path.drive:
        command = ['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',DUMP]
        native = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    text = subprocess.run([*command,'-p','17,17','-v','time_whole,coordx,vals_nod_var1',native],
                          check=True,capture_output=True,text=True,encoding='utf8',timeout=30).stdout.split('data:',1)[1]
    named = dict(re.findall(r'^\s*(time_whole|coordx|vals_nod_var1)\s*=\s*([^;]+);',text,re.M))
    times,coords,nodal = ([float(x) for x in named[name].split(',')] for name in ('time_whole','coordx','vals_nod_var1'))
    assert coords==[i/10 for i in range(11)] and len(nodal)==len(coords)*len(times)
    table = list(csv.DictReader(raw(api,FOLDER+case+'_out.csv').decode('ascii').splitlines()))
    assert [round(float(row['time']),12) for row in table]==[round(value,12) for value in times]
    return times,nodal,[float(row['midpoint']) for row in table]


def accepted(api, name, body=None):
    data = {'input_path':FOLDER+name+'.i',**(body or {})}
    headers = {'Idempotency-Key':uuid.uuid4().hex}
    route = api.prefix+'/moose:solve'
    job = api.call('POST',route,data,extra_headers=headers)['job_id']
    attempt = api.wait_job(job,timeout=180)['attempt']
    assert attempt['process_stop']['verified']
    child, = attempt['child_processes']['children'].values()
    assert child['purpose']=='moose_solve' and child['stop']['verified']
    assert child['identity']['native']['process']['uid']==65534
    publication = attempt['outputs']['publication']
    assert publication['status']=='committed'
    assert api.call('POST',route,data,extra_headers=headers)['job_id']==job
    return job,attempt,publication['id']


def scenario(api, *, store=None, **_):
    deck = (SERVER/'examples/moose/thermal/transient.i').read_text(encoding='ascii')
    assert put(api,FOLDER+'continuous.i',deck.replace('checkpoint = true','checkpoint = false'))['ok']
    full,_,_ = accepted(api,'continuous')
    assert put(api,FOLDER+'part.i',deck.replace('end_time = 0.4','end_time = 0.2'))['ok']
    first,first_attempt,checkpoint_publication = accepted(api,'part')
    archive_path = FOLDER+'part_checkpoint.zip'
    original = raw(api,archive_path)
    with ZipFile(BytesIO(original)) as bundle:
        assert bundle.testzip() is None
        assert len(bundle.namelist())==7 and json.loads(bundle.read('checkpoint.json'))['step']=='0002'
    assert put(api,FOLDER+'part.i',deck.replace('end_time = 0.4','end_time = 0.3'))['ok']
    newer,_,new_publication = accepted(api,'part')
    assert new_publication!=checkpoint_publication and raw(api,archive_path)!=original
    assert put(api,FOLDER+'continued.i',deck)['ok']
    restart = {'restart':{'publication_id':checkpoint_publication,'checkpoint_path':archive_path}}
    destination = {'input_path':FOLDER+'part.i',**restart}
    refusal = api.call('POST',api.prefix+'/moose:solve',destination,expect=400,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
    assert 'different paths' in refusal['detail']
    resumed,attempt,resumed_publication = accepted(api,'continued',restart)
    source, = attempt['workspace']['publications']
    sha = hashlib.sha256(original).hexdigest()
    assert source['publication_id']==checkpoint_publication and source['require_current'] is False
    assert attempt['workspace']['files'][archive_path]['sha256']==sha
    assert first_attempt['outputs']['publication']['id']==checkpoint_publication
    times_full,nodes_full,midpoints_full = fields(api,'continuous')
    times_resume,nodes_resume,midpoints_resume = fields(api,'continued')
    assert [round(t,12) for t in times_full]==[0,.1,.2,.3,.4]
    assert [round(t,12) for t in times_resume]==[.2,.3,.4]
    error = max(abs(a-b) for a,b in zip(nodes_full[-len(nodes_resume):],nodes_resume,strict=True))
    assert error<1e-10 and midpoints_resume==midpoints_full[-3:]
    record = {'passed':True,'storage':'s3' if store else 'fs','continuous_job':full,'checkpoint_job':first,
        'newer_checkpoint_job':newer,'resumed_job':resumed,'checkpoint_publication':checkpoint_publication,
        'resumed_publication':resumed_publication,'sealed_checkpoint_sha256':sha,'native_checkpoint_step':'0002',
        'superseded_checkpoint_used':True,'same_path_refused':True,'time_values':times_resume,
        'midpoints':midpoints_resume,'max_node_field_error':error,'stops_verified':True}
    (api.root/'moose-restart.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    return record


if __name__=='__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
