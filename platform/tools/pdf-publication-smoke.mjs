import assert from 'node:assert/strict';
globalThis.localStorage={getItem:()=> 'isolated-token'};
const {selectPdf,latexMainForPdf} = await import('../web/src/utils/pdfPublication.ts');
const path='E-report/latex/build/main.pdf', main='E-report/latex/main.tex';
const publication={id:'old-id',generation:1,status:'committed',current:false,stale:true};
const document={pdf:path,manifest:{main},publication};
let calls=[];
function serve(value,status=200){calls=[];globalThis.fetch=async(url,options)=>{
  calls.push(url);assert.equal(options.headers.Authorization,'Bearer isolated-token');
  return Response.json(value,{status});
};}
assert.equal(latexMainForPdf(path),main);assert.equal(latexMainForPdf('Ref/plain.pdf'),null);
assert.equal(latexMainForPdf('build/main.pdf'),'main.tex');
serve(document);let selected=await selectPdf(7,path,undefined,'old-id');
assert.match(calls[0],/main=E-report%2Flatex%2Fmain.tex&publication_id=old-id$/);
assert.match(selected.url,/latex\/artifact\?path=.*&publication_id=old-id$/);
assert.equal(selected.publication.id,'old-id');
for(const status of [404,403,409,500]){serve({},status);await assert.rejects(selectPdf(7,path,undefined,'old-id'),/无法核验/);assert.equal(calls.length,1);}
serve({},404);selected=await selectPdf(7,path);assert.equal(selected.publication,null);assert.match(selected.url,/\/raw\?path=/);
for(const [changed,message] of [
 [{...document,pdf:'other.pdf'},/PDF 发布版本信息无效/],
 [{...document,manifest:{main:'other.tex'}},/PDF 发布版本信息无效/],
 [{...document,publication:{...publication,id:'other-id'}},/发布版本信息无法核验/],
 [{...document,publication:{...publication,stale:undefined}},/发布版本信息无法核验/],
]){
 serve(changed);await assert.rejects(selectPdf(7,path,undefined,'old-id'),message);
 assert.equal(calls.length,1);
}
serve(document);selected=await selectPdf(7,'Ref/plain.pdf');assert.equal(calls.length,0);assert.equal(selected.publication,null);
await assert.rejects(selectPdf(7,'Ref/plain.pdf',undefined,'old-id'),/不属于/);
console.log('PDF_PUBLICATION_OK '+JSON.stringify({passed:true,count:6,checks:[
 'main_channel_inference','history_metadata_and_artifact_bound_to_same_id','explicit_selection_errors_never_fallback',
 'only_unpublished_default_falls_back_to_raw','wrong_path_main_id_or_state_refused','ordinary_pdf_keeps_raw_protocol']}));
