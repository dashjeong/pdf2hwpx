"use strict";
// Exercise the real editor handlers without installing a browser or loading
// models. This checks saved schema/state; browser hit-testing remains separate.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");

class Node {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase(); this.id = id; this.style = {};
    this.dataset = {}; this.attributes = {}; this.children = []; this.events = {};
    this.value = ""; this.textContent = ""; this.hidden = false; this.checked = false;
    this.classes = new Set();
    this.classList = { add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      toggle: (name, force) => { const add = force ?? !this.classes.has(name); if (add) this.classes.add(name); else this.classes.delete(name); return add; } };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  append(...nodes) { nodes.forEach((node) => { node.parent = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(name, callback) { this.events[name] = callback; }
  cloneNode() { const node = new Node(this.tagName); node.attributes = { ...this.attributes }; node.classes = new Set(this.classes); return node; }
  getContext() { return { font: "", measureText: (text) => ({ width: text.length * 20 }) }; }
  matches(selector) { return selector.split(",").some((item) => item === "[data-block-id]" ? this.dataset.blockId != null : item === "td" ? this.tagName === "TD" : item.startsWith(".") ? this.classes.has(item.slice(1)) || (this.className || "").split(" ").includes(item.slice(1)) : false); }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
  querySelectorAll(selector) { return this.children.flatMap((node) => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { if (selector === '[value="cell"]') return this.option || (this.option = new Node("option")); return this.querySelectorAll(selector)[0] || null; }
  dispatchEvent(event) { this.events[event.type]?.(event); }
}
const ids = [...fs.readFileSync(path.join(root, "web/index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
const nodes = Object.fromEntries(ids.map((id) => [id, new Node("div", id)]));
const context = vm.createContext({ console, URL, Blob, Set, Map, Event, location: { origin: "http://127.0.0.1:18080" },
  document: { createElement: (tag) => new Node(tag), createElementNS: (_ns, tag) => new Node(tag),
    querySelectorAll: () => Object.values(nodes), getElementById: (id) => nodes[id], addEventListener() {} },
  window: { location: { origin: "http://127.0.0.1:18080" }, addEventListener() {}, getSelection: () => null },
  getComputedStyle: () => ({ fontFamily: "sans-serif", paddingLeft: "24" }),
  ResizeObserver: class { observe() {} }, requestAnimationFrame: (callback) => callback(),
  setTimeout: () => 1, clearTimeout() {}, fetch: () => { throw new Error("Unexpected network request"); } });
const app = fs.readFileSync(path.join(root, "web/app.js"), "utf8").replace(/loadStatus\(\);\s*refreshRecovery\(\);\s*providerChanged\(\);\s*$/, "");
vm.runInContext(app, context, { filename: "web/app.js" });
const run = (source) => vm.runInContext(source, context);

run(`state.document = normalizeDocument({ name: '테스트', pages: [{ width: 500, height: 500, blocks: [
  {id:'line',type:'shape',shape:'line',x:20,y:30,width:100,height:0,stroke_color:'url(javascript:bad)',stroke_width:1,points:[[0,0],[100,0]]},
  {id:'rect',type:'shape',shape:'rect',x:150,y:30,width:100,height:80,fill_color:'#aBcDef',stroke_color:null},
  {id:'table',type:'table',x:30,y:140,width:200,height:100,rows:[['10','20']],border_color:'#112233',border_width:2,fill_color:'#DDEEFF',cells:[null,{row:0,col:0,fill_color:null,borders:{left:{color:'#CC0000',width:5},right:{color:'#00AA00',width:3},top:{color:null,width:2},bottom:{color:'#0000AA',width:1}}}]},
  {id:'native',type:'text',source:'native-pdf-text',x:30,y:260,width:60,height:30,font_size:20,text:'원본 글자',source_font:'Original Font Alias',model_review:{status:'confirmed',message:'원본 확인',numbers_preserved:true}},
  {id:'old-image',type:'image',role:'graphics-background',x:0,y:0,width:500,height:500,image_url:'/api/assets/demo/page.png'}
]}]}); state.baseline=clone(state.document); state.scale=.25; state.page=0;`);
assert.equal(run("state.document.pages[0].blocks[0].height"), 0);
assert.equal(run("state.document.pages[0].blocks[0].stroke_color"), "#52635D");
assert.equal(run("state.document.pages[0].blocks[1].fill_color"), "#ABCDEF");
assert.equal(run("state.document.pages[0].blocks[1].stroke_color"), null);
assert.equal(run("state.document.pages[0].blocks[2].cells.length"), 1);
run("ui.editablePage.replaceChildren(...state.document.pages[0].blocks.map(blockElement)); selectBlock('line');");
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderLeft"), "5px solid #CC0000");
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderRight"), "3px solid #00AA00");
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderTop"), "0px solid #112233");
assert.equal(run("selectedBlock().height"), 0);
assert.equal(run("ui.blockHeight.min"), "0");
assert.equal(run("ui.editablePage.children[0].children[0].children[0].getAttribute('y2')"), "0");
assert.ok(Number(run("ui.editablePage.children[0].children[0].children[1].getAttribute('stroke-width')")) * .25 >= 5);
run("ui.blockStrokeColor.value='#CC0000'; ui.blockStrokeColor.events.input(); ui.blockStrokeWidth.value='3.5'; ui.blockStrokeWidth.events.change();");
assert.equal(run("selectedBlock().stroke_color"), "#CC0000");
assert.equal(run("selectedBlock().stroke_width"), 3.5);
assert.equal(run("selectedBlock().height"), 0);
run("ui.blockWidth.value='200'; ui.blockWidth.events.change();");
assert.equal(run("selectedBlock().points[1][0]"), 200);
assert.equal(run("normalizeDocument(JSON.parse(JSON.stringify(state.document))).pages[0].blocks[0].height"), 0);

run("selectBlock('table',{row:0,col:0}); ui.blockFillEnabled.checked=true; ui.blockFillColor.value='#FFCC00'; ui.blockFillEnabled.events.change();");
assert.equal(run("selectedBlock().cells[0].fill_color"), "#FFCC00");
assert.equal(run("selectedBlock().fill_color"), "#DDEEFF");
assert.equal(run("selectedBlock().cells[0].borders.left.width"), 5);
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderLeft"), "5px solid #CC0000");
run("ui.blockFillEnabled.checked=false; ui.blockFillEnabled.events.change();");
assert.equal(run("selectedBlock().cells[0].fill_color"), null);
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.backgroundColor"), "transparent");
run("selectedBlock().cells.push({row:0,col:1,borders:{left:{color:'#00CCDD',width:3}}}); ui.tableStyleScope.value='all'; applyAppearance('fill_color','#DDEEFF');");
assert.equal(run("selectedBlock().cells[0].borders.left.width"), 5);
assert.equal(run("selectedBlock().cells[1].borders.left.width"), 3);
run("selectBlock('table',{row:0,col:0}); applyAppearance('fill_color',null); ui.blockStrokeColor.value='#884422'; ui.blockStrokeColor.events.input();");
assert.equal(run("selectedBlock().cells[0].borders"), undefined);
assert.equal(run("selectedBlock().cells[1].borders.left.width"), 3);
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderLeft"), "2px solid #884422");
run("ui.blockStrokeColor.value='#112233'; ui.blockStrokeColor.events.input(); ui.blockStrokeEnabled.checked=false; ui.blockStrokeEnabled.events.change();");
assert.equal(run("selectedBlock().cells[0].border_width"), 0);
assert.equal(run("ui.editablePage.querySelectorAll('td')[0].style.borderLeft"), "0px solid #112233");
run("ui.blockStrokeEnabled.checked=true; ui.blockStrokeEnabled.events.change();");
assert.equal(run("selectedBlock().cells[0].border_width"), 1);
run("ui.tableStyleScope.value='all'; ui.blockStrokeWidth.value='4'; ui.blockStrokeWidth.events.change();");
assert.equal(run("selectedBlock().border_width"), 4);
assert.equal(run("selectedBlock().cells[0].border_width"), 4);
assert.equal(run("selectedBlock().cells[0].borders"), undefined);
assert.equal(run("selectedBlock().cells[1].borders"), undefined);
assert.equal(run("ui.editablePage.querySelectorAll('td')[1].style.borderLeft"), "4px solid #112233");

const nativeNode = nodes.editablePage.children[3];
assert.ok(parseFloat(nativeNode.style.fontSize) < 20);
nativeNode.innerText = "길게 수정한 본문은 원래 글자 크기를 유지하고 자연스럽게 줄바꿈해야 합니다.";
nodes.editablePage.events.input({ target: nativeNode });
assert.equal(run("state.document.pages[0].blocks[3].text_edited"), true);
assert.equal(nativeNode.style.fontSize, "20px");
assert.equal(nativeNode.style.whiteSpace, "pre-wrap");
assert.equal(run("blockElement(normalizeDocument(JSON.parse(JSON.stringify(state.document))).pages[0].blocks[3]).style.fontSize"), "20px");
assert.equal(run("normalizeDocument(JSON.parse(JSON.stringify(state.document))).pages[0].blocks[3].source_font"), "Original Font Alias");
assert.equal(run("normalizeDocument(JSON.parse(JSON.stringify(state.document))).pages[0].blocks[3].model_review.message"), "원본 확인");
const legacy = nodes.editablePage.children[4];
assert.equal(legacy.tabIndex, 0);
assert.equal(legacy.getAttribute("aria-hidden"), undefined);
assert.ok(run("state.dirty"));
const polygon = run("blockElement(normalizeDocument({pages:[{width:100,height:100,blocks:[{type:'shape',shape:'polyline',closed:true,width:80,height:80,points:[[0,0],[80,0],[40,80]],fill_color:'#112233'}]}]}).pages[0].blocks[0])");
assert.equal(polygon.children[0].children[0].tagName, "POLYGON");
run("globalThis.dottedCellTable=normalizeDocument({pages:[{width:500,height:500,blocks:[{type:'table',width:300,height:100,rows:[['표 내용 123']],borders:{top:{style:'DOT',width:1,color:'#444444'},bottom:{style:'DASH',width:2,color:'#444444'}}}]}]}).pages[0].blocks[0]");
assert.equal(run("blockElement(dottedCellTable).querySelectorAll('td')[0].style.borderTop"), "1px dotted #444444");
assert.equal(run("blockElement(dottedCellTable).querySelectorAll('td')[0].style.borderBottom"), "2px dashed #444444");
assert.equal(run("normalizeDocument(JSON.parse(JSON.stringify({pages:[{width:500,height:500,blocks:[dottedCellTable]}]}))).pages[0].blocks[0].borders.top.style"), "DOT");
for (const [style, dash] of [["DOT", "2 4"], ["DASH", "8 4"]]) {
  const dotted = run(`normalizeDocument(JSON.parse(JSON.stringify({pages:[{width:100,height:100,blocks:[{type:'shape',shape:'line',width:80,height:0,points:[[0,0],[80,0]],stroke_width:2,stroke_style:'${style}'}]}]}))).pages[0].blocks[0]`);
  assert.equal(dotted.stroke_style, style);
  context.dottedFixture = dotted;
  assert.equal(run("blockElement(dottedFixture).children[0].children[0].getAttribute('stroke-dasharray')"), dash);
}
run("globalThis.longShapeDocument={pages:[{width:500,height:500,blocks:[{type:'shape',shape:'polyline',width:499,height:499,points:Array.from({length:5000},(_,i)=>[i/20,(i%100)/5])}]}]}; globalThis.longRoundTrip=normalizeDocument(JSON.parse(JSON.stringify(normalizeDocument(longShapeDocument))));");
assert.equal(run("longRoundTrip.pages[0].blocks[0].points.length"), 5000);
assert.equal(run("JSON.stringify(longRoundTrip.pages[0].blocks[0].points.at(-1))"), run("JSON.stringify(longShapeDocument.pages[0].blocks[0].points.at(-1))"));
assert.equal(run("blockElement(longRoundTrip.pages[0].blocks[0]).children[0].children[0].getAttribute('points').split(' ').length"), 5000);
assert.throws(() => run("normalizeDocument({pages:[{width:500,height:500,blocks:[{type:'shape',shape:'polyline',width:100,height:100,points:Array.from({length:10001},()=>[0,0])}]}]})"), /10,000/);
assert.equal(run("state.document.pages[0].blocks[0].width"), 200);
if (process.argv[2] === "--export-fixture") {
  const target = path.resolve(process.argv[3]);
  assert.ok(target.startsWith(root + path.sep), "Fixture output must stay in the workspace");
  const fixture = JSON.parse(run("JSON.stringify(state.document)"));
  fixture.pages[0].blocks = fixture.pages[0].blocks.filter((block) => block.type !== "image");
  fixture.pages[0].blocks.push({ id: "polygon", type: "shape", shape: "polyline", closed: true,
    x: 300, y: 30, width: 100, height: 80, points: [[0, 0], [100, 0], [50, 80]],
    stroke_color: "#0055AA", stroke_width: 2, fill_color: "#EEDDCC" });
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, JSON.stringify(fixture), "utf8");
}
console.log("Editor shape/style/native-edit regressions passed (DOM harness; no browser hit-testing claim).");
