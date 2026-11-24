"""Actual pvpython cannot publish an overwritten or deleted platform timeline."""
import asyncio,json
from pathlib import Path
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_engineering_repeat_acceptance import case_root


def scenario(api,*,store=None,**_):
    checks=[]
    for mode,statement in (('overwrite',"history.write_bytes(history.read_bytes()+b'ACTUAL_NATIVE_TIMELINE_CHANGE\\n')"),
                           ('delete','history.unlink()'),('rewrite_same_bytes','history.write_bytes(history.read_bytes())')):
        api.create_case('Actual delta timeline '+mode)
        script="""import os
from pathlib import Path
from paraview.simple import Sphere,SaveData
root=Path(os.environ['VIBEFLOW_CASE_ROOT'])
surface=Sphere(ThetaResolution=8,PhiResolution=8);surface.UpdatePipeline()
SaveData(str(root/'D-post/protected-sphere.vtp'),proxy=surface)
history=root/'0-caseDict/case-events.jsonl'
"""+statement+"\nprint('ACTUAL_PV_NATIVE_TIMELINE_MUTATION',flush=True)\n"
        put(api,'D-post/timeline.py',script)
        before=api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
        source=write(api,'POST',api.prefix+'/paraview:run',{'script_path':'D-post/timeline.py'})['job_id']
        job=api.wait_job(source,expected='succeeded' if mode=='rewrite_same_bytes' else 'failed')
        assert job['attempt']['process_stop']['verified']
        if mode=='rewrite_same_bytes':
            publication=job['attempt']['outputs']['publication'];assert publication['status']=='committed'
        else:
            assert job['attempt']['outputs']['publication']['status']!='committed'
            assert 'Native task changed the preserved platform timeline' in job['log_backlog']
        root=case_root(api,job,store);work=root/job['attempt']['workspace']['run_path']
        log=(work/'.vibeflow/paraview/native.log').read_text(encoding='utf8')
        assert 'ACTUAL_PV_NATIVE_TIMELINE_MUTATION' in log
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
        reader=vtkXMLPolyDataReader();reader.SetFileName(str(work/'D-post/protected-sphere.vtp'));reader.Update()
        assert reader.GetOutput().GetNumberOfPoints()==50 and reader.GetOutput().GetNumberOfCells()==96
        after=api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
        assert after.startswith(before) and b'ACTUAL_NATIVE_TIMELINE_CHANGE' not in after
        assert all(json.loads(line)['case_id']==api.case_id for line in after.splitlines())
        if mode=='rewrite_same_bytes':
            manifest=json.loads((root/'.vibeflow/publications'/publication['id']/'manifest.json').read_text(encoding='utf8'))
            assert '0-caseDict/case-events.jsonl' not in manifest['files']
            assert 'D-post/protected-sphere.vtp' in manifest['files']
        else:api.call('GET',api.prefix+'/raw?path=D-post/protected-sphere.vtp',expect=404)
        checks.append({'mode':mode,'actual_pvpython_completed':True,'actual_generated_points':50,'actual_generated_cells':96,
            'private_timeline_change_detected':mode!='rewrite_same_bytes','publication_refused':mode!='rewrite_same_bytes',
            'live_timeline_prefix_preserved':True,'timeline_not_a_native_output':True,'job_id':source})
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'actual_native_jobs':3,'model_selects_operations':False}
    api.create_case('Independent delta timeline model readback')
    (api.root/'delta-timeline.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario))
