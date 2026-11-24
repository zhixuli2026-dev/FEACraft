async page => {
  const assert=(value,message)=>{if(!value)throw Error(message);};
  const fixture=await page.evaluate(()=>JSON.parse(localStorage.getItem('derivative-history-fixture')));
  const origin=new URL(page.url()).origin,token=await page.evaluate(()=>localStorage.getItem('vf_token'));
  const headers={Authorization:'Bearer '+token},prefix=`/api/v1/cases/${fixture.caseId}`;
  const checks=[],area=page.getByTestId('report-derivatives');
  // First page is deliberately synthetic; the older page is a real owned API response.
  const pattern='**/report/derivatives?**',requests=[];
  const failed=Array.from({length:20},(_,i)=>({id:'failed-metadata-'+i,family:'ppt',output:'failed/output-'+i+'.pptx',generation:i+10,status:'failed',current:false}));
  let failOlder=true;
  await page.route(pattern,async route=>{
    const url=new URL(route.request().url());requests.push(url.searchParams.get('before_id'));
    if(!url.searchParams.has('before_id'))return route.fulfill({json:failed});
    assert(url.searchParams.get('before_id')==='failed-metadata-19','Wrong pagination cursor');
    if(failOlder)return route.fulfill({status:503,json:{detail:'temporary pagination failure'}});
    url.searchParams.delete('before_id');return route.fulfill({response:await route.fetch({url:url.toString()})});
  });
  try {
    await area.getByRole('combobox').selectOption('ppt');
    await area.locator('[data-derivative-id="failed-metadata-19"]').waitFor();
    assert(await area.locator('[data-derivative-id="failed-metadata-0"]').isDisabled(),'Failed reservation is selectable');
    await area.getByRole('button',{name:'加载更早产物'}).click();await area.getByRole('alert').waitFor();failOlder=false;
    await area.getByRole('button',{name:'重试列表'}).click();await area.locator(`[data-derivative-id="${fixture.versions.ppt[0]}"]`).waitFor();
    assert(requests.slice(-2).every(value=>value==='failed-metadata-19'),'Pagination retry reset its cursor');
    assert(await area.getByRole('button',{name:'加载更早产物'}).count()===0,'Last page still loads more');
    assert(await area.locator('[data-derivative-id="failed-metadata-0"]').count()===1,'Pagination lost or duplicated initial rows');
  }finally{await page.unroute(pattern);}
  checks.push('disabled_failed_page_and_retry_append_real_older_versions_with_cursor');
  await area.getByRole('combobox').selectOption('draft');
  const id=fixture.versions['E-report/report.md'][0],selected='**/report/derivative?publication_id='+id;
  let release,started;const held=new Promise(resolve=>release=resolve),arrival=new Promise(resolve=>started=resolve);
  await page.route(selected,async route=>{started();await held;try{await route.continue();}catch{}});
  try {
    await area.locator(`[data-derivative-id="${id}"]`).click();await arrival;
    const spaces=await (await page.request.get(origin+'/api/v1/workspaces',{headers})).json();
    const response=await page.request.post(origin+'/api/v1/workspaces/'+spaces[0].id+'/cases',{headers,data:{name:'history-empty-'+Date.now()}});
    assert(response.ok(),'Create isolated second case');const other=await response.json();
    await page.evaluate(async c=>{const {useStore}=await import('/src/store.ts');await useStore.getState().selectCase(c);},other);
    release();await area.getByRole('button',{name:'草稿、页码 PDF 与 PPT 历史'}).click();
    await area.getByText('此类型暂无已记录的产物版本。').waitFor();
    assert(await area.getByTestId('derivative-state').count()===0,'Late selected version entered another case');
    assert(await area.getByRole('button',{name:'下载所选版本原件'}).count()===0,'Another case can download old selection');
  }finally{release();await page.unroute(selected);}
  checks.push('case_switch_cancels_selected_read_and_empty_case_stays_empty');
  await page.locator('[data-case-name="report-derivative-history"]').click();
  await area.getByRole('button',{name:'草稿、页码 PDF 与 PPT 历史'}).click();
  await area.getByRole('combobox').selectOption('draft');await area.locator(`[data-derivative-id="${id}"]`).click();
  await area.getByLabel('只读历史草稿').waitFor();
  const sizes=[{width:1440,height:1000,name:'desktop'},{width:1100,height:800,name:'narrow'}];
  const geometry=[];
  for(const size of sizes){
    await page.setViewportSize({width:size.width,height:size.height});
    await area.getByRole('region',{name:'所选产物版本'}).evaluate(node=>node.scrollIntoView({block:'start'}));
    geometry.push(await area.evaluate(node=>({width:innerWidth,overflow:node.scrollWidth>node.clientWidth+1})));
    await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-derivative-history-'+size.name+'-review.png'});
  }
  assert(geometry.every(row=>!row.overflow),'New history section overflows');
  const result={passed:true,count:checks.length,checks,geometry,scope:'Mock first-page pagination plus real older API page; real cross-case switch'};
  await page.evaluate(value=>localStorage.setItem('derivative-history-races',JSON.stringify(value)),result);
  return result;
}
