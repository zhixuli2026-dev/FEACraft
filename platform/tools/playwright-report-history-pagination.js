async page => {
  const assert=(ok,message)=>{if(!ok)throw new Error(message);};
  const data=await page.evaluate(()=>JSON.parse(localStorage.getItem('report-history-result')));
  assert(data?.passed && new URL(page.url()).hostname==='127.0.0.1','Owned history browser required');
  const pattern='**/report/publications?**',queries=[];
  await page.route(pattern,async route=>{
    const before=new URL(route.request().url()).searchParams.get('before_generation');queries.push(before);
    if(before===null) await route.fulfill({contentType:'application/json',body:JSON.stringify(Array.from({length:20},(_,i)=>({
      id:'failed-metadata-'+i,generation:22-i,status:'failed',current:false})))});
    else await route.continue();
  });
  try {
    await page.getByRole('button',{name:'刷新列表',exact:true}).click();
    await page.locator('[data-publication-id="failed-metadata-19"]').waitFor();
    assert(await page.locator('[data-publication-id="failed-metadata-0"]').isDisabled(),'Failed reservation selectable');
    await page.getByRole('button',{name:'加载更早版本',exact:true}).click();
    await page.locator(`[data-publication-id="${data.fixture.publicationId}"]`).waitFor();
    assert(queries.includes('3'),'Cursor not based on last generation');
    assert(await page.locator('[data-publication-id]').count()===22,'Older page replaced or duplicated first page');
    await page.waitForFunction(()=>!document.querySelector('.report-history [role="status"]'));
    assert(await page.getByRole('button',{name:'加载更早版本',exact:true}).count()===0,'Terminal page still advertises more');
  } finally { await page.unroute(pattern); }
  await page.getByRole('button',{name:'刷新列表',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('.report-history [role="status"]') && document.querySelectorAll('[data-publication-id]').length===2);
  const result={passed:true,count:2,checks:['failed_reservations_disabled_and_existing_selection_preserved',
    'older_page_uses_generation_cursor_appends_without_duplicates_and_stops_at_end'],
    scope:'First page contains intercepted failed metadata; older page reads two real native publications. Actual >50 HTTP pagination has separate evidence.'};
  await page.evaluate(value=>localStorage.setItem('report-history-pagination-result',JSON.stringify(value)),result);
  return result;
}
