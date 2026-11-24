"""Actual registered validator preparation deaths on owned SQLite/PostgreSQL.

Runs only the explicitly reviewed real compiler/S3/SQL acceptance entry, with
at most three independent owned cases. Stops admitting more work after a real
failure, drains existing subprocesses, and preserves every failed report.
"""
import argparse,concurrent.futures,hashlib,json,os,subprocess,sys,tempfile,threading,time
from datetime import datetime,timezone
from pathlib import Path
from testsupport import SERVER
from app.services import process_identity_native as identity

BOUNDARIES={'report':('reserved','plan','guardian','created','host_ready'),
    'latex':('reserved','plan','prepared','allocated','guardian','admitted','created','host_ready','native_ready')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--review',required=True);parser.add_argument('--previous-matrix');args=parser.parse_args()
    workspace=SERVER.parents[1];review=Path(args.review).resolve(strict=True)
    assert review.is_relative_to(workspace/'tmp')
    reviewed=json.loads(review.read_text(encoding='utf8'))
    for name,expected in reviewed['source_hashes'].items():
        assert hashlib.sha256((SERVER/name).read_bytes()).hexdigest()==expected,name
    root=Path(tempfile.mkdtemp(prefix='validator-preparation-matrix-',dir=workspace/'tmp'))
    lock=threading.RLock();stop=threading.Event()
    report={'passed':False,'started_at':datetime.now(timezone.utc).isoformat(),'review':str(review.relative_to(workspace)),
        'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest(),'model_calls':0,'mock_llm_calls':0,
        'developer_database_modified':False,'target_cases':28,'results':{},'live_children':{}}
    def save():
        pending=root/'matrix-result.pending';pending.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8');pending.replace(root/'matrix-result.json')
    def key(handler,boundary,database):return handler+':'+boundary+':'+database
    if args.previous_matrix:
        previous_path=Path(args.previous_matrix).resolve(strict=True);assert previous_path.is_relative_to(workspace/'tmp')
        previous=json.loads(previous_path.read_text());prior_review=json.loads((workspace/previous['review']).read_text())
        assert hashlib.sha256((workspace/previous['review']).read_bytes()).hexdigest()==previous['review_sha256']
        for p,digest in prior_review['source_hashes'].items():
            if p not in {'attempt_professional_finalizing_acceptance.py','attempt_preparation_matrix_acceptance.py'}:
                assert hashlib.sha256((SERVER/p).read_bytes()).hexdigest()==digest,p
        targets={key(h,b,d) for h,bs in BOUNDARIES.items() for b in bs for d in ('sqlite','postgresql')}
        for name,record in previous['results'].items():
            if not record['passed']:continue
            assert name in targets
            path=(workspace/record['report']).resolve(strict=True);assert path.is_relative_to(workspace/'tmp')
            value=json.loads(path.read_text());handler,boundary,database=name.split(':')
            assert value['passed'] and value['preparation_boundary']==boundary and value['handler']==handler and value['database']==database
            assert value['model_calls']==value['mock_llm_calls']==0 and all(v['state']=='exited' for v in value['API_exit_probes'])
            for p,digest in value['source_hashes'].items():
                if p!='attempt_professional_finalizing_acceptance.py':assert hashlib.sha256((SERVER/p).read_bytes()).hexdigest()==digest,p
            report['results'][name]={'passed':True,'report':record['report'],'production_hashes_current':True,
                'historical_fixture_review':previous['review'],'historical_entry_sha256':value['source_hashes']['attempt_professional_finalizing_acceptance.py'],
                'reused_exact_current_sources':False}
        report['previous_matrix']=str(previous_path.relative_to(workspace))
    save();print('PREPARATION_MATRIX '+str(root)+' TARGET28 REUSED'+str(len(report['results'])),flush=True)
    cases=[(handler,boundary,database) for handler,boundaries in BOUNDARIES.items() for boundary in boundaries for database in ('sqlite','postgresql')
        if key(handler,boundary,database) not in report['results']]
    def run(case):
        handler,boundary,database=case;name=key(*case)
        with lock:
            if stop.is_set():return
        command=[sys.executable,'-X','utf8','-B','attempt_professional_finalizing_acceptance.py',
            '--handler',handler,'--terminal-boundary','before','--preparation-boundary',boundary]
        if database=='postgresql':command.append('--postgres')
        else:command.append('--trace-transactions')
        log=root/(name.replace(':','-')+'.log')
        with log.open('w',encoding='utf8') as stream:
            proc=subprocess.Popen(command,cwd=SERVER,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            birth=identity.observe(proc.pid)
            with lock:report['live_children'][name]={'pid':proc.pid,'birth':birth,'log':str(log.relative_to(workspace))};save()
            code=proc.wait()
        output=log.read_text(encoding='utf8')
        matches=[v.removeprefix('PROFESSIONAL_FINALIZING_RESULT ') for v in output.splitlines() if v.startswith('PROFESSIONAL_FINALIZING_RESULT ')]
        value={'passed':False,'exit_code':code,'log':str(log.relative_to(workspace)),'birth':birth,'exit_probe':identity.probe(birth)}
        if matches:
            path=Path(matches[-1]).resolve(strict=True);assert path.is_relative_to(workspace/'tmp')
            result=json.loads(path.read_text());value.update(report=str(path.relative_to(workspace)),passed=code==0 and result['passed'])
        if value['exit_probe']['state']!='exited':value['passed']=False
        with lock:
            del report['live_children'][name];report['results'][name]=value
            if not value['passed']:stop.set()
            save();done=sum(v['passed'] for v in report['results'].values())
        print('PREPARATION_MATRIX_CASE '+name+' '+('PASS' if value['passed'] else 'FAIL')+' '+str(done)+'/28',flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(run,case) for case in cases]
        for future in concurrent.futures.as_completed(futures):
            try:future.result()
            except BaseException as failure:
                stop.set()
                with lock:report.setdefault('orchestration_errors',[]).append({'type':type(failure).__name__});save()
    report['remaining_cases']=[key(*case) for case in cases if key(*case) not in report['results']]
    report['passed']=len(report['results'])==28 and all(v['passed'] for v in report['results'].values()) and not report['live_children']
    report['finished_at']=datetime.now(timezone.utc).isoformat();save()
    print('PREPARATION_MATRIX_RESULT '+str(root/'matrix-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
