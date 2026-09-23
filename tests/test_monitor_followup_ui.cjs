// Run with: node tests/test_monitor_followup_ui.cjs
// Dependency-free DOM smoke test; no live app or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
const html = fs.readFileSync('templates/_monitor_center.html', 'utf8');
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.textContent=''; this.classList={toggle(){}}; this.style={}; }
  set id(id) { this._id=id; }
  get id() { return this._id; }
  append(...children) { this.children.push(...children); children.forEach(c=> {if(c.id) nodes.set(c.id,c);}); }
  appendChild(child) { this.append(child); }
  prepend(child) { this.children.unshift(child); if(child.id) nodes.set(child.id,child); }
  replaceWith(child) { nodes.set(child.id,child); }
  addEventListener() {}
  querySelector() { return new Element('button'); }
}
const document={body:new Element('body'), createElement:t=>new Element(t), createTextNode:t=>({textContent:t}),
 getElementById(id) { return nodes.get(id) || null; }};
for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) { const el=new Element('div'); el.id=id; nodes.set(id,el); }
const form=document.getElementById('watch-fill-form');
form.elements={price:new Element('input'),quantity:new Element('input')};
let stream;
const context={document,window:{},console,crypto:{},setTimeout(){},setInterval(){},
 fetch:async()=>({ok:true,json:async()=>({events:[],health:{market_state:'closed'}})}),
 EventSource:class {constructor(){stream=this;}}};
const code=html.match(/<script>([\s\S]*)<\/script>/)[1];
vm.runInNewContext(code,context);
const common={name:'测试股',code:'603629',price:116,priority:'observe',message:'状态观察',triggered_at:'2026-09-22 13:25:00',
 action:'reduce',execution_status:'pending',last_execution_id:88};
stream.onmessage({data:JSON.stringify({...common,id:1,event_type:'breakout_followup'})});
const followup=nodes.get('monitor-event-1');
assert.match(followup.children[0].textContent,/持续走强/);
assert.match(followup.children[3].textContent,/不是新的买卖确认/);
assert.equal(followup.children[4].children.length,0,'observation has no feedback or undo buttons');
stream.onmessage({data:JSON.stringify({...common,id:2,event_type:'breakout'})});
const original=nodes.get('monitor-event-2');
assert(original.children[4].children.some(e=>e.textContent==='记录成交'));
assert(original.children[4].children.some(e=>e.textContent==='撤销最近成交'));
stream.onmessage({data:JSON.stringify({...common,id:1,event_type:'breakout_followup',repeat_count:2})});
assert.equal(nodes.get('monitor-event-1').children[4].children.length,0);
console.log('PASS: SSE-rendered followup is observation-only; original trade controls preserved.');
