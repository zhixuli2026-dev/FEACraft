"""Run the actual ParaView 5.11.2 tracking sample and read its published particle paths."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode


UPSTREAM = {
    'flow_0_0.vtu': '73c36fcdb5037215ff7e569b451820e10655d9e384b84c965b1651e6c54a6ade7b9bb9012b1771b658778659a16dbf3e14dbbfbdde5a62edd549af1ac4f06173',
    'seeds.vtp': 'e8a93628de761f918d4d339255755ef7fa9022d6e1e80a49d9d47c9a1771c696c00ae8032ba36a5c95a539f8483614a171d58e6d7f123f7a07820dacaecfa342',
    'surface_0_0.vtp': '69c355be86d1243101cda5577b6abc827a2a0567218787d4ad77e3e358a3169341fe3be53b5a6ec96df828a061d9ebb0b06a2014ca39b5f72bde616bb96c7750',
}


def inspect_paths(artifact):
    from math import isfinite
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

    reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(artifact))
    reader.Update()
    paths = reader.GetOutput()
    assert paths.GetNumberOfLines() == 100
    assert paths.GetNumberOfPoints() > 2000
    cell_data = paths.GetCellData()
    ids = cell_data.GetArray('SeedId')
    assert {int(ids.GetTuple1(i)) for i in range(100)} == set(range(100))
    time_data = paths.GetPointData().GetArray('IntegrationTime')
    velocity = paths.GetPointData().GetArray('ParticleVelocity')
    for index in range(paths.GetNumberOfLines()):
        coordinates = paths.GetCell(index).GetPointIds()
        times = [time_data.GetTuple1(coordinates.GetId(i)) for i in range(coordinates.GetNumberOfIds())]
        assert times[0] == 0 and all(isfinite(value) for value in times)
        assert all(first < second for first, second in zip(times, times[1:]))
    assert velocity.GetNumberOfComponents() == 3
    assert all(isfinite(value) for i in range(velocity.GetNumberOfTuples()) for value in velocity.GetTuple3(i))
    return {'points':paths.GetNumberOfPoints(),'paths':paths.GetNumberOfLines(),
            'integration_time':list(time_data.GetRange()),'bounds':list(paths.GetBounds())}


def acceptance(source, native):
    import httpx
    from testsupport import IsolatedServer

    api = IsolatedServer('ff-d20-lagrangian')
    print('LAGRANGIAN_EVIDENCE '+str(api.root),flush=True)
    receipt = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
    try:
        with api, httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=45) as client:
            api.create_case('Native Lagrangian particle paths')
            client.headers['Authorization'] = 'Bearer '+api.token
            nodes = []
            ports = {}
            for role, name in zip(('FlowInput','ParticleSeeds','Surface'),UPSTREAM):
                data = (source/name).read_bytes()
                assert hashlib.sha512(data).hexdigest() == UPSTREAM[name],name
                relative = 'D-post/'+name
                uploaded = client.post(api.prefix+'/upload',params={'path':relative},
                                       files={'file':(name,data,'application/octet-stream')})
                uploaded.raise_for_status()
                assert uploaded.json()['content_sha256'] == hashlib.sha256(data).hexdigest()
                nodes.append({'id':role,'group':'file','file':relative})
                ports[role] = [role]
            plugin = 'LagrangianParticleTracker'
            nodes.append({'id':'paths','group':'filters','name':plugin,'inputs':ports,'properties':{}})
            reply = api.call('POST',api.prefix+'/paraview/pipeline',{'nodes':nodes,'plugins':[plugin],'backend':'pvpython'})
            job_id = reply['job_id']
            deadline = time.monotonic()+150
            while time.monotonic()<deadline:
                job = api.call('GET',f'/jobs/{job_id}')
                if job['ended_at'] and not job['completion_pending']:
                    assert job['status'] == 'succeeded',job['log_backlog'][-2400:]
                    break
                time.sleep(.15)
            else:
                raise TimeoutError('Lagrangian tracker did not settle')
            raw = api.prefix+'/raw?'
            report = api.call('GET',raw+urlencode({'path':reply['report_path']}))
            assert report['plugins'][0]['name'] == plugin and report['plugins'][0]['loaded']
            output = api.root/'paths.vtp'
            output.write_bytes(api.call('GET',raw+urlencode({'path':report['result']}),raw=True))
            assert job['attempt']['outputs']['files'][report['result']]['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
            measured = api.root/'native-paths.json'
            flags = [] if sys.platform=='win32' else ['--no-mpi']
            subprocess.run([str(native),*flags,__file__,'--inspect',str(output),str(measured)],check=True,timeout=60)
            receipt.update(passed=True,job_id=job_id,report=report,native=json.loads(measured.read_bytes()),
                           fixture_sha512=UPSTREAM)
    finally:
        (api.root/'lagrangian.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect',nargs=2,type=Path)
    parser.add_argument('--native',type=Path)
    parser.add_argument('--source',type=Path)
    options = parser.parse_args()
    if options.inspect:
        artifact, output = options.inspect
        output.write_text(json.dumps(inspect_paths(artifact)),encoding='utf8')
    elif options.source and options.native:
        acceptance(options.source.resolve(strict=True),options.native.resolve(strict=True))
    else:
        parser.error('Select the SHA-verified upstream sample and installed ParaView')
