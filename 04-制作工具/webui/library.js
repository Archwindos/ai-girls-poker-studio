"use strict";
(() => {
  const el = id => document.getElementById(id);
  const text = (id, message, error = false) => { el(id).textContent = message; el(id).dataset.error = String(error); };
  const option = (value, label) => { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; };
  let groups = [], editing = null, regions = [], active = 0, loading = false;
  async function request(url, options) {
    const response = await window.deckApiFetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error((data.errors || [data.error || "操作未完成"]).join("\n"));
    return data;
  }
  const jsonOptions = (method, value) => ({method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
  el("sync-selection").addEventListener("click", async () => {
    el("sync-selection").disabled = true;
    try { const result = await request("/api/selections/export", jsonOptions("POST", {})); showSelectionOutput(result.selected_export); }
    catch (error) { showSelectionOutput({status:"failed",error:error.message}); }
    finally { el("sync-selection").disabled = false; }
  });
  document.addEventListener("library-ready", event => {
    groups = event.detail.catalog.groups.filter(group => !["back", "box"].includes(group.slug));
    el("import-role").replaceChildren(...groups.map(group => option(group.slug, group.name)));
    el("import-role").dispatchEvent(new Event("change"));
  });
  el("open-import").addEventListener("click", () => {
    text("import-status", "");
    el("import-dialog").showModal();
  });
  el("close-import").addEventListener("click", () => el("import-dialog").close());
  el("import-role").addEventListener("change", () => {
    el("import-id").placeholder = `${el("import-role").value.toUpperCase()}-NEW01`;
  });
  el("import-kind").addEventListener("change", () => {
    const kind = el("import-kind").value;
    el("import-role-label").hidden = kind !== "face";
    el("import-face-label").hidden = kind !== "box";
    el("import-layout-section").hidden = kind !== "face";
    el("import-bleed-label").hidden = kind === "face";
    el("import-art-help").textContent = kind === "face" ? "人物与花边可以在原画内。角标和模型名称应留空，由制作程序添加。单图不超过 25 MB。" : "请上传完整面稿。盒前、盒背的生产方向均为竖向 65×90；横看群像请事先旋转到生产方向。单图不超过 25 MB。";
    el("import-id").placeholder = kind === "face" ? `${el("import-role").value.toUpperCase()}-NEW01` : `${kind.toUpperCase()}-NEW01`;
  });
  el("import-form").addEventListener("submit", async event => {
    event.preventDefault();
    const file = el("import-file").files[0];
    if (!file || file.size > 25 * 1024 * 1024) { text("import-status", "请选择不超过 25 MB 的图片。", true); return; }
    el("import-submit").disabled = true;
    text("import-status", "正在保存原画与编号…");
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file);
      });
      const kind = el("import-kind").value;
      const payload = {id: el("import-id").value.trim(), kind, role: el("import-role").value,
        display_name: el("import-name").value.trim(), filename: file.name, image_base64: dataUrl.split(",")[1]};
      if (kind !== "face") { payload.has_bleed = el("import-bleed").checked; delete payload.role; }
      if (kind === "back") payload.back_safety = {method: "edge-background"};
      if (kind === "box") payload.face = el("import-face").value;
      const layoutFile = el("import-layout").files[0];
      if (kind === "face" && layoutFile) { const data = JSON.parse(await layoutFile.text()); payload.layout = data.layout || data; }
      await request("/api/library/import", jsonOptions("POST", payload));
      text("import-status", "已加入素材库。正在重新读取图库…");
      location.reload();
    } catch (error) { text("import-status", error.message, true); }
    finally { el("import-submit").disabled = false; }
  });
  function currentId() { return el("lightbox-title").textContent.match(/[A-Z][A-Z0-9]*-\d[A-Z0-9_-]*|[A-Z][A-Z0-9]*-NEW[A-Z0-9_-]*/)?.[0]; }
  // The lightbox provides a stable ID; its visible title is only a fallback for older pages.
  el("open-layout").addEventListener("click", async () => {
    const id = el("lightbox").dataset.assetId || currentId();
    if (!id || loading) return;
    loading = true;
    try {
      const data = await request(`/api/library/${encodeURIComponent(id)}/layout`);
      const fields = ["top_index_box","bottom_index_box","name_box","name_boxes","art_box","name_color","background","coordinate_system"];
      editing = {id, revision: data.library_revision, layout: structuredClone(Object.fromEntries(
        Object.entries(data.layout || {}).filter(([key]) => fields.includes(key))))};
      const layout = editing.layout;
      layout.top_index_box ||= [.055, .045, .160, .22];
      layout.bottom_index_box ||= [.840, .775, .945, .950];
      layout.name_boxes ||= [{box: [.37, .025, .63, .052], rotation_deg: 0}, {box: [.37, .948, .63, .975], rotation_deg: 180}];
      regions = [
        {label: "左上角标", box: layout.top_index_box},
        {label: "右下角标", box: layout.bottom_index_box},
        ...layout.name_boxes.map((entry, index) => ({label: `模型名 ${index + 1} · ${entry.rotation_deg || 0}°`, box: entry.box})),
      ];
      el("layout-title").textContent = `${id} · 排版框`;
      el("layout-image").src = `/api/library/${encodeURIComponent(id)}/art?v=${data.library_revision}`;
      el("layout-region").replaceChildren(...regions.map((region, index) => option(index, region.label)));
      el("layout-color").value = /^#[a-f0-9]{6}$/i.test(layout.name_color || "") ? layout.name_color : "#34494c";
      active = 0; renderBoxes(); text("layout-status", data.production_ready ? "已读取当前排版。" : "此图尚未完成排版，请检查所有区域。");
      el("layout-dialog").showModal();
    } catch (error) { showNotice(error.message); }
    finally { loading = false; }
  });
  el("close-layout").addEventListener("click", () => el("layout-dialog").close());
  function fields() {
    const [x1, y1, x2, y2] = regions[active].box;
    [x1, y1, x2 - x1, y2 - y1].forEach((value, i) => { el(["layout-x", "layout-y", "layout-w", "layout-h"][i]).value = (value * 100).toFixed(2); });
    el("layout-region").value = active;
  }
  function renderBoxes() {
    el("layout-boxes").replaceChildren();
    regions.forEach((region, index) => {
      const [x1, y1, x2, y2] = region.box;
      const box = document.createElement("div"); box.className = "layout-box" + (index === active ? " active" : "");
      Object.assign(box.style, {left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%`});
      const label = document.createElement("span"); label.textContent = region.label;
      const grip = document.createElement("i"); grip.className = "layout-grip";
      box.append(label, grip); el("layout-boxes").append(box);
      box.addEventListener("pointerdown", event => {
        event.preventDefault(); active = index; fields();
        el("layout-boxes").querySelectorAll(".layout-box").forEach((node, i) => node.classList.toggle("active", i === index));
        const rect = el("layout-stage").getBoundingClientRect(), start = [...region.box], origin = [event.clientX, event.clientY];
        const resize = event.target === grip; box.setPointerCapture(event.pointerId);
        const move = next => {
          const dx = (next.clientX - origin[0]) / rect.width, dy = (next.clientY - origin[1]) / rect.height;
          if (resize) { region.box[2] = Math.min(1, Math.max(start[0] + .008, start[2] + dx)); region.box[3] = Math.min(1, Math.max(start[1] + .008, start[3] + dy)); }
          else { const w = start[2] - start[0], h = start[3] - start[1]; region.box[0] = Math.min(1 - w, Math.max(0, start[0] + dx)); region.box[1] = Math.min(1 - h, Math.max(0, start[1] + dy)); region.box[2] = region.box[0] + w; region.box[3] = region.box[1] + h; }
          const [a,b,c,d] = region.box;
          Object.assign(box.style, {left: `${a*100}%`, top: `${b*100}%`, width: `${(c-a)*100}%`, height: `${(d-b)*100}%`}); fields();
        };
        const end = () => { box.removeEventListener("pointermove", move); box.removeEventListener("pointerup", end); box.removeEventListener("pointercancel", end); };
        box.addEventListener("pointermove", move); box.addEventListener("pointerup", end); box.addEventListener("pointercancel", end);
      });
    });
    fields();
  }
  el("layout-region").addEventListener("change", () => { active = Number(el("layout-region").value); renderBoxes(); });
  for (const id of ["layout-x", "layout-y", "layout-w", "layout-h"]) el(id).addEventListener("change", () => {
    const [x,y,w,h] = ["layout-x","layout-y","layout-w","layout-h"].map(key => Number(el(key).value) / 100);
    if (![x,y,w,h].every(Number.isFinite) || x < 0 || y < 0 || w <= 0 || h <= 0 || x+w > 1 || y+h > 1) { text("layout-status", "区域必须完整落在画面内。", true); fields(); return; }
    regions[active].box.splice(0,4,x,y,x+w,y+h); renderBoxes();
  });
  el("layout-form").addEventListener("submit", async event => {
    event.preventDefault(); el("layout-submit").disabled = true;
    try {
      editing.layout.name_color = el("layout-color").value;
      editing.layout.coordinate_system = "normalized_xyxy";
      const data = await request(`/api/library/${encodeURIComponent(editing.id)}/layout`, jsonOptions("PUT", {base_revision: editing.revision, layout: editing.layout}));
      editing.revision = data.library_revision;
      text("layout-status", "排版已保存。请在制作页重新读取配置并查看预览。");
    } catch (error) { text("layout-status", error.message, true); }
    finally { el("layout-submit").disabled = false; }
  });
})();
