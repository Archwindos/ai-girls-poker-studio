"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  let state = null;
  let savedConfig = "";
  let busy = false;
  let pollTimer = null;
  const dimensionNames = {card_width_mm:"卡牌宽度",card_height_mm:"卡牌高度",dpi:"DPI",box_width_mm:"牌盒宽度",box_height_mm:"牌盒高度",box_depth_mm:"牌盒厚度"};
  const suitNames = {SPADES:"黑桃",HEARTS:"红桃",CLUBS:"梅花",DIAMONDS:"方块",BLACK_JOKER:"黑王",RED_JOKER:"红王"};
  const make = (tag, text, className) => { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(className)node.className=className; return node; };
  async function request(path, options) {
    const response = await window.deckApiFetch(path, options);
    const data = await response.json();
    if(!response.ok) throw new Error((data.errors || [data.error || "本地服务没有完成请求。"]).join("\n"));
    return data;
  }
  function status(message, error=false) { el("production-state").textContent=message; el("production-state").dataset.error=String(error); }
  function switchPage(production) {
    if (!window.deckRuntimeReady) return;
    el("library-pane").hidden=production; el("production-pane").hidden=!production;
    el("show-library").setAttribute("aria-pressed",String(!production)); el("show-production").setAttribute("aria-pressed",String(production));
    if(production) load();
  }
  function dirty() { return state && JSON.stringify(readForm())!==savedConfig; }
  function updateButtons() { el("save-config").disabled=busy || !state; el("export-deck").disabled=busy || !state || dirty() || state.validation.length>0; el("preview-deck").disabled=busy || !state || dirty(); }
  function readForm() {
    const config=structuredClone(state.config);
    for(const input of el("dimension-fields").querySelectorAll("input")) config.settings[input.name]=Number(input.value);
    for(const select of el("production-slots").querySelectorAll("select")) config.slots[select.dataset.role][select.dataset.color]=select.value || null;
    for(const select of el("packaging-assets").querySelectorAll("select")) {
      if(select.dataset.face) config.box[select.dataset.face]=select.value || null; else config.back=select.value || null;
    }
    return config;
  }
  async function load() {
    if(busy) return;
    busy=true; updateButtons();
    try {
      const [result,selection]=await Promise.all([request("/api/deck-config"),request("/api/selections")]);
      state=result; state.selected=new Set(selection.selected_ids); render();
      status(`制作配置版本 ${state.config.version}，关联选图版本 ${state.config.selection_revision}。当前选图版本 ${state.selection_revision}。${state.validation.length?`还有${state.validation.length}项待补全。`:"素材已齐全。"}尺寸或槽位修改后请保存。`);
      const job=localStorage.getItem("ai-deck-production-last-job"); if(job) pollJob(job);
    } catch(error) { status(error.message,true); }
    finally { busy=false; updateButtons(); }
  }
  function optionSelect(identifier, assets, current) {
    const select=make("select"); select.id=identifier;
    const none=make("option","待指定"); none.value=""; select.append(none);
    for(const asset of assets) {
      const option=make("option",`${asset.id} · ${asset.display_name}${asset.production_ready===false?"（需设置排版）":""}`); option.value=asset.id; select.append(option);
    }
    if(current && !assets.some(asset=>asset.id===current)) { const option=make("option",`${current}（当前不可用）`); option.value=current; select.append(option); }
    select.value=current || "";
    return select;
  }
  function facePreview(role,suit,identifier,config, className="slot-image") {
    if(!identifier || !state.selected.has(identifier) || !state.registry.assets[identifier]) return make("div","先在选图册勾选原画，再指定槽位。","slot-empty");
    const img=make("img",undefined,className); img.alt=`${role} ${suitNames[suit]}制作预览`; img.loading="lazy";
    img.src=`/api/production/preview/${encodeURIComponent(role)}/${suit}.png?v=${config.version}`;
    img.addEventListener("error",()=>{ const message=make("div","无法生成预览，请检查原画和排版安全区。","slot-empty"); img.replaceWith(message); });
    return img;
  }
  function render() {
    const config=state.config,assets=Object.values(state.registry.assets);
    const galleryFor = (asset) => Object.values(state.registry.gallery).find(item=>item.production_asset_id===asset.id) || state.registry.gallery[asset.gallery_id];
    const dimensions=el("dimension-fields"); dimensions.replaceChildren();
    for(const [key,label] of Object.entries(dimensionNames)) {
      const wrap=make("label",undefined,"dimension-field"); wrap.append(make("span",label)); const input=make("input"); input.type="number"; input.name=key; input.value=config.settings[key]; input.step=key==="dpi"?"1":"0.1"; input.required=true; wrap.append(input); dimensions.append(wrap);
    }
    const slots=el("production-slots"); slots.replaceChildren();
    for(const [rank,role,name] of state.registry.families) {
      const row=make("section",undefined,"slot-row"); const title=make("h4",undefined,"slot-role"); title.append(make("strong",rank==="JOKER"?"王":rank),make("span",name)); row.append(title);
      for(const color of ["black","red"]) {
        const wrap=make("div",undefined,"slot-control"); const id=`slot-${role}-${color}`; const label=make("label",color==="black"?"黑色槽位":"红色槽位",color==="red"?"red-label":""); label.htmlFor=id;
        const allowed=assets.filter(asset=>asset.kind==="face" && asset.role===role && state.selected.has(asset.id));
        const select=optionSelect(id,allowed,config.slots[role][color]); select.dataset.role=role; select.dataset.color=color;
        const suit=role==="doubao"?(color==="red"?"RED_JOKER":"BLACK_JOKER"):(color==="red"?"HEARTS":"SPADES");
        wrap.append(label,select,facePreview(role,suit,config.slots[role][color],config));
        const selectedAsset=state.registry.assets[config.slots[role][color]];
        const hint=selectedAsset?`${selectedAsset.id} · ${selectedAsset.production_ready===false?"请先在大图中设置排版框。":"从无字原画重新排版。"}`:"尚未指定，不会用其他角色补位。";
        wrap.append(make("p",hint,"slot-hint")); row.append(wrap);
      }
      slots.append(row);
    }
    const packaging=el("packaging-assets"); packaging.replaceChildren();
    const availableBoxFaces=new Set(assets.filter(asset=>asset.kind==="box" && asset.available).map(asset=>asset.face)).size;
    const activePackaging=new Set([config.back,...Object.values(config.box)]);
    const pendingNative=Object.values(state.registry.gallery).filter(item=>!item.archived && activePackaging.has(item.production_asset_id) && item.source_quality?.native2k===false);
    const qualityNote=pendingNative.length?`实际尺寸打样包（原生2K待确认）：${pendingNative.map(item=>`${item.name}原画${item.source_quality.native_pixels.join("×")}`).join("；")}，原生2K尚未满足，可满足${config.settings.dpi}DPI实际尺寸输出。`:"";
    for(const id of ["production-quality-note","export-quality-note"]) { el(id).textContent=qualityNote; el(id).hidden=!qualityNote; }
    const backView=state.registry.gallery["BOX-BACK"]?.orientation;
    el("packaging-summary").textContent=`当前牌背：${state.registry.assets[config.back]?.display_name || "待指定"}。牌背V7整体未经严格180度对称认证。牌盒已有${availableBoxFaces}面可用，点击图片可放大并逐面比较。${backView?"盒背按90×65mm横看；65×90mm生产稿已逆时针旋转90°，顺时针转盒90°后正看，请勿再次旋转生产图。":""}六面为独立面稿，印厂需另行确认刀模。`;
    for(const face of [null,...Object.keys(state.registry.box_faces)]) {
      const kind=face?"box":"back", current=face?config.box[face]:config.back;
      const wrap=make("div",undefined,"package-control"); const id="package-"+(face?"box-"+face:"card-back"); const label=make("label",face?"牌盒"+state.registry.box_faces[face]:"牌背"); label.htmlFor=id;
      const select=optionSelect(id,assets.filter(asset=>asset.kind===kind && (!face || asset.face===face) && (!asset.alias_of || asset.id===current)),current); if(face)select.dataset.face=face;
      wrap.append(label,select);
      if(current && state.registry.assets[current]) {
        const asset=state.registry.assets[current], galleryItem=galleryFor(asset); const img=make("img",undefined,"package-image"); img.src=galleryItem?.file || "/api/production/assets/"+encodeURIComponent(current); img.alt=label.textContent; img.loading="lazy";
        if(galleryItem) {
          const button=make("button",undefined,"package-preview"); button.type="button"; button.setAttribute("aria-label",`放大${label.textContent} ${galleryItem.id}`); button.append(img);
          button.addEventListener("click",()=>openLightbox(galleryItem.id,assets.filter(entry=>["back","box"].includes(entry.kind)).map(galleryFor).filter(Boolean).map(item=>item.id),"制作素材")); wrap.append(button,make("p",`${galleryItem.id} · 点击放大`,"slot-hint"));
        } else wrap.append(img);
      }
      else wrap.append(make("div",face==="back"?"待选定Gemini/GPT黑色槽位服装后制作盒背。":"待登记制作素材。","slot-empty"));
      packaging.append(wrap);
    }
    const errors=el("production-errors"); errors.replaceChildren(...state.validation.map(error=>make("li",error))); errors.hidden=!state.validation.length; el("production-ready").hidden=!!state.validation.length;
    el("jump-validation").textContent=state.validation.length?`查看${state.validation.length}项缺项`:"查看导出检查";
    savedConfig=JSON.stringify(readForm());
    el("deck-preview-panel").hidden=true;
    document.dispatchEvent(new CustomEvent("deck-config-updated",{detail:config}));
  }
  el("show-library").addEventListener("click",()=>switchPage(false)); el("show-production").addEventListener("click",()=>switchPage(true)); el("reload-config").addEventListener("click",load);
  el("production-form").addEventListener("input",()=>{ updateButtons(); status("制作配置有未保存修改。保存后可预览或导出。"); });
  el("production-form").addEventListener("submit",async(event)=>{
    event.preventDefault(); if(busy || !state)return; const config=readForm(); busy=true; updateButtons(); status("正在保存制作配置…");
    try { const result=await request("/api/deck-config",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({base_version:state.config.version,config})}); state={...result,selected:state.selected}; render(); status(`制作配置版本 ${state.config.version} 已保存，关联选图版本 ${state.config.selection_revision}。`); }
    catch(error){status(error.message,true);} finally{busy=false;updateButtons();}
  });
  el("preview-deck").addEventListener("click",()=>{
    const gallery=el("deck-preview"); gallery.replaceChildren();
    for(const [rank,role,name] of state.registry.families) {
      const suits=role==="doubao"?["BLACK_JOKER","RED_JOKER"]:["SPADES","HEARTS","CLUBS","DIAMONDS"];
      for(const suit of suits){const color=["HEARTS","DIAMONDS","RED_JOKER"].includes(suit)?"red":"black";const figure=make("figure");figure.append(facePreview(role,suit,state.config.slots[role][color],state.config,"deck-card-image"),make("figcaption",`${rank==="JOKER"?"":rank+" "}${suitNames[suit]} · ${name}`));gallery.append(figure);}
    }
    el("deck-preview-panel").hidden=false; el("deck-preview-panel").scrollIntoView({behavior:"auto",block:"start"});
  });
  el("export-deck").addEventListener("click",async()=>{
    if(busy || dirty())return; busy=true;updateButtons();status("正在冻结原画与制作配置…");
    try{const job=await request("/api/exports",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config_version:state.config.version,selection_revision:state.selection_revision})});localStorage.setItem("ai-deck-production-last-job",job.id);pollJob(job.id);status("已开始生成发布成品。输出到01-发布成品的独立批次，本次使用提交时的配置与原画。");}
    catch(error){status(error.message,true);}finally{busy=false;updateButtons();}
  });
  async function pollJob(id){
    if(pollTimer)clearTimeout(pollTimer);pollTimer=null;
    try{const job=await request("/api/exports/"+encodeURIComponent(id));el("export-panel").hidden=false;el("export-message").textContent=`${job.message} · 配置版本 ${job.config_version} / 选图版本 ${job.selection_revision}`;el("export-progress").value=job.progress;el("export-download").hidden=job.status!=="complete";el("export-error").hidden=job.status!=="failed";el("export-error").textContent=job.error||"";el("export-location").textContent=job.output_dir || job.folder || `01-发布成品/导出批次/${job.id}`;el("screen-download").hidden=job.status!=="complete" || !job.screen_download_url;if(job.screen_download_url){el("screen-download").href=job.screen_download_url;}if(job.status==="complete"){el("export-download").href=job.download_url;el("export-download").download="ai-girls-deck.zip";}else if(job.status!=="failed"){pollTimer=setTimeout(()=>pollJob(id),1200);}}
    catch(error){el("export-panel").hidden=false;el("export-error").hidden=false;el("export-error").textContent=error.message;}
  }
})();
