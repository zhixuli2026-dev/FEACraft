"""Workspace search through the built UI and API, with real model readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


CONTENTION = """
import asyncio
from contextvars import ContextVar
from fastapi import Request,HTTPException
from app.db import SessionLocal
from app.models import Case
from app.services import owner_storage_budget
from app import storage

original_acquire=owner_storage_budget.acquire
message_admission=ContextVar("workspace_search_message_admission",default=False)
async def observe_acquire(*args,**kwargs):
    try:
        return await original_acquire(*args,**kwargs)
    except HTTPException as error:
        if message_admission.get() and error.status_code==409 and isinstance(error.detail,dict) and error.detail.get('code')=='storage_quota_busy':
            (root/'search-storage-busy').touch()
        raise
owner_storage_budget.acquire=observe_acquire

@app.middleware("http")
async def overlap_model_with_read(request:Request,call_next):
    selected=root/'overlap-model-case'
    if not (storage._use_s3() and selected.is_file() and request.method=='POST' and request.url.path.endswith('/messages')):
        return await call_next(request)
    witness=root/'search-storage-busy'
    witness.unlink(missing_ok=True)
    ready=asyncio.Event()
    async def hold_credit():
        async with SessionLocal() as db:
            case=await db.get(Case,int(selected.read_text()))
            from app.services.case_read_capacity import admitted
            async with admitted(db,case):
                ready.set()
                for _ in range(150):
                    if witness.exists():break
                    await asyncio.sleep(.02)
                await db.rollback()
    task=asyncio.create_task(hold_credit())
    try:
        await asyncio.wait_for(ready.wait(),10)
        token=message_admission.set(True)
        try:response=await call_next(request)
        finally:message_admission.reset(token)
        from sqlalchemy import select
        from app.models import AgentEvent
        cid=int(request.url.path.split('/')[-2])
        async with SessionLocal() as db:
            messages=await db.scalars(select(AgentEvent.payload).where(AgentEvent.conversation_id==cid,AgentEvent.type=='message'))
            users=sum(json.loads(value).get('role')=='user' for value in messages)
        (root/'search-admission-status.json').write_text(json.dumps({'status':response.status_code,'busy':witness.exists(),'user_messages':users}))
        if response.status_code==200:
            assert witness.exists() and users==1
        return response
    finally:
        await task
"""


def browser_check(data):
    from playwright.sync_api import sync_playwright, expect

    root = Path(data["root"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width":1600,"height":1000},locale="zh-CN")
        try:
            page.add_init_script("localStorage.setItem('vf_token',"+json.dumps(data["token"])+")")
            page.goto(data["base"],wait_until="domcontentloaded")
            first, second = data["cases"][:2]
            page.locator('[data-case-name="'+first["name"]+'"]').click(timeout=30000)
            rebuild = page.get_by_test_id("index-rebuild")
            with page.expect_response(lambda r:r.url.endswith("/index:rebuild")) as submitted:
                rebuild.click()
            assert submitted.value.ok
            query = page.get_by_placeholder("搜索文件名/内容…")
            query.fill(data["marker"])
            query.press("Enter")
            hits = page.get_by_test_id("search-hit")
            expect(hits).to_have_count(1,timeout=20000)
            page.get_by_test_id("search-scope").select_option("workspace")
            query.press("Enter")
            expect(hits).to_have_count(2,timeout=20000)
            for case in (first,second):
                expect(hits.filter(has_text=case["name"])).to_be_visible()
            page.screenshot(path=root/"workspace-search.png")
            page.locator(f'[data-testid="search-hit"][data-case-id="{second["id"]}"]').click()
            expect(page.locator(".monaco-editor .view-lines").filter(has_text="SECONDWORKSPACEFILE").first).to_be_visible(timeout=25000)
            page.screenshot(path=root/"opened-target.png")
        finally:
            browser.close()


def scenario(api, *, store=None, **_):
    from live_llm_job_repeat_acceptance import write
    first = api.create_case("Search one")
    owner = api.token
    ws_id = api.call("GET","/workspaces")[0]["id"]
    ws_route = f"/workspaces/{ws_id}"
    cases = [first] + [api.call("POST",ws_route+"/cases",{"name":name}) for name in ("Search two","Unindexed")]
    marker = "lookup"+uuid.uuid4().hex
    target = "Ref/shared-search.txt"
    for case, suffix in zip(cases,("FIRSTWORKSPACEFILE","SECONDWORKSPACEFILE","UNINDEXEDFILE")):
        route = f'/cases/{case["id"]}'
        write(api,"PUT",route+"/file",{"path":target,"content":marker+" "+suffix})
        assert not api.call("GET",route+"/index/status")["indexed"]
    assert api.call("POST",f'/cases/{cases[1]["id"]}/index:rebuild',{})["ok"]
    public_cases = [{k:c[k] for k in ("id","name")} for c in cases]
    args = {"root":str(api.root),"base":api.base,"token":owner,"cases":public_cases,"marker":marker}
    subprocess.run([os.environ["VIBEFLOW_TEST_BROWSER_PYTHON"],str(Path(__file__).resolve()),"--browser"],
                   input=json.dumps(args),text=True,timeout=120,check=True)
    search = ws_route+"/search?q="+marker
    def found():
        result = api.call("GET",search)
        assert result["ready"] and (result["total_cases"],result["indexed_cases"]) == (3,2)
        assert [(v["case_id"],v["case_name"],v["path"],v["via"]) for v in result["hits"]] == [
            (c["id"],c["name"],target,"content") for c in cases[:2]]
        return result["hits"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b = [v.result() for v in [pool.submit(found),pool.submit(found)]]
    assert a == b and len(api.call("GET",search+"&limit=1")["hits"]) == 1
    assert not api.call("GET",search+"&content=false")["hits"]
    names = api.call("GET",ws_route+"/search?q=shared-search")["hits"]
    assert [(v["case_id"],v["via"]) for v in names] == [(c["id"],"name") for c in cases[:2]]
    for query,code in (("%20",400),("x&limit=0",422),("x"*121,422)):
        api.call("GET",ws_route+"/search?q="+query,expect=code)
    org_id = api.call("GET","/org/mine")["org"]["id"]
    email = uuid.uuid4().hex+"@example.com"
    viewer = api.call("POST","/auth/register",{"email":email,"name":"Search viewer","password":"isolated-test-123"})["token"]
    api.token = ""
    api.call("GET",search,expect=401)
    api.token = viewer
    api.call("GET",search,expect=404)
    api.token = owner
    api.call("POST",f"/org/{org_id}/members",{"email":email,"role":"viewer"},expect=201)
    api.token = viewer
    api.call("POST",f"/org/{org_id}/invitation:accept",{})
    api.token = owner
    share = {"org_id":org_id,"role":"viewer","enabled":True}
    api.call("PUT",ws_route+"/sharing",share)
    api.token = viewer
    found()
    assert api.call("GET",api.prefix+"/index/status")["indexed"]
    api.call("POST",api.prefix+"/index:rebuild",{},expect=404)
    api.token = owner
    api.call("PUT",ws_route+"/sharing",{**share,"enabled":False})
    api.token = viewer
    api.call("GET",search,expect=404)
    api.call("GET",api.prefix+"/search?q="+marker,expect=404)
    api.token = owner
    report = {"backend":"s3" if store else "fs","cases":public_cases,"concurrent_searches":2,
              "role_checks":["anonymous","foreign_owner","viewer","revoked"],"browser_opened":cases[1]["id"]}
    (api.root/"workspace-search.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    if store is not None:
        (api.root/"overlap-model-case").write_text(str(api.case_id))
    return report


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser_check(json.load(sys.stdin))
    else:
        from live_llm_job_repeat_acceptance import main
        bundle = Path(os.environ["VIBEFLOW_TEST_WEB_DIST"]).resolve(strict=True)
        assert (bundle/"index.html").is_file()
        fixture = "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
        asyncio.run(main(selected_override=scenario,fixture_override=CONTENTION+fixture))
