"""Read published, native NIfTI and Analyze volumes back into ParaView."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode


SPACING = {'anisotropic':(.75,1.5,2.0), 'isotropic':(.75,.75,.75)}


def create_volumes(root):
    from vtkmodules.vtkCommonCore import vtkShortArray
    from vtkmodules.vtkCommonDataModel import vtkImageData
    from vtkmodules.vtkIOXML import vtkXMLImageDataWriter

    for name, spacing in SPACING.items():
        data = vtkImageData()
        data.SetDimensions(4,4,4)
        data.SetSpacing(*spacing)
        scalars = vtkShortArray()
        scalars.SetName('Intensity')
        for index in range(64):
            scalars.InsertNextValue(index)
        data.GetPointData().SetScalars(scalars)
        writer = vtkXMLImageDataWriter()
        writer.SetInputData(data)
        writer.SetFileName(str(root/f'{name}.vti'))
        assert writer.Write()


def inspect_volume(path, kind):
    from paraview import servermanager as sm, simple

    simple.LoadDistributedPlugin('AnalyzeNIfTIReaderWriter',remote=False)
    reader = sm._getPyProxy(sm.ProxyManager().NewProxy('sources',kind+'Reader'))
    reader.SetPropertyWithName('FileName',str(path.resolve(strict=True)))
    reader.UpdatePipeline()
    data = sm.Fetch(reader)
    values = data.GetPointData().GetScalars()
    assert data.GetDimensions() == (4,4,4)
    expected = SPACING['isotropic' if kind=='Analyze' else 'anisotropic']
    assert data.GetSpacing() == expected,data.GetSpacing()
    assert [values.GetTuple1(index) for index in range(64)] == list(range(64))
    return {'dimensions':list(data.GetDimensions()),'spacing':list(data.GetSpacing()),'scalars':64,
            'range':list(data.GetScalarRange())}


def finished(api, job_id):
    until = time.monotonic()+100
    while time.monotonic()<until:
        job = api.call('GET',f'/jobs/{job_id}')
        if job['ended_at'] and not job['completion_pending']:
            return job
        time.sleep(.15)
    raise TimeoutError('Volume export did not settle')


def acceptance(native):
    import httpx
    from testsupport import IsolatedServer

    api = IsolatedServer('ff-d20-native-volumes')
    print('VOLUME_EVIDENCE '+str(api.root),flush=True)
    receipt = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0,'exports':{}}
    flags = [] if sys.platform=='win32' else ['--no-mpi']
    subprocess.run([str(native),*flags,__file__,'--make',str(api.root)],check=True,timeout=45)
    try:
        with api, httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=45) as client:
            api.create_case('Native volume round-trip')
            client.headers['Authorization'] = 'Bearer '+api.token
            for name in SPACING:
                filename = f'{name}.vti'
                data = (api.root/filename).read_bytes()
                response = client.post(api.prefix+'/upload',params={'path':'D-post/'+filename},
                                       files={'file':(filename,data,'application/octet-stream')})
                response.raise_for_status()
                assert response.json()['content_sha256'] == hashlib.sha256(data).hexdigest()

            def submit(kind, name):
                return api.call('POST',api.prefix+'/paraview/pipeline',{
                    'nodes':[{'id':'input','group':'file','file':f'D-post/{name}.vti'}],
                    'plugins':['AnalyzeNIfTIReaderWriter'],'volume_format':kind})

            artifacts = {}
            for kind, name in (('NIfTI','anisotropic'),('Analyze','isotropic')):
                result = submit(kind,name)
                job = finished(api,result['job_id'])
                assert job['status'] == 'succeeded',job['log_backlog'][-1600:]
                raw = lambda path: api.prefix+'/raw?'+urlencode({'path':path})
                report = api.call('GET',raw(result['report_path']))
                assert report['volume']['spacing'] == list(SPACING[name])
                folder = api.root/kind
                folder.mkdir()
                digests = {}
                for path in report['volume']['files']:
                    data = api.call('GET',raw(path),raw=True)
                    file = PurePosixPath(path).name
                    digest = hashlib.sha256(data).hexdigest()
                    assert digest == job['attempt']['outputs']['files'][path]['sha256']
                    (folder/file).write_bytes(data)
                    artifacts[path] = digest
                    digests[file] = digest
                header = folder/('volume.nii' if kind=='NIfTI' else 'volume.hdr')
                measurement = folder/'native-read.json'
                subprocess.run([str(native),*flags,__file__,'--inspect',str(header),kind,str(measurement)],check=True,timeout=45)
                receipt['exports'][kind] = {'job_id':result['job_id'],'files':digests,'native':json.loads(measurement.read_bytes())}

            failure = finished(api,submit('Analyze','anisotropic')['job_id'])
            assert failure['status'] == 'failed' and 'unequal voxel spacing' in failure['log_backlog']
            for relative, digest in artifacts.items():
                assert hashlib.sha256(api.call('GET',raw(relative),raw=True)).hexdigest() == digest
            receipt.update(passed=True,nonuniform_analyze_rejected=True,published_artifacts_retained=True)
    finally:
        (api.root/'native-volumes.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native',type=Path)
    parser.add_argument('--make',type=Path)
    parser.add_argument('--inspect',nargs=3)
    args = parser.parse_args()
    if args.make:
        create_volumes(args.make)
    elif args.inspect:
        path,kind,output = args.inspect
        Path(output).write_text(json.dumps(inspect_volume(Path(path),kind)),encoding='utf8')
    elif args.native:
        acceptance(args.native.resolve(strict=True))
    else:
        parser.error('Select the real ParaView executable with --native')
