"""Two real native LaTeX versions, remote history and fresh-cache range reads."""
import argparse
from contextlib import nullcontext
import json
from urllib.parse import quote
import httpx
from testsupport import IsolatedServer
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from latex_handler_smoke import MAIN


def api_for(settings,peer=None):
    api=IsolatedServer('latex-history-native',app='history_fixture:app')
    overrides=dict(settings)
    if peer:
        overrides.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')})
        api.token=peer.token
    (api.root/'history_fixture.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
    return api


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    checks=[]
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        settings=store.settings() if store else {}
        with api_for(settings) as api:
            api.create_case('native historical PDF')
            main='E-report/latex/main.tex';versions=[]
            for content in (MAIN,'\n\n'+MAIN.replace('UniqueResultText','NewResultText')):
                api.put(main,content)
                created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
                api.wait_job(created['job_id'],timeout=220,expected='succeeded')
                versions.append(created)
            original=versions[0];base=api.base+'/api/v1'+api.prefix
            selected='/latex/artifact?publication_id='+original['publication_id']+'&path='+quote(original['pdf'])
            headers={'Authorization':'Bearer '+api.token}
            with httpx.Client(trust_env=False,timeout=120) as http:
                head=http.head(base+selected,headers=headers);assert head.status_code==200 and not head.content
                version=head.headers['x-vibeflow-file-version']
                body=http.get(base+selected,headers=headers);assert body.status_code==200 and body.content.startswith(b'%PDF')
                assert len(body.content)==int(head.headers['content-length'])
                for start,end in ((0,99),(100,min(999,len(body.content)-1))):
                    part=http.get(base+selected,headers={**headers,'Range':f'bytes={start}-{end}','X-Vibeflow-File-Version':version})
                    assert part.status_code==206 and part.content==body.content[start:end+1]
                    assert part.headers['x-vibeflow-file-version']==version
                assert http.get(base+selected,headers={**headers,'Range':'bytes=0-99','X-Vibeflow-File-Version':'0'*64}).status_code==409
            checks.append('native_old_PDF_HEAD_full_ranges_and_mismatched_token')
            document=api.call('GET',api.prefix+'/latex/results?publication_id='+original['publication_id'])
            assert not document['publication']['current'] and document['publication']['stale']
            assert api.call('GET',api.prefix+'/latex/source?publication_id='+original['publication_id']+'&path='+quote(main),raw=True)==MAIN.encode()
            location=api.call('POST',api.prefix+'/synctex:query',{'pdf':original['pdf'],'publication_id':original['publication_id'],'page':2,'x':134,'y':157})
            assert location['line']==7 and not location['source_current_matches'] and location['publication_id']==original['publication_id']
            checks.append('native_historical_source_and_SyncTeX_keep_old_line_after_current_edits')
            if store:
                with api_for(settings,peer=api) as peer:
                    assert peer.root!=api.root
                    result=peer.call('GET',api.prefix+'/latex/results?publication_id='+original['publication_id'])
                    assert result['publication']['id']==original['publication_id']
                    with httpx.Client(trust_env=False,timeout=120) as http:
                        part=http.get(peer.base+'/api/v1'+api.prefix+selected,headers={**headers,'Range':'bytes=0-99','X-Vibeflow-File-Version':version})
                        assert part.status_code==206 and part.content==body.content[:100]
                        assert part.headers['x-vibeflow-file-version']==version
                    checks.append('fresh_API_independent_cache_retains_publication_range_version')
            result={'passed':True,'storage':'s3' if store else 'fs','database':'sqlite','count':len(checks),'checks':checks,
                    'scope':'Two actual pdflatex reports; S3 fresh-cache reader when selected'}
        (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print('LATEX_HISTORY_NATIVE_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
