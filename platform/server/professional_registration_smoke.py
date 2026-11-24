"""Professional HTTP conflicts retain their durable Job without releasing tools."""
import json
from io import BytesIO

from pypdf import PdfWriter
from testsupport import IsolatedServer
from udf_scope_smoke import CONTROLLED
from mpi_scope_smoke import inspect_native
from app.services import process_identity_native as identity


def main():
    api=IsolatedServer('professional-registration',app='controlled_professional:app')
    extra="\n@app.get('/api/v1/fixture/no-job')\nasync def no_job():\n    raise job_execution.ExecutionConflict('invalid_request','Fixture without a Job')\n"
    (api.root/'controlled_professional.py').write_text(CONTROLLED+extra,encoding='utf8')
    (api.root/'mode').write_text('bad_registration')
    checks=[];receipts=[]
    with api:
        for backend in ('calculix','su2','paraview','ppt','pdf','flotherm_convert','flotherm_library','udf_build','udf_integrate'):
            api.create_case('Registration '+backend)
            directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            if backend=='calculix':
                api.put('input.inp','*NODE\n1,0,0,0\n')
                endpoint,body='/calculix:solve',{'input_path':'input.inp'}
            elif backend=='su2':
                api.put('input.cfg','SOLVER= EULER\n')
                endpoint,body='/su2:solve',{'config_path':'input.cfg'}
            elif backend=='paraview':
                api.put('input.py',"from pathlib import Path\nPath('payload-entered').touch()\n")
                endpoint,body='/paraview:run',{'script_path':'input.py'}
            elif backend=='ppt':
                endpoint,body='/ppt:build',{'slides':[{'title':'Fixture','text':'Never released'}],'output':'output.pptx'}
            elif backend=='pdf':
                pdf=PdfWriter();pdf.add_blank_page(width=100,height=100)
                buffer=BytesIO();pdf.write(buffer)
                (directory/'input.pdf').write_bytes(buffer.getvalue())
                endpoint,body='/pdf:number',{'input':'input.pdf','output':'output.pdf'}
            elif backend=='flotherm_convert':
                api.put('ir.json','{}')
                endpoint,body='/flotherm:convert',{'ir_path':'ir.json','output_dir':'conversion-output'}
            else:
                api.call('POST',api.prefix+'/udf/scaffold',{'name':'fixture','objective':'Registration guard'})
                if backend=='flotherm_library':
                    endpoint,body='/flotherm:build-library',{'source_dir':'UDF/fixture','destination_dir':'UDF/fixture'}
                elif backend=='udf_build':
                    endpoint,body='/udf:build',{'name':'fixture'}
                else:
                    api.put('C-run/case-1/system/controlDict','application icoFoam;\n')
                    endpoint,body='/udf:integrate',{'name':'fixture','run_dir':'C-run/case-1'}
            response=api.call('POST',api.prefix+endpoint,body,expect=409)
            launch=json.loads((api.root/'launch.json').read_text())
            assert response['detail']=={'code':'evidence_invalid','message':'Process identity is invalid for this attempt',
                                        'job_id':launch['job_id']},response
            job=api.call('GET',f"/jobs/{launch['job_id']}")
            assert job['status']=='failed' and job['ended_at'] is not None
            assert job['attempt']['process_identity'] is None and job['attempt']['reason_code']=='process_start_failed'
            assert identity.probe(launch['identity']['host'])['state']=='exited'
            if launch['identity']['native'] is not None:
                assert inspect_native({'probe':[launch['identity']['native']['process']]})[0]['state']=='exited'
            assert not any((directory/name).exists() for name in (
                'payload-entered','output.pptx','output.pdf','conversion-output','input.frd','flow.vtu','UDF/fixture/libmyFunctionObject.so'))
            receipts.append({'backend':backend,'response':response,'job':job,'prepared_identity':launch['identity']})
            checks.append(backend+'_conflict_links_failed_job_before_payload_release')
        assert 'job_id' not in api.call('GET','/fixture/no-job',expect=409)['detail']
        checks.append('conflicts_without_job_do_not_invent_an_identifier')
        api.create_case('Unrelated user')
        for receipt in receipts:api.call('GET',f"/jobs/{receipt['job']['id']}",expect=404)
        checks.append('failure_identifier_does_not_grant_cross_user_history_access')
        (api.root/'registration-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_launch_gates':True,'professional_payloads_not_released':True}
    (api.root/'registration-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
