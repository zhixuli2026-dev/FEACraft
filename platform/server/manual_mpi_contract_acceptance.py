"""Run actual user-entered decomposition, MPI ranks and reconstruction as separate jobs."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request
import uuid

from cae_fixtures import cavity_2d
from testsupport import IsolatedServer, SERVER


RUN='C-run/case-1'
DECOMPOSE=RUN+'/system/decomposeParDict'
BASHRC='/usr/lib/openfoam/openfoam2406/etc/bashrc'
TIMES=('0.1','0.2','0.3','0.4','0.5')
DECOMPOSE_TEXT='''FoamFile
{
    version 2.0;
    format ascii;
    class dictionary;
    location "system";
    object decomposeParDict;
}
numberOfSubdomains 2;
method simple;
coeffs { n (2 1 1); }
'''
NUMBER=re.compile(rb'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def field(data,components):
    match=re.search(rb'internalField\s+nonuniform\s+List<\w+>\s+(\d+)\s*\((.*?)\)\s*;',data,re.S)
    assert match is not None
    count=int(match[1]);values=[float(token) for token in NUMBER.findall(match[2])]
    assert count==400 and len(values)==count*components and all(math.isfinite(v) for v in values)
    return values


def relative(reference,candidate):
    return max(abs(a-b) for a,b in zip(reference,candidate))/max(1.0,max(map(abs,reference)))


def original_serial(folder):
    report_path=folder/'icofoam-contract.json'
    report=json.loads(report_path.read_text(encoding='utf-8'))
    assert report['passed'] and report['isolated_api_exited']
    row=next(item for item in report['checks'] if item['name']==
        'repaired_original_cavity_publishes_five_verified_U_and_p_outputs_and_100_residual_steps')
    identifier=str(uuid.UUID(row['publication']))
    assert row['cells']==400
    bundles=list((folder/'files/workspaces').glob(f'*/cases/*/.vibeflow/publications/{identifier}/files'))
    assert len(bundles)==1
    published=bundles[0]
    paths=[RUN+'/0.5/'+name for name in ('U','p')]
    source={path:(published/path).read_bytes() for path in paths}
    assert all(digest(raw)==row['files'][path] for path,raw in source.items())
    return {name:field(source[path],components) for name,path,components in zip(('U','p'),paths,(3,1))}, {
        'serial_receipt':str(report_path),'serial_receipt_sha256':digest(report_path.read_bytes()),
        'serial_publication':identifier,'serial_fields':{path:digest(data) for path,data in source.items()}}


def mpi_command(count):
    # Same standard Open MPI settings as the installed v4 runtime. The
    # allocated names are private to this explicitly submitted command.
    return ('mpi_tmp=$(mktemp -d) && mkdir "$mpi_tmp/session" "$mpi_tmp/shared" && '
            'trap \'rmdir "$mpi_tmp/shared" "$mpi_tmp/session" "$mpi_tmp"\' EXIT && '
            'PMIX_MCA_gds=hash mpirun --noprefix --oversubscribe '
            '--mca orte_tmpdir_base "$mpi_tmp/session" --mca btl_vader_backing_directory "$mpi_tmp/shared" '
            f'-np {count} icoFoam -parallel')


class Acceptance:
    def __init__(self,api,serial,serial_evidence):
        self.api=api
        self.serial=serial
        self.serial_evidence=serial_evidence
        self.result=api.root/'manual-mpi-contract.json'
        self.checks=[]
        self.jobs={}

    def save(self,**status):
        self.result.write_text(json.dumps({'checks':self.checks,'jobs':self.jobs,**self.serial_evidence,
            'real_model_calls':0,'mock_llm_calls':0,**status},ensure_ascii=False,indent=2),encoding='utf-8')

    def record(self,name,**details):
        self.checks.append({'name':name,**details})
        self.save(passed=False)
        print('PASS '+name,flush=True)

    def raw(self,path,expect=200,headers=None):
        return self.api.call('GET',self.api.prefix+'/raw?'+urlencode({'path':path}),
            raw=True,expect=expect,extra_headers=headers)

    def command(self,name,kind,line,*,expect='succeeded'):
        api=self.api
        command=f'source {BASHRC} && cd {RUN} && printf "NATIVE_UID=%s\\n" "$(id -u)" && {line}'
        payload={'type':kind,'runtime':'wsl','command':command,'timeout_seconds':120,'max_log_bytes':2097152}
        request=Request(api.base+'/api/v1'+api.prefix+'/jobs',data=json.dumps(payload).encode(),method='POST',
            headers={'Authorization':'Bearer '+api.token,'Content-Type':'application/json','Idempotency-Key':uuid.uuid4().hex})
        started=time.monotonic()
        # This public endpoint waits for owned native launch before replying.
        # Keep the launch budget unchanged and preserve the measured HTTP cost.
        with api.opener.open(request,timeout=100) as response:
            assert response.status==200
            sent=json.load(response)
        admission_seconds=time.monotonic()-started
        job=api.wait_job(sent['id'],expected=expect)
        attempt=job['attempt']
        log=api.call('GET',f'/jobs/{job["id"]}/log')['chunk']
        (api.root/(name+'.log')).write_text(log,encoding='utf-8')
        assert attempt['handler']['key']=='command.'+kind and job['run_id'] is None,job
        stop=attempt['process_stop']
        assert stop and stop['verified'] and stop['native_state']=='exited' and stop['scope_empty'],job
        uid=re.search(r'(?m)^NATIVE_UID=(\d+)\s*$',log)
        assert uid and int(uid[1])>0,log
        publication=(attempt.get('outputs') or {}).get('publication') or {}
        if expect=='succeeded':
            assert (job['exit_code']==0 and publication.get('status')=='committed' and
                publication.get('kind','').startswith('case-command:')),job
        else:
            assert job['exit_code'] and publication.get('status')=='failed',job
        self.jobs[name]={'id':job['id'],'status':job['status'],'native_uid':int(uid[1]),
            'stop':stop,'publication':publication,'log_sha256':digest(log.encode()),
            'submission_seconds':admission_seconds}
        return job['id'],log

    def run(self):
        api=self.api
        api.create_case('User-entered OpenFOAM MPI')
        inputs={**cavity_2d(),DECOMPOSE:DECOMPOSE_TEXT}
        for path,data in inputs.items():
            api.put(path,data)
        owner=api.token
        outsider=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'parallel-outsider-123','name':'Different MPI owner'})['token']
        headers={'Authorization':'Bearer '+outsider}
        assert api.token==owner
        api.call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl','command':'decomposePar'},
            expect=404,extra_headers=headers)
        api.call('PUT',api.prefix+'/file',{'path':DECOMPOSE,'content':DECOMPOSE_TEXT},
            expect=404,extra_headers=headers)

        _,log=self.command('mesh','mesh','blockMesh && checkMesh -constant -allGeometry -allTopology')
        assert 'Exec   : blockMesh' in log and 'Mesh OK.' in log,log
        job,log=self.command('decompose','mesh','decomposePar')
        assert 'Exec   : decomposePar' in log and 'Processor 0' in log and 'Processor 1' in log,log
        base=[RUN+f'/processor{rank}/{path}' for rank in range(2) for path in ('constant/polyMesh/points','0/U','0/p')]
        original={path:digest(self.raw(path)) for path in base}
        self.raw(RUN+'/processor2/0/U',expect=404)
        self.raw(RUN+'/0.5/U',expect=404)
        self.record('manual_dictionary_and_decompose_create_two_processor_meshes',job=job,processor_inputs=original)

        failed,log=self.command('incorrect-ranks','solve',mpi_command(3),expect='failed')
        mismatch=r'Number of processor directories\s*=\s*2 is not equal to the number of ranks\s*=\s*3'
        assert 'FOAM FATAL' in log and re.search(mismatch,log) and 'MPI_ABORT was invoked' in log,log
        for path,sha in original.items():
            assert digest(self.raw(path))==sha
        for rank in range(2):
            self.raw(RUN+f'/processor{rank}/0.5/U',expect=404)
        self.raw(RUN+'/0.5/U',expect=404)
        self.record('real_wrong_rank_count_fails_without_publishing_partial_fields',job=failed)

        corrected,log=self.command('correct-ranks','solve',mpi_command(2))
        assert re.search(r'(?m)^nProcs\s*:\s*2\s*$',log) and 'Time = 0.5' in log,log
        for rank in range(2):
            assert self.raw(RUN+f'/processor{rank}/0.5/U')
        self.raw(RUN+'/0.5/U',expect=404)
        self.record('corrected_independent_mpirun_publishes_per_rank_fields',job=corrected)

        job,log=self.command('reconstruct','solve','reconstructPar -noZero')
        assert 'Exec   : reconstructPar -noZero' in log and 'Time = 0.5' in log,log
        fields={RUN+'/'+label+'/'+name:self.raw(RUN+'/'+label+'/'+name)
                for label in TIMES for name in ('U','p')}
        differences={name:relative(self.serial[name],field(fields[RUN+'/0.5/'+name],components))
                     for name,components in (('U',3),('p',1))}
        assert differences['U']<=1e-5,differences
        assert all(self.raw(path)==data.encode() for path,data in inputs.items())
        assert api.call('GET',f'/jobs/{failed}')['status']=='failed'
        self.record('manual_reconstruction_matches_serial_field_reference',job=job,times=TIMES,
            relative_differences=differences,fields={path:digest(raw) for path,raw in fields.items()})

        api.call('GET',f'/jobs/{job}/log',expect=404,extra_headers=headers)
        api.call('POST',f'/jobs/{failed}:cancel',expect=404,extra_headers=headers)
        self.raw(RUN+'/processor0/0.5/U',expect=404,headers=headers)
        self.raw(RUN+'/0.5/U',expect=404,headers=headers)
        self.record('separate_account_cannot_submit_reconfigure_cancel_or_read_manual_mpi')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--serial-evidence',type=Path,required=True)
    args=parser.parse_args()
    serial,evidence=original_serial(args.serial_evidence)
    server=IsolatedServer('manual-mpi-c06a')
    suite=Acceptance(server,serial,evidence)
    try:
        with server:
            suite.run()
        suite.save(passed=True,isolated_api_exited=server.proc.poll() is not None)
    except BaseException as error:
        suite.save(passed=False,error={'type':type(error).__name__,'message':str(error)})
        raise


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    main()
