"""I02 input matrix through the CLI and optional isolated Studio HTTP API."""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from app.services.flotherm_inputs import inspect_inputs


ROOT = Path(__file__).resolve().parent
CLI = ROOT/'flotherm2foam.py'
FIXTURES = {
    'demo.ecxml':'<model><bbox>0 0 0 1 1 1</bbox><bbox>0 0 0 1 1 1</bbox></model>',
    'demo.pdml':'#FFFB V2310 sample\nENTITY serial\n',
    'demo.xtxml':'<assembly><part id="unit"/></assembly>',
    'demo.xtxmla':'<assembly><part id="unit"/></assembly>',
    'invalid.ecxml':'<model>',
    'wrong.pdml':'#FFFB V2200 sample\nNotes mention V2310.\n',
    'demo.pack':'PACK',
    'other.pdml':'#FFFB V2310 sample\nENTITY other\n',
}
MATRIX = (
    (['demo.ecxml'],'ECXML'), (['demo.pdml'],'PDML'),
    (['demo.ecxml','demo.pdml'],'ECXML+PDML'), (['demo.xtxml','demo.pdml'],'XTXML+PDML'),
    (['demo.xtxmla','demo.pdml'],'XTXMLA+PDML'),
    (['demo.xtxml','demo.pdml','demo.ecxml'],'XTXML+PDML'),
    (['demo.xtxmla','demo.pdml','demo.ecxml'],'XTXMLA+PDML'),
)


def validate(run, *, studio):
    for paths, kind in MATRIX:
        report = run(paths, True)
        assert report['ok'] and report['kind']==kind and report['studio'] is studio, report
        assert {f['name']:f['sha256'] for f in report['files']} == {
            path:hashlib.sha256(FIXTURES[path].encode()).hexdigest() for path in paths}
    assert run(['demo.ecxml'], True)['bbox_duplicates'][0]['count']==2
    for bad in (['demo.xtxml'],['demo.ecxml','other.pdml'],['wrong.pdml'],['invalid.ecxml']):
        assert not run(bad, False)['ok'], bad
    assert run(['demo.pack'], not studio)['ok'] is (not studio)


def cli_matrix():
    with tempfile.TemporaryDirectory(prefix='vf-inspect-cli-') as directory:
        root = Path(directory)
        for path, value in FIXTURES.items():
            (root/path).write_bytes(value.encode())

        def run(paths, accepted):
            result = subprocess.run([sys.executable,'-B',str(CLI),'--inspect',*(str(root/p) for p in paths)],
                cwd=ROOT, check=False, capture_output=True, text=True, encoding='utf-8', timeout=25)
            assert result.returncode==(0 if accepted else 2), result
            return json.loads(result.stdout)

        validate(run, studio=False)
        for path, text in [('broken.pdml','description contains V2310'), ('renamed.pdml','<other version="V2310"/>')]:
            (root/path).write_bytes(text.encode())
            assert not run([path], False)['ok']
        assert len(list(root.iterdir()))==len(FIXTURES)+2
    print('I02 CLI seven combinations, format validation, hashes and read-only output: OK',flush=True)


def api_matrix(s3):
    from s3testsupport import ObjectStore
    from testsupport import IsolatedServer, SERVER

    store_context = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                                volume_max=9, authenticated=True) if s3 else nullcontext()
    api = IsolatedServer('flotherm-i02-s3' if s3 else 'flotherm-i02-fs',app='flotherm_i02_fixture:app')
    fixture = api.root/'flotherm_i02_fixture.py'
    report = {'passed':False,'backend':'s3' if s3 else 'fs','actual_model_calls':0,'mock_llm_calls':0}
    print('I02_API_EVIDENCE '+str(api.root),flush=True)
    try:
        with store_context as store:
            settings = store.settings() if store else {}
            fixture.write_text('import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n',encoding='utf-8')
            try:
                with api:
                    case = api.create_case('FloTHERM real HTTP input matrix')
                    if store:
                        report['s3_receipt'] = str(store.root/'store.json')
                    for path, value in FIXTURES.items():
                        api.put('Ref/'+path,value)

                    def run(paths, _):
                        return api.call('POST',api.prefix+'/flotherm:inspect',{'paths':['Ref/'+p for p in paths]})

                    validate(run, studio=True)
                    assert not run(['demo.ecxml','demo.pdml','demo.xtxml','demo.xtxmla'],False)['ok']
                    inspect_route = api.prefix+'/flotherm:inspect'
                    api.call('POST',inspect_route,{'paths':['../demo.ecxml']},expect=400)
                    api.call('POST',inspect_route,{'paths':['Ref/missing.ecxml']},expect=400)
                    api.call('POST',inspect_route,{'paths':['Ref/demo.ecxml','Ref/demo.ecxml']},expect=400)
                    outsider = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                        'name':'Second inspection account','password':'isolated-test-123'})
                    api.call('POST',inspect_route,{'paths':['Ref/demo.ecxml']},expect=404,
                             extra_headers={'Authorization':'Bearer '+outsider['token']})
                    for flag in ('force','keep_stage'):
                        rejected = api.call('POST',api.prefix+'/flotherm:convert',
                            {'ir_path':'Ref/missing-ir.json',flag:True},expect=422)
                        assert 'force/keep-stage' in rejected['detail'][0]['msg']
                    database = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
                    with sqlite3.connect(database,uri=True) as db:
                        prefix = db.execute('select s3_prefix from cases where id=?',(case['id'],)).fetchone()[0]
                    large = api.root/'large-sample.ecxml'
                    with large.open('wb') as stream:
                        stream.truncate(32*1024*1024+1)
                    if store:
                        with large.open('rb') as stream:
                            store.client.put_object(Bucket=store.bucket,Key=prefix+'/Ref/too-big.ecxml',Body=stream)
                    else:
                        ref = api.root/'files'/prefix/'Ref'
                        large.rename(ref/'too-big.ecxml')
                        other_case_file = api.root/'separate-unselected.ecxml'
                        other_case_file.write_bytes(b'<model/>')
                        (ref/'linked.ecxml').symlink_to(other_case_file)
                        api.call('POST',inspect_route,{'paths':['Ref/linked.ecxml']},expect=400)
                        assert other_case_file.read_bytes() == b'<model/>'
                    limit = api.call('POST',inspect_route,{'paths':['Ref/too-big.ecxml']},expect=400)
                    assert '32 MiB' in limit['detail'], limit
                    assert run(['demo.ecxml','demo.pdml'],True)['ok']
                    report.update(passed=True,combinations=len(MATRIX),foreign_account_denied=True,
                                  invalid_input_preserved=True,oversized_input_denied=True,
                                  studio_override_flags_denied=True)
            finally:
                fixture.unlink(missing_ok=True)
    finally:
        (api.root/'inspection.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

with tempfile.TemporaryDirectory(prefix="vf-input-i02-") as temp:
    root = Path(temp)
    ecxml = root / "demo.ecxml"
    ecxml.write_text('<root><bbox>0 0 0 1 1 1</bbox><bbox>0 0 0 1 1 1</bbox></root>', encoding="utf-8")
    pdml = root / "demo.pdml"
    pdml.write_text('<pdml version="V2310"><bbox>0 0 0 2 2 2</bbox></pdml>', encoding="utf-8")
    xtxml = root / "demo.xtxml"
    xtxml.write_text("<root/>", encoding="utf-8")
    xtxmla = root / "demo.xtxmla"
    xtxmla.write_text("<root/>", encoding="utf-8")
    pack = root / "demo.pack"
    pack.write_text("PACK", encoding="utf-8")
    floscript = root / "demo.floscript"
    floscript.write_text("// FloSCRIPT stage fixture", encoding="utf-8")

    result = inspect_inputs([ecxml])
    assert result["ok"] and result["kind"] == "ECXML" and result["bbox_duplicates"][0]["count"] == 2
    assert inspect_inputs([pdml])["kind"] == "PDML"
    assert inspect_inputs([ecxml, pdml])["kind"] == "ECXML+PDML"
    assert inspect_inputs([xtxml, pdml])["kind"] == "XTXML+PDML"
    assert inspect_inputs([xtxmla, pdml])["kind"] == "XTXMLA+PDML"
    assert inspect_inputs([pack], studio=False)["ok"] is True
    assert inspect_inputs([floscript], studio=False)["kind"] == "FloSCRIPT"
    assert not inspect_inputs([floscript], studio=True)["ok"]
    studio_pack = inspect_inputs([pack], studio=True)
    assert not studio_pack["ok"] and "Studio 不支持" in studio_pack["blockers"][0]
    assert not inspect_inputs([xtxml])["ok"]

    other = root / "other.ecxml"
    other.write_text("<root/>", encoding="utf-8")
    mismatched = inspect_inputs([ecxml, other])
    assert not mismatched["ok"] and "相同主文件名" in mismatched["blockers"][0]
    pdml.write_text('<pdml version="V2200"/>', encoding="utf-8")
    old = inspect_inputs([pdml])
    assert not old["ok"] and "V2310" in old["blockers"][0]
    pdml.write_text('<pdml version="V2310"/>', encoding="utf-8")
    pair = root / "other.pdml"
    pair.write_text('<pdml version="V2310"/>', encoding="utf-8")
    bad_pair = inspect_inputs([ecxml, pair])
    assert not bad_pair["ok"] and "相同主文件名" in bad_pair["blockers"][0]
    try:
        inspect_inputs([ecxml, ecxml])
    except ValueError as error:
        assert "重复输入" in str(error)
    else:
        raise AssertionError("重复路径未拒绝")
print("I02 all input combinations, V2310, pairing, bbox uniqueness and Pack Studio/CLI: OK")
print("\n=== I02 Flotherm 输入检查冒烟通过 ===")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--http',action='store_true')
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    cli_matrix()
    if args.http or args.s3:
        api_matrix(args.s3)
