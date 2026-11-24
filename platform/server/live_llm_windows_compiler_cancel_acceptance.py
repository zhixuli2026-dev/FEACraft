"""Cancel a real native compiler and prove an unrelated user's Python/Node job survives."""
import asyncio
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from app.services import process_identity_native as identity
from app.services.windows_process_scope import ProcessHandle
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from testsupport import SERVER


DRIVER = '''
import json,os,subprocess
from pathlib import Path
job = subprocess.Popen(ARGV)
witness = Path(WITNESS)
temp = witness.with_suffix('.tmp')
temp.write_text(json.dumps({'driver':os.getpid(),'shell':os.getppid(),'child':job.pid,'cwd':os.getcwd()}))
temp.replace(witness)
raise SystemExit(job.wait(timeout=150))
'''


def submit(api, argv, witness, inputs):
    script = "ARGV = "+repr(argv)+"\nWITNESS = "+repr(str(witness))+"\n"+DRIVER
    put(api, "Ref/driver.py", script)
    request = {"runtime":"windows", "command":"cmd.exe /d /c python Ref/driver.py",
               "timeout_seconds":180, "input_paths":["Ref/driver.py",*inputs]}
    result = api.call("POST",api.prefix+"/jobs",request,expect=(200,202),
                      extra_headers={"Idempotency-Key":uuid.uuid4().hex})
    return result["id"]


def image_name(handle):
    query = ctypes.WinDLL("kernel32",use_last_error=True).QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    length = wintypes.DWORD(32768)
    name = ctypes.create_unicode_buffer(length.value)
    if not query(handle.handle,0,name,ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(name.value).name.lower()


def scenario(api, **_):
    compiler = SERVER.parent / "web/node_modules/@typescript/typescript-win32-x64/lib/tsc.exe"
    node = shutil.which("node")
    assert os.name == "nt" and compiler.is_file() and node
    target = (api.token,api.case_id,api.prefix)
    source = "export const result: number = 42;\n"
    put(api,"Ref/field.ts",source)
    left = api.root/"compiler-processes.json"
    right = api.root/"peer-processes.json"
    challenge, answer = api.root/"challenge.txt",api.root/"answer.txt"
    peer_code = f"""
const fs=require('node:fs'), crypto=require('node:crypto');
const timer=setInterval(() => {{
    if (!fs.existsSync({json.dumps(str(challenge))})) return;
    const digest=crypto.createHash('sha256').update(fs.readFileSync({json.dumps(str(challenge))})).digest('hex');
    fs.writeFileSync({json.dumps(str(answer))},digest);
    console.log(digest); clearInterval(timer);
}},50);
"""
    live_jobs = []
    try:
        compiler_job = submit(api,[str(compiler),"--watch","--pretty","false","--skipLibCheck","--target","es2020",
                                   "--outDir","build","Ref/field.ts"],left,["Ref/field.ts"])
        live_jobs.append((api.token,compiler_job))
        api.create_case("Independent Python and Node")
        peer = (api.token,api.case_id,api.prefix)
        put(api,"Ref/peer.cjs",peer_code)
        peer_job = submit(api,[node,"Ref/peer.cjs"],right,["Ref/peer.cjs"])
        live_jobs.append((api.token,peer_job))
        wait_for(lambda:left.exists() and right.exists(),"Missing native child witnesses",60)
        compiler_tree, peer_tree = [json.loads(p.read_text()) for p in (left,right)]
        output = Path(compiler_tree["cwd"])/"build/field.js"
        wait_for(lambda:output.exists() and "42" in output.read_text(encoding="utf8"),"Compiler did not emit JavaScript",40)
        emitted = output.read_bytes()
        assert b"const result = 42" in emitted
        assert Path(compiler_tree["cwd"]) != Path(peer_tree["cwd"])
        records = {}
        with ExitStack() as handles:
            observed = {}
            for name,tree in (("compiler",compiler_tree),("peer",peer_tree)):
                observed[name] = []
                records[name] = {}
                for role in ("shell","driver","child"):
                    record = identity.observe(tree[role])
                    handle = ProcessHandle.query(record)
                    handles.callback(handle.close)
                    expected_name = {"shell":"cmd.exe","driver":"python.exe",
                                     "child":"tsc.exe" if name=="compiler" else "node.exe"}[role]
                    assert image_name(handle) == expected_name
                    observed[name].append(handle)
                    records[name][role] = record
            assert len({r["pid"] for tree in records.values() for r in tree.values()}) == 6
            api.call("POST",f"/jobs/{compiler_job}:cancel",expect=404)
            api.token,api.case_id,api.prefix = target
            assert api.call("GET",f"/jobs/{compiler_job}")["status"] == "running"
            assert api.call("POST",f"/jobs/{compiler_job}:cancel")["ok"]
            route = f"/jobs/{compiler_job}"
            def stopped():
                state = api.call("GET",route)
                stop = state["attempt"].get("process_stop") or {}
                return state if state["status"]=="cancelled" and stop.get("verified") else None
            cancelled = wait_for(stopped,"Cancellation did not complete verified stop",60)
            for handle in observed["compiler"]:
                handle.wait_exit(5)
            live_jobs.remove((target[0],compiler_job))
            assert api.call("GET","/org/mine")["usage"]["running_jobs"] == 0
            assert api.call("GET",api.prefix+"/raw?path=Ref/field.ts",raw=True).decode() == source
            api.token,api.case_id,api.prefix = peer
            assert api.call("GET",f"/jobs/{peer_job}")["status"] == "running"
            for role,handle in zip(("shell","driver","child"),observed["peer"]):
                assert handle.identity() == records["peer"][role]
            nonce = uuid.uuid4().hex
            temp = challenge.with_suffix(".tmp")
            temp.write_bytes(nonce.encode())
            temp.replace(challenge)
            done = api.wait_job(peer_job)
            expected = hashlib.sha256(nonce.encode()).hexdigest()
            assert answer.read_text() == expected and expected in api.call("GET",f"/jobs/{peer_job}/log?offset=0")["chunk"]
            assert done["attempt"]["process_stop"]["verified"]
            for handle in observed["peer"]:
                handle.wait_exit(5)
            live_jobs.remove((peer[0],peer_job))
            assert api.call("GET","/org/mine")["usage"]["running_jobs"] == 0
        report = {"compiler_job":compiler_job,"independent_job":peer_job,"compiler":str(compiler),
                  "processes":records,"compiler_output_sha256":hashlib.sha256(emitted).hexdigest(),
                  "stop":cancelled["attempt"]["process_stop"],"peer_computation":expected}
        (api.root/"compiler-cancel.json").write_text(json.dumps(report,indent=2),encoding="utf8")
        return report
    finally:
        try:
            for token,jid in live_jobs:
                api.token = token
                api.call("POST",f"/jobs/{jid}:cancel")
        finally:
            api.token,api.case_id,api.prefix = target


if __name__ == "__main__":
    asyncio.run(main(selected_override=scenario))
