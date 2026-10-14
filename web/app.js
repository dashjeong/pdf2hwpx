"use strict";

const byId = (id) => document.getElementById(id);
const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map((node) => [node.id, node]));
const state = { file: null, document: null, baseline: null, page: 0, zoom: 1, scale: 1, selected: null, busy: false, exporting: false, stopped: false, shuttingDown: false, status: null, epoch: 0, previewUrl: null, edited: new Set(), dirty: false, editVersion: 0, providers: { local: {}, sllm: {}, openai: {}, gemini: {}, ollama: { base_url: "http://127.0.0.1:11434" } }, modalProvider: "local" };
const providerNames = { local: "내 PC · 오프라인 OCR", native: "PDF 원본 글자 추출", sllm: "내 PC · 소형 AI 검수", openai: "OpenAI API", gemini: "Gemini API", ollama: "Ollama" };
const engineNames = { ...providerNames, sllm: "한국어 OCR + 소형 AI 검수" };
const clone = (value) => JSON.parse(JSON.stringify(value));
const nativeTextMeasure = document.createElement('canvas');
state.jobId = null;
state.lazyDocumentId = null;
state.pageLoading = new Map();
let toastTimer;

function mayReplaceDocument() {
  return !state.dirty || window.confirm('저장하지 않은 수정이 있어요. 편집 내용 저장을 먼저 하면 다시 열 수 있어요. 지금 문서를 바꿀까요?');
}
function lazyEdits() {
  return state.document.pages.flatMap((page, position) => page._loaded !== false &&
    [...state.edited].some((key) => key.startsWith(`${position}:`)) ? [{ position, page }] : []);
}
function documentPayload() {
  return state.lazyDocumentId ? { document_id: state.lazyDocumentId, edits: lazyEdits() } : { document: state.document };
}
async function ensurePageLoaded(position) {
  const identity = state.lazyDocumentId;
  const doc = state.document;
  if (!identity || !doc || doc.pages[position]?._loaded !== false) return;
  const key = `${identity}:${position}`;
  if (!state.pageLoading.has(key)) {
    state.pageLoading.set(key, (async () => {
      const page = await (await request(`/api/jobs/${identity}/pages/${position}`)).json();
      if (doc !== state.document || identity !== state.lazyDocumentId) return;
      const normalized = normalizeDocument({ name: doc.name, pages: [page] }).pages[0];
      normalized._loaded = true;
      doc.pages[position] = normalized;
      state.baseline.pages[position] = clone(normalized);
    })().finally(() => state.pageLoading.delete(key)));
  }
  await state.pageLoading.get(key);
}
function releaseUneditedPages() {
  if (!state.lazyDocumentId) return;
  state.document.pages.forEach((page, position) => {
    if (Math.abs(position - state.page) <= 1 || [...state.edited].some((key) => key.startsWith(`${position}:`))) return;
    page.blocks = [];
    page._loaded = false;
    state.baseline.pages[position] = { ...page, blocks: [] };
  });
}

function notice(message = "", error = false) {
  ui.notice.textContent = message;
  ui.notice.hidden = !message;
  ui.notice.classList.toggle("error", error);
}
function toast(message) {
  ui.toast.textContent = message;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 4500);
}
function errorText(error) { return error instanceof Error ? error.message : String(error || "알 수 없는 오류가 발생했습니다."); }
async function request(url, options = {}) {
  let response;
  try { response = await fetch(url, options); }
  catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("프로그램 서버에 연결할 수 없어요. 실행 창이 열려 있는지 확인해주세요.");
  }
  if (!response.ok) {
    let message = `요청을 처리할 수 없어요 (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (typeof body.error === "string") message = body.error;
      else if (typeof body.message === "string") message = body.message;
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg || "잘못된 입력").join(" · ");
    } catch (_) { /* HTTP status provides a safe fallback. */ }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response;
}
const jsonRequest = async (url, payload) => (await request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })).json();
function setBusy(value) {
  state.busy = value;
  ui.convertButton.disabled = value || state.stopped || !state.file;
  ui.chooseFileButton.disabled = value || state.stopped;
  ui.demoButton.disabled = value || state.stopped;
  ui.openProjectButton.disabled = value;
  ui.engineSelect.disabled = value || state.stopped;
  ui.pageRange.disabled = value || state.stopped;
  ui.progressSection.hidden = !value;
  updateDocumentControls();
}
function updateDocumentControls() {
  const hasDocument = Boolean(state.document?.pages.length);
  ui.convertButton.disabled = state.busy || state.stopped || state.shuttingDown || !state.file;
  for (const id of ["zoomOut", "zoomIn", "zoomValue", "saveProjectButton"]) ui[id].disabled = !hasDocument || state.busy;
  ui.checkQualityButton.disabled = !hasDocument || state.busy || state.stopped;
  ui.exportHtmlButton.disabled = !hasDocument || state.busy || state.exporting || state.stopped || state.shuttingDown;
  ui.exportHwpxButton.disabled = !hasDocument || state.busy || state.exporting || state.stopped || state.shuttingDown;
  ui.shutdownButton.disabled = state.stopped || state.shuttingDown || state.exporting;
  ui.settingsButton.disabled = state.stopped || state.shuttingDown;
  ui.chooseFileButton.disabled = state.busy || state.stopped || state.shuttingDown;
  ui.demoButton.disabled = state.busy || state.stopped || state.shuttingDown;
  ui.engineSelect.disabled = state.busy || state.stopped || state.shuttingDown;
  ui.pageRange.disabled = state.busy || state.stopped || state.shuttingDown;
  ui.previousPage.disabled = !hasDocument || state.page === 0 || state.busy;
  ui.nextPage.disabled = !hasDocument || state.page >= state.document.pages.length - 1 || state.busy;
}
function formatBytes(bytes) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function releasePreview() {
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
  ui.filePreview.replaceChildren();
  ui.filePreview.hidden = true;
}
function acceptFile(file) {
  if (!file || state.busy || state.stopped || state.shuttingDown) return;
  if (!mayReplaceDocument()) return;
  const extension = file.name.split(".").pop()?.toLowerCase();
  if (!["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"].includes(extension)) {
    notice("PDF 또는 이미지 파일을 선택해주세요. 지원 형식: PDF, JPG, PNG, WEBP, BMP, TIFF", true);
    return;
  }
  const maxUploadMb = Number(state.status?.resources?.max_upload_mb) || 1024;
  if (file.size > maxUploadMb * 1024 * 1024) {
    notice(`파일은 ${maxUploadMb}MB 이내로 올려주세요. 큰 문서는 나누어 변환할 수 있어요.`, true);
    return;
  }
  releasePreview();
  state.epoch += 1;
  state.file = file;
  ui.pageRange.value = "";
  state.document = null;
  state.lazyDocumentId = null;
  state.baseline = null;
  state.selected = null;
  state.edited.clear();
  state.dirty = false;
  state.page = 0;
  state.zoom = 1;
  ui.fileName.textContent = file.name;
  ui.fileInfo.textContent = `${extension.toUpperCase()} · ${formatBytes(file.size)} · 변환 준비 완료`;
  ui.dropzone.hidden = true;
  ui.sourceStage.hidden = true;
  ui.editorStage.hidden = true;
  ui.editorEmpty.hidden = false;
  ui.blockTools.hidden = true;
  ui.pageIndicator.textContent = "— / —";
  ui.sourceMeta.textContent = "변환 전 미리보기";
  ui.convertStep.classList.remove("active");
  ui.saveStep.classList.remove("active");
  ui.editorHint.textContent = "ⓘ 문서 변환을 누르면 글자, 표, 그림을 읽기 시작해요.";
  ui.resultTitle.textContent = "완성된 문서를 한글에서 이어서 편집하세요";
  ui.resultDescription.textContent = "HWPX로 저장하면 글자와 표를 다시 수정할 수 있어요.";
  ui.warningsList.hidden = true;
  resetReferenceQuality();
  updateMetrics();
  notice();
  let preview;
  if (extension === "pdf" && file.size <= 8 * 1024 * 1024) {
    state.previewUrl = URL.createObjectURL(file);
    preview = document.createElement("iframe");
    preview.src = `${state.previewUrl}#toolbar=0&navpanes=0`;
    preview.title = `${file.name} 원본 미리보기`;
  } else {
    preview = document.createElement("div");
    preview.className = "preview-info";
    preview.textContent = "파일을 준비했어요. 메모리를 확인해 안전한 원본 미리보기를 만들어요. 문서 변환을 눌러주세요.";
  }
  ui.filePreview.append(preview);
  ui.filePreview.hidden = false;
  ui.convertButton.disabled = false;
  updateDocumentControls();
}

function safeAssetUrl(value) {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/assets/")) return "";
    return url.pathname + url.search;
  } catch (_) { return ""; }
}
function normalizeDocument(data) {
  if (!data || typeof data !== "object" || !Array.isArray(data.pages) || !data.pages.length) throw new Error("변환 결과에 문서 페이지가 없어요. 파일을 확인하고 다시 시도해주세요.");
  if (data.pages.length > 1000) throw new Error("한 번에 열 수 있는 문서는 1,000쪽까지예요. 파일을 나누어 열어주세요.");
  const normalized = clone(data);
  normalized.name = String(data.name || "변환 문서");
  normalized.warnings = Array.isArray(data.warnings) ? data.warnings.map(String) : [];
  normalized.pages.forEach((page, pageIndex) => {
    page.width = Math.max(1, Math.min(1000000, Number(page.width) || 1240));
    page.height = Math.max(1, Math.min(1000000, Number(page.height) || 1754));
    page.image_url = safeAssetUrl(page.image_url);
    page.blocks = (Array.isArray(page.blocks) ? page.blocks : []).filter((block) => block && ["text", "table", "image"].includes(block.type));
    page.blocks.forEach((block, blockIndex) => {
      block.id = String(block.id || `p${pageIndex}-b${blockIndex}`);
      block.x = Math.max(0, Math.min(page.width - 1, Number(block.x) || 0));
      block.y = Math.max(0, Math.min(page.height - 1, Number(block.y) || 0));
      block.width = Math.max(1, Math.min(page.width - block.x, Number(block.width) || 100));
      block.height = Math.max(1, Math.min(page.height - block.y, Number(block.height) || 25));
      if (block.type === "text") block.text = String(block.text ?? "");
      if (block.type === "table") block.rows = (Array.isArray(block.rows) ? block.rows : []).map((row) => (Array.isArray(row) ? row : []).map((cell) => String(cell ?? "")));
      if (block.type === "image") block.image_url = safeAssetUrl(block.image_url);
    });
  });
  return normalized;
}
function loadDocument(data, restored = false) {
  const normalized = normalizeDocument(data);
  releasePreview();
  state.document = normalized;
  state.lazyDocumentId = data.lazy_document ? String(data.id) : null;
  state.baseline = clone(normalized);
  state.page = 0;
  state.zoom = 1;
  state.selected = null;
  state.edited.clear();
  state.dirty = false;
  ui.fileName.textContent = normalized.name;
  ui.fileInfo.textContent = `${normalized.pages.length}쪽 · ${engineNames[normalized.engine] || normalized.engine || "문서"} · ${restored ? "저장한 편집 내용" : "변환 완료"}`;
  ui.dropzone.hidden = true;
  ui.editorEmpty.hidden = true;
  ui.sourceStage.hidden = false;
  ui.editorStage.hidden = false;
  ui.blockTools.hidden = true;
  ui.convertStep.classList.add("active");
  ui.saveStep.classList.add("active");
  ui.editorHint.textContent = "ⓘ 글자나 표를 눌러 수정하세요. 선택한 부분의 위치와 크기도 조정할 수 있어요.";
  ui.resultTitle.textContent = `${normalized.pages.length}쪽 문서를 ${restored ? "불러왔어요" : "변환했어요"}`;
  const elapsed = Number(normalized.elapsed_seconds);
  ui.resultDescription.textContent = `${Number.isFinite(elapsed) && !restored ? `${elapsed.toFixed(1)}초 소요 · ` : ""}글자와 숫자를 확인하고 HWPX로 저장하세요.`;
  ui.warningsList.replaceChildren();
  normalized.warnings.forEach((warning) => { const item = document.createElement("li"); item.textContent = warning; ui.warningsList.append(item); });
  const largeFigure = normalized.engine !== "demo" && normalized.pages.some((page) => page.blocks.some((block) => block.type === "image" && block.role !== "graphics-background" && block.width * block.height > page.width * page.height * .06));
  if (largeFigure) { const item = document.createElement("li"); item.textContent = "큰 그림이나 조직도의 글자는 이미지로 남아 있을 수 있어요. 글자를 누르면 수정할 수 있는지, 작은 숫자가 원본과 같은지 따로 확인해주세요."; ui.warningsList.append(item); }
  ui.warningsList.hidden = !ui.warningsList.children.length;
  resetReferenceQuality();
  renderPage();
  updateMetrics();
  updateDocumentControls();
  if (!state.lazyDocumentId && !normalized.pages.some((page) => page.blocks.some((block) => block.type === "text" || block.type === "table"))) notice("읽어낸 글자가 없어요. 원본 해상도와 인식 설정을 확인해주세요.", true);
}
function confidenceValue(block) {
  if (block.confidence == null || block.confidence === "") return null;
  const confidence = Number(block.confidence);
  if (!Number.isFinite(confidence)) return null;
  return Math.max(0, Math.min(1, confidence > 1 ? confidence / 100 : confidence));
}
function blockElement(block) {
  let element;
  if (block.type === "image") {
    element = document.createElement("img");
    element.src = block.image_url;
    element.alt = "문서 속 그림";
    element.draggable = false;
    element.className = "edit-block image-block";
    if (block.role === "graphics-background") { element.classList.add("graphics-background"); element.alt = "원본 표 테두리와 그림 배경"; element.setAttribute("aria-hidden", "true"); }
    else element.tabIndex = 0;
  } else if (block.type === "table") {
    element = document.createElement("table");
    element.className = "edit-block table-block";
    element.setAttribute("aria-label", "편집 가능한 표");
    const columnCount = Math.max(1, ...block.rows.map((row) => row.length));
    const widths = Array.isArray(block.column_widths) && block.column_widths.length === columnCount ? block.column_widths.map((value) => Math.max(1, Number(value) || 1)) : Array(columnCount).fill(1);
    const widthSum = widths.reduce((sum, value) => sum + value, 0);
    const colgroup = document.createElement("colgroup");
    widths.forEach((width) => { const col = document.createElement("col"); col.style.width = `${width / widthSum * 100}%`; colgroup.append(col); });
    element.append(colgroup);
    const body = document.createElement("tbody");
    const rowCount = Math.max(1, block.rows.length);
    const rowHeights = Array.isArray(block.row_heights) && block.row_heights.length === rowCount ? block.row_heights.map((value) => Math.max(1, Number(value) || 1)) : Array(rowCount).fill(1);
    const rowHeightSum = rowHeights.reduce((sum, value) => sum + value, 0);
    const cellMap = new Map();
    const coveredCells = new Set();
    if (Array.isArray(block.cells)) block.cells.forEach((cell) => {
      const row = Number(cell.row ?? cell.row_index);
      const col = Number(cell.col ?? cell.column ?? cell.col_index);
      if (!Number.isInteger(row) || !Number.isInteger(col) || row < 0 || col < 0 || row >= rowCount || col >= columnCount) return;
      const rowspan = Math.max(1, Math.min(rowCount - row, Number(cell.rowspan) || 1));
      const colspan = Math.max(1, Math.min(columnCount - col, Number(cell.colspan) || 1));
      cellMap.set(`${row}:${col}`, { ...cell, rowspan, colspan });
      for (let r = row; r < row + rowspan; r += 1) for (let c = col; c < col + colspan; c += 1) if (r !== row || c !== col) coveredCells.add(`${r}:${c}`);
    });
    block.rows.forEach((row, rowIndex) => {
      const tr = document.createElement("tr");
      tr.style.height = `${rowHeights[rowIndex] / rowHeightSum * 100}%`;
      row.forEach((text, colIndex) => {
        if (coveredCells.has(`${rowIndex}:${colIndex}`)) return;
        const td = document.createElement("td");
        td.contentEditable = "plaintext-only";
        td.spellcheck = false;
        td.textContent = text;
        td.dataset.row = String(rowIndex);
        td.dataset.col = String(colIndex);
        td.setAttribute("aria-label", `표 ${rowIndex + 1}행 ${colIndex + 1}열 편집`);
        const cell = cellMap.get(`${rowIndex}:${colIndex}`);
        if (cell?.rowspan > 1) td.rowSpan = cell.rowspan;
        if (cell?.colspan > 1) td.colSpan = cell.colspan;
        const confidence = cell ? confidenceValue(cell) : null;
        if (confidence != null) {
          td.title = `셀 인식 신뢰도 ${(confidence * 100).toFixed(1)}%`;
          td.dataset.confidence = String(confidence);
          td.classList.toggle("low-confidence", ui.confidenceToggle.checked && confidence < .9);
        }
        tr.append(td);
      });
      body.append(tr);
    });
    element.append(body);
  } else {
    element = document.createElement("div");
    element.className = "edit-block text-block";
    element.contentEditable = "plaintext-only";
    element.spellcheck = false;
    element.textContent = block.text;
    element.setAttribute("role", "textbox");
    element.setAttribute("aria-label", "글자 편집");
    element.setAttribute("aria-multiline", "true");
  }
  element.dataset.blockId = block.id;
  setBlockStyle(element, block);
  // PDF text bounds use the original font. A substituted browser font can
  // wrap the last character onto the following table and hide it there.
  // Keep original single-line runs on one line and fit their display width.
  if (block.type === 'text' && block.source === 'native-pdf-text' && !block.text.includes('\n')) {
    element.style.whiteSpace = 'pre';
    element.style.wordBreak = 'normal';
    const context = nativeTextMeasure.getContext('2d');
    const size = parseFloat(element.style.fontSize);
    context.font = `${element.style.fontWeight || 400} ${size}px ${getComputedStyle(ui.editablePage).fontFamily}`;
    const measured = context.measureText(block.text).width;
    if (measured > 0) element.style.fontSize = `${Math.max(5, size * Math.min(1, Math.max(1, block.width - 2) / measured))}px`;
  }
  const confidence = confidenceValue(block);
  if (confidence != null) element.title = `인식 신뢰도 ${(confidence * 100).toFixed(1)}% · 눌러서 확인 및 수정`;
  element.classList.toggle("low-confidence", ui.confidenceToggle.checked && confidence != null && confidence < .9 && block.type !== "image");
  return element;
}
function setBlockStyle(element, block) {
  element.style.left = `${block.x}px`;
  element.style.top = `${block.y}px`;
  element.style.width = `${block.width}px`;
  element.style.height = `${block.height}px`;
  const fontSize = Number(block.font_size) || (block.type === "table" ? Math.max(12, Math.min(24, block.height / Math.max(1, block.rows.length) * .36)) : 20);
  element.style.fontSize = `${Math.max(5, Math.min(200, fontSize))}px`;
  if (typeof block.color === "string" && /^#[0-9a-f]{6}$/i.test(block.color)) element.style.color = block.color;
  if (block.font_weight || block.bold) element.style.fontWeight = block.font_weight === "bold" || block.bold ? "700" : String(Number(block.font_weight) || 400);
  if (["left", "center", "right", "justify"].includes(block.text_align)) element.style.textAlign = block.text_align;
}
async function renderPage() {
  const position = state.page;
  const doc = state.document;
  try { await ensurePageLoaded(position); }
  catch (error) { notice(errorText(error), true); return; }
  if (position !== state.page || doc !== state.document) return;
  releaseUneditedPages();
  const page = state.document?.pages[state.page];
  if (!page) return;
  state.selected = null;
  ui.blockTools.hidden = true;
  ui.sourceHighlight.hidden = true;
  ui.sourceImage.src = page.image_url;
  const sourcePage = Number.isInteger(Number(page.index)) && Number(page.index) >= 0 ? Number(page.index) + 1 : state.page + 1;
  ui.sourceImage.alt = `원본 문서 ${sourcePage}쪽`;
  ui.sourceMeta.textContent = `원본 ${sourcePage}쪽 · ${Math.round(page.width)} × ${Math.round(page.height)}`;
  ui.pageIndicator.textContent = `${state.page + 1} / ${state.document.pages.length}`;
  ui.pageIndicator.title = `변환 결과 ${state.page + 1}/${state.document.pages.length}쪽 · 원본 파일 ${sourcePage}번째 페이지`;
  ui.editablePage.replaceChildren(...[...page.blocks].sort((a, b) => Number(b.role === "graphics-background") - Number(a.role === "graphics-background")).map(blockElement));
  const overlay = document.createElement("img");
  overlay.className = "reference-overlay";
  overlay.alt = "";
  overlay.src = page.image_url;
  overlay.hidden = !ui.overlayToggle.checked;
  overlay.draggable = false;
  ui.editablePage.prepend(overlay);
  ui.editablePage.style.width = `${page.width}px`;
  ui.editablePage.style.height = `${page.height}px`;
  ui.sourceViewport.scrollTop = 0;
  ui.editorViewport.scrollTop = 0;
  resizePages();
  updateDocumentControls();
  updateMetrics();
}
function resizePages() {
  const page = state.document?.pages[state.page];
  if (!page) return;
  const available = Math.min(ui.sourceViewport.clientWidth - parseFloat(getComputedStyle(ui.sourceViewport).paddingLeft) * 2, ui.editorViewport.clientWidth - parseFloat(getComputedStyle(ui.editorViewport).paddingLeft) * 2);
  state.scale = Math.max(.00001, available / page.width) * state.zoom;
  for (const stage of [ui.sourceStage, ui.editorStage]) { stage.style.width = `${page.width * state.scale}px`; stage.style.height = `${page.height * state.scale}px`; }
  ui.editablePage.style.transform = `scale(${state.scale})`;
  ui.zoomValue.textContent = state.zoom === 1 ? "맞춤" : `${Math.round(state.zoom * 100)}%`;
  updateSourceHighlight();
}
function selectedBlock() { return state.document?.pages[state.page]?.blocks.find((block) => block.id === state.selected); }
function selectBlock(blockId) {
  state.selected = blockId;
  for (const node of ui.editablePage.children) node.classList.toggle("selected", node.dataset.blockId === blockId);
  const block = selectedBlock();
  ui.blockTools.hidden = !block;
  showModelReview(block);
  if (!block) { ui.sourceHighlight.hidden = true; return; }
  ui.selectedBlockLabel.textContent = block.type === "table" ? "선택한 표" : block.type === "image" ? "선택한 그림" : "선택한 글자";
  for (const [id, field] of [["blockX", "x"], ["blockY", "y"], ["blockWidth", "width"], ["blockHeight", "height"], ["blockFontSize", "font_size"]]) ui[id].value = Math.round(Number(block[field]) || (field === "font_size" ? 20 : 0));
  ui.fontSizeField.hidden = block.type !== "text";
  updateSourceHighlight();
}
function showModelReview(block) {
  const review = block?.model_review && typeof block.model_review === "object" ? block.model_review : null;
  const suggestion = typeof block?.suggested_text === "string" ? block.suggested_text : "";
  ui.modelReviewPanel.hidden = !review && !suggestion;
  ui.modelSuggestionSection.hidden = !suggestion.trim();
  ui.modelSuggestedText.textContent = suggestion;
  const fallback = review?.status === "confirmed" ? "소형 AI가 같은 표기로 읽었어요. 원본도 확인해주세요." : review?.status === "failed" ? "소형 AI가 검수를 마치지 못했어요. 기존 인식 글자를 원본과 비교해주세요." : "소형 AI가 다른 표기를 제안했어요.";
  ui.modelReviewMessage.textContent = typeof review?.message === "string" && review.message ? review.message : fallback;
  const differences = [];
  if (review?.numbers_preserved === false) differences.push("제안의 숫자가 기존 결과와 달라요. 원본 숫자를 먼저 확인해주세요.");
  if (review?.latin_preserved === false) differences.push("제안의 영문 표기가 기존 결과와 달라요.");
  ui.modelReviewDifference.textContent = differences.join(" ");
  ui.modelReviewDifference.hidden = !differences.length;
}
function updateSourceHighlight() {
  const block = selectedBlock();
  ui.sourceHighlight.hidden = !block;
  if (!block) return;
  for (const field of ["x", "y", "width", "height"]) ui.sourceHighlight.style[{ x: "left", y: "top", width: "width", height: "height" }[field]] = `${block[field] * state.scale}px`;
}
function markEdited(block) {
  state.edited.add(`${state.page}:${block.id}`);
  state.dirty = true;
  state.editVersion += 1;
  resetReferenceQuality(true);
  updateMetrics();
}
function updateMetrics() {
  const blocks = state.document?.pages.flatMap((page) => page.blocks) || [];
  const confidences = blocks.filter((block) => block.type !== "image").map(confidenceValue).filter((value) => value != null);
  ui.confidenceMetric.textContent = confidences.length ? `${(confidences.reduce((sum, value) => sum + value, 0) / confidences.length * 100).toFixed(1)}%` : "—";
  ui.reviewMetric.textContent = confidences.length ? `${confidences.filter((value) => value < .9).length}곳` : "—";
  ui.editedMetric.textContent = state.document ? `${state.edited.size}곳` : "—";
  if (state.lazyDocumentId) {
    ui.confidenceMetric.title = '메모리를 아끼기 위해 현재 불러온 페이지의 신뢰도를 표시합니다.';
    ui.reviewMetric.title = '현재 불러온 페이지 기준';
  }
}
function resetReferenceQuality(stale = false) {
  for (const id of ["accuracyMetric", "cerMetric", "werMetric"]) ui[id].textContent = "—";
  ui.qualityExplanation.textContent = stale ? "내용이 바뀌었어요. 정답과 비교하기를 다시 눌러 확인해주세요." : "정답 문장이 있을 때만 실제 오류율을 계산해요. 목표 정확도는 95% 이상입니다.";
}
function documentText(scope = "all") {
  if (!state.document) return "";
  const pages = scope === "page" ? [state.document.pages[state.page]] : state.document.pages;
  return pages.map((page) => [...page.blocks].sort((a, b) => Math.round(a.y / 12) - Math.round(b.y / 12) || a.x - b.x).map((block) => block.type === "text" ? block.text : block.type === "table" ? block.rows.map((row) => row.join("\t")).join("\n") : "").filter(Boolean).join("\n")).join("\n\n");
}
async function convertDocument() {
  if (!state.file || state.busy || state.stopped || state.shuttingDown) return;
  const provider = ui.engineSelect.value;
  const range = ui.pageRange.value.trim().replace(/[–—]/g, "-").replace(/\s+/g, "");
  if (range && !/^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(range)) { notice("변환할 쪽을 숫자로 입력해주세요. 예: 4-5,7-8,13. 비워두면 전체를 변환해요.", true); ui.pageRange.focus(); return; }
  const options = { provider, ...state.providers[provider], page_range: range };
  if (["openai", "gemini"].includes(provider) && !options.api_key?.trim()) { notice("API 키를 입력한 뒤 변환해주세요.", true); openSettings(provider); return; }
  const form = new FormData();
  form.append("file", state.file);
  form.append("engine", provider);
  form.append("options", JSON.stringify(options));
  const epoch = ++state.epoch;
  setBusy(true);
  notice();
  ui.progressTitle.textContent = "문서 변환을 시작해요";
  ui.progressText.textContent = "파일 준비 중";
  ui.conversionProgress.value = 0;
  try {
    const body = await (await request("/api/convert", { method: "POST", body: form })).json();
    if (body.document || body.pages) loadDocument(body.document || body);
    else if (body.id || body.job_id) await pollJob(body.id || body.job_id, epoch);
    else throw new Error("변환 작업 번호를 받지 못했어요. 다시 시도해주세요.");
  } catch (error) { if (epoch === state.epoch) notice(errorText(error), true); }
  finally { if (epoch === state.epoch) setBusy(false); }
}
async function pollJob(jobId, epoch) {
  state.jobId = jobId;
  ui.cancelConversion.disabled = false;
  while (epoch === state.epoch) {
    const job = await (await request(`/api/jobs/${encodeURIComponent(jobId)}?include_document=false`)).json();
    if (epoch !== state.epoch) return;
    const progress = Number(job.progress) || 0;
    ui.conversionProgress.value = Math.max(0, Math.min(100, progress));
    ui.progressTitle.textContent = "글자, 표, 그림을 읽고 있어요";
    ui.progressText.textContent = String(job.message || `${Math.round(ui.conversionProgress.value)}% 진행 중`);
    if (["done", "completed"].includes(job.status)) {
      if (!job.document_summary && !job.document) throw new Error("작업은 끝났지만 문서를 받지 못했어요. 다시 시도해주세요.");
      loadDocument(job.document_summary || job.document);
      state.jobId = null;
      await refreshRecovery();
      toast("변환을 마쳤어요. 원본과 비교하며 글자를 확인하세요.");
      return;
    }
    if (["error", "failed", "cancelled"].includes(job.status)) {
      state.jobId = null;
      await refreshRecovery();
      throw new Error(typeof job.error === "string" ? job.error : job.message || "문서 변환 중 오류가 발생했어요.");
    }
    await new Promise((resolve) => setTimeout(resolve, 1100));
  }
}
async function loadDemo() {
  if (state.busy || state.stopped || state.shuttingDown) return;
  if (!mayReplaceDocument()) return;
  const epoch = ++state.epoch;
  setBusy(true);
  notice();
  ui.progressTitle.textContent = "예시 문서를 준비해요";
  ui.progressText.textContent = "원본과 편집 문서 불러오는 중";
  ui.conversionProgress.value = 10;
  try {
    const result = await (await request("/api/demo")).json();
    if (result.pages || result.document) {
      state.file = null;
      loadDocument(result.document || result);
      toast("예시 문서예요. 오른쪽 글자와 표를 눌러 편집해보세요.");
    } else if (result.id || result.job_id) await pollJob(result.id || result.job_id, epoch);
    else throw new Error("예시 문서를 불러올 수 없어요.");
  } catch (error) { notice(errorText(error), true); }
  finally { if (epoch === state.epoch) setBusy(false); }
}

function filename(extension) { return `${(state.document?.name || "변환문서").replace(/\.[^.]+$/, "").replace(/[<>:"/\\|?*\u0000-\u001F]/g, "_").slice(0, 120) || "변환문서"}.${extension}`; }
function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function exportDocument(format) {
  if (!state.document || state.exporting || state.busy || state.stopped || state.shuttingDown) return;
  state.exporting = true;
  updateDocumentControls();
  const button = format === "hwpx" ? ui.exportHwpxButton : ui.exportHtmlButton;
  const label = button.textContent;
  const version = state.editVersion;
  button.textContent = "저장 준비 중…";
  notice();
  try {
    const response = await request("/api/export?download=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...documentPayload(), format }) });
    const saved = await response.json();
    const url = new URL(saved.download_url, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/downloads/')) throw new Error('저장 링크를 확인할 수 없어요.');
    const link = document.createElement('a');
    link.href = url.pathname;
    link.download = filename(format);
    document.body.append(link);
    link.click();
    link.remove();
    if (version === state.editVersion) state.dirty = false;
    toast(`${format.toUpperCase()} 파일을 저장했어요. 브라우저 다운로드 폴더를 확인하세요.`);
  } catch (error) { notice(errorText(error), true); }
  finally { button.textContent = label; state.exporting = false; updateDocumentControls(); }
}
async function saveProject() {
  if (!state.document) return false;
  try {
    if (state.lazyDocumentId && !state.stopped) {
      const saved = await jsonRequest('/api/export?download=1', { ...documentPayload(), format: 'json' });
      const link = document.createElement('a');
      link.href = saved.download_url;
      link.download = filename('scannote.json');
      document.body.append(link);
      link.click();
      link.remove();
    } else if (!state.lazyDocumentId) {
      download(new Blob([JSON.stringify({ version: 1, document: state.document }, null, 2)], { type: "application/json;charset=utf-8" }), filename("scannote.json"));
    } else throw new Error('편집 내용 저장을 먼저 마친 뒤 프로그램을 종료해 주세요.');
    state.dirty = false;
    toast("편집 내용을 저장했어요. 다시 열려면 원본 이미지 자료가 이 PC에 남아 있어야 해요.");
    return true;
  } catch (error) { notice(errorText(error), true); return false; }
}
async function openProject(file) {
  if (!file) return;
  if (!mayReplaceDocument()) return;
  if (file.size > 64 * 1024 * 1024) { notice('편집 자료는 64MB 이내로 열 수 있어요. 변환할 쪽 범위를 나누어 저장해 주세요.', true); return; }
  notice();
  try {
    const saved = JSON.parse(await file.text());
    const data = saved.document || saved;
    loadDocument(data, true);
    state.file = null;
    state.epoch += 1;
    updateDocumentControls();
    toast("저장한 편집 내용을 불러왔어요.");
  } catch (error) { notice(error instanceof SyntaxError ? "스캔노트에서 저장한 JSON 파일을 골라주세요." : errorText(error), true); }
}
function percentage(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}
async function checkQuality() {
  if (!state.document || state.busy || state.stopped) return;
  const reference = ui.referenceText.value;
  const scope = ui.qualityScope.value;
  const observed = documentText(scope);
  if (!reference.trim()) { toast("먼저 원본의 정확한 문장을 입력해주세요."); ui.referenceText.focus(); return; }
  ui.checkQualityButton.disabled = true;
  const label = ui.checkQualityButton.textContent;
  ui.checkQualityButton.textContent = "비교하는 중…";
  try {
    const payload = state.lazyDocumentId && scope === 'all' ? { ...documentPayload(), reference } : { reference, text: observed };
    const result = await jsonRequest("/api/quality", payload);
    if (reference !== ui.referenceText.value || scope !== ui.qualityScope.value || observed !== documentText(scope)) { resetReferenceQuality(true); return; }
    ui.accuracyMetric.textContent = percentage(result.accuracy ?? result.character_accuracy ?? (result.cer != null ? Math.max(0, 1 - result.cer) : null));
    ui.cerMetric.textContent = percentage(result.cer ?? result.CER);
    ui.werMetric.textContent = percentage(result.wer ?? result.WER);
    const cer = Number(result.cer ?? result.CER);
    ui.qualityExplanation.textContent = Number.isFinite(cer) ? cer <= .05 ? "정답 문장 기준으로 글자 정확도 95% 목표를 충족했어요. 표와 그림 배치도 원본과 비교해주세요." : "정답 문장 기준으로 오류가 있어요. 원본과 다른 글자를 수정하고 다시 비교해주세요." : "입력한 정답 문장과 문서 전체를 비교한 결과예요.";
  } catch (error) { notice(errorText(error), true); }
  finally { ui.checkQualityButton.textContent = label; updateDocumentControls(); }
}

async function refreshRecovery() {
  if (state.stopped) return;
  try {
    const response = await (await request('/api/jobs')).json();
    const jobs = Array.isArray(response) ? response : response.jobs || [];
    ui.recoveryJobs.replaceChildren();
    jobs.forEach((job) => {
      if (!job.can_resume) return;
      const button = document.createElement('button');
      button.className = 'button secondary small';
      button.textContent = `${job.name || '중단한 문서'} · ${job.completed_pages || 0}쪽 완료 · 이어서 변환`;
      button.disabled = state.busy;
      button.addEventListener('click', async () => {
        if (state.busy || !mayReplaceDocument()) return;
        const epoch = ++state.epoch;
        setBusy(true);
        notice();
        try {
          await jsonRequest(`/api/jobs/${job.id}/resume`, { ...state.providers[job.engine || 'local'] });
          await pollJob(job.id, epoch);
        } catch (error) { notice(errorText(error), true); }
        finally { if (epoch === state.epoch) { setBusy(false); refreshRecovery(); } }
      });
      ui.recoveryJobs.append(button);
    });
    ui.recoverySection.hidden = !ui.recoveryJobs.children.length;
  } catch (_) { /* Recovery list never prevents a new conversion. */ }
}

ui.cancelConversion.addEventListener('click', async () => {
  if (!state.jobId) return;
  ui.cancelConversion.disabled = true;
  try {
    await jsonRequest(`/api/jobs/${state.jobId}/cancel`, {});
    ui.progressText.textContent = '지금 읽는 부분까지 마치고 멈춰요. 완료한 페이지는 보관합니다.';
  } catch (error) { notice(errorText(error), true); ui.cancelConversion.disabled = false; }
});

function privacyFor(provider) {
  if (provider === "local" || provider === "sllm") return "내 PC 방식은 문서가 외부로 전송되지 않아요.";
  if (provider === "ollama") return "문서가 설정한 Ollama 서버로 전송돼요. 내 PC 주소를 사용하면 로컬에서 처리해요.";
  return `변환할 문서 이미지가 ${providerNames[provider]} 서버로 전송돼요.`;
}
function providerChanged() { ui.privacyNote.textContent = privacyFor(ui.engineSelect.value); }
function modelDescription(provider) {
  const engine = state.status?.engines?.find?.((item) => item.id === provider);
  if (engine) {
    const resources = state.status?.resources;
    const resourceInfo = resources ? `\n메모리 사용 목표: ${resources.ram_budget_gb || 8}GB 이내 · 한 쪽씩 처리\nNPU: ${resources.npu || "상태 확인 필요"}` : "";
    const reviewInfo = provider === "sllm" ? "\n한국어 OCR로 배치와 내용을 읽은 뒤, 소형 AI가 일부 글자를 추가로 확인해요.\nOCR만 사용할 때보다 시간이 더 걸려요. 제안은 원본과 비교해 직접 수정하세요." : "";
    return `${engine.available ? "✓ 사용할 수 있어요" : "설치된 모델 또는 실행 환경을 확인해주세요"}\n${engine.detail || ""}${reviewInfo}${resourceInfo}`.trim();
  }
  if (!state.status) return "프로그램 상태를 확인하지 못했어요. 실행 창이 열려 있는지 확인해주세요.";
  return "이 방식의 모델 설치 상태를 서버에서 확인해주세요.";
}
function rememberModalSettings() {
  const provider = state.modalProvider;
  if (["local", "sllm"].includes(provider)) return;
  state.providers[provider] = { api_key: ui.apiKey.value.trim(), base_url: ui.baseUrl.value.trim(), model: ui.modelName.value.trim() };
}
function renderProviderSettings(provider) {
  state.modalProvider = provider;
  ui.settingsProvider.value = provider;
  const local = ["local", "sllm"].includes(provider);
  const settings = state.providers[provider] || {};
  ui.remoteSettings.hidden = local;
  ui.localModelInfo.hidden = !local;
  ui.localModelInfo.textContent = modelDescription(provider);
  ui.apiKey.value = settings.api_key || "";
  ui.baseUrl.value = settings.base_url || "";
  ui.modelName.value = settings.model || "";
  ui.apiKey.disabled = provider === "ollama";
  ui.apiKey.placeholder = provider === "ollama" ? "Ollama 로컬 서버는 키가 필요 없어요" : "제공받은 API 키를 입력하세요";
  ui.baseUrl.placeholder = provider === "ollama" ? "http://127.0.0.1:11434" : "기본 서버 주소를 사용합니다";
  ui.providerPrivacy.textContent = privacyFor(provider);
  ui.settingsTestResult.hidden = true;
}
function openSettings(provider = ui.engineSelect.value) {
  renderProviderSettings(provider);
  if (!ui.settingsDialog.open) ui.settingsDialog.showModal();
}
async function testSettings() {
  rememberModalSettings();
  const provider = state.modalProvider;
  ui.testSettingsButton.disabled = true;
  ui.settingsProvider.disabled = true;
  ui.settingsTestResult.hidden = false;
  ui.settingsTestResult.classList.remove("error");
  ui.settingsTestResult.textContent = "연결을 확인하고 있어요…";
  try {
    const result = await jsonRequest("/api/settings/test", { provider, engine: provider, ...state.providers[provider] });
    const success = result.ok !== false && result.available !== false && result.success !== false;
    ui.settingsTestResult.textContent = String(result.message || result.detail || (success ? "연결을 확인했어요." : "연결을 확인할 수 없어요. 입력한 설정을 확인해주세요."));
    ui.settingsTestResult.classList.toggle("error", !success);
  } catch (error) { ui.settingsTestResult.textContent = errorText(error); ui.settingsTestResult.classList.add("error"); }
  finally { ui.testSettingsButton.disabled = false; ui.settingsProvider.disabled = false; }
}
async function loadStatus() {
  try {
    state.status = await (await request("/api/status")).json();
    if (!state.file && !state.document) ui.fileInfo.textContent = `PDF · JPG · PNG · WEBP · BMP · TIFF · ${state.status.resources?.max_upload_mb || 1024}MB 이내`;
    const local = state.status.engines?.find?.((engine) => engine.id === "local");
    ui.serviceStatus.classList.toggle("error", local?.available === false);
    ui.serviceStatus.replaceChildren();
    const dot = document.createElement("i");
    ui.serviceStatus.append(dot, document.createTextNode(local?.available === false ? "로컬 OCR 모델 확인 필요" : "내 PC에서 실행 중"));
    ui.serviceStatus.title = local?.detail || "로컬 서버 실행 상태";
    if (ui.settingsDialog.open) ui.localModelInfo.textContent = modelDescription(state.modalProvider);
  } catch (error) {
    ui.serviceStatus.classList.add("error");
    ui.serviceStatus.replaceChildren(document.createElement("i"), document.createTextNode("실행 서버 연결 확인"));
    ui.serviceStatus.title = errorText(error);
  }
}

async function shutdownProgram(discard = false) {
  if (state.stopped || state.shuttingDown) return;
  ui.shutdownDialog.close();
  state.shuttingDown = true;
  updateDocumentControls();
  ui.shutdownButton.textContent = "종료 중…";
  try {
    await request("/api/shutdown", { method: "POST" });
    state.stopped = true;
    state.shuttingDown = false;
    state.epoch += 1;
    state.busy = false;
    if (discard) state.dirty = false;
    ui.progressSection.hidden = true;
    ui.shutdownButton.textContent = "종료됨";
    ui.serviceStatus.classList.remove("error");
    ui.serviceStatus.replaceChildren(document.createElement("i"), document.createTextNode("프로그램 종료됨"));
    notice("프로그램을 종료했어요. 이 화면을 닫아도 됩니다. 화면에 남은 편집 내용은 JSON 파일로 저장할 수 있어요.");
    updateDocumentControls();
  } catch (error) {
    state.shuttingDown = false;
    ui.shutdownButton.textContent = "프로그램 종료";
    notice(error.status === 409 ? error.message || "문서 변환이 끝난 뒤 종료해주세요." : errorText(error), true);
    updateDocumentControls();
  }
}

ui.chooseFileButton.addEventListener("click", () => { ui.fileInput.value = ""; ui.fileInput.click(); });
ui.dropzone.addEventListener("click", () => { ui.fileInput.value = ""; ui.fileInput.click(); });
ui.fileInput.addEventListener("change", () => acceptFile(ui.fileInput.files[0]));
for (const eventName of ["dragenter", "dragover"]) ui.sourceViewport.addEventListener(eventName, (event) => { event.preventDefault(); if (!state.busy) ui.dropzone.classList.add("dragover"); });
for (const eventName of ["dragleave", "drop"]) ui.sourceViewport.addEventListener(eventName, (event) => { event.preventDefault(); ui.dropzone.classList.remove("dragover"); });
ui.sourceViewport.addEventListener("drop", (event) => acceptFile(event.dataTransfer.files[0]));
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => event.preventDefault());
ui.convertButton.addEventListener("click", convertDocument);
ui.demoButton.addEventListener("click", loadDemo);
ui.engineSelect.addEventListener("change", providerChanged);
ui.previousPage.addEventListener("click", () => { if (state.page > 0) { state.page -= 1; renderPage(); } });
ui.nextPage.addEventListener("click", () => { if (state.document && state.page < state.document.pages.length - 1) { state.page += 1; renderPage(); } });
ui.zoomOut.addEventListener("click", () => { state.zoom = Math.max(.5, Math.round((state.zoom - .25) * 100) / 100); resizePages(); });
ui.zoomIn.addEventListener("click", () => { state.zoom = Math.min(3, Math.round((state.zoom + .25) * 100) / 100); resizePages(); });
ui.zoomValue.addEventListener("click", () => { state.zoom = 1; resizePages(); });
ui.overlayToggle.addEventListener("change", () => { const overlay = ui.editablePage.querySelector(".reference-overlay"); if (overlay) overlay.hidden = !ui.overlayToggle.checked; });
ui.confidenceToggle.addEventListener("change", () => {
  const page = state.document?.pages[state.page];
  if (!page) return;
  for (const node of ui.editablePage.children) {
    const block = page.blocks.find((item) => item.id === node.dataset.blockId);
    const confidence = block ? confidenceValue(block) : null;
    node.classList.toggle("low-confidence", ui.confidenceToggle.checked && confidence != null && confidence < .9 && block.type !== "image");
    for (const cell of node.querySelectorAll("td[data-confidence]")) cell.classList.toggle("low-confidence", ui.confidenceToggle.checked && Number(cell.dataset.confidence) < .9);
  }
});
ui.editablePage.addEventListener("click", (event) => { const node = event.target.closest("[data-block-id]"); selectBlock(node?.dataset.blockId || null); });
ui.editablePage.addEventListener("focusin", (event) => { const node = event.target.closest("[data-block-id]"); if (node) selectBlock(node.dataset.blockId); });
ui.editablePage.addEventListener("input", (event) => {
  const node = event.target.closest("[data-block-id]");
  const block = state.document?.pages[state.page]?.blocks.find((item) => item.id === node?.dataset.blockId);
  if (!block) return;
  if (block.type === "text") block.text = node.innerText.replace(/\r\n/g, "\n");
  else if (block.type === "table") {
    const td = event.target.closest("td");
    if (td) block.rows[Number(td.dataset.row)][Number(td.dataset.col)] = td.innerText.replace(/\r\n/g, "\n");
  }
  markEdited(block);
});
// Paste is plain text even on browsers without contenteditable=plaintext-only support.
ui.editablePage.addEventListener("paste", (event) => {
  if (!event.target.isContentEditable) return;
  event.preventDefault();
  const text = event.clipboardData.getData("text/plain");
  const selection = window.getSelection();
  if (!selection?.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (!ui.editablePage.contains(range.commonAncestorContainer)) return;
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
  event.target.dispatchEvent(new Event("input", { bubbles: true }));
});
for (const [id, field] of [["blockX", "x"], ["blockY", "y"], ["blockWidth", "width"], ["blockHeight", "height"], ["blockFontSize", "font_size"]]) ui[id].addEventListener("change", () => {
  const block = selectedBlock();
  const page = state.document?.pages[state.page];
  if (!block || !page) return;
  const value = Number(ui[id].value);
  if (!Number.isFinite(value)) return;
  if (field === "x") block.x = Math.max(0, Math.min(page.width - block.width, value));
  if (field === "y") block.y = Math.max(0, Math.min(page.height - block.height, value));
  if (field === "width") block.width = Math.max(1, Math.min(page.width - block.x, value));
  if (field === "height") block.height = Math.max(1, Math.min(page.height - block.y, value));
  if (field === "font_size") block.font_size = Math.max(5, Math.min(160, value));
  const node = [...ui.editablePage.children].find((item) => item.dataset.blockId === block.id);
  if (node) setBlockStyle(node, block);
  ui[id].value = Math.round(block[field]);
  updateSourceHighlight();
  markEdited(block);
});
ui.restoreBlock.addEventListener("click", () => {
  const block = selectedBlock();
  const original = state.baseline?.pages[state.page]?.blocks.find((item) => item.id === state.selected);
  if (!block || !original) return;
  const id = block.id;
  Object.keys(block).forEach((key) => delete block[key]);
  Object.assign(block, clone(original));
  const node = [...ui.editablePage.children].find((item) => item.dataset.blockId === id);
  node?.replaceWith(blockElement(block));
  state.edited.delete(`${state.page}:${id}`);
  state.editVersion += 1;
  state.dirty = true;
  selectBlock(id);
  resetReferenceQuality(true);
  updateMetrics();
});
ui.deselectBlock.addEventListener("click", () => selectBlock(null));
ui.exportHwpxButton.addEventListener("click", () => exportDocument("hwpx"));
ui.exportHtmlButton.addEventListener("click", () => exportDocument("html"));
ui.saveProjectButton.addEventListener("click", saveProject);
ui.openProjectButton.addEventListener("click", () => { ui.projectInput.value = ""; ui.projectInput.click(); });
ui.projectInput.addEventListener("change", () => openProject(ui.projectInput.files[0]));
ui.checkQualityButton.addEventListener("click", checkQuality);
ui.referenceText.addEventListener("input", () => resetReferenceQuality());
ui.qualityScope.addEventListener("change", () => resetReferenceQuality());
ui.settingsButton.addEventListener("click", () => openSettings());
ui.closeSettingsButton.addEventListener("click", () => ui.settingsDialog.close());
ui.settingsDialog.addEventListener("click", (event) => { if (event.target === ui.settingsDialog) { const rect = ui.settingsDialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) ui.settingsDialog.close(); } });
ui.settingsProvider.addEventListener("change", () => { const next = ui.settingsProvider.value; rememberModalSettings(); renderProviderSettings(next); });
ui.settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  rememberModalSettings();
  ui.engineSelect.value = state.modalProvider;
  providerChanged();
  ui.settingsDialog.close();
  toast("인식 설정을 적용했어요.");
});
ui.testSettingsButton.addEventListener("click", testSettings);
ui.shutdownButton.addEventListener("click", () => { if (state.dirty) ui.shutdownDialog.showModal(); else shutdownProgram(); });
ui.cancelShutdownButton.addEventListener("click", () => ui.shutdownDialog.close());
ui.discardShutdownButton.addEventListener("click", () => shutdownProgram(true));
ui.saveShutdownButton.addEventListener("click", async () => { if (await saveProject()) shutdownProgram(); });
ui.sourceImage.addEventListener("error", () => { if (state.document && !state.stopped) notice("원본 이미지를 불러오지 못했어요. 저장한 편집 내용은 원본 이미지 자료가 이 PC에 있을 때 다시 열 수 있어요.", true); });
let syncingScroll = false;
for (const [from, to] of [[ui.sourceViewport, ui.editorViewport], [ui.editorViewport, ui.sourceViewport]]) from.addEventListener("scroll", () => {
  if (!state.document || syncingScroll) return;
  syncingScroll = true;
  to.scrollTop = from.scrollTop;
  to.scrollLeft = from.scrollLeft;
  requestAnimationFrame(() => { syncingScroll = false; });
}, { passive: true });
new ResizeObserver(resizePages).observe(ui.editorViewport);
window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
loadStatus();
refreshRecovery();
providerChanged();
