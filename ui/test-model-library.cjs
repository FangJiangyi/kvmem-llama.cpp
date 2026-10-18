/* Exercise the real model-library API and browser UI without loading any model. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
module.exports=async({page,api,state})=>{
 const library=path.join(state,'library with spaces'),model=path.join(library,'library-model.ninfer');
 fs.mkdirSync(library);fs.writeFileSync(model,'metadata-only fixture');
 fs.writeFileSync(path.join(library,'empty.ninfer'),'');fs.writeFileSync(path.join(library,'other-format.gguf'),'metadata-only fixture');
 const open=()=>page.locator('[data-scan-models]').click();
 const close=()=>page.locator('.l-model-dialog .l-heading button').click();
 const ready=()=>page.waitForFunction(()=>document.querySelector('[data-scan-status]').textContent.startsWith('找到'));
 // Newly copied UI assets must explain when an older controller is still running.
 const legacy={...(await api('info')).body};delete legacy.model_library_version;
 await page.route('**/api/launcher/info',route=>route.fulfill({json:legacy}));
 await page.reload();await page.locator('[data-param=Model]').waitFor();
 await open();await page.locator('[data-dialog=error]').waitFor();
 assert.ok((await page.locator('[data-dialog=error]').textContent()).includes('重新启动控制台'));
 await page.locator('[data-dialog=error] [data-dialog-close]').click();
 await page.unroute('**/api/launcher/info');await page.reload();await page.locator('[data-param=Model]').waitFor();
 const original=await page.locator('[data-param=Model]').inputValue();
 await page.route('**/api/launcher/browse-model-folder',route=>route.fulfill({status:400,json:{error:'无法打开系统选择窗口。请重试，或直接粘贴模型文件或目录的完整路径。'}}));
 await open();await ready();
 await page.locator('[data-scan-folder]').click();
 await page.waitForFunction(()=>document.querySelector('[data-scan-folder-error]').textContent.includes('无法打开系统选择窗口'));
 assert.ok(!(await page.locator('[data-scan-folder-error]').textContent()).includes('\ufffd'));
 assert.equal(await page.locator('[data-param=Model]').inputValue(),original);
 // Pasting a quoted Windows path is a complete fallback when native dialogs fail.
 await page.locator('[data-scan-directory]').fill('"'+library+'"');
 await page.locator('[data-scan-submit]').click();await ready();
 await page.waitForFunction(p=>document.querySelector('[data-scan-saved]').value===p,library);
 assert.equal(await page.locator('[data-model-candidate]').count(),2);
 assert.ok((await page.locator('[data-scan-format-hint]').textContent()).includes('1 个 .gguf'));
 const empty=page.locator('[data-model-candidate]').filter({hasText:'empty.ninfer'});
 assert.equal(await empty.isDisabled(),true);assert.ok((await empty.textContent()).includes('文件为空'));
 const remembered=await api('model-directories');assert.equal(remembered.code,200);
 assert.ok(remembered.body.directories.some(item=>item.path===library));
 if(process.env.SCREENSHOT_DIR){
  await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'model-library-desktop.png')});
  await page.setViewportSize({width:390,height:900});
  const box=await page.locator('.l-model-dialog').boundingBox();
  assert.ok(box.x>=0&&box.x+box.width<=391,'model dialog overflows mobile viewport');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile page overflow');
  await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'model-library-mobile.png')});
  await page.setViewportSize({width:1920,height:1080});
 }
 await page.locator('[data-scan-filter]').fill('library-model');
 assert.equal(await page.locator('[data-model-candidate]').count(),1);
 await close();await page.reload();await page.locator('[data-param=Model]').waitFor();
 await open();await ready();
 assert.ok(await page.locator('[data-scan-saved] option').evaluateAll((items,p)=>items.some(item=>item.value===p),library));
 await page.locator('[data-scan-saved]').selectOption(library);await ready();
 await page.locator('[data-model-candidate]').filter({hasText:'library-model.ninfer'}).click();
 assert.equal(await page.locator('[data-param=Model]').inputValue(),model);
 assert.equal((await api('status')).body.state,'stopped','scanning and selecting must not start inference');
 await open();await ready();await page.locator('[data-scan-saved]').selectOption(library);await ready();
 await page.locator('[data-scan-forget]').click();
 await page.waitForFunction(p=>!Array.from(document.querySelector('[data-scan-saved]').options).some(item=>item.value===p),library);await ready();
 assert.ok(!(await api('model-directories')).body.directories.some(item=>item.path===library));
 assert.ok(fs.existsSync(model),'forgetting a library must keep model files');
 await close();await page.unroute('**/api/launcher/browse-model-folder');
 // A late scan response from a closed dialog must never replace the next scan's results.
 let calls=0;
 await page.route('**/api/launcher/scan-models',async route=>{
  const call=++calls;if(call===1)await delay(600);
  try{await route.fulfill({json:{models:[{path:call===1?'C:/stale.ninfer':'C:/fresh.ninfer',name:call===1?'stale.ninfer':'fresh.ninfer',bytes:1024,format:'ninfer'}],roots:[],limited:false,skipped:0}});}catch{}
 });
 await open();for(let i=0;i<100&&calls<1;i++)await delay(10);assert.equal(calls,1);
 await close();await open();await page.locator('[data-model-candidate]').filter({hasText:'fresh.ninfer'}).waitFor();
 await delay(700);assert.equal(await page.locator('[data-model-candidate]').filter({hasText:'stale.ninfer'}).count(),0);
 await close();await page.unroute('**/api/launcher/scan-models');
 console.log('Model library PASS (readable picker error, pasted path, remembered directories, filter, format hint, empty file, forget, stale response).');
};
