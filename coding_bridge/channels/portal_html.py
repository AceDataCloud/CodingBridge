"""The portal's single-page UI, embedded as a string so it ships in the wheel.

Kept dependency-free on purpose: no build step, no CDN, no runtime network for
assets — a local operator tool must work offline and inside a pip install. The
look is a compact, modern neutral theme (light + dark via ``prefers-color-scheme``).
``serve()`` substitutes ``__PORTAL_TOKEN__`` before sending this to the browser.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="referrer" content="no-referrer" />
<title>Coding Bridge · Channels</title>
<style>
:root{
  --bg:#f7f7f8; --card:#ffffff; --fg:#18181b; --muted:#71717a; --border:#e4e4e7;
  --accent:#4f46e5; --accent-fg:#ffffff; --ok:#16a34a; --danger:#dc2626;
  --chip:#eef2ff; --chip-fg:#4338ca; --shadow:0 1px 2px rgba(0,0,0,.06),0 8px 24px rgba(0,0,0,.06);
  --radius:14px;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0b0b0e;--card:#161619;--fg:#fafafa;--muted:#a1a1aa;--border:#27272a;
  --accent:#6366f1;--chip:#1e1b4b;--chip-fg:#c7d2fe;--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.5);}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 80px}
header.top{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,var(--accent),#a855f7);
  display:grid;place-items:center;color:#fff;font-weight:700;font-size:18px;box-shadow:var(--shadow)}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-top:1px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:20px;margin-bottom:18px}
.card h2{font-size:14px;font-weight:650;margin:0 0 2px;letter-spacing:.01em}
.card .hint{color:var(--muted);font-size:12.5px;margin:0 0 16px}
.row{display:flex;align-items:center;gap:12px}
.acct{display:flex;align-items:center;gap:12px}
.avatar{width:44px;height:44px;border-radius:50%;object-fit:cover;background:var(--chip);
  display:grid;place-items:center;color:var(--chip-fg);font-weight:650;font-size:16px;flex:0 0 auto;border:1px solid var(--border)}
.avatar.sm{width:30px;height:30px;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px}
.dot.off{background:var(--muted)}
.pill{font-size:12px;color:var(--muted)}
label.fld{display:block;font-size:12.5px;font-weight:600;color:var(--muted);margin:16px 0 7px}
label.fld:first-of-type{margin-top:4px}
input[type=text],input[type=number],select{width:100%;padding:9px 11px;border:1px solid var(--border);
  border-radius:10px;background:var(--bg);color:var(--fg);font:inherit;outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 25%,transparent)}
.seg{display:inline-flex;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:3px;gap:3px}
.seg button{border:0;background:transparent;color:var(--muted);padding:7px 14px;border-radius:8px;
  font:inherit;font-weight:600;cursor:pointer}
.seg button.on{background:var(--card);color:var(--fg);box-shadow:var(--shadow)}
.search{position:relative}
.results{position:absolute;z-index:20;left:0;right:0;top:calc(100% + 6px);background:var(--card);
  border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);max-height:280px;overflow:auto;display:none}
.results.show{display:block}
.opt{display:flex;align-items:center;gap:11px;padding:9px 12px;cursor:pointer}
.opt:hover{background:var(--bg)}
.opt .nm{font-weight:600;font-size:14px}
.opt .id{color:var(--muted);font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:8px;background:var(--chip);color:var(--chip-fg);
  border-radius:999px;padding:5px 6px 5px 8px;font-size:13px;font-weight:600}
.chip .x{cursor:pointer;width:18px;height:18px;border-radius:50%;display:grid;place-items:center;
  background:color-mix(in srgb,var(--chip-fg) 18%,transparent);font-size:12px;line-height:1}
.grouplist{display:flex;flex-direction:column;gap:2px;max-height:230px;overflow:auto;margin-top:4px}
.grp{display:flex;align-items:center;gap:11px;padding:8px 6px;border-radius:10px}
.grp:hover{background:var(--bg)}
.grp .nm{font-weight:600;font-size:14px}
.muted{color:var(--muted)}
.bar{position:fixed;left:0;right:0;bottom:0;background:color-mix(in srgb,var(--card) 92%,transparent);
  backdrop-filter:blur(8px);border-top:1px solid var(--border);padding:12px 20px}
.bar .inner{max-width:860px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px}
.btn{border:0;border-radius:10px;padding:10px 18px;font:inherit;font-weight:650;cursor:pointer}
.btn.primary{background:var(--accent);color:var(--accent-fg)}
.btn.primary:disabled{opacity:.5;cursor:default}
.btn.ghost{background:transparent;color:var(--muted)}
.toast{position:fixed;top:18px;left:50%;transform:translateX(-50%) translateY(-20px);opacity:0;
  background:var(--fg);color:var(--bg);padding:10px 16px;border-radius:10px;font-weight:600;font-size:13px;
  transition:.25s;pointer-events:none;z-index:50;box-shadow:var(--shadow)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{background:var(--danger);color:#fff}
select#inst{width:auto;min-width:170px}
.switch{position:relative;width:42px;height:24px;flex:0 0 auto}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--border);border-radius:999px;transition:.2s;cursor:pointer}
.slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:var(--accent)}
.switch input:checked+.slider:before{transform:translateX(18px)}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.appr{display:flex;align-items:flex-start;gap:12px;padding:11px 0;border-top:1px solid var(--border)}
.appr:first-of-type{border-top:0}
.appr .pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-all;margin-top:4px;max-height:66px;overflow:auto}
.appr .btn{padding:7px 14px}
.btn.approve{background:var(--ok);color:#fff}
.btn.deny{background:transparent;color:var(--danger);border:1px solid var(--danger)}
.chanlist{display:flex;flex-direction:column;gap:6px;margin-top:6px}
.chan{display:flex;align-items:center;gap:12px;padding:11px 12px;border:1px solid var(--border);border-radius:12px;cursor:pointer;background:var(--bg)}
.chan:hover{border-color:var(--accent)}
.chan.active{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 20%,transparent)}
.chan .nm{font-weight:600;font-size:14px}
.chan .id{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chan-ic{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:#fff;font-weight:700;font-size:16px;flex:0 0 auto}
.chan-ic.wechat{background:#09b83e}
.chan-ic.telegram{background:#2aabee}
.badge{display:inline-block;margin-left:8px;font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;padding:2px 7px;border-radius:999px;background:var(--chip);color:var(--chip-fg);vertical-align:middle}
.badge.telegram{background:#e8f6fd;color:#1c7ab5}
.badge.wechat{background:#e7f8ec;color:#12833a}
textarea.ta{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:10px;background:var(--bg);color:var(--fg);font:inherit;resize:vertical;min-height:64px;outline:none}
textarea.ta:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 25%,transparent)}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="logo">CB</div>
    <div>
      <h1>Channels</h1>
      <div class="sub" id="cfgpath">loading config…</div>
    </div>
    <div style="margin-left:auto" id="instwrap"></div>
  </header>

  <div id="app"></div>
</div>

<div class="bar" id="bar" style="display:none">
  <div class="inner">
    <div class="pill" id="savehint">Edits are written to channels.toml. Restart <code>channels start</code> to apply.</div>
    <div class="row">
      <button class="btn ghost" id="reload">Reload</button>
      <button class="btn primary" id="save">Save changes</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const TOKEN = "__PORTAL_TOKEN__";
const $ = (s,r=document)=>r.querySelector(s);
const el = (t,a={},...k)=>{const n=document.createElement(t);for(const[x,v]of Object.entries(a)){
  if(x==="class")n.className=v;else if(x==="html")n.innerHTML=v;else if(x.startsWith("on"))n.addEventListener(x.slice(2),v);else n.setAttribute(x,v);}
  for(const c of k)n.append(c);return n;};
const initials = s => (s||"?").trim().slice(0,2).toUpperCase();
function avatar(url,name,cls=""){ if(url) return el("img",{class:"avatar "+cls,src:url,alt:""});
  return el("div",{class:"avatar "+cls},initials(name)); }
let toastT;
function toast(msg,err=false){const t=$("#toast");t.textContent=msg;t.className="toast show"+(err?" err":"");
  clearTimeout(toastT);toastT=setTimeout(()=>t.className="toast",2400);}

async function api(path,opts={}){
  const o=Object.assign({headers:{}},opts); o.headers["X-Portal-Token"]=TOKEN;
  if(o.body){o.headers["Content-Type"]="application/json";}
  const r=await fetch(path,o); const txt=await r.text(); let data={};
  try{data=txt?JSON.parse(txt):{};}catch{ data={error:txt}; }
  if(!r.ok) throw new Error(data.error||("HTTP "+r.status)); return data;
}

let STATE={instances:[],idx:0,contacts:new Map()};
let qrPollTimer=null;
let approvalPollTimer=null;
function stopQrPoll(){ if(qrPollTimer){ clearInterval(qrPollTimer); qrPollTimer=null; } }
function stopApprovalPoll(){ if(approvalPollTimer){ clearInterval(approvalPollTimer); approvalPollTimer=null; } }

function current(){return STATE.instances[STATE.idx];}

async function load(){
  stopQrPoll(); stopApprovalPoll();
  const cfg=await api("/api/config"); STATE.instances=cfg.instances; STATE.idx=0;
  $("#cfgpath").textContent=cfg.config_path;
  $("#instwrap").innerHTML="";
  if(!STATE.instances.length){ $("#app").innerHTML=""; $("#app").append(noInstances()); $("#bar").style.display="none"; return; }
  $("#bar").style.display="block"; render();
  loadForCurrent(); startApprovalPoll();
}

function loadForCurrent(){
  const it=current(); if(!it) return;
  if(it.type==="telegram"){ loadTelegramStatus(); }
  else { loadAccount(); loadGroups(); warmContacts(); }
}

function warmContacts(){
  const it=current(); if(!it||!it.token_resolvable) return;
  // Prime the server-side contact cache in the background so the first search
  // is fast instead of blocking ~seconds on a cold 4k-row fetch.
  api("/api/wechat/contacts?instance="+encodeURIComponent(it.instance_id)+"&q=&limit=1").catch(()=>{});
}

function startApprovalPoll(){
  stopApprovalPoll();
  const tick=async ()=>{
    let d; try{ d=await api("/api/approvals"); }catch(e){ return; }
    renderApprovals(d.approvals||[]);
  };
  tick();
  approvalPollTimer=setInterval(tick, 2500);
}

function renderApprovals(list){
  const box=$("#approvals"); if(!box) return;
  if(!list.length){ box.innerHTML=""; return; }
  box.innerHTML="";
  const card=el("div",{class:"card",style:"border-color:var(--accent)"},
    el("h2",{},"Tool approvals — "+list.length+" pending"),
    el("p",{class:"hint"},"The agent wants to run these on your machine. Approve to let it proceed, Deny to block."));
  list.forEach(a=>{
    card.append(el("div",{class:"appr"},
      el("div",{style:"flex:1;min-width:0"},
        el("div",{class:"nm"}, (a.instance_id?("["+a.instance_id+"] "):"")+(a.tool||"tool")),
        a.title?el("div",{class:"id"}, a.title):el("span",{}),
        a.input_preview?el("div",{class:"pre"}, a.input_preview):el("span",{})),
      el("div",{class:"row",style:"gap:8px;flex:0 0 auto"},
        el("button",{class:"btn deny",onclick:()=>decide(a.id,"deny")},"Deny"),
        el("button",{class:"btn approve",onclick:()=>decide(a.id,"allow")},"Approve"))));
  });
  box.replaceChildren(card);
}

async function decide(id, decision){
  try{
    await api("/api/approvals",{method:"POST",body:JSON.stringify({id:id,decision:decision})});
    toast(decision==="allow"?"Approved":"Denied");
    const d=await api("/api/approvals"); renderApprovals(d.approvals||[]);
  }catch(e){ toast(e.message,true); }
}

function noInstances(){
  return el("div",{class:"card"},
    el("h2",{},"No channels configured"),
    el("p",{class:"hint"},"Run `coding-bridge channels init`, add a [[channels.wechat]] or [[channels.telegram]] block with a token, then reload. The portal edits existing instances."));
}

function chanIcon(type){ return el("div",{class:"chan-ic "+type}, type==="telegram"?"✈":"微"); }

function renderOverview(){
  const card=el("div",{class:"card",id:"overview"},
    el("h2",{},"Channels — "+STATE.instances.length),
    el("p",{class:"hint"},"Every configured channel. Click one to edit it; the editor adapts to each channel type."));
  const list=el("div",{class:"chanlist"});
  STATE.instances.forEach((it,i)=>{
    const active=i===STATE.idx;
    list.append(el("div",{class:"chan"+(active?" active":""),onclick:()=>select(i)},
      chanIcon(it.type),
      el("div",{style:"flex:1;min-width:0"},
        el("div",{class:"nm"}, it.instance_id, el("span",{class:"badge "+it.type}, it.type)),
        el("div",{class:"id"}, it.type==="telegram"?(it.api_base||"telegram"):it.base_url)),
      el("span",{class:"pill"}, el("span",{class:"dot"+(it.enabled?"":" off")}), it.enabled?"enabled":"disabled")));
  });
  card.append(list);
  return card;
}

function refreshOverview(){
  const cur=$("#overview"); if(cur) cur.replaceWith(renderOverview());
}

function select(i){
  if(i===STATE.idx) return;
  stopQrPoll(); STATE.idx=i; render(); loadForCurrent();
}

function render(){
  const it=current(); const app=$("#app"); app.innerHTML="";
  app.append(el("div",{id:"approvals"}));
  app.append(renderOverview());
  app.append(el("div",{id:"onboarding"}));
  if(it.type==="telegram") renderTelegramEditor(app,it);
  else renderWeChatEditor(app,it);
}

function behaviorCard(it,opts){
  opts=opts||{};
  const freeform=it.free_form!==false && (it.trigger_prefix===""||it.free_form===true);
  const beh=el("div",{class:"card"},
    el("h2",{},"Behavior"),
    el("p",{class:"hint"},"How the bot responds."));
  beh.append(el("label",{class:"fld"},"Trigger"));
  const seg=el("div",{class:"seg"},
    el("button",{class:freeform?"on":"",id:"tf-free",onclick:()=>setTrigger(true)},"Free-form"),
    el("button",{class:freeform?"":"on",id:"tf-prefix",onclick:()=>setTrigger(false)},"Require prefix"));
  beh.append(seg);
  const pfx=el("input",{type:"text",id:"prefix",placeholder:"/ask ",style:"margin-top:10px;"+(freeform?"display:none":""),
    value: freeform? "/ask " : (it.trigger_prefix||"/ask ")});
  pfx.addEventListener("input",()=>{it.trigger_prefix=pfx.value;});
  beh.append(pfx);
  beh.append(el("label",{class:"fld"},"Provider"));
  const prov=el("select",{id:"prov",onchange:e=>{it.default_provider=e.target.value;}});
  ["claude","codex","copilot"].forEach(p=>prov.append(el("option",{value:p,...(it.default_provider===p?{selected:"selected"}:{})},p)));
  beh.append(prov);
  beh.append(el("label",{class:"fld"},"Rate limit (messages / sender / minute, 0 = off)"));
  const rl=el("input",{type:"number",id:"rl",min:"0",value:String(it.rate_limit_per_min)});
  rl.addEventListener("input",()=>{const v=parseInt(rl.value||"0",10);it.rate_limit_per_min=isNaN(v)?0:Math.max(0,v);});
  beh.append(rl);
  if(opts.dedup){
    beh.append(el("label",{class:"fld"},"Dedup window (seconds, 0 = off)"));
    const dd=el("input",{type:"number",id:"dedup",min:"0",step:"1",value:String(it.dedup_window_seconds)});
    dd.addEventListener("input",()=>{const v=parseFloat(dd.value||"0");it.dedup_window_seconds=isNaN(v)?0:Math.max(0,v);});
    beh.append(dd);
  }
  return beh;
}

function safetyCard(it){
  const card=el("div",{class:"card"},
    el("h2",{},"Safety"),
    el("p",{class:"hint"},"Require approval holds every tool action for Approve/Deny in this portal instead of running unattended."));
  card.append(el("label",{class:"row",style:"cursor:pointer;gap:12px"},
    el("label",{class:"switch"},
      el("input",{type:"checkbox",id:"reqappr",...(it.require_approval?{checked:"checked"}:{}),onchange:e=>{it.require_approval=e.target.checked;}}),
      el("span",{class:"slider"})),
    el("div",{}, el("div",{style:"font-weight:600"},"Require approval for tool use"),
      el("div",{class:"pill"},"Off = run tools unattended (default)"))));
  return card;
}

function renderWeChatEditor(app,it){
  // account card
  const acct=el("div",{class:"card"},
    el("div",{class:"row"},
      el("div",{class:"acct",id:"acct"}, avatar(null,it.instance_id), el("div",{},
        el("div",{style:"font-weight:650"},it.instance_id),
        el("div",{class:"pill",id:"acctline"}, it.base_url))),
      el("label",{class:"switch",style:"margin-left:auto",title:"Enabled"},
        el("input",{type:"checkbox",...(it.enabled?{checked:"checked"}:{}),onchange:e=>{it.enabled=e.target.checked;refreshOverview();}}),
        el("span",{class:"slider"}))));
  app.append(acct);
  if(!it.token_resolvable){
    acct.append(el("p",{class:"hint",style:"color:var(--danger);margin:12px 0 0"},
      "⚠ Token not resolvable ("+(it.token_source.kind==="none"?"no token_env/token_file set":it.token_source.kind+": "+it.token_source.ref)+"). Contacts/groups won't load until it's set."));
  }

  // access card
  const access=el("div",{class:"card"},
    el("h2",{},"Access — who can drive the bot"),
    el("p",{class:"hint"},"Only these people (by WeChat id) trigger a turn. Empty = anyone who can message the bot. Search your contacts:"));
  const search=el("div",{class:"search"},
    el("input",{type:"text",id:"q",placeholder:"Search name or WeChat id…",autocomplete:"off"}),
    el("div",{class:"results",id:"results"}));
  access.append(search);
  const chips=el("div",{class:"chips",id:"chips"}); access.append(chips);
  app.append(access);
  renderChips();
  const q=$("#q");
  let dbt; q.addEventListener("input",()=>{clearTimeout(dbt);dbt=setTimeout(()=>doSearch(q.value),200);});
  q.addEventListener("focus",()=>{ if(q.value)doSearch(q.value); });

  app.append(behaviorCard(it));

  // groups — allowed_groups picker
  const g=el("div",{class:"card"},
    el("h2",{},"Groups — where the bot may answer"),
    el("p",{class:"hint"},"Check groups to restrict the bot to only those. None checked = it may answer in every group it's in (still gated by prefix/sender). Keep a prefix in busy groups."),
    el("div",{class:"grouplist",id:"groups"}, el("div",{class:"empty"},"loading…")));
  app.append(g);

  app.append(safetyCard(it));
}

function renderTelegramEditor(app,it){
  // account / token-status card
  const acct=el("div",{class:"card"},
    el("div",{class:"row"},
      el("div",{class:"acct"}, chanIcon("telegram"), el("div",{},
        el("div",{style:"font-weight:650"}, it.instance_id, el("span",{class:"badge telegram"},"telegram")),
        el("div",{class:"pill"}, it.api_base))),
      el("label",{class:"switch",style:"margin-left:auto",title:"Enabled"},
        el("input",{type:"checkbox",...(it.enabled?{checked:"checked"}:{}),onchange:e=>{it.enabled=e.target.checked;refreshOverview();}}),
        el("span",{class:"slider"}))));
  app.append(acct);
  if(!it.token_resolvable){
    acct.append(el("p",{class:"hint",style:"color:var(--danger);margin:12px 0 0"},
      "⚠ Bot token not resolvable ("+(it.token_source.kind==="none"?"no token_env/token_file set":it.token_source.kind+": "+it.token_source.ref)+"). Create a bot with @BotFather and set the token env var."));
  } else {
    acct.append(el("div",{class:"pill",id:"tgstatus",style:"margin-top:10px"},"checking bot token…"));
  }

  // access — manual sender-id allowlist
  const access=el("div",{class:"card"},
    el("h2",{},"Access — who can drive the bot"),
    el("p",{class:"hint"},"Telegram numeric user IDs, one per line. Empty = anyone who can message the bot. Message @userinfobot to find an id."));
  const senders=el("textarea",{class:"ta",id:"tgsenders",rows:"3",placeholder:"123456789\n987654321",autocomplete:"off",spellcheck:"false"});
  senders.value=(it.allowed_senders||[]).join("\n");
  senders.addEventListener("input",()=>{ it.allowed_senders=splitLines(senders.value); });
  access.append(senders);
  app.append(access);

  app.append(behaviorCard(it,{dedup:true}));

  // groups — manual chat-id allowlist
  const g=el("div",{class:"card"},
    el("h2",{},"Groups — where the bot may answer"),
    el("p",{class:"hint"},"Group / supergroup chat IDs (usually negative), one per line. Empty = every group the bot is in. Disable the bot's privacy mode in @BotFather to receive group messages."));
  const groups=el("textarea",{class:"ta",id:"tggroups",rows:"3",placeholder:"-1001234567890",autocomplete:"off",spellcheck:"false"});
  groups.value=(it.allowed_groups||[]).join("\n");
  groups.addEventListener("input",()=>{ it.allowed_groups=splitLines(groups.value); });
  g.append(groups);
  app.append(g);

  app.append(safetyCard(it));

  if(it.token_resolvable) loadTelegramStatus();
}

function splitLines(s){ return (s||"").split(/\r?\n/).map(x=>x.trim()).filter(Boolean); }

async function loadTelegramStatus(){
  const it=current(); if(!it||it.type!=="telegram"||!it.token_resolvable) return;
  const box=$("#tgstatus"); if(!box) return;
  try{
    const s=await api("/api/telegram/status?instance="+encodeURIComponent(it.instance_id));
    box.innerHTML=""; box.append(el("span",{class:"dot"}),
      document.createTextNode(" bot "+(s.username?("@"+s.username):("id "+(s.id||"?")))+" — token OK"));
  }catch(e){
    box.innerHTML=""; box.append(el("span",{class:"dot off"}),
      document.createTextNode(" "+String(e.message||"token check failed")));
  }
}

function setTrigger(free){
  const it=current(); it.free_form=free; it.trigger_prefix = free ? "" : ($("#prefix").value||"/ask ");
  $("#tf-free").className=free?"on":""; $("#tf-prefix").className=free?"":"on";
  $("#prefix").style.display=free?"none":"";
}

function renderChips(){
  const it=current(); const box=$("#chips"); box.innerHTML="";
  if(!it.allowed_senders.length){ box.append(el("div",{class:"empty"},"Anyone who can message the bot (no allowlist).")); return; }
  it.allowed_senders.forEach(id=>{
    const c=STATE.contacts.get(id);
    box.append(el("span",{class:"chip"}, avatar(c&&c.avatar_url,(c&&(c.nickname||c.remark))||id,"sm"),
      el("span",{}, (c&&(c.remark||c.nickname))||id),
      el("span",{class:"x",title:"remove",onclick:()=>{it.allowed_senders=it.allowed_senders.filter(x=>x!==id);renderChips();}},"×")));
  });
}

async function doSearch(q){
  const it=current(); const box=$("#results");
  if(!it.token_resolvable){ box.replaceChildren(el("div",{class:"opt muted"},"token not set — cannot load contacts")); box.classList.add("show"); return; }
  box.replaceChildren(el("div",{class:"opt muted"},"searching…")); box.classList.add("show");
  try{
    const d=await api("/api/wechat/contacts?instance="+encodeURIComponent(it.instance_id)+"&q="+encodeURIComponent(q)+"&limit=25");
    box.innerHTML="";
    if(!d.contacts.length){ box.append(el("div",{class:"opt muted"},"no matches")); }
    d.contacts.forEach(c=>{
      STATE.contacts.set(c.wechat_id,c);
      const chosen=it.allowed_senders.includes(c.wechat_id);
      box.append(el("div",{class:"opt",onclick:()=>addSender(c)},
        avatar(c.avatar_url,c.nickname||c.remark||c.wechat_id,"sm"),
        el("div",{}, el("div",{class:"nm"}, c.remark||c.nickname||c.wechat_id), el("div",{class:"id"}, c.wechat_id)),
        chosen?el("div",{class:"pill",style:"margin-left:auto"},"added"):el("div",{class:"pill",style:"margin-left:auto;color:var(--accent)"},"+ add")));
    });
    box.classList.add("show");
  }catch(e){ box.innerHTML=""; box.append(el("div",{class:"opt muted"},String(e.message))); box.classList.add("show"); }
}

function addSender(c){
  const it=current(); if(!it.allowed_senders.includes(c.wechat_id)){ it.allowed_senders.push(c.wechat_id); renderChips(); }
  $("#q").value=""; $("#results").classList.remove("show");
}

async function loadAccount(){
  const it=current(); const onb=$("#onboarding"); if(!it) return;
  if(!it.token_resolvable){ if(onb) onb.innerHTML=""; return; }
  let st=null;
  try{ st=await api("/api/wechat/status?instance="+encodeURIComponent(it.instance_id)); }
  catch(e){ if(onb) onb.innerHTML=""; return; }
  if(st && st.logged_in===false){ renderOnboarding(it, st); return; }
  stopQrPoll(); if(onb) onb.innerHTML="";
  try{
    const a=await api("/api/wechat/account?instance="+encodeURIComponent(it.instance_id));
    const acct=$("#acct");
    if(acct){ acct.replaceChildren(avatar(a.avatar_url,a.nickname||it.instance_id),
      el("div",{}, el("div",{style:"font-weight:650"}, a.nickname||a.wechat_id||it.instance_id),
        el("div",{class:"pill"}, el("span",{class:"dot"+((a.status==="online")?"":" off")}), (a.wechat_id||"")+" · "+it.base_url))); }
  }catch(e){ /* leave the fallback header */ }
}

function renderOnboarding(it, st){
  const onb=$("#onboarding"); if(!onb) return;
  onb.innerHTML="";
  onb.append(el("div",{class:"card",style:"text-align:center"},
    el("h2",{style:"text-align:left"},"Connect WeChat"),
    el("p",{class:"hint",style:"text-align:left"},"This account is signed out"+((st&&st.page)?(" ("+st.page+")"):"")+". Scan the QR below with the WeChat app to sign in."),
    el("div",{id:"qrbox",style:"margin:10px auto"}, el("div",{class:"empty"},"loading QR…")),
    el("button",{class:"btn ghost",onclick:()=>loadQR(it)},"Refresh QR")));
  loadQR(it);
  stopQrPoll();
  qrPollTimer=setInterval(async ()=>{
    try{ const s=await api("/api/wechat/status?instance="+encodeURIComponent(it.instance_id));
      if(s && s.logged_in){ stopQrPoll(); toast("WeChat connected"); load(); } }catch(e){}
  }, 3000);
}

async function loadQR(it){
  const box=$("#qrbox"); if(!box) return;
  box.replaceChildren(el("div",{class:"empty"},"loading QR…"));
  try{
    const d=await api("/api/wechat/qr?instance="+encodeURIComponent(it.instance_id));
    box.replaceChildren(el("img",{src:"data:image/png;base64,"+d.base64,alt:"WeChat login QR",
      style:"width:220px;height:220px;border-radius:12px;border:1px solid var(--border)"}));
  }catch(e){
    const msg=String(e.message||"");
    if(msg.indexOf("already logged in")>=0){ stopQrPoll(); load(); return; }
    box.replaceChildren(el("div",{class:"empty"},"QR error: "+msg));
  }
}

async function loadGroups(){
  const it=current(); const box=$("#groups"); if(!box) return;
  if(!it.token_resolvable){ box.innerHTML=""; box.append(el("div",{class:"empty"},"token not set")); return; }
  try{
    const d=await api("/api/wechat/groups?instance="+encodeURIComponent(it.instance_id));
    box.innerHTML="";
    if(!d.groups.length){ box.append(el("div",{class:"empty"},"bot is not in any group")); return; }
    d.groups.forEach(gr=>{
      const on=it.allowed_groups.includes(gr.id);
      const cb=el("input",{type:"checkbox",style:"width:auto;accent-color:var(--accent)",...(on?{checked:"checked"}:{})});
      cb.addEventListener("change",()=>{
        if(cb.checked){ if(!it.allowed_groups.includes(gr.id)) it.allowed_groups.push(gr.id); }
        else { it.allowed_groups=it.allowed_groups.filter(x=>x!==gr.id); }
      });
      box.append(el("label",{class:"grp",style:"cursor:pointer"}, cb,
        avatar(gr.avatar_url,gr.name,"sm"), el("div",{}, el("div",{class:"nm"},gr.name))));
    });
  }catch(e){ box.innerHTML=""; box.append(el("div",{class:"empty"},String(e.message))); }
}

async function save(){
  const btn=$("#save"); btn.disabled=true;
  try{
    const payload={instances:STATE.instances.map(it=>{
      const o={
        type: it.type,
        instance_id: it.instance_id,
        token_env: it.token_source.kind==="env"?it.token_source.ref:null,
        token_file: it.token_source.kind==="file"?it.token_source.ref:null,
        enabled: it.enabled,
        require_approval: it.require_approval===true,
        default_provider: it.default_provider,
        free_form: it.free_form===true || it.trigger_prefix==="",
        trigger_prefix: it.trigger_prefix,
        allowed_senders: it.allowed_senders,
        allowed_groups: it.allowed_groups,
        rate_limit_per_min: it.rate_limit_per_min,
        dedup_window_seconds: it.dedup_window_seconds};
      if(it.type==="telegram") o.api_base=it.api_base; else o.base_url=it.base_url;
      return o;
    })};
    const res=await api("/api/config",{method:"POST",body:JSON.stringify(payload)});
    STATE.instances=res.instances; toast("Saved to channels.toml"); render(); loadForCurrent();
  }catch(e){ toast(e.message,true); }
  finally{ btn.disabled=false; }
}

$("#save").addEventListener("click",save);
$("#reload").addEventListener("click",load);
// One global listener (added once) closes the contact-search dropdown on an
// outside click — avoids stacking a fresh listener on every editor render.
document.addEventListener("click",e=>{
  const box=$("#results"); if(!box) return;
  const s=box.closest(".search");
  if(s && !s.contains(e.target)) box.classList.remove("show");
});
load().catch(e=>{ $("#app").innerHTML=""; $("#app").append(el("div",{class:"card"},el("h2",{},"Failed to load"),el("p",{class:"hint"},String(e.message)))); });
</script>
</body>
</html>
"""

__all__ = ["INDEX_HTML"]
