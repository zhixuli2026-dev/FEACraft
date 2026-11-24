"""Real registered engineering workflow timings; no model calls or replacements."""
import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]


def hardware():
    cpu = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        'Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress'],
        capture_output=True, text=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    memory = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        'Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | ConvertTo-Json -Compress'],
        capture_output=True, text=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    linux = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '--exec', 'python3', '-I', '-S', '-c',
        "import json,platform;from pathlib import Path;print(json.dumps({'kernel':platform.release(),'meminfo':Path('/proc/meminfo').read_text(),'loadavg':Path('/proc/loadavg').read_text()}))"],
        capture_output=True, text=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    return {'host':platform.platform(), 'CPU':json.loads(cpu.stdout), 'memory':json.loads(memory.stdout),
            'WSL':json.loads(linux.stdout), 'cpu_count':os.cpu_count(),
            'scope':'single local sequential workflow; no intentional concurrent load; cold chain then warm sequential reads'}


def main():
    sys.path.insert(0, str(ROOT/'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['fs','s3'], required=True)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--scenario', choices=['conduction','legacy_conduction'], default='conduction')
    args = parser.parse_args()
    assert 1 <= args.samples <= 100
    from registered_cae_benchmarks import run_benchmark
    store = None
    api_holder = []
    timings = []
    host = hardware()

    def factory(label):
        api = IsolatedServer('performance-'+args.backend+'-'+label,
                             app='performance_fixture:app' if store else 'app.main:app')
        api_holder.append(api)
        if store:
            (api.root/'performance_fixture.py').write_text(
                'import os\nos.environ.update('+repr(store.settings())+')\nfrom app.main import app\n',encoding='utf8')
        actual_open = api.opener.open
        def diagnostic_wait(request,*a,**kw):
            # Keep slow-request evidence instead of mistaking a 120 s client
            # timeout for acceptance: the original 30 s target is reported below.
            kw['timeout'] = 120
            return actual_open(request,*a,**kw)
        api.opener.open = diagnostic_wait
        actual_call = api.call
        def measured(method,path,body=None,**kw):
            operation = method+' '+path.split('?')[0]
            if path.endswith('/caseDict:advance'):
                operation += ' '+body['state']
            started = time.perf_counter()
            row = {'operation':operation, 'success':False}
            try:
                result = actual_call(method,path,body,**kw)
                row['success'] = True
                return result
            except BaseException as error:
                row['error_type'] = type(error).__name__
                raise
            finally:
                row['seconds'] = time.perf_counter()-started
                timings.append(row)
                with (api.root/'performance-HTTP.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps(row)+'\n')
        api.call = measured
        return api

    def measure(api, flow, report):
        paths = ['/workflow-runs/'+flow['id'], api.prefix+'/jobs', api.prefix+'/report/results']
        samples = {}
        for path in paths:
            values = []
            for _ in range(args.samples):
                start = time.perf_counter()
                api.call('GET',path)
                values.append(time.perf_counter()-start)
            samples[path] = distribution(values)
        report['performance_read_samples'] = samples

    result = {'passed':False, 'backend':args.backend, 'scenario':args.scenario, 'hardware':host,
              'real_model_calls':0, 'mock_llm_calls':0, 'client_diagnostic_timeout_seconds':120,
              'original_request_limit_seconds':30, 'read_samples_per_endpoint':args.samples,
              'scope':'engineering response only; cancellation/recovery/fairness measured separately'}
    try:
        if args.backend == 's3':
            from s3testsupport import ObjectStore
            store = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed')
            store.__enter__()
            result['object_store_evidence'] = str(store.root)
        result['benchmark'] = run_benchmark(args.scenario,api_factory=factory,measure_completed=measure)
        result['functional_passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        if api_holder and (api_holder[0].root/'benchmark.json').exists():
            result['benchmark'] = json.loads((api_holder[0].root/'benchmark.json').read_text(encoding='utf8'))
    finally:
        if store:
            try:
                store.__exit__(None,None,None)
                result['store_host_exit_verified'] = store.proc is not None and store.proc.poll() is not None
                owner_probe = """import json,sys;from pathlib import Path
r=Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=Path('/proc')/str(o['pid'])
same=p.exists() and (p/'stat').read_text().rsplit(')',1)[1].split()[19]==o['start'] and p.stat().st_uid==o['uid']
assert not same,'Original object-store process is still present'
print(json.dumps({'original_native_owner_exited':True}))
"""
                probe = subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',owner_probe,store.linux],
                    check=True,capture_output=True,text=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
                result['store_native_exit'] = json.loads(probe.stdout)
            except BaseException as error:
                result['store_cleanup_failure'] = type(error).__name__+': '+str(error)[:500]
        grouped = defaultdict(list)
        for row in timings:
            grouped[row['operation']].append(row['seconds'])
        result['HTTP_distributions'] = {name:distribution(values) for name,values in grouped.items()}
        stage = [row for row in timings if '/caseDict:advance ' in row['operation']]
        result['stage_confirmation_requests'] = stage
        result['all_stage_requests_below_original_30_seconds'] = len(stage)==4 and all(r['success'] and r['seconds']<30 for r in stage)
        result['passed'] = bool(result.get('functional_passed') and not result.get('store_cleanup_failure'))
        result['performance_target_met'] = bool(result['passed'] and result['all_stage_requests_below_original_30_seconds'])
        result['statistics_note'] = 'Nearest-rank p95. Each stage has n=1: exploratory observation, not a stable production SLA. Read repetitions are warm sequential requests.'
        destination = api_holder[0].root if api_holder else store.root if store else ROOT/'tmp'
        name = destination/'performance-result.json'
        name.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('CAE_PERFORMANCE_RESULT '+str(name),flush=True)
    return 0 if result['passed'] else 1


def distribution(values):
    ordered = sorted(values)
    return {'n':len(ordered), 'min_seconds':ordered[0], 'median_seconds':ordered[(len(ordered)-1)//2],
            'p95_seconds':ordered[math.ceil(.95*len(ordered))-1], 'max_seconds':ordered[-1]}


if __name__ == '__main__':
    raise SystemExit(main())
