"use strict";

window.deckApiFetch = (url, options = {}) => {
  const headers = new Headers(options.headers);
  if (["POST", "PUT", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase())) {
    if (!window.deckRuntimeReady) return Promise.reject(new Error("请先连接新版工坊。"));
    headers.set("X-Deck-Code-Version", window.deckCodeFingerprint);
  }
  return fetch(url, {...options, headers});
};

const $ = (id) => document.getElementById(id);
let items = [];
let selected = new Set();
let showingSelected = false;
let readyToSave = false;
let saving = false;
let restoring = false;
let loadingLatest = false;
let saveFailed = false;
let conflict = false;
let serverRevision = null;
let savedSignature = "";
let savedAt = null;
let historyEntries = [];
let historyCurrentRevision = null;
let previewRevision = null;
let historyLoading = false;
let historyRequest = 0;
let lightboxIds = [];
let lightboxIndex = 0;
let lightboxReadOnly = false;
let lightboxContext = "选择记录";
let lightboxArtwork = false;
const cards = new Map();
const itemById = new Map();
const materialButtons = new Map();
let materialConfig = null;
let configuringMaterial = false;

function setStatus(message, state = "") {
  $("save-status").textContent = message;
  $("save-status").dataset.state = state;
}

function showNotice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function formatTime(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "保存时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(date);
}

function selectedIds() {
  return [...items.filter((item) => selected.has(item.id)).map((item) => item.id),
    ...[...selected].filter((id) => !itemById.has(id)).sort()];
}

function signature(ids) {
  return JSON.stringify([...new Set(ids)].sort());
}

function hasPendingSelection() {
  return readyToSave && (saveFailed || conflict || signature(selectedIds()) !== savedSignature);
}

function unknownIds(ids = selectedIds()) {
  return ids.filter((id) => !itemById.has(id));
}

function canEdit() {
  return readyToSave && !restoring && !loadingLatest && !unknownIds().length;
}

function readSnapshot(data) {
  if (!data || !Array.isArray(data.selected_ids) || data.selected_ids.some((id) => typeof id !== "string") || !Number.isInteger(data.revision) || data.revision < 0) {
    throw new Error("无法读取保存结果，请重新运行根目录的“启动选图.cmd”后再试");
  }
  if (data.selected_export) showSelectionOutput(data.selected_export);
  return data;
}

function showSelectionOutput(result) {
  $("selection-output").textContent = result.status === "complete"
    ? `所选原画已输出：${result.directory}`
    : `选择已保存，但原画输出未完成：${result.error || "请重试"}`;
  $("selection-output").dataset.error = String(result.status !== "complete");
  $("sync-selection").hidden = result.status === "complete";
}

function applySnapshot(data, message) {
  readSnapshot(data);
  selected = new Set(data.selected_ids);
  serverRevision = data.revision;
  savedSignature = signature(data.selected_ids);
  savedAt = data.updated_at;
  readyToSave = true;
  conflict = false;
  saveFailed = false;
  $("conflict-notice").hidden = true;
  $("resume-summary").textContent = `${message} ${selected.size} 张。保存于 ${formatTime(savedAt)}。`;
  const missing = unknownIds();
  showNotice(missing.length ? `已保存选择中有当前图库没有的编号（${missing.join("、")}）。本页暂不能更改或保存，请先导出 JSON 保留完整选择。` : "");
}

function visibleItems() {
  const model = $("model-filter").value;
  return items.filter((item) => (!model || item.model === model) && (!showingSelected || selected.has(item.id)));
}

function refresh() {
  const visible = visibleItems();
  const visibleIds = new Set(visible.map((item) => item.id));
  for (const item of items) {
    const card = cards.get(item.id);
    card.element.hidden = !visibleIds.has(item.id);
    card.element.classList.toggle("selected", selected.has(item.id));
    card.checkbox.checked = selected.has(item.id);
    card.checkbox.disabled = !canEdit();
  }
  $("selected-count").textContent = selected.size;
  $("result-count").textContent = `显示 ${visible.length} / ${items.length} 张`;
  $("empty-state").hidden = visible.length > 0;
  $("empty-state").textContent = showingSelected ? "这里还没有已选图片。切回“全部”，勾选喜欢的图片。" : "这个人物暂时没有图片。";
  $("copy-selection").disabled = !selected.size;
  $("export-selection").disabled = !readyToSave;
  $("open-history").disabled = !readyToSave || restoring || loadingLatest;
  $("retry-save").hidden = !saveFailed || conflict || !hasPendingSelection();
  $("retry-save").disabled = saving || restoring || loadingLatest;
  $("load-latest").disabled = saving || restoring || loadingLatest;
  $("load-latest").textContent = loadingLatest ? "正在读取…" : "读取最新选择";
  $("view-all").classList.toggle("active", !showingSelected);
  $("view-selected").classList.toggle("active", showingSelected);
  $("view-all").setAttribute("aria-pressed", String(!showingSelected));
  $("view-selected").setAttribute("aria-pressed", String(showingSelected));
  if ($("lightbox").open) refreshLightbox();
  refreshHistoryControls();
}

function changeSelection(id, value) {
  if (!canEdit() || itemById.get(id)?.production_asset_id || itemById.get(id)?.archived) return;
  if (value) selected.add(id); else selected.delete(id);
  refresh();
  if (conflict) return;
  if (saveFailed && hasPendingSelection()) {
    setStatus("未确认保存，本页勾选已保留", "error");
  } else {
    saveFailed = false;
    saveSelection();
  }
}

function handleConflict(message = "保存版本已变化，请读取最新选择", detail = "已保存的选择发生了变化，可能来自另一页，也可能是上次保存后没有收到确认。你在本页的勾选仍保留，可先导出。读取最新选择后，会替换本页勾选。") {
  conflict = true;
  saveFailed = false;
  setStatus(message, "error");
  $("conflict-message").textContent = detail;
  $("conflict-notice").hidden = false;
  if ($("history-dialog").open) $("history-dialog").close();
  if ($("lightbox").open) $("lightbox").close();
  refresh();
  setTimeout(() => $("load-latest").focus(), 0);
}

async function saveSelection() {
  if (saving || restoring || loadingLatest || !readyToSave || conflict || unknownIds().length) return;
  if (!hasPendingSelection()) {
    saveFailed = false;
    setStatus("已自动保存", "saved");
    refresh();
    return;
  }
  let retryingFailedSave = saveFailed;
  saving = true;
  saveFailed = false;
  setStatus("正在保存…");
  refresh();
  try {
    while (hasPendingSelection() || retryingFailedSave) {
      retryingFailedSave = false;
      const sendingIds = selectedIds();
      const response = await window.deckApiFetch("api/selections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_ids: sendingIds, base_revision: serverRevision }),
        cache: "no-store",
      });
      if (response.status === 409) { handleConflict(); return; }
      if (!response.ok) throw new Error("保存失败");
      const data = readSnapshot(await response.json());
      serverRevision = data.revision;
      savedSignature = signature(data.selected_ids);
      savedAt = data.updated_at;
      $("resume-summary").textContent = `上次保存了 ${data.selected_ids.length} 张。保存于 ${formatTime(savedAt)}。`;
    }
    setStatus("已自动保存", "saved");
  } catch (error) {
    saveFailed = true;
    setStatus("未确认保存，本页勾选已保留", "error");
  } finally {
    saving = false;
    refresh();
    if ($("history-dialog").open) loadHistory();
  }
}

async function loadLatest() {
  if (saving || restoring || loadingLatest) return;
  loadingLatest = true;
  setStatus("正在读取最新选择…");
  refresh();
  try {
    const response = await fetch("api/selections", { cache: "no-store" });
    if (!response.ok) throw new Error("读取失败");
    applySnapshot(await response.json(), "已读取最新选择");
    showingSelected = selected.size > 0;
    $("model-filter").value = "";
    setStatus("已读取最新选择", "saved");
  } catch (error) {
    setStatus("读取失败，本页勾选仍保留", "error");
    showNotice("暂时无法读取最新选择。请确认本地服务仍在运行，再点击“读取最新选择”。本页勾选仍可导出。");
  } finally {
    loadingLatest = false;
    refresh();
  }
}

function createCard(item) {
  const element = document.createElement("article");
  element.className = "art-card";
  const imageButton = document.createElement("button");
  imageButton.type = "button";
  imageButton.className = "image-button";
  imageButton.setAttribute("aria-label", `查看 ${item.id} ${item.name} 大图`);
  const image = document.createElement("img");
  image.src = item.file;
  image.alt = `${item.id} · ${item.name} · ${item.stage}`;
  image.loading = "lazy";
  image.decoding = "async";
  imageButton.append(image);
  imageButton.addEventListener("click", () => openLightbox(item.id));
  const info = document.createElement("div");
  info.className = "card-info";
  const firstLine = document.createElement("div");
  firstLine.className = "card-first-line";
  const id = document.createElement("span");
  id.className = "card-id";
  id.textContent = item.id;
  const label = document.createElement("label");
  label.className = "select-label";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.disabled = true;
  checkbox.setAttribute("aria-label", `选择 ${item.id}`);
  checkbox.addEventListener("change", () => changeSelection(item.id, checkbox.checked));
  const labelText = document.createElement("span");
  labelText.textContent = "选择";
  label.append(checkbox, labelText);
  firstLine.append(id);
  if (item.production_asset_id) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.model === "back" ? "设为牌背" : "用于此面";
    button.disabled = true;
    button.addEventListener("click", () => useMaterial(item));
    materialButtons.set(item.id, button);
    firstLine.append(button);
  } else if (item.archived) {
    const archivedLabel = document.createElement("span");
    archivedLabel.textContent = "旧版回看";
    firstLine.append(archivedLabel);
  } else firstLine.append(label);
  const name = document.createElement("h2");
  name.className = "card-name";
  name.textContent = item.name;
  const stage = document.createElement("p");
  stage.className = "card-stage";
  stage.textContent = item.stage;
  info.append(firstLine, name, stage);
  if (item.note && item.note !== "待选择；未预选。") {
    const note = document.createElement("p");
    note.className = "card-note";
    note.textContent = item.note;
    info.append(note);
  }
  element.append(imageButton, info);
  cards.set(item.id, { element, checkbox });
  return element;
}

function refreshMaterialActions() {
  for (const [id,button] of materialButtons) {
    const item = itemById.get(id);
    const current = item.model === "back" ? materialConfig?.back : materialConfig?.box?.[item.production_asset_id.slice(4)];
    const active = current === item.production_asset_id;
    button.disabled = configuringMaterial || !materialConfig || active;
    button.textContent = active ? (item.model === "back" ? "当前牌背" : "当前使用") : (item.model === "back" ? "设为牌背" : "用于此面");
  }
}

async function readMaterialConfig() {
  const response = await fetch("api/deck-config", { cache: "no-store" });
  if (!response.ok) throw new Error("暂时无法读取制作配置，请刷新后重试。");
  const data = await response.json();
  materialConfig = data.config;
  refreshMaterialActions();
  return data;
}

async function useMaterial(item) {
  if (configuringMaterial) return;
  configuringMaterial = true;
  refreshMaterialActions();
  try {
    const latest = await readMaterialConfig();
    const config = structuredClone(latest.config);
    if (item.model === "back") config.back = item.production_asset_id;
    else config.box[item.production_asset_id.slice(4)] = item.production_asset_id;
    const response = await window.deckApiFetch("api/deck-config", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_version: latest.config.version, config }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "制作配置未保存，请刷新后重试。");
    materialConfig = data.config;
    showNotice(`${item.id} 已用于${item.model === "back" ? "牌背" : item.name}，制作配置版本 ${materialConfig.version} 已保存。`);
  } catch (error) { showNotice(error.message); }
  finally { configuringMaterial = false; refreshMaterialActions(); }
}

document.addEventListener("deck-config-updated", (event) => {
  materialConfig = event.detail;
  refreshMaterialActions();
});

function previewEntry() {
  return historyEntries.find((entry) => entry.revision === previewRevision);
}

function refreshHistoryControls() {
  const entry = previewEntry();
  const busy = saving || restoring || loadingLatest;
  $("restore-selection").disabled = !entry || historyLoading || busy || conflict || hasPendingSelection() || unknownIds().length > 0 || unknownIds(entry?.selected_ids || []).length > 0 || entry.revision === serverRevision;
  $("restore-selection").textContent = restoring ? "正在使用这次选择…" : "用这版继续修改";
  $("history-retry-save").hidden = !saveFailed || conflict || !hasPendingSelection();
  $("history-retry-save").disabled = busy;
  if (restoring) $("history-help").textContent = "正在保存这次选择，请稍候。";
  else if (saving) $("history-help").textContent = "正在保存本页更改，保存后就能使用这版。";
  else if (conflict) $("history-help").textContent = "请先关闭记录，导出本页勾选或读取最新选择。";
  else if (hasPendingSelection()) $("history-help").textContent = "本页更改尚未确认保存，请先重试保存。";
  else if (unknownIds().length || (entry && unknownIds(entry.selected_ids).length)) $("history-help").textContent = "有编号不在当前图库中，暂不能使用。请先导出本页选择。";
  else if (entry?.revision === serverRevision) $("history-help").textContent = "这就是本页当前使用的选择，可关闭窗口继续修改。";
  else $("history-help").textContent = "使用后会保留现有记录，并另存这次选择。";
}

function renderHistoryPreview() {
  const entry = previewEntry();
  $("history-thumbnails").replaceChildren();
  $("history-empty").hidden = true;
  if (!entry) {
    $("history-preview-title").textContent = "选择一次记录回看";
    $("history-preview-count").textContent = "";
    refreshHistoryControls();
    return;
  }
  $("history-preview-title").textContent = formatTime(entry.updated_at);
  $("history-preview-count").textContent = `${entry.selected_ids.length} 张已选`;
  const previewIds = entry.selected_ids.filter((id) => itemById.has(id));
  for (const id of previewIds) {
    const item = itemById.get(id);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-thumbnail";
    button.setAttribute("aria-label", `查看记录中的 ${id} ${item.name} 大图`);
    const image = document.createElement("img");
    image.src = item.file;
    image.alt = `${item.name} · ${item.stage}`;
    image.loading = "lazy";
    image.decoding = "async";
    const caption = document.createElement("span");
    caption.textContent = id;
    button.append(image, caption);
    button.addEventListener("click", () => openLightbox(id, previewIds));
    $("history-thumbnails").append(button);
  }
  if (!previewIds.length) {
    $("history-empty").hidden = false;
    $("history-empty").textContent = entry.selected_ids.length ? "这次记录中的图片不在当前图库中。" : "这次没有选择图片。";
  }
  for (const button of $("history-list").children) {
    const active = Number(button.dataset.revision) === previewRevision;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  refreshHistoryControls();
}

function renderHistory() {
  $("history-list").replaceChildren();
  for (const entry of historyEntries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-entry";
    button.dataset.revision = entry.revision;
    const date = document.createElement("span");
    date.className = "history-entry-time";
    date.textContent = formatTime(entry.updated_at);
    const detail = document.createElement("span");
    detail.className = "history-entry-detail";
    detail.textContent = `${entry.selected_ids.length} 张已选${entry.revision === historyCurrentRevision ? "　当前保存" : ""}`;
    button.append(date, detail);
    button.addEventListener("click", () => { previewRevision = entry.revision; renderHistoryPreview(); });
    $("history-list").append(button);
  }
  renderHistoryPreview();
}

async function loadHistory() {
  const request = ++historyRequest;
  historyLoading = true;
  $("history-status").textContent = "正在读取选择记录…";
  $("retry-history").hidden = true;
  refreshHistoryControls();
  try {
    const response = await fetch("api/history", { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取记录");
    const data = await response.json();
    if (!Array.isArray(data.entries) || !Number.isInteger(data.current_revision)) throw new Error("无法读取记录");
    data.entries.forEach(readSnapshot);
    if (request !== historyRequest) return;
    historyEntries = data.entries;
    historyCurrentRevision = data.current_revision;
    if (!previewEntry()) previewRevision = historyEntries[0]?.revision ?? null;
    $("history-status").textContent = historyEntries.length ? `共 ${historyEntries.length} 次保存，最近的在前。点击缩略图可看大图。` : "还没有保存记录，选择图片后会自动记录。";
    renderHistory();
  } catch (error) {
    if (request !== historyRequest) return;
    $("history-status").textContent = "暂时无法读取选择记录。请确认本地服务仍在运行，再重新读取。";
    $("retry-history").hidden = false;
  } finally {
    if (request === historyRequest) {
      historyLoading = false;
      refreshHistoryControls();
    }
  }
}

async function restoreSelection() {
  const entry = previewEntry();
  if (!entry || $("restore-selection").disabled) return;
  restoring = true;
  setStatus("正在使用记录中的选择…");
  $("history-status").textContent = "正在保存这次选择…";
  refresh();
  try {
    const response = await window.deckApiFetch("api/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision: entry.revision, base_revision: serverRevision }),
      cache: "no-store",
    });
    if (response.status === 409) { handleConflict(); return; }
    if (!response.ok) throw new Error("无法使用记录");
    applySnapshot(await response.json(), "已使用记录中的选择");
    showingSelected = selected.size > 0;
    $("model-filter").value = "";
    setStatus("已另存这次选择", "saved");
    $("history-dialog").close();
  } catch (error) {
    handleConflict("未确认恢复结果，请读取最新选择后继续", "这次恢复的结果尚未确认。你在本页原有的勾选仍保留，可先导出。请读取最新选择后再继续；读取后会替换本页勾选。");
  } finally {
    restoring = false;
    refresh();
  }
}

function openLightbox(id, previewIds = null, context = "选择记录") {
  lightboxReadOnly = previewIds !== null;
  lightboxContext = context;
  lightboxArtwork = false;
  lightboxIds = previewIds || visibleItems().map((item) => item.id);
  lightboxIndex = lightboxIds.indexOf(id);
  $("lightbox-selection").hidden = lightboxReadOnly;
  $("lightbox").showModal();
  refreshLightbox();
}

function refreshLightbox() {
  const item = itemById.get(lightboxIds[lightboxIndex]);
  if (!item) return;
  $("lightbox").dataset.assetId = item.id;
  $("open-layout").hidden = !window.deckRuntimeReady || ["back","box"].includes(item.model) || !!item.production_asset_id;
  $("lightbox-selection").hidden = lightboxReadOnly || !!item.production_asset_id || !!item.archived;
  $("lightbox-title").textContent = `${item.id} · ${item.name}${lightboxReadOnly ? `（${lightboxContext}）` : ""}`;
  $("lightbox-stage").textContent = item.stage;
  $("lightbox-note").textContent = item.note || "";
  $("lightbox-note").hidden = !item.note;
  if (!item.artwork_file) lightboxArtwork = false;
  $("lightbox-artwork-controls").hidden = !item.artwork_file;
  for (const [id,active] of [["lightbox-show-card",!lightboxArtwork],["lightbox-show-artwork",lightboxArtwork]]) {
    $(id).setAttribute("aria-pressed",String(active));
    $(id).classList.toggle("active",active);
  }
  $("lightbox-image").src = lightboxArtwork ? item.artwork_file : item.file;
  $("lightbox-image").alt = `${item.name} · ${lightboxArtwork ? "无字原画" : item.stage}`;
  $("lightbox-checkbox").checked = selected.has(item.id);
  $("lightbox-checkbox").disabled = lightboxReadOnly || !canEdit();
  $("lightbox-position").textContent = `${lightboxIndex + 1} / ${lightboxIds.length}`;
  $("previous-image").disabled = lightboxIds.length < 2;
  $("next-image").disabled = lightboxIds.length < 2;
}

function stepImage(direction) {
  if (!lightboxIds.length) return;
  lightboxIndex = (lightboxIndex + direction + lightboxIds.length) % lightboxIds.length;
  refreshLightbox();
}

function exportSelection() {
  const data = { exported_at: new Date().toISOString(), selected_ids: selectedIds(), items: items.filter((item) => selected.has(item.id)) };
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "ai-girls-selections.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("model-filter").addEventListener("change", refresh);
$("view-all").addEventListener("click", () => { showingSelected = false; refresh(); });
$("view-selected").addEventListener("click", () => { showingSelected = true; refresh(); });
$("retry-save").addEventListener("click", saveSelection);
$("history-retry-save").addEventListener("click", saveSelection);
$("load-latest").addEventListener("click", loadLatest);
$("open-history").addEventListener("click", () => {
  previewRevision = null;
  $("history-dialog").showModal();
  loadHistory();
});
$("close-history").addEventListener("click", () => $("history-dialog").close());
$("retry-history").addEventListener("click", loadHistory);
$("restore-selection").addEventListener("click", restoreSelection);
$("close-lightbox").addEventListener("click", () => $("lightbox").close());
$("previous-image").addEventListener("click", () => stepImage(-1));
$("next-image").addEventListener("click", () => stepImage(1));
$("lightbox-show-card").addEventListener("click", () => { lightboxArtwork = false; refreshLightbox(); });
$("lightbox-show-artwork").addEventListener("click", () => { lightboxArtwork = true; refreshLightbox(); });
$("lightbox-checkbox").addEventListener("change", (event) => changeSelection(lightboxIds[lightboxIndex], event.target.checked));
for (const id of ["lightbox", "history-dialog"]) {
  $(id).addEventListener("click", (event) => {
    if (event.target !== $(id)) return;
    const rect = $(id).getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $(id).close();
  });
}
document.addEventListener("keydown", (event) => {
  if (!$("lightbox").open) return;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    stepImage(event.key === "ArrowLeft" ? -1 : 1);
  }
});
window.addEventListener("beforeunload", (event) => {
  if (hasPendingSelection() || saving || restoring) { event.preventDefault(); event.returnValue = ""; }
});

$("copy-selection").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(selectedIds().join("\n"));
    $("copy-selection").textContent = "已复制";
    setTimeout(() => { $("copy-selection").textContent = "复制编号"; }, 1800);
  } catch (error) {
    showNotice("复制未获浏览器允许，请使用“导出 JSON”保存所选编号。");
  }
});
$("export-selection").addEventListener("click", exportSelection);
$("export-local-selection").addEventListener("click", exportSelection);

async function init() {
  if (location.protocol === "file:") {
    setStatus("请通过本地服务打开", "error");
    $("result-count").textContent = "尚未连接图库";
    $("resume-summary").textContent = "通过本地服务打开后，会自动恢复上次选择。";
    showNotice("请运行项目根目录的“启动选图.cmd”。浏览器直接打开文件无法读取图库或保存选择。");
    return;
  }
  try {
    const healthResponse = await fetch("api/health", { cache: "no-store" });
    const health = await healthResponse.json();
    if (!healthResponse.ok || health.api_version < 3) {
      $("runtime-notice").hidden = false;
      $("runtime-notice").textContent = "这是旧版服务页面。请双击项目根目录的「启动选图.cmd」，它会打开新版工坊。此页暂停编辑，已有选择已保留。";
      $("show-production").disabled = true;
      throw new Error("请通过启动入口打开新版工坊");
    }
    window.deckRuntimeReady = true;
    window.deckCodeFingerprint = health.code_fingerprint;
    $("open-import").disabled = false;
    const response = await fetch("catalog.json", { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取图库");
    const catalog = await response.json();
    items = catalog.groups.flatMap((group) => group.items);
    for (const item of items) itemById.set(item.id, item);
    for (const group of catalog.groups) {
      const option = document.createElement("option");
      option.value = group.slug;
      option.textContent = `${group.name}（${group.items.length}）`;
      $("model-filter").append(option);
    }
    $("model-filter").disabled = false;
    $("gallery").append(...items.map(createCard));
    if (catalog.not_drawn_this_round?.length) $("footnote").textContent += ` ${catalog.not_drawn_this_round.join("、")} 尚未制作。`;
    refresh();
    const selectionResponse = await fetch("api/selections", { cache: "no-store" });
    if (!selectionResponse.ok) throw new Error("无法读取已保存选择");
    applySnapshot(await selectionResponse.json(), "已恢复上次选择");
    showingSelected = selected.size > 0;
    refresh();
    setStatus("已读取上次选择", "saved");
    readMaterialConfig().catch((error) => showNotice(error.message));
    document.dispatchEvent(new CustomEvent("library-ready", {detail: {catalog, health}}));
  } catch (error) {
    setStatus("未连接保存服务", "error");
    $("resume-summary").textContent = "尚未恢复上次选择。";
    showNotice(`${error.message}。请确认本地服务仍在运行，然后刷新页面；读取成功前暂不能更改选择。`);
  }
}

init();
