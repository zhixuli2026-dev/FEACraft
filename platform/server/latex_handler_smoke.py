"""Registered native LaTeX, owned sources, dynamic dependencies and publication races."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote

from pypdf import PdfReader
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from geometry_publication_smoke import wait_file
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json,sys
from pathlib import Path
root=Path(__file__).parent
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if len(args)>1 and Path(str(args[1])).name=='registered_latex_worker.py':
        identity=json.loads((Path(kwargs['cwd']).parent/'manifest.json').read_text(encoding='utf-8'))
        case_id=identity['source']['case_id']
        marker=root/('latex-control-'+str(case_id))
        control=marker.read_text() if marker.exists() else ''
        if control=='noop': args=(sys.executable,'-c','pass')
        elif control: args=(sys.executable,str(root/'controlled_latex_worker.py'),control,str(case_id),*args[1:])
    return await original(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original,spawn)
from app.main import app
'''

WORKER='''import gzip,hashlib,io,json,runpy,sys,time
from pathlib import Path
root=Path(__file__).parent
control,case_id=sys.argv[1:3]
sys.argv=sys.argv[3:]
runpy.run_path(sys.argv[0],run_name='__main__')
if control in {'hold','hold_failure'}:
    marker=root/('hold-latex-'+case_id)
    marker.with_suffix('.ready').touch()
    while not marker.with_suffix('.release').exists(): time.sleep(.03)
    if control=='hold_failure': raise SystemExit(7)
else:
    manifest_path=next(Path('.').glob('E-report/latex/build/*.latex-manifest.json'))
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    pdf=next(Path(path) for path in manifest['artifacts'] if path.endswith('.pdf') and '/latex-history/' not in path)
    if control=='badpdf':
        from pypdf import PdfReader,PdfWriter
        writer=PdfWriter(clone_from=PdfReader(pdf));writer.add_blank_page(width=100,height=100)
        buffer=io.BytesIO();writer.write(buffer);pdf.write_bytes(buffer.getvalue())
    elif control=='badsync':
        sidecar=pdf.with_suffix('.synctex.gz')
        value=gzip.decompress(sidecar.read_bytes())
        changed=value.replace(b'Unit:1\\n',b'Unit:2\\n',1)
        assert changed!=value
        sidecar.write_bytes(gzip.compress(changed,mtime=0))
    elif control=='badbbl': pdf.with_suffix('.bbl').write_bytes(b'forged bibliography')
    elif control=='badsource':
        Path(manifest['source_files'][manifest['main']]).write_bytes(b'forged source')
    elif control=='badmanifest': manifest['main']='E-report/latex/other.tex'
    elif control=='badtoolchain':
        path=pdf.with_suffix('.latex-toolchain.json')
        toolchain=json.loads(path.read_text(encoding='utf-8'));toolchain['execution_id']='00000000-0000-0000-0000-000000000000'
        path.write_text(json.dumps(toolchain),encoding='utf-8')
    elif control=='badlog': pdf.with_suffix('.latex.log').write_bytes(b'forged compiler transcript')
    else: raise ValueError(control)
    for path,record in manifest['artifacts'].items():
        data=Path(path).read_bytes();record.update(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
'''

MAIN=('\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n'
      '\\section{Introduction}\nIntro text.\\newpage\n\\section{Results}\nUniqueResultText and conclusion.\n\\end{document}\n')
BIB='@article{demo,author={Doe, Jane},title={A Test},journal={Journal},year={2026}}\n'
COMPLEX=('\\documentclass{article}\n\\usepackage{graphicx}\n\\usepackage[backend=biber,style=authoryear]{biblatex}\n'
    '\\addbibresource{../shared/refs.bib}\n\\begin{document}\n'
    '\\def\\partfile{chapters/detail}\\input{\\partfile}\n'
    '\\includegraphics[width=30pt]{../assets/plot.png}\nCitation \\cite{demo}.\n\\printbibliography\n\\end{document}\n')


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name);print('PASS '+name,flush=True)
    server=IsolatedServer('latex-handler',app='controlled_latex:app',database_url=database_url)
    (server.root/'controlled_latex.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'controlled_latex_worker.py').write_text(WORKER,encoding='utf-8')
    with server as api:
        api.create_case('registered LaTeX')
        root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        main='E-report/latex/main.tex'
        paper='E-report/latex/paper.tex'
        for path,value in ((main,MAIN),(paper,COMPLEX),('E-report/shared/refs.bib',BIB),
                           ('E-report/latex/chapters/detail.tex','Dynamic chapter and $E=mc^2$.\n')):
            api.put(path,value)
        from PIL import Image
        picture=root/'E-report/assets/plot.png';picture.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(16,16),(30,90,150)).save(picture)
        def build(path=main,recipe='pdflatex',key=None,client=api,resources=None,expect=200):
            body={'main':path,'recipe':recipe}
            if resources is not None: body['resources']=resources
            return client.call('POST',api.prefix+'/latex:build',body,expect=expect,
                extra_headers={'Idempotency-Key':key} if key else None)
        def result(identifier=None,path=main,client=api,expect=200):
            query='publication_id='+identifier if identifier else 'main='+quote(path,safe='')
            return client.call('GET',api.prefix+'/latex/results?'+query,expect=expect)
        def finish(submitted,client=api,expected='succeeded',applied=True):
            job=client.wait_job(submitted['job_id'],timeout=220,expected=expected)
            if expected=='succeeded' and applied:
                deadline=time.monotonic()+30
                while True:
                    document=result(submitted['publication_id'],client=client)
                    if document['publication']['aliases_applied']: return job,document
                    if time.monotonic()>deadline: raise AssertionError(document['publication'])
                    time.sleep(.1)
            return job,None
        for recipe in ('pdflatex','xelatex','lualatex'):
            submitted=build(recipe=recipe,key='recipe-'+recipe)
            job,current=finish(submitted)
            assert job['command']=='Registered latexmk' and not current['publication']['stale']
            assert current['manifest']['pages']==2 and current['manifest']['input_publications']==[]
            pdf=(root/submitted['pdf']).read_bytes()
            assert 'UniqueResultText' in PdfReader(io.BytesIO(pdf)).pages[1].extract_text()
            if recipe=='pdflatex': first=current
            done(recipe+'_produces_verified_PDF_SyncTeX_and_owned_sources_without_postprocess_requirement')
        archived=first['publication']['id']
        assert not result(archived)['publication']['current']
        assert build(key='recipe-pdflatex')['job_id']==first['job_id']
        build(recipe='xelatex',key='recipe-pdflatex',expect=409)
        source=api.call('GET',api.prefix+'/latex/source?publication_id='+archived+'&path='+main,raw=True)
        assert source==MAIN.encode('utf-8')
        assert api.call('GET',api.prefix+'/latex/artifact?publication_id='+archived+'&path='+first['pdf'],raw=True).startswith(b'%PDF')
        done('idempotent_recipe_replay_and_explicit_history_keep_original_source_bytes')
        def locate(identifier=None):
            body={'pdf':first['pdf'],'page':2,'x':134,'y':157}
            if identifier: body['publication_id']=identifier
            return api.call('POST',api.prefix+'/synctex:query',body)
        location=locate(archived)
        assert location['file']==main and location['line']==7 and location['source_current_matches']
        api.put(main,'\n\n'+MAIN)
        location=locate(archived)
        assert location['line']==7 and not location['source_current_matches']
        assert api.call('GET',api.prefix+'/latex/source?publication_id='+archived+'&path='+main,raw=True)==source
        api.put(main,MAIN)
        done('historical_SyncTeX_uses_retained_sources_after_temporary_cleanup_and_live_edits')
        immutable=root/'.vibeflow/publications'/archived/'files'/first['pdf']
        original=immutable.read_bytes()
        try:
            immutable.write_bytes(original+b'broken')
            result(archived,expect=409)
        finally: immutable.write_bytes(original)
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'other-latex-123','name':'Other'})['token']
        for suffix in ('/latex/results','/latex/publications','/latex/source?path='+main,'/latex/artifact?path='+first['pdf']):
            api.call('GET',api.prefix+suffix,expect=404,extra_headers={'Authorization':'Bearer '+other})
        api.call('GET',api.prefix+'/latex/source?publication_id='+archived+'&path=../outside',expect=404)
        done('immutable_corruption_foreign_ownership_and_unlisted_sources_are_rejected')
        submitted=build(paper,'xelatex')
        _,bibliography=finish(submitted)
        assert any(path.endswith('.bbl') for path in bibliography['manifest']['artifacts'])
        assert {'E-report/shared/refs.bib','E-report/assets/plot.png','E-report/latex/chapters/detail.tex'}<=set(bibliography['manifest']['source_files'])
        assert b'demo' in (root/submitted['pdf']).with_suffix('.bbl').read_bytes()
        assert result()['publication']['current'] and result(path=paper)['publication']['current']
        done('biber_relative_shared_assets_and_macro_selected_chapter_are_frozen_and_two_mains_have_independent_heads')
        # Lua file I/O is absent from many .fls records; the native syscall trace
        # must capture it as a real source dependency.
        custom='E-report/shared/message.custom'
        api.put(custom,'Recorded Lua resource')
        lua='E-report/latex/dynamic.tex'
        api.put(lua,r'\documentclass{article}\begin{document}\directlua{local f=assert(io.open("../shared/message.custom")); tex.print(f:read("*a")); f:close()}\end{document}')
        dynamic=build(lua,'lualatex',resources=[custom])
        _,dynamic_result=finish(dynamic)
        assert custom in dynamic_result['manifest']['source_files']
        assert b'Recorded Lua resource'==api.call('GET',api.prefix+'/latex/source?publication_id='+dynamic['publication_id']+'&path='+custom,raw=True)
        done('explicit_custom_resource_read_by_Lua_is_recorded_beyond_TeX_recorder_inputs')
        # Refresh after adding source files to the conservative compilation envelope.
        current=finish(build())[1]
        bibliography=finish(build(paper,'xelatex'))[1]
        peer=IsolatedServer('latex-peer');peer.token=api.token;peer.prefix=api.prefix
        def start_peer():
            peer.log=(peer.root/'api.log').open('a',encoding='utf-8')
            peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_latex:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],cwd=api.root,env=api.env,
                stdout=peer.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if peer.proc.poll() is not None: raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
                try:
                    if peer.call('GET','/api/health',api_prefix=False).get('ok'): return
                except (OSError,ValueError): pass
                time.sleep(.1)
            raise TimeoutError('Peer API did not start')
        def stop_peer():
            if peer.proc:
                peer.proc.terminate()
                try: peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired: peer.proc.kill();peer.proc.wait(timeout=5)
            peer.log.close()
        marker=api.root/f'latex-control-{api.case_id}'
        start_peer()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending=[pool.submit(build,key='two-apis',client=client) for client in (api,peer)]
                responses=[future.result(timeout=30) for future in pending]
            assert responses[0]['job_id']==responses[1]['job_id']
            current=finish(responses[0])[1]
            stop_peer();start_peer()
            assert result(client=peer)['publication']['id']==current['publication']['id']
            assert result(archived,client=peer)['manifest']['main']==main
            done('cross_API_submission_is_unique_and_reader_restart_preserves_current_and_history')
            for control in ('noop','badpdf','badsync','badsource','badmanifest','badtoolchain','badlog','badbbl'):
                selected=paper if control=='badbbl' else main
                old=result(path=selected)
                previous=(root/old['pdf']).read_bytes()
                marker.write_text(control)
                try:
                    submitted=build(selected,'xelatex' if control=='badbbl' else 'pdflatex')
                    failed,_=finish(submitted,expected='failed')
                finally: marker.unlink(missing_ok=True)
                assert result(path=selected)['publication']['id']==old['publication']['id'] and (root/old['pdf']).read_bytes()==previous
                assert failed['attempt']['outputs']['publication_error']['message']
                assert api.call('GET',api.prefix+'/latex/errors?job_id='+str(submitted['job_id']))['items']
                done(control+'_cannot_publish_even_after_rehashing_the_manifest')
            for scenario in ('source','resource','bibliography','head_edit','superseded','independent_main','cancel','late_failure'):
                selected=paper if scenario in {'resource','bibliography'} else main
                recipe='xelatex' if selected==paper else 'pdflatex'
                old=result(path=selected)
                previous=(root/old['pdf']).read_bytes()
                barrier=api.root/f'hold-latex-{api.case_id}'
                barrier.with_suffix('.ready').unlink(missing_ok=True);barrier.with_suffix('.release').unlink(missing_ok=True)
                marker.write_text('hold_failure' if scenario=='late_failure' else 'hold')
                changed=None
                try:
                    submitted=build(selected,recipe)
                    wait_file(barrier.with_suffix('.ready'),timeout=120)
                    marker.unlink()
                    if scenario in {'source','resource','bibliography'}:
                        changed=root/({'source':main,'resource':'E-report/assets/plot.png','bibliography':'E-report/shared/refs.bib'}[scenario])
                        before=changed.read_bytes();changed.write_bytes(before+b'\nchanged')
                    elif scenario=='head_edit':
                        log=old['pdf'].removesuffix('.pdf')+'.latex.log'
                        api.put(log,(root/log).read_text(encoding='utf-8'))
                    elif scenario in {'superseded','late_failure','independent_main'}:
                        winner=build(paper if scenario=='independent_main' else selected,
                            'xelatex' if scenario=='independent_main' else recipe,client=peer)
                    else: api.call('POST',f'/jobs/{submitted["job_id"]}:cancel',{})
                finally:
                    marker.unlink(missing_ok=True);barrier.with_suffix('.release').touch()
                if scenario in {'superseded','late_failure','independent_main'}:
                    # A held case writer excludes final validation for all mains.
                    finish(winner,client=peer)
                expected='cancelled' if scenario=='cancel' else 'failed' if scenario in {'source','resource','bibliography','late_failure'} else 'succeeded'
                finish(submitted,expected=expected,applied=False)
                if changed:
                    assert (root/old['pdf']).read_bytes()==previous
                    changed.write_bytes(before)
                elif scenario=='head_edit':
                    result(path=selected,expect=404)
                    assert result(submitted['publication_id'])['publication']['status']=='superseded'
                    finish(build(selected,recipe))
                elif scenario in {'superseded','late_failure'}:
                    assert result(path=selected)['publication']['id']==winner['publication_id']
                elif scenario=='independent_main':
                    assert result(path=main)['publication']['id']==submitted['publication_id']
                    assert result(path=paper)['publication']['id']==winner['publication_id']
                else: assert (root/old['pdf']).read_bytes()==previous
                done('held_'+scenario+'_preserves_durable_generation_source_and_cancel_fences')
        finally: stop_peer()
        # A no-bibliography replacement retires only previously owned, unchanged BBL.
        previous=finish(build(paper,'xelatex'))[1]
        bbl=(root/previous['pdf']).with_suffix('.bbl')
        api.put(paper,MAIN)
        no_bib=finish(build(paper,'xelatex'))[1]
        assert not bbl.exists() and not any(path.endswith('.bbl') for path in no_bib['manifest']['artifacts'])
        api.put(paper,COMPLEX);finish(build(paper,'xelatex'))
        old_pdf=(root/previous['pdf']).read_bytes();bbl.write_bytes(b'manually preserved bibliography')
        api.put(paper,MAIN)
        submitted=build(paper,'xelatex');finish(submitted,applied=False)
        assert not result(submitted['publication_id'])['publication']['aliases_applied']
        assert bbl.read_bytes()==b'manually preserved bibliography' and (root/previous['pdf']).read_bytes()==old_pdf
        bbl.unlink();finish(build(paper,'xelatex'))
        done('obsolete_BBL_is_retired_only_when_owned_bytes_match_and_manual_edits_survive')
        from app.services.jobrunner import win_to_wsl_path
        external=api.root/'external-latex.custom'
        external.write_text('External resource must not publish',encoding='utf-8')
        absolute=win_to_wsl_path(external)
        api.put(lua,r'\documentclass{article}\begin{document}\directlua{local f=assert(io.open([['+absolute+r']])); tex.print(f:read("*a")); f:close()}\end{document}')
        rejected=build(lua,'lualatex')
        failed,_=finish(rejected,expected='failed')
        assert 'external or private resource' in failed['log_backlog']
        assert result(path=lua)['publication']['id']==dynamic['publication_id']
        done('absolute_Lua_read_outside_the_frozen_case_cannot_publish')
        named='E-report/latex/-中文 report.tex'
        api.put(named,MAIN)
        special=finish(build(named,'xelatex'))[1]
        assert named in special['manifest']['source_files']
        assert api.call('GET',api.prefix+'/latex/source?publication_id='+special['publication']['id']+'&path='+quote(named,safe=''),raw=True)==MAIN.encode()
        done('Unicode_spaces_and_leading_dash_in_main_paths_preserve_native_source_identity')
        before=api.call('GET',api.prefix+'/jobs')
        build('../outside.tex',expect=400);build(recipe='latex',expect=400)
        build('E-report/latex/`bad`.tex',expect=400)
        build('E-report/latex/$bad.tex',expect=400)
        build(resources=['.vibeflow/attempts'],expect=400)
        build('E-report/latex/missing.tex',expect=404)
        assert api.call('GET',api.prefix+'/jobs')==before
        done('invalid_main_recipe_metadata_resources_and_missing_source_do_not_create_jobs')
        original=(root/main).read_bytes()
        api.put(main,'\\documentclass{article}\n\\begin{document}\n\\UndefinedCommandHere\n\\end{document}\n')
        submitted=build();finish(submitted,expected='failed')
        errors=api.call('GET',api.prefix+'/latex/errors?job_id='+str(submitted['job_id']))['items']
        assert any(item['file']==main and item['line']==3 for item in errors),errors
        (root/main).write_bytes(original)
        done('real_compile_failure_retains_original_source_path_and_line_diagnostics')
        for name in verify_engineering(api): done(name)
        result_data={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'latex-handler-result.json').write_text(json.dumps(result_data,indent=2)+'\n',encoding='utf-8')
        return result_data


def verify_engineering(api):
    from app.services.casedict import set_block
    from cae_benchmarks import thermal_files
    from geometry_publication_smoke import script
    api.create_case('LaTeX engineering publications')
    root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
    run_dir='C-run/conduction'
    saved=api.call('GET',api.prefix+'/caseDict')
    content=set_block(saved['content'],'mesh',{'source':'blockMesh',
        'qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
    content=set_block(content,'numericalMethod',{'solver':'laplacianFoam'})
    content=set_block(content,'postProcessing',{'runDir':run_dir,'fields':{'T':{'unit':'K'}},
        'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}},
            'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
        'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,'formats':['pdf','png']}}}})
    api.call('PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
    for path,value in thermal_files().items():
        destination=root/path;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(value.replace('(20 2 2)','(20 4 4)'),encoding='utf-8')
    (root/'A-geo/geometry.py').write_text(script(2),encoding='utf-8')
    revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
    flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'postprocess','run_dir':run_dir,
        'expected_revision':revision,'budget':{'timeout_seconds':120,'max_log_bytes':1048576}})
    for key in ('confirm_B','confirm_C','confirm_D','confirm_E'):
        deadline=time.monotonic()+160
        while True:
            detail=api.call('GET',f'/workflow-runs/{flow["id"]}')
            step=next(row for row in detail['steps'] if row['key']==key)
            if step['status']=='awaiting_confirmation': break
            assert detail['status'] not in {'failed','cancelled','interrupted'},detail
            if time.monotonic()>deadline: raise TimeoutError(detail)
            time.sleep(.15)
        for producer in detail['steps']:
            if producer['status']=='succeeded' and producer['attempts']:
                api.wait_job(producer['attempts'][-1]['job_id'])
        api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
    post=api.call('GET',api.prefix+'/postprocess/results')
    mean=next(item for item in post['metrics']['metrics'] if item['name']=='mean.T@1')
    assert abs(mean['value']-350)<1e-6,mean
    plot=next(item['path'] for item in post['manifest']['artifacts'] if item['path'].endswith('.pdf'))
    main='E-report/latex/main.tex'
    api.put(main,r'\documentclass{article}\usepackage{graphicx}\begin{document}Verified thermal results.\includegraphics[width=100pt]{../../'+plot+r'}\end{document}')
    def build(): return api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'xelatex'})
    submitted=build();api.wait_job(submitted['job_id'],timeout=220)
    published=api.call('GET',api.prefix+'/latex/results')
    assert not published['publication']['stale']
    refs=published['manifest']['input_publications']
    assert len(refs)==2 and any(item['publication_id']==post['publication']['id'] for item in refs),refs
    assert plot in published['manifest']['source_files']
    pdf=(root/submitted['pdf']).read_bytes()
    assert b'%PDF'==pdf[:4]
    marker=api.root/f'latex-control-{api.case_id}'
    barrier=api.root/f'hold-latex-{api.case_id}'
    marker.write_text('hold')
    try:
        held=build();wait_file(barrier.with_suffix('.ready'),timeout=120)
        metrics=post['result_dir']+'/metrics.json'
        api.put(metrics,(root/metrics).read_text(encoding='utf-8'))
    finally:
        marker.unlink(missing_ok=True);barrier.with_suffix('.release').touch()
    api.wait_job(held['job_id'],timeout=220,expected='failed')
    assert (root/submitted['pdf']).read_bytes()==pdf
    stale=api.call('GET',api.prefix+'/latex/results')
    assert stale['publication']['id']==published['publication']['id'] and stale['publication']['stale']
    api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'xelatex'},expect=409)
    assert api.call('GET',api.prefix+'/latex/artifact?publication_id='+submitted['publication_id']+'&path='+submitted['pdf'],raw=True)==pdf
    return ['real_thermal_postprocess_plot_and_mesh_quality_are_pinned_as_LaTeX_publication_inputs',
        'invalidated_engineering_head_blocks_new_and_inflight_LaTeX_but_preserves_history']


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='latex-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_latex_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_latex_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('LATEX_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__': asyncio.run(main())
