// Actual standalone HTML rendering of the S3 engineering export; no mocked responses.
const fs = require('fs');
const path = require('path');
const {pathToFileURL} = require('url');
const crypto = require('crypto');
const {chromium} = require('C:/Users/Administrator/AppData/Local/npm-cache/_npx/314abffa6aaf2cd1/node_modules/playwright');
const root = path.resolve(__dirname, '../..');
const review = JSON.parse(fs.readFileSync(path.resolve(root, process.argv[2]), 'utf8'));
if (review.classification !== 'no_llm') throw Error('Unreviewed entry');
for (const [name, digest] of Object.entries(review.sources)) {
  if (crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex') !== digest) throw Error('Changed source: ' + name);
}
const directory = path.resolve(root, process.argv[3]);
const acceptance = JSON.parse(fs.readFileSync(path.join(directory, 'result.json'), 'utf8'));
if (!acceptance.passed) throw Error('Actual S3 acceptance must pass first');
(async () => {
  const result = {passed:false, real_model_calls:0, mock_llm_calls:0, requests:[], page_errors:[]};
  const browser = await chromium.launch({headless:true, executablePath:'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    page.on('request', request => {if (/^https?:/.test(request.url())) result.requests.push(request.url());});
    page.on('pageerror', error => result.page_errors.push(error.message));
    await page.goto(pathToFileURL(path.join(directory,'report.html')).href);
    await page.evaluate(() => document.fonts.ready);
    result.rendered = await page.evaluate(() => {
      const row = [...document.querySelectorAll('tr')].find(row => row.cells[0]?.textContent.trim() === 'mean.T@1' && row.cells[1]?.textContent.trim() === 'volumeAverages');
      return {heading:document.querySelector('h1')?.textContent, heading_color:getComputedStyle(document.querySelector('h1')).color,
        temperature:Number(row?.cells[3]?.textContent),unit:row?.cells[4]?.textContent.trim()};
    });
    if (result.rendered.heading_color !== 'rgb(19, 92, 114)' || Math.abs(result.rendered.temperature-350)>1e-6 || result.rendered.unit!=='K') throw Error('Rendered engineering report differs');
    if (result.requests.length || result.page_errors.length) throw Error('Unexpected network or browser error');
    await page.screenshot({path:path.join(directory,'engineering-report.png'),fullPage:true});
    result.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(directory,'render-result.json'),JSON.stringify(result,null,2)+'\n');
  }
  console.log(JSON.stringify(result));
})().catch(error => {console.error(error.message);process.exitCode=1;});
