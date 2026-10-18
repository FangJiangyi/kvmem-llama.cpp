/* Configuration stays separate from the running service. No shell text is accepted. */
"use strict";
(() => {
  const root = document.querySelector('[data-launch-root]');
  const tr = (zh,en) => window.monitorI18n.language === 'en' ? en : zh;
  let info, busy=false, activeTab=0, runtime=null, startPending=false, launchFailure="";
  const perBackend = {};
  // Credentials are kept in memory only, separate from drafts and templates.
  const launchKeys = {};
  let pickingModel = false;
  function recentModels(backend) {
    try {
      const items = JSON.parse(localStorage.getItem('kvmem-recent-models-'+backend) || '[]');
      return Array.isArray(items) ? items.filter(x => typeof x === 'string' && x.length < 8192).slice(0,8) : [];
    } catch { return []; }
  }
  function rememberModel(backend, path) {
    try { localStorage.setItem('kvmem-recent-models-'+backend, JSON.stringify([path,...recentModels(backend).filter(x=>x!==path)].slice(0,8))); } catch {}
  }
  let selectedPreset = '', presetUndo = null;
  const DRAFT_KEY='kvmem-launch-draft-v2', TEMPLATE_KEY='kvmem-launch-templates-v1';
  let saveTimer=null, dirty=false, saveError=false, templateTab='builtin';
  // v0.18.0 Windows NInfer recipe, including the fd77613 release launcher update.
  // Machine-specific paths and GPU placement are selected locally.
  const presets = [
    {id:'ninfer-v018', title:['NInfer · 0.18.0 默认','NInfer · 0.18.0 default'], note:['200K 上下文 · 36K 历史 KV · 12 GiB RAM · 图像 / MTP','200K context · 36K history KV · 12 GiB RAM · Vision / MTP'], parameters:{Backend:'ninfer',Context:204800,Budget:36864,Reserve:16384,HostMiB:12288,Prefill:256,MaxTokens:16384,SinkTokens:12288,RecentTokens:1024,KvType:'int8',Concurrency:1,RetainedSessions:1,MtpDrafts:4,AdaptiveMtp:true,NgramDrafts:31,NgramMinMatch:12,Vision:true,VisionResidency:'cpu',VisionTokens:1024,ThinkingBudget:8192,NoLrfu:false,LrfuLambda:0.5,LrfuTopFraction:0.9,ScoreAggregation:'max',DeviceProfile:'off',FastPrefill:false}},
  ];
  const defaults={Backend:'ninfer',Model:'',Worker:'',Gpu:'',Context:262144,Budget:32768,HostMiB:16384,Reserve:1024,Prefill:128,MaxTokens:1024,Port:18200,ListenHost:'127.0.0.1',SinkTokens:12288,RecentTokens:1024};
  // Switching engines keeps the current common settings. Model assets, native
  // formats, feature overrides and raw arguments retain each backend's draft.
  const sharedSettings=['Gpu','Context','Budget','Reserve','HostMiB','Prefill','MaxTokens',
    'Port','ListenHost','SinkTokens','RecentTokens','RetainedSessions','Concurrency',
    'DiskEnabled','DiskPath','DiskMiB','NoLrfu','LrfuLambda','LrfuTopFraction','ScoreAggregation',
    'ModelAlias','LogLevel'];
  const names={llamacpp:'llama.cpp',ninfer:'NInfer'};
  const groups=[['缓存与历史','Cache & history'],['硬件与加速','Hardware & acceleration'],['生成与思考','Generation & thinking'],['模板与图像','Templates & vision'],['服务与并发','Service & concurrency']];
  const make=(tag,attrs={},text)=>{const el=document.createElement(tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));if(text!==undefined)el.textContent=text;return el;};
  let openHelp=null,helpOpenTimer=null,helpCloseTimer=null,helpId=0;
  function closeHelp(){
    clearTimeout(helpOpenTimer);clearTimeout(helpCloseTimer);
    if(!openHelp)return;
    const current=openHelp;openHelp=null;current.pinned=false;
    current.button.setAttribute('aria-expanded','false');
    if(current.tip.hidePopover&&current.tip.matches(':popover-open'))current.tip.hidePopover();
    current.tip.hidden=true;
  }
  function showHelp(item){
    clearTimeout(helpOpenTimer);clearTimeout(helpCloseTimer);
    if(!item.button.isConnected)return;
    item.reason.textContent=item.input?.dataset.disabledReason||'';item.reason.hidden=!item.reason.textContent;
    if(openHelp!==item){closeHelp();openHelp=item;item.tip.hidden=false;if(item.tip.showPopover)item.tip.showPopover();}
    item.button.setAttribute('aria-expanded','true');
    const anchor=item.button.getBoundingClientRect(),box=item.tip.getBoundingClientRect(),edge=12;
    item.tip.style.left=Math.max(edge,Math.min(anchor.left,innerWidth-box.width-edge))+'px';
    const below=anchor.bottom+8,top=below+box.height<=innerHeight-edge?below:anchor.top-box.height-8;
    item.tip.style.top=Math.max(edge,Math.min(top,innerHeight-box.height-edge))+'px';
  }
  function help(parent,title,text,input=null){
    const id='launch-tooltip-'+(++helpId),button=make('button',{type:'button',class:'l-help','aria-label':tr('关于：','About: ')+title,'aria-describedby':id,'aria-expanded':'false'},'i');
    const tip=make('div',{id,class:'l-help-popover',role:'tooltip',popover:'manual',hidden:''}),reason=make('p',{class:'l-help-reason',hidden:''});tip.append(make('strong',{},title),make('p',{},text),reason);root.append(tip);parent.append(button);
    if(input)input.setAttribute('aria-describedby',id);
    const item={button,tip,input,reason,pinned:false,hover:false,focused:false};
    const leave=()=>{clearTimeout(helpOpenTimer);clearTimeout(helpCloseTimer);helpCloseTimer=setTimeout(()=>{if(openHelp===item&&!item.hover&&!item.focused&&!item.pinned)closeHelp();},140);};
    button.addEventListener('pointerenter',()=>{item.hover=true;clearTimeout(helpCloseTimer);helpOpenTimer=setTimeout(()=>showHelp(item),180);});
    button.addEventListener('pointerleave',()=>{item.hover=false;leave();});
    tip.addEventListener('pointerenter',()=>{item.hover=true;clearTimeout(helpCloseTimer);});
    tip.addEventListener('pointerleave',()=>{item.hover=false;leave();});
    button.addEventListener('focus',()=>{item.focused=true;showHelp(item);});
    button.addEventListener('blur',()=>{item.focused=false;leave();});
    button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();if(openHelp===item&&item.pinned)closeHelp();else{showHelp(item);item.pinned=true;}});
    return button;
  }
  document.addEventListener('pointerdown',event=>{if(openHelp&&!openHelp.button.contains(event.target)&&!openHelp.tip.contains(event.target))closeHelp();},true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&openHelp){closeHelp();event.preventDefault();event.stopPropagation();}},true);
  window.addEventListener('scroll',event=>{if(openHelp&&event.target!==openHelp.tip)closeHelp();},true);
  window.addEventListener('resize',closeHelp);
  document.addEventListener('change',event=>{if(event.target.matches('[data-density-select]'))closeHelp();});
  function captureDraft(){
    const parameters={};
    root.querySelectorAll('[data-param]').forEach(el=>{
      if((el.disabled&&el.dataset.keepDraft!=='true')||el.dataset.secret==='true')return;
      const value=el.type==='checkbox'?(el.dataset.inverted==='true'?!el.checked:el.checked):el.multiple?Array.from(el.selectedOptions).map(x=>x.value).join(','):el.type==='number'&&el.value!==''?Number(el.value):el.value;
      parameters[el.dataset.param]=value;
    });
    const raw={StageLayers:String(parameters.StageLayers||''),WorkerArgs:root.querySelector('[data-native]')?.value||'[]'};
    parameters.StageLayers=raw.StageLayers.trim()?raw.StageLayers.split(',').map(x=>Number(x.trim())).filter(Number.isFinite):[];
    try{parameters.WorkerArgs=JSON.parse(raw.WorkerArgs);}catch{parameters.WorkerArgs=[];}
    return {version:2,parameters,raw};
  }
  function usableDraft(value){return value&&value.version===2&&value.parameters&&typeof value.parameters==='object'&&info.backends.includes(value.parameters.Backend)&&Array.isArray(value.parameters.StageLayers||[])&&(!value.raw||typeof value.raw==='object');}
  function draftStatus(){
    const el=root.querySelector('[data-draft-state]');if(!el)return;
    el.textContent=saveError?tr('保存失败，点击重试','Save failed. Click to retry'):dirty?tr('正在保存编辑…','Saving edits…'):tr('编辑已自动保存 · 下次启动生效','Edits saved automatically · Apply on next launch');
    el.classList.toggle('l-error',saveError);el.disabled=!saveError;
  }
  function saveEditing(){
    clearTimeout(saveTimer);if(!dirty&&!saveError)return;
    try{localStorage.setItem(DRAFT_KEY,JSON.stringify(captureDraft()));dirty=false;saveError=false;}
    catch{saveError=true;}
    draftStatus();
  }
  function scheduleSave(){dirty=true;saveError=false;draftStatus();clearTimeout(saveTimer);saveTimer=setTimeout(saveEditing,400);}
  function restoreDraft(draft){
    render(draft.parameters);
    if(draft.raw){const layers=root.querySelector('[data-param=StageLayers]');if(layers&&typeof draft.raw.StageLayers==='string'){layers.value=draft.raw.StageLayers;layers.dispatchEvent(new Event('input'));}const native=root.querySelector('[data-native]');if(native&&typeof draft.raw.WorkerArgs==='string')native.value=draft.raw.WorkerArgs;}
    draftStatus();
  }
  root.addEventListener('input',event=>{if(event.target.matches('[data-param], [data-native]')){syncDependencies();scheduleSave();}});
  root.addEventListener('change',event=>{if(event.target.matches('[data-param], [data-native]')){syncDependencies();scheduleSave();}});
  window.addEventListener('pagehide',saveEditing);
  function readTemplates(){
    const values=JSON.parse(localStorage.getItem(TEMPLATE_KEY)||'[]');
    if(!Array.isArray(values))throw Error(tr('个人模板数据无法读取','Cannot read personal templates'));
    return values.filter(x=>x&&typeof x.id==='string'&&typeof x.name==='string'&&usableDraft(x.draft)).slice(0,100).map(x=>{delete x.draft.parameters.ApiKey;return x;});
  }
  function writeTemplates(values){if(values.length>100)throw Error(tr('最多保存 100 个模板','Up to 100 templates can be saved'));localStorage.setItem(TEMPLATE_KEY,JSON.stringify(values));}
  function dialog(title,kind){
    closeHelp();
    const el=make('dialog',{class:'l-dialog','data-dialog':kind,'aria-label':title});
    const head=make('div',{class:'l-heading'}),close=make('button',{type:'button',class:'s-button','data-dialog-close':''},tr('关闭','Close'));
    head.append(make('h2',{class:'s-title'},title),close);el.append(head);root.append(el);
    close.onclick=()=>el.close();el.addEventListener('close',()=>el.remove());el.showModal();return el;
  }
  function showError(error){const el=dialog(tr('操作未完成','Action could not complete'),'error');el.append(make('p',{class:'l-error',role:'alert'},error.message||String(error)));}
  function refreshTemplates(){const current=root.querySelector('.l-presets');if(current){const side=current.parentElement;current.querySelectorAll('.l-help').forEach(button=>{if(openHelp?.button===button)closeHelp();document.getElementById(button.getAttribute('aria-describedby'))?.remove();});current.remove();presetPanel(side);}}
  function templateEditor(mode='create',item=null){
    const snapshot=captureDraft(),title=mode==='delete'?tr('删除模板','Delete template'):mode==='rename'?tr('重命名模板','Rename template'):mode==='update'?tr('更新模板','Update template'):tr('另存为模板','Save as template');
    const el=dialog(title,'template'),form=make('form'),error=make('p',{class:'l-error',role:'alert'});
    const label=make('label',{class:'l-field'},tr('模板名称','Template name')),name=make('input',{required:'',maxlength:'80','data-template-name':''});
    name.value=item?.name||'';label.append(name);
    const descLabel=make('label',{class:'l-field'},tr('说明（可选）','Description (optional)')),description=make('textarea',{maxlength:'240',rows:'2','data-template-description':''});description.value=item?.description||'';descLabel.append(description);
    if(mode==='delete')form.append(make('p',{},tr(`删除“${item.name}”？已填入编辑区的配置不受影响。`,`Delete “${item.name}”? The current draft will remain unchanged.`)));
    else form.append(label,descLabel,make('p',{class:'l-muted'},mode==='rename'?tr('仅修改名称和说明。','Only the name and description change.'):tr('保存当前模型路径、显卡顺序和参数；不保存 API 密钥。模板保存在当前浏览器，保存不会启动服务。','Save the current model path, GPU order and settings, excluding the API key. Templates stay in this browser; saving does not start a service.')));
    const submit=make('button',{type:'submit',class:'s-button l-primary','data-template-confirm':''},mode==='delete'?tr('删除模板','Delete template'):mode==='update'?tr('确认更新','Update template'):tr('保存模板','Save template'));
    form.append(error,submit);el.append(form);if(mode!=='delete')name.focus();
    form.onsubmit=event=>{event.preventDefault();try{
      const list=readTemplates(),index=item?list.findIndex(x=>x.id===item.id):-1;
      if(item&&index<0)throw Error(tr('模板已不存在，请重新打开列表','Template no longer exists. Reopen the list.'));
      const trimmed=name.value.trim();if(mode!=='delete'&&!trimmed)throw Error(tr('请填写模板名称','Enter a template name'));
      if(mode!=='delete'&&list.some(x=>x.id!==item?.id&&x.name.toLocaleLowerCase()===trimmed.toLocaleLowerCase()))throw Error(tr('已有同名模板，请使用其他名称','A template with this name already exists'));
      if(mode==='delete')list.splice(index,1);
      else{const entry={id:item?.id||crypto.randomUUID(),name:trimmed,description:description.value.trim(),draft:mode==='rename'?item.draft:snapshot};if(index<0)list.push(entry);else list[index]=entry;}
      writeTemplates(list);templateTab='mine';el.close();refreshTemplates();message(mode==='delete'?tr('模板已删除。','Template deleted.'):tr('模板已保存到“我的模板”。','Saved to My templates.'));
    }catch(e){error.textContent=e.message;}};
  }
  function focusSetting(key){
    let el=key==='WorkerArgs'?root.querySelector('[data-native]'):root.querySelector(`[data-param=${key}]`);
    if(key==='Gpu')el=root.querySelector('[data-gpu-choice]')||el;
    const panel=el?.closest('[role=tabpanel]');if(panel)root.querySelector(`[aria-controls=${panel.id}]`)?.click();
    const details=el?.closest('details');if(details)details.open=true;
    el?.scrollIntoView({block:'center',behavior:'smooth'});el?.focus();
  }
  function launchData(){
    const data=collect(),p=data.parameters;
    if(p.Backend==='llamacpp'&&info.kv_cache_version!==1)throw Error(tr('K/V 独立格式设置已更新，请先关闭原启动窗口并重新启动控制台，然后刷新页面。','Independent K/V settings have been updated. Close the original startup window, restart the console, then refresh this page.'));
    const disk=root.querySelector('[data-param=DiskEnabled]');
    if(disk?.checked&&!disk.disabled){
      if(!p.DiskPath?.trim())throw Error(tr('启用 SSD 缓存后，请填写 SSD 缓存目录。','Enter an SSD cache directory when SSD cache is enabled.'));
      if(p.DiskMiB!==undefined&&p.DiskMiB<=0)throw Error(tr('启用 SSD 缓存后，SSD 缓存预算必须大于 0。','The SSD cache budget must be positive when SSD cache is enabled.'));
    }else{delete p.DiskPath;delete p.DiskMiB;}
    if(!p.Vision){delete p.VisionResidency;delete p.VisionTokens;}
    return data;
  }
  async function checkConfiguration(){
    const el=dialog(tr('检查配置','Check configuration'),'check'),summary=make('p',{'data-check-summary':'',role:'status'},tr('正在检查…','Checking…')),list=make('div',{class:'l-check-list'});el.append(summary,list);
    const errors=[];
    function row(label,value,key,ok=true){const card=make('div',{class:'l-check-row'});card.append(make('strong',{},label),make('span',{class:ok?'':'l-error'},String(value)));if(key){const fix=make('button',{type:'button',class:'s-button'},tr('前往修改','Edit setting'));fix.onclick=()=>{el.close();focusSetting(key);};card.append(fix);}list.append(card);if(!ok)errors.push(label);}
    let data;
    try{
      data=launchData();const p=data.parameters;
      row(tr('推理引擎','Backend'),names[p.Backend]||p.Backend,'Backend');
      row(tr('模型文件','Model file'),p.Model||tr('请选择模型','Choose a model'),'Model',!!p.Model);
      const ids=(p.Gpu||'').split(',').filter(Boolean),missing=ids.filter(id=>!info.gpus.some(g=>g.uuid===id));
      const gpuIssue=root.querySelector('[data-gpu-choice]')?.validationMessage||'';
      const gpuSummary=ids.length?ids.map((id,i)=>`${i+1}. ${info.gpus.find(g=>g.uuid===id)?.name||id}${p.StageLayers?.[i]?tr(` · ${p.StageLayers[i]} 层`,` · ${p.StageLayers[i]} layers`):''}`).join('\n'):tr('请选择显卡','Choose a GPU');
      row(tr('显卡顺序','GPU order'),gpuSummary+(gpuIssue?'\n'+gpuIssue:''),'Gpu',ids.length>0&&!missing.length&&!gpuIssue);
      row(tr('上下文 / GPU 历史 / RAM 缓存','Context / GPU history / RAM cache'),`${p.Context??'—'} tokens / ${p.Budget??'—'} tokens / ${p.HostMiB??'—'} MiB`,'Context');
      row(tr('KV 缓存格式','KV cache format'),p.Backend==='llamacpp'?`K: ${p.KvTypeK||'—'} / V: ${p.KvTypeV||'—'}`:p.KvType||'—',p.Backend==='llamacpp'?'KvTypeK':'KvType');
      for(const input of root.querySelectorAll('form [data-param]'))if(input.dataset.param!=='Gpu'&&input.willValidate&&!input.checkValidity())row(input.getAttribute('aria-label')||input.dataset.param,input.validationMessage,input.dataset.param,false);
      if(errors.length){summary.textContent=tr('需要补全或修改配置','Some settings need attention');return;}
      const result=await api('preview',data);if(!el.isConnected)return;
      row(tr('后端程序','Backend executable'),result.argv[0],'Worker');
      row(tr('对外模型名','Served model name'),p.ModelAlias||tr('沿用模型名称','Use model name'),'ModelAlias');
      row(tr('API 访问密钥','API access key'),p.ApiKey?tr('已设置 · 预览隐藏密钥','Configured · Hidden in preview'):tr('未设置','Not configured'),'ApiKey');
      const reserveAt=result.argv.indexOf('--kvmem-gen-reserve'),outputAt=result.argv.indexOf(p.Backend==='ninfer'?'--default-max-tokens':'-n');
      if(reserveAt>=0&&outputAt>=0){const reserve=Number(result.argv[reserveAt+1]),requested=Number(result.argv[outputAt+1]),effective=p.Backend==='llamacpp'?Math.min(requested,reserve):requested;row(tr('默认输出预算 / 追加空间','Default output budget / append workspace'),`${effective} / ${reserve} tokens · `+(p.Backend==='llamacpp'?tr('当前 llama.cpp 的输出预算受 R 限制','Current llama.cpp output budget is limited by R'):tr('NInfer 可复用追加空间，输出预算可超过 R','NInfer can reuse append workspace; output budget may exceed R')),'Reserve');}
      const port=result.preflight?.port??p.Port??18200,available=result.preflight?.port_available;
      row(tr('服务端口','Service port'),`${result.preflight?.listen_host||p.ListenHost||'127.0.0.1'}:${port} · `+(available===true?tr('可用','Available'):available===false?tr('已占用或无法绑定；启动前需释放或更换','In use or unavailable; release or change before starting'):tr('启动时检查','Checked on start')),'Port',available!==false);
      summary.textContent=errors.length?tr('配置需要调整','Configuration needs attention'):tr('启动前检查通过','Preflight checks passed');
      if(result.warnings)el.append(make('p',{class:'l-muted'},result.warnings));
      const advanced=make('details',{class:'l-check-command'});advanced.append(make('summary',{},tr('高级：查看启动命令','Advanced: launch command')));
      const quoted=result.argv.map(x=>"'"+x.replaceAll("'","''")+"'").join(' ');
      const command=Object.entries(result.environment||{}).map(([k,v])=>`$env:${k} = '${String(v).replaceAll("'","''")}'`).join('\n')+'\n& '+quoted;
      const code=make('pre',{class:'l-result'},command),copy=make('button',{type:'button',class:'s-button','data-copy-command':''},tr('复制启动命令','Copy launch command'));copy.onclick=async()=>{try{await navigator.clipboard.writeText(command);copy.textContent=tr('已复制','Copied');}catch{copy.textContent=tr('复制失败，请手动选择文本','Copy failed; select the text manually');}};advanced.append(code,copy);el.append(advanced);
      if(p.ApiKey)advanced.append(make('p',{class:'l-muted'},tr('复制的命令含 <redacted> 占位符，手动运行前需替换为密钥。页面启动服务会使用当前填写的密钥。','The copied command contains a <redacted> placeholder. Replace it before running manually. Starting here uses the key entered on this page.')));
    }catch(error){if(!el.isConnected)return;summary.textContent=tr('检查未通过','Check failed');const text=error.message||String(error);const key=/JSON|原生参数|WorkerArgs/i.test(text)?'WorkerArgs':/Worker|后端程序/i.test(text)?'Worker':/Model|模型/i.test(text)?'Model':/StageLayers|层数/i.test(text)?'StageLayers':/Gpu|GPU|显卡/i.test(text)?'Gpu':/Port|端口/i.test(text)?'Port':null;row(tr('需要处理','Needs attention'),text,key,false);}
    finally{if(el.isConnected)el.append(make('p',{class:'l-muted'},tr('检查不会加载模型。兼容性和实际资源需求仍需启动验证；检查通过也不会自动启动。','This check does not load a model. Compatibility and resource needs are verified at startup; passing does not start a service.')));}
  }
  function message(text,error=false){const el=root.querySelector('[data-launch-message]');if(el){el.textContent=text;el.classList.toggle('l-error',error);el.setAttribute('role',error?'alert':'status');}}
  async function api(path,data,timeout=60000){const response=await fetch('/api/launcher/'+path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-KVMem-Token':info.token},body:data===undefined?undefined:JSON.stringify(data),signal:AbortSignal.timeout(timeout)});const value=await response.json();if(!response.ok)throw Error(value.error||'HTTP '+response.status);return value;}
  function collect(){const params={};root.querySelectorAll('[data-param]').forEach(el=>{if(el.disabled||el.dataset.uiOnly==='true')return;const k=el.dataset.param;if(el.type==='checkbox'){if(el.dataset.inverted==='true'?!el.checked:el.checked)params[k]=true;}else if(el.multiple){params[k]=Array.from(el.selectedOptions).map(x=>x.value).join(',');}else if(el.value!==''){params[k]=el.type==='number'?Number(el.value):el.value;}});const layers=params.StageLayers;if(layers)params.StageLayers=layers.split(',').map(x=>Number(x.trim()));const raw=root.querySelector('[data-native]');if(raw?.value.trim()){const args=JSON.parse(raw.value);if(!Array.isArray(args))throw Error(tr('原生参数必须为 JSON 数组','Native arguments must be a JSON array'));params.WorkerArgs=args;}return {schema_version:1,parameters:params};}
  function field(parent,p,key,zh,en,type='text',options=null,note=null,disabled=false){
    const label=make('label',{class:'l-field l-row'}),title=tr(zh,en);
    if(type==='checkbox'){
      label.classList.add('l-toggle');
      const copy=make('span',{class:'l-toggle-copy'});copy.append(make('span',{},title));
      const input=make('input',{type:'checkbox',role:'switch','aria-label':title,'data-param':key});
      input.checked=!!p[key];input.disabled=disabled;input.dataset.backendSupported=String(!disabled);
      if(note||disabled)help(copy.firstChild,title,(disabled?tr('当前启动器对此后端暂未开放此设置。','This launcher does not expose this setting for this backend yet. '):'')+(note?tr(...note):''),input);
      const control=make('span',{class:'l-toggle-control'}),state=make('span',{class:'l-toggle-state','aria-hidden':'true'});
      state.append(make('span',{'data-toggle-on':''},tr('已开启','On')),make('span',{'data-toggle-off':''},tr('已关闭','Off')),make('span',{'data-toggle-unavailable':''},tr('不可用','Unavailable')));
      control.append(input,state);label.append(copy,control);parent.append(label);return input;
    }
    label.append(make('span',{},title));let input;
    if(options){input=make('select');options.forEach(([value,text])=>input.append(make('option',{value},text)));}else input=make('input',{type});
    input.dataset.param=key;input.disabled=disabled;input.dataset.backendSupported=String(!disabled);input.setAttribute('aria-label',title);
    if(type==='number'){input.min='0';input.step='1';}input.value=p[key]??'';label.append(input);
    if(note||disabled)help(label.firstChild,title,(disabled?tr('当前启动器对此后端暂未开放此设置。','This launcher does not expose this setting for this backend yet. '):'')+(note?tr(...note):''),input);
    if(disabled)label.append(make('small',{class:'l-unavailable'},tr('此后端暂不可用','Unavailable for this backend')));
    parent.append(label);return input;
  }

  function dependency(key,enabled,reason){
    const input=root.querySelector(`[data-param=${key}]`);
    if(!input||input.dataset.backendSupported==='false')return;
    // Disabled by a feature dependency, not unsupported by this backend.
    // Preserve values for drafts/templates while collect() omits the override.
    input.dataset.keepDraft='true';input.disabled=!enabled;input.dataset.disabledReason=enabled?'':reason;
    const state=input.closest('label')?.querySelector('[data-toggle-unavailable]');
    if(state)state.textContent=tr('未生效','Inactive');
  }
  function syncDependencies(){
    const get=key=>root.querySelector(`[data-param=${key}]`);
    if(!get('Backend'))return;
    const diskAllowed=get('Backend').value!=='llamacpp'||Number(get('Concurrency')?.value||1)<=1;
    const diskConflict=tr('当前 llama.cpp 并发大于 1，暂不支持 SSD 缓存。','SSD cache is unavailable with llama.cpp concurrency above 1.');
    dependency('DiskEnabled',diskAllowed,diskConflict);
    const disk=!!get('DiskEnabled')?.checked&&!get('DiskEnabled').disabled,lrfu=!!get('NoLrfu')?.checked,vision=!!get('Vision')?.checked;
    for(const key of ['DiskPath','DiskMiB'])dependency(key,disk,diskAllowed?tr('先开启 SSD 缓存。','Enable SSD cache first.'):diskConflict);
    if(get('DiskPath'))get('DiskPath').required=disk;
    for(const key of ['LrfuLambda','LrfuTopFraction'])dependency(key,lrfu,tr('先开启 LRFU。','Enable LRFU first.'));
    for(const key of ['VisionTokens','VisionResidency'])dependency(key,vision,tr('先开启图像输入。','Enable vision input first.'));
    dependency('ThinkingBudget',get('ThinkingMode')?.value!=='off',tr('默认思考模式已关闭；重新开启或选择默认后可设置预算。','Default thinking is off. Select on or default to set a budget.'));
    const mtpEnabled=!!get('MtpEnabled')?.checked&&!get('MtpEnabled').disabled;
    dependency('MtpDrafts',mtpEnabled,tr('先开启 MTP。','Enable MTP first.'));
    if(get('MtpDrafts'))get('MtpDrafts').required=mtpEnabled;
    const mtp=mtpEnabled&&Number(get('MtpDrafts')?.value)>0;
    for(const key of ['AdaptiveMtp','NgramDrafts'])dependency(key,mtp,tr('先开启 MTP，并设置有效的推测长度。','Enable MTP and set a valid draft length first.'));
    dependency('NgramMinMatch',mtp&&Number(get('NgramDrafts')?.value)>0,tr('先启用 MTP，并将 N-gram 推测 token 数设为大于 0。','Enable MTP and set N-gram draft tokens greater than 0 first.'));
    dependency('FastPrefill',['int8','rk8v4','rk4v4','rk4v4-e8','rk2v4-e8'].includes(get('KvType')?.value),tr('当前 KV 格式不支持快速预填充。','The current KV format does not support fast prefill.'));
    const gpu=get('Gpu'),gpuCount=gpu?.multiple?gpu.selectedOptions.length:(gpu?.value||'').split(',').filter(Boolean).length;
    dependency('StageLayers',gpuCount>1,tr('至少选择两张显卡后才能设置层数分配。','Select at least two GPUs to assign layers.'));
    const keyType=get('KvTypeK'),valueType=get('KvTypeV'),pairStatus=root.querySelector('[data-kv-pair-status]');
    if(keyType&&valueType){
      const quantized=['q8_0','q5_0','q4_0'],mixed=keyType.value&&valueType.value&&quantized.includes(keyType.value)!==quantized.includes(valueType.value);
      const reason=mixed?tr('当前后端不能混用浮点与量化 K/V。请两边都选 q8_0、q5_0 或 q4_0，或两边都选 f16、f32。','This backend cannot mix floating-point and quantized K/V. Use q8_0, q5_0 or q4_0 for both sides, or f16/f32 for both.'):'';
      keyType.setCustomValidity(reason);valueType.setCustomValidity(reason);
      if(pairStatus){pairStatus.textContent=reason;pairStatus.hidden=!mixed;}
    }
    if(openHelp)showHelp(openHelp);
  }

  function inputActions(input){
    const row=make('div',{class:'l-input-actions'});input.replaceWith(row);row.append(input);return row;
  }

  function credentialField(parent,p){
    const input=field(parent,{ApiKey:launchKeys[p.Backend]||''},'ApiKey','API 访问密钥（可选）','API access key (optional)','password',null,['仅在当前页面保留，不写入草稿或模板；刷新后如需重启服务，请重新填写。监控自动连接当前服务；外部聊天客户端需填写此密钥。','Kept only on this page, excluded from drafts and templates. Re-enter after refreshing to restart. Monitoring connects automatically; external chat clients need this key.']);
    input.closest('label').classList.add('l-wide');input.dataset.secret='true';input.autocomplete='off';input.spellcheck=false;input.maxLength=512;input.pattern='[A-Za-z0-9._~+\\x2f\\x2d]+=*';
    input.addEventListener('input',()=>{launchKeys[p.Backend]=input.value;});
    const row=inputActions(input),show=make('button',{type:'button',class:'s-button','aria-pressed':'false'},tr('显示','Show')),generate=make('button',{type:'button',class:'s-button','data-generate-key':''},tr('生成','Generate'));
    show.onclick=()=>{const visible=input.type==='password';input.type=visible?'text':'password';show.setAttribute('aria-pressed',String(visible));show.textContent=visible?tr('隐藏','Hide'):tr('显示','Show');};
    generate.onclick=()=>{const bytes=crypto.getRandomValues(new Uint8Array(32));input.value=Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');input.dispatchEvent(new Event('input',{bubbles:true}));};
    row.append(show,generate);
  }

  function templateFileField(parent,p){
    const advanced=make('details',{class:'l-advanced l-wide'});advanced.append(make('summary',{},tr('高级 · 自定义聊天模板','Advanced · Custom chat template')));const grid=make('div',{class:'l-fields l-template-fields'});advanced.append(grid);parent.append(advanced);
    const input=field(grid,p,'ChatTemplateFile','模板文件（可选）','Template file (optional)','text',null,['留空使用模型与引擎默认模板。只选择模板文件，不在页面中编辑模板。','Leave empty to use the model and engine default. Choose a template file; template editing is not provided here.']);input.closest('label').classList.add('l-wide');
    const row=inputActions(input),browse=make('button',{type:'button',class:'s-button','data-browse-template':''},tr('选择…','Choose…')),reset=make('button',{type:'button',class:'s-button','data-reset-template':''},tr('恢复默认','Use default'));
    browse.onclick=async()=>{if(pickingModel)return;pickingModel=true;browse.disabled=true;try{const result=await api('browse-template',{backend:p.Backend,current_path:input.value||p.Model||''},315000);if(result.path&&input.isConnected){input.value=result.path;input.dispatchEvent(new Event('input',{bubbles:true}));}}catch(error){showError(error);}finally{pickingModel=false;browse.disabled=false;}};
    reset.onclick=()=>{input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));};row.append(browse,reset);
  }

  function modelPicker(parent, p) {
    const box = make('div', {class:'l-field l-model-picker'});
    const label = make('label', {for:'launch-model'}, tr('模型文件','Model file'));
    const row = make('div', {class:'l-model-row'});
    const input = make('input', {id:'launch-model', type:'text', 'data-param':'Model', required:'',
      placeholder:tr('点击选择模型，也可粘贴文件路径','Choose a model, or paste a file path')});
    input.value = p.Model || '';
    const browse = make('button', {type:'button', class:'s-button', 'data-browse-model':''}, tr('选择模型…','Choose model…'));
    browse.disabled = pickingModel;
    row.append(input, browse);box.append(label, row);
    const recent = make('select', {'aria-label':tr('最近选择的模型','Recently selected models'),'data-recent-model':''});
    function refreshRecent() {
      const items = recentModels(p.Backend);
      recent.replaceChildren(make('option',{value:''},tr('最近选择…','Recent models…')));
      for (const path of items) recent.append(make('option',{value:path},path.split(/[\\/]/).pop()+' — '+path));
      recent.hidden = !items.length;
    }
    refreshRecent();
    recent.onchange = () => { if(recent.value){input.value=recent.value;input.dispatchEvent(new Event('input',{bubbles:true}));recent.value='';} };
    box.append(recent);
    const scan = make('button',{type:'button',class:'s-button l-scan-trigger','data-scan-models':''},tr('查找模型…','Find models…'));
    const modelTools=make('div',{class:'l-model-tools'});
    modelTools.append(make('small',{},(p.Backend==='ninfer'?'.ninfer':'.gguf')+tr(' · 本机文件',' · Local file')),scan);
    const dialog = make('dialog',{class:'l-model-dialog','aria-labelledby':'model-scan-title'});
    const head = make('div',{class:'l-heading'});
    const close = make('button',{type:'button',class:'s-button'},tr('关闭','Close'));
    head.append(make('h2',{id:'model-scan-title',class:'s-title'},tr('查找模型','Find models')+' · '+(p.Backend==='ninfer'?'.ninfer':'.gguf')),close);
    const folderLabel=make('label',{class:'l-field'},tr('模型所在文件夹','Model folder'));
    const directory=make('input',{type:'text','data-scan-directory':'',placeholder:tr('粘贴文件夹路径，或点击浏览文件夹','Paste a folder path, or browse for a folder')});
    folderLabel.append(directory);
    const folderActions=make('div',{class:'l-actions l-scan-actions'});
    const other=make('button',{type:'button',class:'s-button','data-scan-folder':''},tr('浏览文件夹…','Browse folder…'));
    const submit=make('button',{type:'button',class:'s-button l-primary','data-scan-submit':''},tr('扫描此目录并记住','Scan and remember folder'));
    const rescan=make('button',{type:'button',class:'s-button','data-scan-all':'','aria-describedby':'model-scan-locations-help'},tr('扫描常用位置','Scan known locations'));
    folderActions.append(other,submit,rescan);
    const locationHelp=make('p',{id:'model-scan-locations-help',class:'l-muted l-scan-help'},tr('常用位置：已记住的模型目录、当前模型所在文件夹、程序所在文件夹，以及附近名为 models 或 model 的文件夹。不会扫描整个硬盘；其他位置请在上方选择或粘贴目录。本次具体位置见“扫描范围”。','Known locations include remembered model folders, the current model’s folder, the program folders, and nearby folders named models or model. This does not scan entire drives. For other locations, choose or paste a folder above. Expand Search locations to see the actual folders searched.'));
    const savedRow=make('div',{class:'l-scan-saved-row'});
    const saved=make('select',{'data-scan-saved':'','aria-label':tr('已记住的模型目录','Remembered model folders')});
    const forget=make('button',{type:'button',class:'s-button','data-scan-forget':'',disabled:'',title:tr('只移除目录记录，不删除模型文件','Remove the folder record; keep all model files')},tr('移除目录记录','Forget folder'));
    savedRow.append(saved,forget);
    const folderError=make('p',{class:'l-error',role:'alert','data-scan-folder-error':'',hidden:''});
    const progress = make('p',{class:'l-muted','aria-live':'polite','data-scan-status':''});
    const scope = make('details',{class:'l-scan-scope'});scope.append(make('summary',{},tr('扫描范围','Search locations')));
    const paths = make('div',{class:'l-muted'});scope.append(paths);
    const filter=make('input',{type:'search','data-scan-filter':'','aria-label':tr('筛选模型','Filter models'),placeholder:tr('按模型名称或路径筛选结果','Filter results by name or path')});
    const formatHint=make('p',{class:'l-muted','data-scan-format-hint':'',hidden:''});
    const results = make('div',{class:'l-scan-results','data-scan-results':''});
    dialog.append(head,folderLabel,folderActions,locationHelp,savedRow,folderError,
      make('p',{class:'l-muted'},tr('扫描指定目录后会记住位置，下次可直接选择。模型文件保留在本机。','Scanned folders are remembered for next time. Model files stay on this computer.')),
      progress,scope,filter,formatHint,results,make('p',{class:'l-muted'},tr('选择结果只填入启动配置。文件名和大小不能证明模型兼容，启动时还会检查。','Selecting a result only fills the launch settings. File names and sizes do not prove compatibility; startup checks still apply.')));
    box.append(modelTools,dialog);
    close.onclick=()=>dialog.close();
    let scanning=false, scanTask=null, scanGeneration=0, latest=null;
    function folderMessage(text='',error=true){folderError.textContent=text;folderError.hidden=!text;folderError.classList.toggle('l-error',error);folderError.classList.toggle('l-muted',!error);folderError.setAttribute('role',error?'alert':'status');}
    async function refreshLocations(selected='',generation=scanGeneration){
      const data=await api('model-directories');
      if(!dialog.isConnected||!dialog.open||generation!==scanGeneration)return;
      saved.replaceChildren(make('option',{value:''},tr('已记住的目录（选择后扫描）','Remembered folders (select to scan)')));
      for(const item of data.directories||[])saved.append(make('option',{value:item.path},item.path+(item.available?'':tr(' · 当前不可访问',' · Unavailable'))));
      saved.value=selected;forget.disabled=!saved.value;
    }
    function showLocations(result){
      paths.replaceChildren();
      const labels={scanned:['已扫描','Scanned'],covered:['已包含','Included'],partial:['部分扫描','Partly scanned'],unavailable:['不存在或无法访问','Missing or inaccessible'],pending:['尚未扫描','Not scanned']};
      for(const item of result.locations||result.roots.map(path=>({path,status:'scanned'}))){
        const row=make('div',{class:'l-scan-location'});
        row.append(make('span',{},item.path),make('small',{},tr(...(labels[item.status]||labels.pending))));paths.append(row);
      }
      if(!paths.childElementCount)paths.textContent=tr('尚未找到常用模型目录，请在上方选择模型文件夹。','No known model folder was found. Choose a model folder above.');
      scope.open=!result.models.length||result.limited||!!result.skipped;
    }
    function renderResults(){
      if(!latest)return;
      const query=filter.value.trim().toLocaleLowerCase(),models=latest.models.filter(model=>(model.name+' '+model.path).toLocaleLowerCase().includes(query));
      results.replaceChildren();
      progress.textContent=query?tr(`显示 ${models.length} / ${latest.models.length} 个候选模型`,`Showing ${models.length} of ${latest.models.length} candidate models`):tr(`找到 ${latest.models.length} 个候选模型`,`Found ${latest.models.length} candidate models`);
      const reasons={time:['扫描时间已达上限','Time limit reached'],entries:['检查的文件数量已达上限','Entry limit reached'],results:['候选数量已达上限','Result limit reached'],depth:['部分目录层级较深','Some folders are too deeply nested']};
      if(latest.limited)progress.textContent+=tr(' · 仅显示部分结果：',' · Partial results: ')+(latest.limit_reasons?.length?latest.limit_reasons.map(key=>tr(...(reasons[key]||['已达扫描限制','Scan limit reached']))).join('；'):tr('已达扫描限制','Scan limit reached'))+tr('，可选择更具体的文件夹。','; choose a more specific folder.');
      if(latest.skipped)progress.textContent+=tr(' · 部分文件或目录无法读取。',' · Some files or folders could not be read.');
      if(!models.length)results.append(make('p',{class:'l-scan-empty'},query?tr('没有符合筛选条件的模型。','No models match this filter.'):tr(`扫描范围内未找到 ${p.Backend==='ninfer'?'.ninfer':'.gguf'} 模型。请在上方添加实际存放模型的文件夹，或直接使用“选择模型”。`,`No ${p.Backend==='ninfer'?'.ninfer':'.gguf'} models were found in these locations. Add the folder containing your models above, or use Choose model.`)));
      for(const model of models){
        const pick=make('button',{type:'button',class:'l-model-candidate','data-model-candidate':model.path});
        const empty=model.bytes===0||model.warnings?.includes('empty');
        pick.disabled=!!empty;
        const unit=model.bytes>=1024**3?['GiB',1024**3]:model.bytes>=1024**2?['MiB',1024**2]:model.bytes>=1024?['KiB',1024]:['B',1];
        pick.append(make('span',{},model.name),make('small',{},`${(model.bytes/unit[1]).toLocaleString(window.monitorI18n.language,{maximumFractionDigits:2})} ${unit[0]} · ${model.format}`),make('small',{class:'l-candidate-path'},model.path));
        if(empty)pick.append(make('small',{class:'l-error'},tr('文件为空，可能尚未下载完成。','This file is empty; its download may be incomplete.')));
        pick.onclick=()=>{if(!input.isConnected||!dialog.open)return;input.value=model.path;rememberModel(p.Backend,model.path);refreshRecent();input.dispatchEvent(new Event('input',{bubbles:true}));dialog.close();input.focus();message(tr('已选用候选模型；下次启动生效。','Candidate selected; applies on next launch.'));};
        results.append(pick);
      }
      const different=p.Backend==='ninfer'?'gguf':'ninfer',count=latest.other_formats?.[different]||0;
      formatHint.hidden=!count;formatHint.textContent=count?tr(`扫描范围还发现 ${count} 个 .${different} 文件；当前后端需要 ${p.Backend==='ninfer'?'.ninfer':'.gguf'} 模型。`,`The scanned locations also contain ${count} .${different} files; this backend needs ${p.Backend==='ninfer'?'.ninfer':'.gguf'} models.`):'';
    }
    dialog.addEventListener('close',()=>{scanGeneration++;scanTask?.abort();scanning=false;});
    filter.oninput=renderResults;
    async function runScan(searchDirectory='') {
      if(scanning||!dialog.open)return;
      const generation=++scanGeneration,controller=new AbortController();scanTask=controller;
      const timer=setTimeout(()=>controller.abort(),12000);
      scanning=true;other.disabled=true;submit.disabled=true;rescan.disabled=true;saved.disabled=true;forget.disabled=true;filter.disabled=true;results.replaceChildren();formatHint.hidden=true;folderMessage();
      progress.textContent=tr('正在扫描模型目录…','Scanning model folders…');paths.textContent=tr('正在确定扫描范围…','Determining search locations…');
      try {
        const response=await fetch('/api/launcher/scan-models',{method:'POST',headers:{'Content-Type':'application/json','X-KVMem-Token':info.token},body:JSON.stringify({backend:p.Backend,current_path:input.value,worker_path:root.querySelector('[data-param=Worker]')?.value||'',...(searchDirectory?{directory:searchDirectory}:{})}),signal:controller.signal});
        const result=await response.json();if(!response.ok)throw Error(result.error||'HTTP '+response.status);
        if(generation!==scanGeneration||!dialog.open||!input.isConnected)return;
        latest=result;showLocations(result);
        if(searchDirectory&&result.roots[0])directory.value=result.roots[0];
        try{await refreshLocations(searchDirectory?directory.value:'',generation);}catch(error){if(generation===scanGeneration&&dialog.open)folderMessage(error.message);}
        if(generation===scanGeneration&&dialog.open&&input.isConnected)renderResults();
      }catch(error){if(generation===scanGeneration&&dialog.open){latest=null;progress.textContent=tr('本次扫描未完成，可以修改目录后重试。','This scan did not complete. Adjust the folder and try again.');folderMessage(error.name==='AbortError'?tr('扫描超时，请选择更具体的模型文件夹，或直接选择模型文件。','The scan timed out. Choose a more specific folder or select the model file directly.'):error.message);}}
      finally{clearTimeout(timer);if(generation===scanGeneration){scanning=false;other.disabled=false;submit.disabled=false;rescan.disabled=false;saved.disabled=false;forget.disabled=!saved.value;filter.disabled=false;scanTask=null;}}
    }
    scan.onclick=()=>{if(busy)return;if(info.model_library_version!==1){showError(Error(tr('模型查找功能已更新，请先关闭原启动窗口并重新启动控制台，然后刷新页面。','Model discovery has been updated. Close the original startup window, restart the console, then refresh this page.')));return;}dialog.showModal();other.disabled=false;submit.disabled=false;rescan.disabled=false;filter.disabled=false;filter.value='';runScan();};
    submit.onclick=()=>{const value=directory.value.trim();if(!value){folderMessage(tr('请先填写模型文件夹路径，或点击“浏览文件夹”。','Enter a model folder path or click Browse folder first.'));directory.focus();return;}filter.value='';runScan(value);};
    directory.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();submit.click();}};
    rescan.onclick=()=>{directory.value='';filter.value='';runScan();};
    saved.onchange=()=>{forget.disabled=!saved.value;if(saved.value){directory.value=saved.value;filter.value='';runScan(saved.value);}};
    forget.onclick=async()=>{if(!saved.value||scanning)return;try{await api('model-directories',{action:'remove',path:saved.value});directory.value='';await refreshLocations();runScan();}catch(error){folderMessage(error.message);}};
    other.onclick=async()=>{
      if(pickingModel||scanning)return;
      pickingModel=true;other.disabled=true;
      try {
        folderMessage(tr('请在系统窗口中选择模型文件夹；也可取消后直接粘贴路径。','Choose the model folder in the system dialog, or cancel and paste its path.'),false);
        const result=await api('browse-model-folder',{backend:p.Backend,current_path:directory.value||input.value},315000);
        if(!dialog.open||!input.isConnected)return;
        if(result.path){directory.value=result.path;filter.value='';await runScan(result.path);}else folderMessage(tr('已取消选择，当前扫描结果保持不变。','Folder selection cancelled; current results are unchanged.'),false);
      }catch(error){if(dialog.open)folderMessage(error.message);}
      finally{pickingModel=false;other.disabled=false;}
    };
    browse.onclick = async () => {
      if(pickingModel || busy) return;
      pickingModel = true; browse.disabled = true;
      const original = input.value;
      browse.textContent = tr('等待选择…','Waiting…');
      message(tr('请在系统文件选择窗口中选择模型。','Choose a model in the system file picker.'));
      try {
        const response = await fetch('/api/launcher/browse-model',{method:'POST',headers:{'Content-Type':'application/json','X-KVMem-Token':info.token},
          body:JSON.stringify({backend:p.Backend,current_path:original}),signal:AbortSignal.timeout(315000)});
        const result = await response.json();
        if(!response.ok) throw Error(result.error || 'HTTP '+response.status);
        if(result.path) {
          rememberModel(p.Backend,result.path);
          if(input.isConnected && input.value === original){input.value=result.path;input.dispatchEvent(new Event('input',{bubbles:true}));refreshRecent();message(tr('已选择模型；下次启动生效。','Model selected; applies on next launch.'));}
          else message(tr('已加入该引擎的最近选择列表。','Added to the recent models for that backend.'));
        } else message(tr('已取消，原模型保持不变。','Cancelled; the original model is unchanged.'));
      } catch(error) {message(error.message,true);}
      finally {pickingModel=false;root.querySelectorAll('[data-browse-model]').forEach(button=>{button.disabled=false;button.textContent=tr('选择模型…','Choose model…');});}
    };
    parent.append(box);
  }
  function applyPreset(preset) {
    if (busy || !info.backends.includes(preset.parameters.Backend)) return;
    try {
      const before = captureDraft().parameters;
      presetUndo = {draft: captureDraft(), selected: selectedPreset, tab: activeTab};
      perBackend[before.Backend] = captureDraft();
      const assets = perBackend[preset.parameters.Backend]?.parameters || {};
      const next = {...defaults, ...structuredClone(preset.parameters)};
      for (const key of ['Model','Worker']) {
        if (assets[key]) next[key] = assets[key];
      }
      next.Port = before.Port ?? defaults.Port;
      next.Gpu = before.Gpu || '';
      next.StageLayers=before.Backend===next.Backend?before.StageLayers||[]:[];
      selectedPreset = preset.id;
      activeTab = 0;
      render(next);
      scheduleSave();
      message(tr('模板已填入编辑区，尚未启动。可以撤销本次套用。','Template applied to the draft; no service started. You can undo this change.'));
      root.querySelector(`[data-preset="${preset.id}"]`).focus();
    } catch (error) { message(error.message, true); }
  }
  function presetPanel(side) {
    const panel = make('section', {class:'s-card l-presets'});
    const title=make('h2',{class:'s-title'},tr('启动模板','Launch templates'));panel.append(title);
    help(title,tr('启动模板','Launch templates'),tr('套用参数后再点击启动，模型路径按引擎保留。草稿与个人模板保存在当前浏览器，不包含 API 密钥；清除网站数据会删除这些设置。','Apply a recipe, then start. Model paths are retained per backend. Drafts and templates stay in this browser without API keys; clearing site data removes them.'));
    const list = make('div', {class:'l-preset-list'});
    const switcher=make('div',{class:'l-template-tabs'});
    for(const [id,zh,en] of [['builtin','内置模板','Built-in'],['mine','我的模板','My templates']]){const tab=make('button',{type:'button',class:'s-button','data-template-tab':id,'aria-pressed':String(templateTab===id)},tr(zh,en));tab.onclick=()=>{templateTab=id;refreshTemplates();};switcher.append(tab);}panel.append(switcher);
    for (const preset of templateTab==='builtin'?presets:[]) {
      const available = info.backends.includes(preset.parameters.Backend);
      const button = make('button', {type:'button', class:'l-preset', 'data-preset':preset.id,
        'aria-pressed':String(selectedPreset === preset.id)});
      button.disabled = busy || !available;
      button.append(make('span', {}, tr(...preset.title)), make('small', {},
        available ? tr(...preset.note) : tr('当前后端目录未安装此引擎','Not installed in this backend checkout')));
      button.onclick = () => applyPreset(preset);
      list.append(button);
    }
    if(templateTab==='mine'){
      try{
        const saved=readTemplates();
        if(!saved.length)list.append(make('p',{class:'l-muted'},tr('还没有个人模板。点击页面顶部“另存为模板”保存当前设置。','No templates yet. Use Save as template above to keep these settings.')));
        for(const item of saved){
          const wrapper=make('div',{class:'l-user-template','data-user-template':item.id}),button=make('button',{type:'button',class:'l-preset','data-apply-template':item.id,'aria-pressed':String(selectedPreset===item.id)});
          button.append(make('span',{},item.name),make('small',{},item.description||names[item.draft.parameters.Backend]));
          button.onclick=()=>{if(busy)return;presetUndo={draft:captureDraft(),selected:selectedPreset,tab:activeTab};selectedPreset=item.id;restoreDraft(structuredClone(item.draft));scheduleSave();message(tr('模板已填入编辑区，尚未启动。可以撤销本次套用。','Template applied to the draft; no service started. You can undo this change.'));};
          const menu=make('details',{class:'l-template-menu'});menu.append(make('summary',{},tr('管理','Manage')));
          for(const [mode,zh,en] of [['update','用当前设置更新','Update from current settings'],['rename','重命名','Rename'],['delete','删除','Delete']]){const action=make('button',{type:'button',class:'s-button','data-template-edit':mode},tr(zh,en));action.onclick=()=>templateEditor(mode,item);menu.append(action);}
          wrapper.append(button,menu);list.append(wrapper);
        }
      }catch(error){list.append(make('p',{class:'l-error'},error.message));}
    }
    panel.append(list);
    if (presetUndo) {
      const undo = make('button', {type:'button', class:'s-button l-secondary', 'data-preset-undo':''}, tr('撤销套用','Undo template'));
      undo.disabled = busy;
      undo.onclick = () => {
        if (busy) return;
        const previous = presetUndo;
        presetUndo = null; selectedPreset = previous.selected; activeTab = previous.tab;
        restoreDraft(previous.draft);scheduleSave();
        message(tr('已恢复套用模板前的配置。','Restored the configuration before applying the template.'));
        root.querySelector('[data-param=Backend]').focus();
      };
      panel.append(undo);
    }
    const log=side.querySelector('.l-log');if(log)side.insertBefore(panel,log);else side.append(panel);
  }
  function render(p){
    let refreshGpuOrder=()=>{};
    const b=p.Backend,n=b==='ninfer',l=b==='llamacpp';
    closeHelp();root.replaceChildren();
    const heading=make('div',{class:'l-heading l-page-heading'}),intro=make('div',{class:'l-intro'}),savedState=make('button',{type:'button',class:'l-save-state l-muted','data-draft-state':'','aria-live':'polite'});savedState.onclick=saveEditing;intro.append(make('h1',{},tr('启动配置','Launch configuration')),savedState);heading.append(intro);root.append(heading);
    const layout=make('div',{class:'l-layout'}),main=make('div',{class:'l-main'}),side=make('aside',{class:'l-side'});layout.append(main,side);root.append(layout);
    const form=make('form',{'aria-label':tr('启动配置','Launch configuration')});form.style.display='contents';main.append(form);form.addEventListener('submit',e=>e.preventDefault());
    const core=make('section',{class:'s-card l-essential'});core.append(make('h2',{class:'s-title'},tr('模型与资源','Model & resources')));const fields=make('div',{class:'l-fields l-core'});core.append(fields);form.append(core);
    const backend=field(fields,p,'Backend','推理引擎','Backend','text',Object.entries(names).map(([v,t])=>[v,t]));Array.from(backend.options).forEach(o=>{o.disabled=!info.backends.includes(o.value);if(o.disabled)o.textContent+=' · '+tr('未安装','not installed');});
    modelPicker(fields,p);
    const gpu=field(fields,p,'Gpu','推理显卡','Inference GPUs','text',info.gpus.map(g=>[g.uuid,g.name+' · '+g.uuid.slice(-8)]),['选择要使用的显卡。多卡按勾选顺序排列，可前移／后移；切换引擎保留当前选择。','Choose the GPUs to use. Multiple GPUs follow selection order and can be reordered. Switching engines retains your selection.']);
    gpu.closest('label').classList.add('l-gpu-field');
    gpu.multiple=true;gpu.size=2;Array.from(gpu.options).forEach(o=>o.selected=(p.Gpu||'').split(',').includes(o.value));
    gpu.required=true;
    if(!info.gpus.length){gpu.replaceChildren(make('option',{value:''},tr('未检测到 NVIDIA 显卡','No NVIDIA GPU detected')));}
    if(info.gpus.length){
      const old=gpu.closest('label'),box=make('div',{class:old.className});
      box.append(...old.childNodes);old.replaceWith(box);
      gpu.hidden=true;gpu.required=false;gpu.setAttribute('aria-hidden','true');
      const choices=make('div',{class:'l-gpu-choices',role:'group','aria-label':tr('推理显卡','Inference GPUs')});
      const checks=[],options=new Map(Array.from(gpu.options).map(option=>[option.value,option]));
      let order=[...new Set((p.Gpu||'').split(',').filter(id=>options.has(id)))];
      const ordered=make('div',{class:'l-gpu-order','data-gpu-order':'','aria-label':tr('显卡执行顺序','GPU execution order')});
      const gpuLimit=make('p',{class:'l-muted l-gpu-limit','data-gpu-limit':'',hidden:''});
      function clearLayers(){
        const input=root.querySelector('[data-param=StageLayers]');
        if(input?.value.trim()){input.value='';message(tr('显卡数量已改变，原层数分配已清空，请重新设置。','GPU count changed. Previous layer allocation cleared; configure it again.'));}
      }
      for(const device of info.gpus){
        const option=Array.from(gpu.options).find(x=>x.value===device.uuid);
        const card=make('label',{class:'l-gpu-choice'}),check=make('input',{type:'checkbox','data-gpu-choice':device.uuid});
        check.checked=option.selected;
        const caption=make('span');caption.append(make('strong',{},device.name.replace(/^NVIDIA GeForce /,'')),make('small',{},'UUID · '+device.uuid.slice(-8)));
        card.append(check,caption);choices.append(card);checks.push(check);
        check.onchange=()=>{clearLayers();order=check.checked?[...order.filter(id=>id!==device.uuid),device.uuid]:order.filter(id=>id!==device.uuid);sync();gpu.dispatchEvent(new Event('input',{bubbles:true}));};
      }
      function drawOrder(){
        ordered.replaceChildren();ordered.hidden=order.length<2;
        if(order.length<2)return;
        ordered.append(make('span',{class:'l-muted'},tr('执行顺序 · 从前到后','Execution order · first to last')));
        const input=root.querySelector('[data-param=StageLayers]'),layers=(input?.value||'').split(',').map(x=>x.trim());
        const valid=layers.length===order.length&&layers.every(x=>/^[1-9]\d*$/.test(x));
        order.forEach((id,i)=>{
          const device=info.gpus.find(g=>g.uuid===id),row=make('div',{class:'l-gpu-order-row','data-ordered-gpu':id,tabindex:'-1'});
          row.append(make('span',{class:'l-gpu-position'},String(i+1)),make('span',{class:'l-gpu-order-name'},device.name.replace(/^NVIDIA GeForce /,'')+' · '+id.slice(-8)),make('small',{'data-gpu-layers':''},valid?tr(layers[i]+' 层',layers[i]+' layers'):input?.value.trim()?tr('待校验','Needs validation'):tr('自动分配','Automatic')));
          for(const [delta,zh,en] of [[-1,'前移','Move earlier'],[1,'后移','Move later']]){
            const move=make('button',{type:'button',class:'s-button','data-gpu-move':String(delta),'aria-label':tr(zh,en)+' · '+device.name+' · '+id.slice(-8)},tr(zh,en));
            move.disabled=i+delta<0||i+delta>=order.length;
            move.onclick=()=>{
              const target=i+delta;
              if(input?.value.trim()){
                if(valid){[layers[i],layers[target]]=[layers[target],layers[i]];input.value=layers.join(',');}
                else{input.value='';message(tr('原层数分配不完整，已清空；请按新的显卡顺序重新设置。','Incomplete layer allocation cleared; configure it for the new GPU order.'));}
              }
              [order[i],order[target]]=[order[target],order[i]];sync();gpu.dispatchEvent(new Event('input',{bubbles:true}));
              Array.from(ordered.querySelectorAll('[data-ordered-gpu]')).find(el=>el.dataset.orderedGpu===id)?.focus();
            };
            row.append(move);
          }
          ordered.append(row);
        });
      }
      function sync(){
        for(const id of [...order,...info.gpus.map(g=>g.uuid).filter(id=>!order.includes(id))]){const option=options.get(id);gpu.append(option);option.selected=order.includes(id);}
        checks.forEach(check=>{check.checked=order.includes(check.dataset.gpuChoice);check.closest('label').classList.toggle('is-selected',check.checked);});
        const singleOnly=l&&order.length>1;
        const reason=singleOnly?tr('当前启动器对 llama.cpp 仅接入单卡。已保留原选择，请只勾选一张显卡后再启动。','This launcher currently exposes one GPU for llama.cpp. Your selection is retained; select just one GPU before starting.'):!order.length?tr('请至少选择一张显卡','Select at least one GPU'):'';
        gpuLimit.textContent=reason;gpuLimit.hidden=!reason;gpuLimit.classList.toggle('l-error',singleOnly);
        checks[0].setCustomValidity(reason);choices.setAttribute('aria-invalid',String(!!reason));drawOrder();
      }
      gpu.addEventListener('change',()=>{order=Array.from(gpu.selectedOptions).map(option=>option.value);sync();});sync();gpu.after(choices);box.append(ordered,gpuLimit);
      refreshGpuOrder=drawOrder;
    }
    field(fields,p,'Context','最大上下文 · tokens','Context limit · tokens','number');
    field(fields,p,'Budget','GPU 历史 KV 预算 · tokens','GPU history KV budget · tokens','number',null,['每条活动请求选中的历史 KV，不含生成预留空间；不是整卡显存上限','Selected history KV per active request, excluding append workspace; not a device VRAM limit']);
    field(fields,p,'Reserve','生成预留空间 · tokens','Generation reserve · tokens','number',null,n?['每条活动请求的追加 KV 工作空间；可循环复用，输出预算可以超过此值','Append KV workspace per active request; reusable, so output budget may exceed this value']:['每条活动请求的追加 KV 工作空间；当前 llama.cpp 的单次输出预算受此容量限制','Append KV workspace per active request; current llama.cpp output budget is limited by this capacity']);
    for(const key of ['Context','Budget','Reserve'])fields.querySelector(`[data-param=${key}]`).closest('label').classList.add('l-resource-field');
    const section=make('section',{class:'s-card'}),tabs=make('div',{class:'l-tabs',role:'tablist','aria-label':tr('共性参数分组','Common settings groups')});section.append(tabs);form.append(section);const panels=[];
    groups.forEach((g,i)=>{const tab=make('button',{type:'button',class:'l-tab',role:'tab',id:'launch-tab-'+i,'aria-selected':String(activeTab===i),'aria-controls':'launch-panel-'+i},tr(...g));tabs.append(tab);const panel=make('div',{class:'l-panel',id:'launch-panel-'+i,role:'tabpanel','aria-labelledby':tab.id});panel.hidden=activeTab!==i;const grid=make('div',{class:'l-fields'});panel.append(grid);panels.push(grid);section.append(panel);tab.onclick=()=>{activeTab=i;tabs.querySelectorAll('button').forEach((x,j)=>x.setAttribute('aria-selected',String(i===j)));section.querySelectorAll('.l-panel').forEach((x,j)=>x.hidden=i!==j);};});
    field(panels[0],p,'HostMiB','RAM 历史缓存 · MiB','RAM history cache · MiB','number',null,['活动与空闲历史共享的原生 KV 数据硬预算；不含模型、统计索引等其他内存；1024 MiB = 1 GiB','Shared hard budget for active and idle native KV data; excludes model weights, statistics and other memory; 1024 MiB = 1 GiB']);
    if(l){
      const kv=['q8_0','q5_0','q4_0','f16','f32'],aliases={fp16:'f16',fp32:'f32',q8:'q8_0',q5:'q5_0',q4:'q4_0'};
      const canonical=value=>aliases[value]||value;
      const types={KvTypeK:canonical(p.KvTypeK||p.KvType||'q8_0'),KvTypeV:canonical(p.KvTypeV||p.KvType||'q8_0')};
      const kvField=make('div',{class:'l-field l-row l-kv-field',role:'group','aria-label':tr('KV 格式','KV format')});
      const kvTitle=make('span',{},tr('KV 格式','KV format')),pair=make('div',{class:'l-kv-pair'});
      const keyType=field(pair,types,'KvTypeK','K 缓存格式','K cache format','text',kv.map(v=>[v,v]));
      const valueType=field(pair,types,'KvTypeV','V 缓存格式','V cache format','text',kv.map(v=>[v,v]));
      for(const [input,side] of [[keyType,'K'],[valueType,'V']]){const label=input.closest('label');label.className='l-kv-side';label.firstChild.textContent=side;}
      keyType.required=true;valueType.required=true;
      help(kvTitle,tr('KV 格式','KV format'),tr('K 和 V 可分别选择：q8_0、q5_0、q4_0 可互配；f16、f32 可互配。浮点与量化不能混搭。','Choose K and V independently: q8_0, q5_0 and q4_0 can be combined; f16 and f32 can be combined. Floating-point and quantized types cannot be mixed.'));
      kvField.append(kvTitle,pair,make('p',{class:'l-muted l-error l-kv-status','data-kv-pair-status':'','aria-live':'polite',hidden:''}));panels[0].append(kvField);
    }else{
      const kv=['int8','bf16','fp8','nvfp4','k8v4','rk8v4','rk4v4','rk4v4-e8','rk2v4-e8'];
      field(panels[0],{...p,KvType:p.KvType||kv[0]},'KvType','KV 格式','KV format','text',kv.map(v=>[v,v]));
    }
    // UI state only: the worker still receives the existing path/budget pair.
    const diskEnabled=field(panels[0],{DiskEnabled:typeof p.DiskEnabled==='boolean'?p.DiskEnabled:!!p.DiskPath?.trim()&&p.DiskMiB!==0},'DiskEnabled','启用 SSD 缓存','Enable SSD cache','checkbox',null,['关闭后不传 SSD 参数；目录与预算仍保存在草稿和模板中，重新开启时可继续使用。','When off, no SSD arguments are passed. The directory and budget stay in drafts and templates for later reuse.']);
    diskEnabled.dataset.uiOnly='true';diskEnabled.closest('label').classList.add('l-wide');
    field(panels[0],p,'DiskPath','SSD 缓存目录','SSD cache directory','text',null,['开启 SSD 缓存时必填；配合正的 SSD 缓存预算使用。','Required when SSD cache is enabled, together with a positive SSD cache budget.']);
    const diskBudget=field(panels[0],{DiskMiB:4096,...p},'DiskMiB','SSD 缓存预算 · MiB','SSD cache budget · MiB','number');
    diskBudget.min='1';
    field(panels[0],p,'SinkTokens','保留开头 · tokens','Keep prefix · tokens','number');field(panels[0],p,'RecentTokens','保留最近 · tokens','Keep recent · tokens','number');
    field(panels[0],p,'RetainedSessions','保留会话数量（可选）','Retained sessions (optional)','number',null,['保留的空闲会话数量，不含正在使用的会话','Idle sessions to retain; excludes active sessions']);
    const advanced=make('details',{class:'l-advanced'});advanced.append(make('summary',{},tr('高级 · 历史检索与保留策略','Advanced · History retrieval & retention')));const ag=make('div',{class:'l-fields'});advanced.append(ag);['SinkTokens','RecentTokens','RetainedSessions'].forEach(k=>{const el=panels[0].querySelector(`[data-param="${k}"]`);if(el)ag.append(el.closest('label'));});panels[0].append(advanced);
    const retrieval=make('section',{class:'l-retrieval l-wide','data-lrfu-settings':''});
    retrieval.append(make('h3',{class:'s-title'},tr('LRFU 与检索评分','LRFU & retrieval scoring')));
    const retrievalFields=make('div',{class:'l-fields'});retrieval.append(retrievalFields);panels[0].append(retrieval);
    // Keep persisted/native NoLrfu semantics; only the UI uses a positive toggle.
    const lrfu=field(retrievalFields,p,'NoLrfu','启用 LRFU','Enable LRFU','checkbox');lrfu.dataset.inverted='true';lrfu.checked=!p.NoLrfu;
    for(const [key,zh,en,fallback] of [['LrfuLambda','LRFU 衰减系数 λ','LRFU decay λ','0.5'],['LrfuTopFraction','LRFU 候选比例','LRFU candidate fraction','0.9']]){
      const input=field(retrievalFields,p,key,zh,en,'number',null,['开启 LRFU 时生效；关闭后保留填写的值。','Applies when LRFU is enabled; values are retained when off.']);input.step='any';input.max='1';input.placeholder=tr('默认 '+fallback,'Default '+fallback);
    }
    field(retrievalFields,{ScoreAggregation:'max',...p},'ScoreAggregation','注意力分数组合方式','Attention score aggregation','text',[['max',tr('最大值','Maximum')],['mean',tr('平均值','Mean')]]);
    field(panels[1],p,'Prefill','预填充块大小 · tokens','Prefill chunk · tokens','number');
    const layerSplit=field(panels[1],{...p,StageLayers:(p.StageLayers||[]).join(',')},'StageLayers','多卡层数分配（可选）','Layer split (optional)','text',null,[n?'依次对应上方第 1、2…张显卡，用逗号分隔；留空由后端分配':'当前接入暂不支持设置多卡层数',n?'Comma-separated counts for GPU 1, 2, … above; empty uses backend allocation':'Layer assignment is unavailable in this integration'],!n);
    layerSplit.addEventListener('input',refreshGpuOrder);refreshGpuOrder();
    const mtpEnabled=field(panels[1],{MtpEnabled:typeof p.MtpEnabled==='boolean'?p.MtpEnabled:Number(p.MtpDrafts)>0},'MtpEnabled','启用 MTP','Enable MTP','checkbox',null,['需要模型支持。关闭后保留推测长度、自适应 MTP 和 N-gram 设置，但不启用它们；重新开启时恢复。','Requires model support. Turning this off retains draft length, adaptive MTP and N-gram settings while keeping them inactive. Turn it on to restore them.'],l);
    mtpEnabled.dataset.uiOnly='true';
    const mtpDrafts=field(panels[1],{...p,MtpDrafts:p.MtpDrafts??4},'MtpDrafts','MTP 推测长度 · tokens','MTP draft length · tokens','number',null,['开启 MTP 时生效，范围 1–15。首次开启默认 4；已有填写值会保留。','Applies when MTP is enabled, range 1–15. The initial default is 4; existing values are retained.'],l);mtpDrafts.min='1';mtpDrafts.max='15';
    mtpEnabled.addEventListener('change',()=>{if(mtpEnabled.checked&&(!mtpDrafts.value.trim()||Number(mtpDrafts.value)<=0))mtpDrafts.value='4';});
    field(panels[1],p,'AdaptiveMtp','启用自适应 MTP','Enable adaptive MTP','checkbox',null,['需要先启用 MTP；关闭自适应后仍可使用固定长度 MTP。','Requires MTP. Turning adaptive mode off still allows fixed-length MTP.'],!n);field(panels[1],p,'FastPrefill','启用快速预填充','Enable fast prefill','checkbox',null,['支持 int8、rk8v4、rk4v4、rk4v4-e8、rk2v4-e8；还需模型支持。与 MTP、普通预填充块大小分别设置。','Supports int8, rk8v4, rk4v4, rk4v4-e8 and rk2v4-e8; model support is also required. Independent of MTP and the standard prefill chunk size.'],!n);
    field(panels[2],p,'MaxTokens','默认最大输出 · tokens','Default output limit · tokens','number',null,['聊天请求可以覆盖默认输出预算。温度、top-p 等采样参数由聊天请求设置。','Chat requests can override the default output budget. Temperature, top-p and other sampling controls are set by chat requests.']);
    const thinking=field(panels[2],{ThinkingMode:'default',...p},'ThinkingMode','默认思考模式','Default thinking mode','text',[['default',tr('沿用引擎 / 模型默认','Use engine / model default')],['on',tr('开启','On')],['off',tr('关闭','Off')]],n?['NInfer 启动参数支持默认与关闭；显式开启由聊天请求选择，仍需模型支持。','NInfer supports default/off at startup; explicit thinking-on belongs to chat requests and requires model support.']:['llama.cpp 当前默认关闭；开启仍需模型与模板支持。聊天请求可以覆盖此默认值。','llama.cpp currently defaults to off. Enabling requires model/template support; chat requests can override this default.']);
    if(n){thinking.querySelector('[value=on]').disabled=true;thinking.querySelector('[value=on]').textContent=tr('开启 · 由聊天请求设置','On · Set by chat request');}
    field(panels[2],p,'ThinkingBudget','默认思考预算（可选）','Default thinking budget (optional)','number',null,['限制思考阶段的 token 数；留空使用引擎默认值','Limits thinking tokens; leave empty for the engine default'],!n);
    field(panels[3],p,'Vision','启用图像输入','Enable vision','checkbox',null,['需要模型及配套资源支持','Requires compatible model and resources'],l);
    field(panels[3],p,'VisionTokens','图像 token 上限（可选）','Image token limit (optional)','number',null,['开启图像输入时生效。','Applies when vision input is enabled.'],l);field(panels[3],{VisionResidency:'resident',...p},'VisionResidency','图像编码器存放位置','Vision encoder placement','text',[['resident','GPU'],['cpu','CPU']],['开启图像输入时生效；关闭后保留原选择。','Applies when vision input is enabled; the selection is retained when off.'],l);
    templateFileField(panels[3],p);
    field(panels[4],{ListenHost:'127.0.0.1',...p},'ListenHost','监听地址','Listen address','text',[['127.0.0.1',tr('127.0.0.1 · 仅本机','127.0.0.1 · This computer')],['0.0.0.0',tr('0.0.0.0 · 所有网卡','0.0.0.0 · All interfaces')]],['选择所有网卡后，其他设备可通过本机 IP 和端口访问推理服务','All interfaces allows other devices to access inference through this computer’s IP and port']);field(panels[4],p,'Port','服务端口','Service port','number');
    const concurrency=field(panels[4],p,'Concurrency','同时处理的请求数（可选）','Simultaneous requests (optional)','number',null,n?['范围 1–8；留空为 1。并发增加会提高 KV 和显存需求。','Range 1–8; empty uses 1. More concurrent requests require more KV and VRAM.']:['范围 1–4；留空为 1。当前控制台使用单卡；并发大于 1 时需关闭 SSD 缓存。','Range 1–4; empty uses 1. This console uses one GPU; concurrency above 1 requires SSD cache disabled.']);concurrency.min='1';concurrency.max=n?'8':'4';
    credentialField(panels[4],p);
    const serviceAdvanced=make('details',{class:'l-advanced l-wide'});serviceAdvanced.append(make('summary',{},tr('高级 · 服务标识与日志','Advanced · Service identity & logs')));const serviceFields=make('div',{class:'l-fields l-service-fields'});serviceAdvanced.append(serviceFields);panels[4].append(serviceAdvanced);
    field(serviceFields,p,'ModelAlias','对外模型名（可选）','Served model name (optional)','text',null,['供 API 客户端使用的稳定名称；留空沿用模型名称。','A stable name for API clients; leave empty to inherit the model name.']);
    field(serviceFields,{LogLevel:'',...p},'LogLevel','日志级别','Log level','text',[['',tr('引擎默认 · Info','Engine default · Info')],['info','Info'],['debug','Debug'],['trace','Trace'],['warning','Warning'],['error','Error'],['off',tr('关闭日志','Off')]],['通常使用 Info；排查问题时可临时选择 Debug。','Use Info normally; select Debug temporarily for troubleshooting.']);
    const native=make('details',{class:'s-card l-native'});native.append(make('summary',{},tr('引擎专属参数 · ','Engine-specific settings · ')+names[b]));const ng=make('div',{class:'l-fields'});native.append(ng);
    if(n||l){
      const detected=info.workers?.[b];
      const worker=field(ng,p,'Worker','后端程序（可选）','Backend executable (optional)','text',null,
        detected?.available?['留空自动使用已检测到的后端程序：'+detected.path,'Leave empty to use the detected backend executable: '+detected.path]:['可将后端程序放在启动器同目录或 bin 目录，再重新打开控制台；也可直接填写程序路径','Place the backend executable beside the launcher or in bin, then reopen the console; or enter its path here']);
      worker.placeholder=detected?.available?detected.path:tr('请选择已安装的后端程序','Enter the installed backend executable');
      if(!detected?.available&&!p.Worker)worker.parentElement.append(make('small',{class:'l-unavailable'},tr('未检测到默认程序','No default executable detected')));
    }
    if(n){
      const ngram=field(ng,p,'NgramDrafts','N-gram 推测 token 数','N-gram draft tokens','number',null,['依赖 MTP；0 或留空关闭 N-gram，1–63 启用。并发时后端可能缩小实际推测宽度。','Requires MTP. 0 or empty disables N-gram; 1–63 enables it. The backend may reduce its width under concurrency.']);ngram.max='63';
      const minMatch=field(ng,p,'NgramMinMatch','N-gram 最短匹配','N-gram minimum match','number',null,['同时启用 MTP 和 N-gram 时生效，范围 4–64。','Applies when both MTP and N-gram are enabled; range 4–64.']);minMatch.min='4';minMatch.max='64';
      field(ng,p,'DeviceProfile','设备性能配置','Device profile','text',[['off',tr('关闭','Off')],['auto',tr('自动','Auto')]]);

    }
    const nativeLabel=make('label',{class:'l-field l-wide'}),nativeTitle=make('span',{},tr('原生选项 · JSON 参数数组','Native options · JSON argument array'));nativeLabel.append(nativeTitle);const raw=make('textarea',{'data-native':'',rows:'2',spellcheck:'false','aria-label':tr('原生选项 · JSON 参数数组','Native options · JSON argument array')});raw.value=JSON.stringify(p.WorkerArgs||[]);nativeLabel.append(raw);help(nativeTitle,tr('原生选项','Native options'),tr('按选项和值成对填写 JSON 数组。当前允许：','Use a JSON array of option/value pairs. Currently allowed: ')+(info.native_options[b].join(', ')||tr('暂无附加选项','none')),raw);ng.append(nativeLabel);form.append(native);
    const actions=make('div',{class:'l-actions l-draft-actions'});const save=make('button',{type:'button',class:'s-button','data-action':'template'},tr('另存为模板','Save as template'));const preview=make('button',{type:'button',class:'s-button','data-action':'preview'},tr('检查配置','Check configuration'));actions.append(preview,save);heading.append(actions);const output=make('pre',{class:'l-result l-notice','data-launch-message':'','aria-live':'polite'});heading.after(output);
    const status=make('section',{class:'s-card l-runtime'}),statusTitle=make('h2',{class:'s-title'},tr('当前运行服务','Running service'));status.append(statusTitle,make('p',{class:'l-status s-badge','data-runtime-state':''},tr('读取中','Loading')));help(statusTitle,tr('当前运行服务','Running service'),tr('只管理此控制台启动的服务。停止会卸载模型并清空其内存缓存。','Only services started here are managed. Stopping unloads the model and its in-memory cache.'));const progress=make('div',{class:'l-launch-progress','data-launch-progress':'',hidden:''});progress.append(make('p',{'data-launch-phase':'',role:'status'}),make('div',{class:'l-launch-track',role:'progressbar','data-launch-track':''}),make('small',{class:'l-muted','data-launch-help':''}));status.append(progress,make('dl',{'data-runtime-details':''}));const lifecycle=make('div',{class:'l-actions'});const start=make('button',{type:'button',class:'s-button l-primary','data-action':'start'},tr('启动服务','Start service'));const stop=make('button',{type:'button',class:'s-button','data-action':'stop',disabled:''},tr('停止服务','Stop service'));lifecycle.append(start,stop);status.append(lifecycle);side.append(status);
    presetPanel(side);
    const log=make('details',{class:'s-card l-log'});log.append(make('summary',{},tr('启动日志','Startup log')));const refresh=make('button',{type:'button',class:'s-button l-secondary'},tr('刷新日志','Refresh log'));log.append(refresh,make('pre',{class:'l-result','data-log':''}));side.append(log);refresh.onclick=async()=>{try{log.querySelector('[data-log]').textContent=(await api('log')).text||tr('暂无日志','No log yet');}catch(e){message(e.message,true);}};
    const origin=make('div',{class:'l-environment'});origin.append(make('span',{class:'l-muted'},tr('后端安装目录','Backend checkout')),make('p',{class:'l-muted',style:'overflow-wrap:anywhere'},info.backend_root));log.append(origin);
    backend.onchange=()=>{
      try{
        const target=backend.value,previous=captureDraft();previous.parameters.Backend=b;perBackend[b]=previous;
        const draft=structuredClone(perBackend[target]||{version:2,parameters:{...defaults,Backend:target}});
        const oldOrder=(draft.parameters.Gpu||'').split(',').filter(Boolean);
        for(const key of sharedSettings)if(Object.hasOwn(previous.parameters,key))draft.parameters[key]=previous.parameters[key];
        const newOrder=(draft.parameters.Gpu||'').split(',').filter(Boolean),layers=draft.parameters.StageLayers||[];
        let layersCleared=false;
        if(oldOrder.join(',')!==newOrder.join(',')){
          if(layers.length===oldOrder.length&&layers.every(x=>Number.isInteger(x)&&x>0)&&oldOrder.length===newOrder.length&&newOrder.every(id=>oldOrder.includes(id))){
            draft.parameters.StageLayers=newOrder.map(id=>layers[oldOrder.indexOf(id)]);
            if(draft.raw)draft.raw.StageLayers=draft.parameters.StageLayers.join(',');
          }else{
            layersCleared=!!(draft.raw?.StageLayers?.trim()||layers.length);
            draft.parameters.StageLayers=[];if(draft.raw)draft.raw.StageLayers='';
          }
        }
        selectedPreset='';restoreDraft(draft);scheduleSave();
        if(layersCleared)message(tr('显卡选择已改变，原层数分配已清空，请重新设置。','GPU selection changed. Previous layer allocation cleared; configure it again.'));
      }catch(e){backend.value=b;message(e.message,true);}
    };
    [save,preview,start,stop].forEach(button=>button.onclick=()=>act(button.dataset.action));
    syncDependencies();
    updateStatus(runtime);
    draftStatus();
  }
  function updateStatus(status){runtime=status;if(!status)return;window.dispatchEvent(new CustomEvent('kvmem-runtime-status',{detail:status}));const running=['starting','ready'].includes(status.state);const label=root.querySelector('[data-runtime-state]');if(!label)return;const translations={stopped:['未启动','Stopped'],starting:['正在加载模型','Loading model'],ready:['服务已就绪','Ready'],exited:['进程已退出','Process exited']};label.textContent=tr(...(translations[status.state]||translations.stopped));label.dataset.state=status.state;const dl=root.querySelector('[data-runtime-details]');dl.replaceChildren();const p=status.active_config?.parameters;for(const [zh,en,value] of [['引擎','Backend',p?names[p.Backend]:null],['模型','Model',p?.Model],['监听地址','Listen address',p?p.ListenHost||'127.0.0.1':null],['本机地址','Local URL',status.url],['进程','PID',status.pid],['退出码','Exit code',status.exit_code]]){if(value!==null&&value!==undefined){dl.append(make('dt',{},tr(zh,en)),make('dd',{title:String(value)},zh==='模型'?String(value).split(/[\\/]/).pop():String(value)));}}root.querySelector('[data-action=start]').disabled=busy||running;root.querySelector('[data-action=stop]').disabled=busy||!running;const chat=document.querySelector('[data-chat-link]');if(info){const ready=status.state==='ready'&&!!status.url;chat.hidden=false;chat.setAttribute('aria-disabled',String(!ready));if(ready)chat.href=status.url+'/';else chat.removeAttribute('href');}renderLaunchProgress();}
  function renderLaunchProgress(){
    const box=root.querySelector('[data-launch-progress]');if(!box)return;
    const state=startPending?'preparing':launchFailure?'failed':runtime?.state;
    box.hidden=!['preparing','starting','ready','exited','failed'].includes(state);
    if(box.hidden)return;
    const labels={preparing:['检查配置并启动进程…','Checking settings and starting process…'],starting:['正在加载模型，等待服务就绪…','Loading model; waiting for readiness…'],ready:['启动完成，可以开始聊天','Ready to chat'],exited:['服务进程已退出','Service process exited'],failed:['启动请求未完成','Launch request did not complete']};
    box.dataset.state=state;
    const phase=box.querySelector('[data-launch-phase]'),track=box.querySelector('[data-launch-track]');
    phase.textContent=tr(...labels[state]);track.setAttribute('aria-label',phase.textContent);
    track.hidden=state==='failed'||state==='exited';
    if(state==='ready')track.setAttribute('aria-valuenow','100');else track.removeAttribute('aria-valuenow');
    box.querySelector('[data-launch-help]').textContent=state==='failed'?launchFailure:state==='exited'?tr('请查看下方运行日志中的退出原因。','Check the service log below for the cause.'):state==='ready'?tr('右上角聊天入口已可用。','Chat is available in the top right.'):tr('请稍候。大模型首次加载可能需要较长时间。','Please wait. Loading a large model for the first time may take a while.');
  }
  async function act(action){
    if(busy)return;
    if(action==='template'){templateEditor();return;}
    if(action==='preview'){await checkConfiguration();return;}
    try{
      const data=action==='stop'?{}:launchData();
      if(action==='start'&&!root.querySelector('form').reportValidity())return;
      saveEditing();busy=true;root.querySelectorAll('[data-action]').forEach(button=>button.disabled=true);
      if(action==='start'){startPending=true;launchFailure='';renderLaunchProgress();root.querySelector('[data-launch-progress]').scrollIntoView({block:'nearest',behavior:'smooth'});}
      const result=await api(action,data);startPending=false;launchFailure='';updateStatus(result);
      message(action==='start'?tr('启动请求已提交，可查看右侧状态和日志。','Launch submitted; see status and logs.'):tr('服务已停止，配置页面仍可使用。','Service stopped; configuration remains available.'));
    }catch(error){if(action==='start')launchFailure=error.message||String(error);showError(error);}
    finally{startPending=false;busy=false;root.querySelectorAll('[data-action]').forEach(button=>button.disabled=false);updateStatus(runtime);}
  }
  async function poll(){if(info&&!busy){try{updateStatus(await api('status'));}catch(e){message(e.message,true);}}setTimeout(poll,document.hidden?5000:2000);}
  window.addEventListener('kvmem-language-change',()=>{if(info){try{restoreDraft(captureDraft());}catch(e){message(e.message,true);}}document.querySelector('[data-page=config]').textContent=tr('启动配置','Launch configuration');});
  async function init(){try{info=await api('info');let saved;try{saved=JSON.parse(localStorage.getItem(DRAFT_KEY)||'null');}catch{saveError=true;}if(usableDraft(saved))restoreDraft(saved);else render(info.draft.parameters);updateStatus(info.status);if(!['starting','ready'].includes(info.status.state))document.querySelector('[data-page=config]').click();poll();}catch(e){root.replaceChildren(make('section',{class:'s-card'},tr('启动配置需要独立的本地控制台。请运行 scripts/local-console.py，然后打开它显示的地址；推理服务停止后，这个控制台仍然可用。','Launch settings require the independent local console. Run scripts/local-console.py and open its printed URL; the console remains available when inference stops.')));}}
  init();
})();
