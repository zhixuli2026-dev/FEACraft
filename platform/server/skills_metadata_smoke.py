"""Verify the built-in skill catalog and edited metadata without sending a model turn."""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from testsupport import IsolatedServer, SERVER


FIELDS = ('display_name', 'short_description', 'default_prompt')
EXPECTED = {
    'vibeflow-abaqus': 'VibeFlow Abaqus',
    'vibeflow-aircraft-design': 'VibeFlow Aircraft Design',
    'vibeflow-comsol': 'VibeFlow COMSOL',
    'vibeflow-fluent': 'VibeFlow Fluent',
    'vibeflow-skill-creator': 'VibeFlow Skill Creator',
    'vibeflow-starccm': 'VibeFlow STAR-CCM+',
}


def browser(data):
    from playwright.sync_api import expect, sync_playwright

    root = Path(data['root'])
    dispatched = []
    selected = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1520, 'height': 900})
        page.add_init_script('localStorage.setItem("vf_token", '+json.dumps(data['token'])+');')

        def observe(request):
            if request.method=='POST' and ('/messages' in request.url or '/turns' in request.url):
                dispatched.append(request.method)

        page.on('request', observe)
        page.goto(data['base']+'/', wait_until='domcontentloaded')
        page.locator(f'[data-case-name="{data["case_name"]}"]').click()
        page.locator('.dv-tab').filter(has_text='Agent').first.click()
        conversation = page.get_by_test_id('agent-conversation')
        conversation.select_option(str(data['conversation_id']))
        draft = page.get_by_test_id('agent-input')
        for name, interface in data['interfaces'].items():
            draft.fill('$'+name)
            choice = page.get_by_text(interface['display_name'], exact=True)
            expect(choice).to_be_visible()
            expect(choice.locator('..')).to_contain_text(interface['short_description'][:40])
            choice.click()
            expect(draft).to_have_value(interface['default_prompt']+' ')
            selected.append(name)
        for name in data['catalog']:
            if name in data['interfaces']:
                continue
            draft.fill('$'+name)
            choice = page.get_by_text('$'+name, exact=True).and_(page.locator('b'))
            expect(choice).to_be_visible()
            choice.click()
            expect(draft).to_have_value('$'+name+' ')
            selected.append(name)
        draft.fill('$vibeflow-ppt')
        presentation = page.get_by_text('$vibeflow-PPT', exact=True).and_(page.locator('b'))
        expect(presentation).to_be_visible()
        page.screenshot(path=str(root/'skill-picker-draft.png'))
        presentation.click()
        expect(draft).to_have_value('$vibeflow-PPT ')
        draft.fill('$unregistered-catalog-entry')
        expect(page.get_by_text('无匹配技能', exact=True)).to_be_visible()
        browser.close()
    assert not dispatched
    (root/'browser-result.json').write_text(json.dumps({'selected':selected,'model_requests':len(dispatched),
        'mixed_case_selection':True,'unknown_skill':True},indent=2),encoding='utf8')


def verify(args):
    api = IsolatedServer('skill-metadata', app='skill_metadata_fixture:app')
    fixture = 'from app.main import app\n'
    if args.dist is not None:
        dist = args.dist.resolve(strict=True)
        assert (dist/'index.html').is_file()
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    (api.root/'skill_metadata_fixture.py').write_text(fixture, encoding='utf8')
    result = {'passed':False,'actual_model_requests':0,'mock_llm_calls':0,'checks':{}}
    print('SKILL_METADATA_EVIDENCE', api.root, flush=True)

    def listed(name):
        return next(row for row in api.call('GET','/skills')['skills'] if row['name']==name)

    def interface_of(row):
        return {key:row[key] for key in FIELDS if key in row}

    try:
        with api:
            api.call('GET','/skills',expect=401)
            api.call('GET','/skills/vibeflow-PPT',expect=401)
            case = api.create_case('Skill Catalog Acceptance')
            original = SERVER.parents[1]/'extracted/share'
            original_daemon = {p.parent.name for p in (original/'vibeflow-agent/skills').glob('*/SKILL.md')}
            original_desktop = {p.parent.name for p in (original/'vibeflow/skills').glob('*/SKILL.md')}
            assert original_daemon==original_desktop and len(original_daemon)==37
            listed_rows = api.call('GET','/skills')['skills']
            catalog = {item['name']:item for item in listed_rows}
            bundled = {p.parent.name for p in (SERVER/'skills').glob('*/SKILL.md')}
            assert len(catalog)==len(listed_rows)
            assert original_daemon.issubset(bundled) and bundled==catalog.keys()
            details = {}
            for name,item in catalog.items():
                detail = api.call('GET','/skills/'+name)
                assert detail['name']==name and detail['description']==item['description']
                assert detail['description'].strip() and detail['body'].strip()
                assert 'body' not in item
                details[name]=hashlib.sha256(detail['body'].encode('utf8')).hexdigest()
            api.call('GET','/skills/unregistered-catalog-entry',expect=404)
            result['checks']['catalog']={
                'original':sorted(original_daemon),'additional':sorted(bundled-original_daemon),
                'details_sha256':details,'auth_required':True,'unknown_status':404}
            owner = api.call('GET','/auth/me')['id']
            interfaces = {}
            for name,display in EXPECTED.items():
                row=interface_of(listed(name))
                assert row['display_name']==display and row['short_description']
                assert row['default_prompt'].startswith('Use $'+name)
                assert api.call('GET','/skills/'+name)['interface']==row
                interfaces[name]=row
            result['checks']['builtins']=list(interfaces)

            private_name = 'vibeflow-abaqus'
            values = {'display_name':'Local "Abaqus"', 'short_description':'Windows and multiline metadata',
                      'default_prompt':'Use $vibeflow-abaqus with C:\\new\\project\nCheck the result.'}
            payload = {'name':private_name,'description':'Private metadata fixture','body':'Isolated example.',**values}
            api.call('POST','/skills',payload,expect=(200,201))
            assert interface_of(listed(private_name))==values
            assert api.call('GET','/skills/'+private_name)['interface']==values
            archive = api.call('POST','/skills/'+private_name+'/package',raw=True)
            with zipfile.ZipFile(BytesIO(archive)) as content:
                metadata,=[name for name in content.namelist() if name.endswith('/agents/openai.yaml') or name=='agents/openai.yaml']
                assert json.loads(content.read(metadata))['interface']==values
            original_token=api.token
            api.token=api.call('POST','/auth/register',{'email':'skill-metadata-outsider@example.com',
                'password':'isolated-test-234','name':'Skill metadata outsider'})['token']
            assert interface_of(listed(private_name))==interfaces[private_name]
            api.token=original_token
            result['checks']['private_metadata_and_archive']=True

            preview_name='metadata-roundtrip'
            preview = dict(values, display_name='Metadata "roundtrip"', default_prompt=values['default_prompt'].replace(private_name,preview_name))
            api.call('POST','/skills', {**payload, **preview,'name':preview_name},expect=(200,201))
            assert interface_of(listed(preview_name))==preview
            private_root=Path(api.env['VIBEFLOW_SKILLS_STORAGE_ROOT'])/'users'/str(owner)/private_name
            metadata=private_root/'agents'/'openai.yaml'
            metadata.write_text('{broken',encoding='utf8')
            assert interface_of(listed(private_name))=={}
            assert api.call('GET','/skills/'+private_name)['interface']=={}
            legacy = 'interface:\n  display_name: "Legacy title"\n  default_prompt: "Use C:\\new\\project"\n'
            metadata.write_text(legacy,encoding='utf8')
            assert listed(private_name)['default_prompt']=='Use C:\\new\\project'
            metadata.unlink()
            assert interface_of(listed(private_name))=={}
            definition = private_root/'SKILL.md'
            for invalid in (b'Missing frontmatter', b'\xff\xfe'):
                definition.write_bytes(invalid)
                assert interface_of(listed(private_name))==interfaces[private_name]
                assert api.call('GET','/skills/'+private_name)['interface']==interfaces[private_name]
            definition.unlink()
            assert interface_of(listed(private_name))==interfaces[private_name]
            assert api.call('GET','/skills/'+private_name)['interface']==interfaces[private_name]
            result['checks']['changed_malformed_and_removed_metadata']=True
            result['checks']['invalid_or_missing_private_definition_falls_back']=3

            if args.dist is not None:
                conversation = api.call('POST','/conversations',{'case_id':case['id'],'title':'Skills metadata'})
                data={'root':str(api.root),'base':api.base,'token':api.token,'case_name':case['name'],
                      'conversation_id':conversation['id'],'interfaces':{**interfaces,preview_name:preview},
                      'catalog':list(catalog)}
                subprocess.run([str(args.browser_python),str(Path(__file__).resolve()),'--browser'],
                               input=json.dumps(data),text=True,timeout=120,check=True)
                assert api.call('GET','/conversations/'+str(conversation['id']))['events']==[]
                result['checks']['browser']=json.loads((api.root/'browser-result.json').read_text(encoding='utf8'))
                assert set(result['checks']['browser']['selected'])==catalog.keys() | {preview_name}
        result['passed']=True
    except BaseException as error:
        result['failure_type']=type(error).__name__
        raise
    finally:
        result['api_exited']=api.proc is not None and api.proc.poll() is not None
        (api.root/'skill-metadata-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist',type=Path)
        parser.add_argument('--browser-python',type=Path)
        arguments=parser.parse_args()
        if arguments.dist is not None and arguments.browser_python is None:
            parser.error('--browser-python is required for the browser acceptance')
        verify(arguments)
