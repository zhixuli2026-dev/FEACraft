// Real upload/delete acceptance through a disposable ui_test_server preview.
async page => {
  page.setDefaultTimeout(15000);
  const assert=(value,message)=>{if(!value)throw new Error(message);};
  const origin=new URL(page.url());
  assert(origin.hostname==="127.0.0.1"&&!["8000","5173"].includes(origin.port),"Disposable preview required");
  const fixture=await page.evaluate(async()=>{
    const auth=await fetch("/api/v1/auth/register",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email:`tree-${Date.now()}@example.com`,name:"tree-test",password:"isolated-tree-123"})}).then(r=>r.json());
    if(!auth.token)throw new Error("Registration failed");
    localStorage.setItem("vf_token",auth.token);
    const headers={Authorization:"Bearer "+auth.token,"Content-Type":"application/json"};
    const ws=await fetch("/api/v1/workspaces",{headers}).then(r=>r.json());
    const cases=[];
    for(const name of ["tree-write-browser","tree-other-case"])cases.push(await fetch(`/api/v1/workspaces/${ws[0].id}/cases`,
      {method:"POST",headers,body:JSON.stringify({name})}).then(r=>r.json()));
    return {caseId:cases[0].id,otherId:cases[1].id};
  });
  const prefix=`/api/v1/cases/${fixture.caseId}`,other=`/api/v1/cases/${fixture.otherId}`;
  const request=(method,path,body)=>page.evaluate(async({method,path,body})=>{
    const r=await fetch(path,{method,headers:{Authorization:"Bearer "+localStorage.getItem("vf_token"),"Content-Type":"application/json"},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    return {status:r.status,body:await r.json()};
  },{method,path,body});
  const version=async(path,p=prefix)=>(await request("GET",p+"/file:version?path="+encodeURIComponent(path))).body;
  const put=(path,content)=>request("PUT",prefix+"/file",{path,content});
  const file=page.getByTestId("tree-upload-file"),field=page.getByTestId("file-operation-path"),submit=page.getByTestId("file-operation-submit");
  const inspect=async()=>{
    await page.getByTestId("file-operation-inspect").click();
    await page.getByTestId("file-target-version").waitFor();
    await page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-inspect"]')?.disabled);
  };
  const cancel=()=>page.getByTestId("file-operation-cancel").click();
  const select=async(name,buffer=Buffer.from([0,255,13,10,66]))=>{
    await file.setInputFiles({name,mimeType:"application/octet-stream",buffer});
    await field.waitFor();
  };
  const success=()=>page.getByTestId("file-operation-form").waitFor({state:"detached"});
  const checks=[],requests=[],routes=[];
  const record=req=>{if(req.url().startsWith(origin.origin+prefix)&&["PUT","POST","DELETE"].includes(req.method()))
    requests.push({method:req.method(),url:req.url()});};
  page.on("request",record);
  let release;
  const hold=async(pattern,match,afterCommit=true,abort=false)=>{
    let reached,handled,heldRequest;
    const ready=new Promise(r=>reached=r),blocked=new Promise(r=>release=r),done=new Promise(r=>handled=r);
    let used=false;
    const handler=async route=>{
      if(used||!match(route.request()))return route.continue();
      used=true;heldRequest=route.request();
      const response=afterCommit?await route.fetch():null;
      reached();await blocked;
      try{if(abort)await route.abort("failed");else if(response)await route.fulfill({response});else await route.continue();}
      finally{handled();}
    };
    await page.route(pattern,handler);routes.push([pattern,handler]);
    return {ready,getRequest:()=>heldRequest,finish:async()=>{release();await done;await page.unroute(pattern,handler);}};
  };
  const chunkPattern="**"+prefix+"/uploads/*?offset=*";
  const rawPattern="**"+prefix+"/upload?*";
  const uploadId=r=>new URL(r.url()).pathname.split("/").at(-1);
  try {
    await page.setViewportSize({width:1440,height:1000});await page.reload();
    await page.locator('[data-case-name="tree-write-browser"]').click();
    await select("cancel.bin");await cancel();await success();
    assert(!requests.some(r=>r.url.includes("/upload")),"Cancelling selected file started upload");
    assert(!(await version("Ref/cancel.bin")).exists,"Cancel created a file");
    checks.push("cancel_preserves_server_and_never_converts_cancellation_to_root_upload");

    const name="数据🙂-binary.bin",path="Ref/"+name;
    await select(name);await inspect();
    assert((await version(path)).exists===false,"Target must start absent");
    await put(path,"OTHER CREATOR");await submit.click();
    await page.getByTestId("file-operation-error").filter({hasText:"目标文件已变化"}).waitFor();
    assert(await field.inputValue()===path,"Conflict lost selected path");
    assert(requests.some(r=>r.url.includes("expected_absent=true")),"No create-only condition");
    assert((await request("GET",prefix+"/file?path="+encodeURIComponent(path))).body.kind==="binary","Binary fixture not binary");
    await inspect();const reviewed=await version(path);await put(path,"NEWER REPLACEMENT");
    await submit.click();await page.getByTestId("file-operation-error").filter({hasText:"目标文件已变化"}).waitFor();
    assert((await version(path)).content_sha256!==reviewed.content_sha256,"Stale replacement overwrote server");
    await page.screenshot({path:"tmp/mainline-N2-browser-upload-evidence/upload-conflict.png"});
    await inspect();
    const inline=await hold(rawPattern,r=>r.method()==="POST");
    const beforeUploads=requests.filter(r=>r.url.includes("/upload?")).length;
    await submit.click();await inline.ready;
    assert(await submit.isDisabled(),"Repeated inline upload enabled");
    await inline.finish();await success();
    assert(requests.filter(r=>r.url.includes("/upload?")).length===beforeUploads+1,"Duplicate inline upload sent");
    const uploaded=await page.evaluate(async path=>{
      const r=await fetch(path,{headers:{Authorization:"Bearer "+localStorage.getItem("vf_token")}});
      return [...new Uint8Array(await r.arrayBuffer())];
    },prefix+"/raw?path="+encodeURIComponent(path));
    assert(JSON.stringify(uploaded)==="[0,255,13,10,66]","Binary bytes changed");
    checks.push("binary_inline_upload_absence_hash_conflicts_keep_selection_and_confirm_once");

    await page.getByTestId("tree-delete-file").click();await field.fill(path);await inspect();
    await put(path,"NEW BEFORE DELETE");await submit.click();
    await page.getByTestId("file-operation-error").filter({hasText:"本次未删除"}).waitFor();
    assert((await version(path)).exists,"Stale delete removed newer bytes");
    await inspect();await submit.click();await success();
    assert(!(await version(path)).exists,"Reviewed deletion failed");
    checks.push("delete_rejects_changed_binary_target_then_deletes_only_reviewed_version");

    await page.getByTestId("tree-delete-file").click();await field.fill("0-CASEDICT/CASEDICT");await inspect();
    await page.getByTestId("file-operation-error").filter({hasText:"caseDict 需要通过编辑器保存"}).waitFor();
    assert(await submit.isDisabled(),"Protected dictionary alias can be deleted");
    await cancel();await success();
    await select("read-failure.bin");
    const denied=async route=>route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({detail:"temporary metadata unavailable"})});
    await page.route("**"+prefix+"/file:version?*",denied);
    await page.getByTestId("file-operation-inspect").click();
    await page.getByTestId("file-operation-error").filter({hasText:"temporary metadata unavailable"}).waitFor();
    assert(await submit.isDisabled(),"Failed version read permits unconditional upload");
    await page.unroute("**"+prefix+"/file:version?*",denied);await inspect();await cancel();await success();
    checks.push("protected_alias_and_failed_metadata_read_cannot_enable_mutation");

    const largeSize=17*1024*1024+13,large=Buffer.alloc(largeSize,73);
    await select("resumable.bin",large);await inspect();
    const dropped=await hold(chunkPattern,r=>r.method()==="PUT",true,true);
    await submit.click();await dropped.ready;
    const resumedId=uploadId(dropped.getRequest());await dropped.finish();
    await page.getByTestId("file-operation-error").filter({hasText:"网络请求中断"}).waitFor();
    assert((await request("GET",prefix+"/uploads/"+resumedId)).body.offset===4*1024*1024,"Fault did not commit exactly one chunk");
    await submit.click();await success();
    const offsets=requests.filter(r=>r.url.includes("/uploads/"+resumedId+"?offset=")).map(r=>Number(new URL(r.url).searchParams.get("offset")));
    assert(offsets.filter(v=>v===0).length===1&&offsets[1]===4*1024*1024,"Resume resent an acknowledged chunk");
    const verified=await page.evaluate(async({prefix,size})=>{
      const r=await fetch(prefix+"/raw?path=Ref/resumable.bin",{headers:{Authorization:"Bearer "+localStorage.getItem("vf_token")}});
      const data=new Uint8Array(await r.arrayBuffer());return data.length===size&&data.every(b=>b===73);
    },{prefix,size:largeSize});
    assert(verified,"Resumed upload bytes differ");
    checks.push("large_upload_recovers_lost_chunk_response_from_server_offset_without_duplicate_bytes");

    await select("concurrent-large.bin",large);await inspect();
    const completing=await hold("**"+prefix+"/uploads/*:complete",r=>r.method()==="POST",false);
    await submit.click();await completing.ready;
    const staleId=uploadId(completing.getRequest()).replace(":complete","");
    await put("Ref/concurrent-large.bin","CONCURRENT OWNER");await completing.finish();
    await page.getByTestId("file-operation-error").filter({hasText:"目标文件已变化"}).waitFor();
    assert((await request("GET",prefix+"/uploads/"+staleId)).body.offset===largeSize,"Conflict deleted staged bytes");
    assert((await version("Ref/concurrent-large.bin")).size===16,"Conflict replaced newer file");
    await cancel();await success();assert((await request("GET",prefix+"/uploads/"+staleId)).status===404,"Cancel left obsolete session");
    await select("concurrent-large.bin",large);await inspect();
    await submit.click();await success();assert((await version("Ref/concurrent-large.bin")).size===largeSize,"Reviewed large retry failed");
    checks.push("large_publish_conflict_retains_chunks_until_explicit_cancel_and_fresh_review");

    await select("uncertain-complete.bin",large);await inspect();
    const uncertain=await hold("**"+prefix+"/uploads/*:complete",r=>r.method()==="POST",true,true);
    await submit.click();await uncertain.ready;await uncertain.finish();
    await page.getByTestId("file-operation-error").filter({hasText:"网络请求中断"}).waitFor();
    assert((await version("Ref/uncertain-complete.bin")).size===largeSize,"Complete response loss fixture did not commit");
    await put("Ref/uncertain-complete.bin","LATER EDIT");
    await submit.click();await success();
    assert((await version("Ref/uncertain-complete.bin")).size===10,"Receipt replay replaced later edit");
    checks.push("lost_completion_response_resolves_receipt_without_overwriting_later_edit");

    await select("stopped.bin",large);await inspect();
    const stopped=await hold(chunkPattern,r=>r.method()==="PUT");
    await submit.click();await stopped.ready;const stoppedId=uploadId(stopped.getRequest());
    await page.getByRole("button",{name:"暂停上传",exact:true}).click();await stopped.finish();
    await page.getByTestId("file-operation-notice").filter({hasText:"已暂停"}).waitFor();
    assert(!(await version("Ref/stopped.bin")).exists,"Stop published target");
    assert((await request("GET",prefix+"/uploads/"+stoppedId)).body.offset===4*1024*1024,"Pause discarded progress");
    assert(await field.inputValue()==="Ref/stopped.bin","Stop discarded local selection");
    await cancel();await success();
    assert((await request("GET",prefix+"/uploads/"+stoppedId)).status===404,"Explicit cancel retained session");
    checks.push("pause_retains_progress_and_explicit_cancel_removes_session_without_publishing");

    await select("switch-case.bin",large);await inspect();
    const switched=await hold(chunkPattern,r=>r.method()==="PUT");
    await submit.click();await switched.ready;const switchedId=uploadId(switched.getRequest());
    await page.locator('[data-case-name="tree-other-case"]').click();await switched.finish();
    assert((await request("GET",prefix+"/uploads/"+switchedId)).body.offset===4*1024*1024,"Case switch discarded progress");
    assert(!(await version("Ref/switch-case.bin")).exists&&!(await version("Ref/switch-case.bin",other)).exists,"Case switch published wrong target");
    assert(await page.getByTestId("file-operation-form").count()===0&&await page.getByTestId("file-operation-error").count()===0,
      "Old upload contaminated new case UI");
    checks.push("case_switch_preserves_old_session_without_new_case_mutation");
    await page.locator('[data-case-name="tree-write-browser"]').click();
    await page.getByTestId("file-resume-source").waitFor({state:"attached"});
    await page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-cancel"]')?.disabled);
    await cancel();await success();
    const longName="long-".repeat(22)+"资料🙂.bin";
    await select(longName);
    const late=await hold("**"+prefix+"/file:version?*",r=>r.method()==="GET");
    await page.getByTestId("file-operation-inspect").click();await late.ready;
    const overflow=await page.getByTestId("case-file-actions").evaluate(e=>e.scrollWidth>e.clientWidth+1);
    assert(!overflow,"Long selected filename overflows action panel");
    await page.screenshot({path:"tmp/mainline-N2-browser-upload-evidence/tree-long-filename.png"});
    await page.locator('[data-case-name="tree-other-case"]').click();await late.finish();
    assert(await page.getByTestId("file-operation-form").count()===0,"Late metadata injected old case form");
    checks.push("long_filename_wraps_and_late_metadata_cannot_rebind_other_case");
    await page.locator('[data-case-name="tree-write-browser"]').click();
    await page.getByTestId("case-file-actions").waitFor();
    await page.screenshot({path:"tmp/mainline-N2-browser-upload-evidence/tree-uploads-complete.png"});
    const result={passed:true,count:checks.length,checks,caseId:fixture.caseId,browser:page.context().browser().version(),
      real_api:true,isolated:true,large_file_bytes:largeSize};
    await page.evaluate(r=>window.__treeWriteEvidence=r,result);return result;
  } catch(error) {
    await page.evaluate(result=>window.__treeWriteEvidence=result,{passed:false,checks,error:String(error)});throw error;
  } finally {
    release?.();for(const [pattern,handler] of routes)await page.unroute(pattern,handler);
    page.off("request",record);
  }
}
