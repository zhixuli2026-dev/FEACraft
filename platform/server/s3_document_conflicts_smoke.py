"""Real remote source/destination changes and cancellation fence document publication."""
import json

from app.services.casedict import set_block
from job_release_smoke import wait_file
from pdf_publication_smoke import pdf
from ppt_publication_smoke import PNG
from professional_launch_smoke import settled
from s3_core_workflow_smoke import workflow_api
from s3_document_workflows_smoke import new_case,raw,seed
from s3_typed_publication_smoke import RUNTIME
from s3testsupport import ObjectStore


def main():
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        with workflow_api(store) as api:
            for kind in ('compose','pdf','ppt'):
                for mode in ('source','destination','cancel'):
                    case=new_case(api,'Remote '+kind+' '+mode)
                    for marker in ('ready','release'):(api.root/marker).unlink(missing_ok=True)
                    target='E-report/report.md' if kind=='compose' else 'E-report/output.'+('pdf' if kind=='pdf' else 'pptx')
                    previous=b'Original editable draft' if kind=='compose' else None
                    if previous is not None:seed(store,case,target,previous)
                    if kind=='compose':
                        endpoint='/report:compose?wait=false';body={'overwrite':True}
                        source='0-caseDict/caseDict'
                        changed=set_block(raw(api,source).decode(),'report',{'title':'"Changed while composing"'}).encode()
                    elif kind=='pdf':
                        endpoint='/pdf:number';body={'input':'E-report/input.pdf','output':target}
                        source=body['input'];initial=pdf();seed(store,case,source,initial);changed=initial+b'\n% changed source'
                    else:
                        endpoint='/ppt:build';body={'output':target,'title':'Remote conflict',
                            'slides':[{'title':'Picture','image':{'path':'pixel.png'}}]}
                        source='pixel.png';seed(store,case,source,PNG);changed=PNG+b'changed image'
                    (api.root/'mode').write_text(mode)
                    created=api.call('POST',api.prefix+endpoint,body)
                    wait_file(api.root/'ready',seconds=90)
                    if previous is None:api.call('GET',api.prefix+'/raw?path='+target,expect=404)
                    else:assert raw(api,target)==previous
                    if mode=='source':seed(store,case,source,changed)
                    if mode=='destination':seed(store,case,target,b'Later user destination')
                    if mode=='cancel':api.call('POST',f'/jobs/{created["job_id"]}:cancel')
                    (api.root/'release').touch()
                    job=settled(api,created['job_id'])
                    assert job['status']==('cancelled' if mode=='cancel' else 'failed'),job
                    assert job['attempt']['outputs']['publication']['status']!='committed'
                    if mode=='destination':assert raw(api,target)==b'Later user destination'
                    elif previous is not None:assert raw(api,target)==previous
                    else:api.call('GET',api.prefix+'/raw?path='+target,expect=404)
                    checks.append(kind+'_'+mode+'_preserves_remote_user_files_and_rejects_publication')
                    print('PASS '+checks[-1],flush=True)
            (api.root/'mode').write_text('normal')
            for kind in ('pdf','ppt'):
                case=new_case(api,'Invalid remote '+kind)
                if kind=='pdf':
                    seed(store,case,'broken.pdf',b'invalid PDF')
                    endpoint='/pdf:number';body={'input':'broken.pdf','output':'out.pdf'}
                else:
                    seed(store,case,'broken.pptx',b'invalid template')
                    endpoint='/ppt:build';body={'template':'broken.pptx','output':'out.pptx','slides':[{'title':'Bad template'}]}
                created=api.call('POST',api.prefix+endpoint,body)
                job=settled(api,created['job_id']);assert job['status']=='failed'
                assert job['attempt']['outputs']['publication']['status']!='committed'
                api.call('GET',api.prefix+'/raw?path='+body['output'],expect=404)
                checks.append(kind+'_native_invalid_input_has_no_published_alias')
            result={'passed':True,'count':len(checks),'checks':checks,'api':str(api.root),'evidence':str(store.root)}
    (store.root/'conflicts-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_DOCUMENT_CONFLICTS_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
