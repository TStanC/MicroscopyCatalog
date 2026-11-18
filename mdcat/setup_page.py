"""Setup / settings page of the catalog server (served at /setup)."""

from __future__ import annotations

import json

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalog settings</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#667085;--line:#d9dee7;--acc:#2f6fdb;--ok:#1f8a4c;--bad:#c0392b;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e9ef;--muted:#98a2b3;--line:#323844;--acc:#6ea0ff;--ok:#4cc38a;--bad:#ff7b6b;--warn:#f0b75a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
label{display:block;font-weight:600;margin:12px 0 4px}label:first-of-type{margin-top:0}
.row{display:flex;gap:8px}.row input{flex:1;min-width:0}
input[type=text],input[type=number],select{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
input[type=number]{width:90px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:inherit;border-radius:6px;padding:6px 12px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:default}
.hint{color:var(--muted);font-size:12.5px;margin-top:3px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.cb{display:flex;gap:8px;align-items:center;font-weight:400;margin-top:12px}
.mono{font-family:Consolas,monospace;font-size:12.5px;word-break:break-all}
table{border-collapse:collapse}td{padding:2px 12px 2px 0;vertical-align:top}
#msg{margin-top:12px}a{color:var(--acc)}
</style></head><body><main>
<h1>Microscopy catalog – settings</h1>
<div class="hint" id="top"></div>

<div class="card"><h2>Folders</h2>
<label for="labdata_root">Labdata folder (read-only)</label>
<div class="row"><input type="text" id="labdata_root" placeholder="X:\Titus\Labdata"><button data-pick="labdata_root">Browse…</button></div>
<div class="hint">The data that is catalogued. Nothing in it is ever changed – except inside the catalog folder below.</div>
<div class="hint" id="lab_info"></div>

<label for="catalog_dir">Catalog folder</label>
<div class="row"><input type="text" id="catalog_dir" placeholder="X:\Titus\Labdata\MicroscopyCatalog"><button data-pick="catalog_dir">Browse…</button></div>
<div class="hint">Holds everything: database with comments and manual values, previews, the published page (index.html).
If it already contains a catalog, that one is used. Inside the Labdata folder it must be a folder directly in it.
<button id="suggest" style="padding:1px 8px;margin-left:4px">use &lt;Labdata&gt;\MicroscopyCatalog</button></div>
<div class="hint" id="cat_info"></div>
</div>

<div class="card"><h2>Updates and previews</h2>
<label for="auto_update_min">Look for new data every</label>
<input type="number" id="auto_update_min" min="0" step="5"> minutes <span class="hint">(0 = only after edits)</span>
<label for="previews">Preview images</label>
<select id="previews"><option value="network">network-friendly (read ≤ 300 MB per image)</option>
<option value="full">full (read ≤ 3 GB per image – for fast connections)</option><option value="off">off</option></select>
<span class="hint">at most</span> <input type="number" id="preview_budget_min" min="1" step="5"> <span class="hint">minutes per update</span>
<label class="cb"><input type="checkbox" id="autostart"> Start the catalog server automatically when I log in</label>
</div>

<div class="card"><h2>This installation</h2><table id="inst"></table></div>

<button class="primary" id="save">Save and start</button> <a href="/" id="back" style="margin-left:10px">back to the catalog</a>
<div id="msg"></div>
</main>
<script>
const S=__STATE__;const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-MDCAT':'1'},body:JSON.stringify(body)});
 let j={};try{j=await r.json()}catch(e){}if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));return j}
for(const k of ['labdata_root','catalog_dir','auto_update_min','previews','preview_budget_min'])$(k).value=S.settings[k]??'';
$('autostart').checked=!!S.autostart;
$('top').innerHTML=S.configured?'Status: '+esc(S.status_msg):'First start: choose the folders, then <b>Save and start</b>.';
if(!S.configured)$('back').style.display='none';
$('inst').innerHTML=[['Program folder',S.program_dir],['Settings file',S.settings_path],
 ['Java',S.java.java_ok?'<span class="ok">ok</span> '+esc(S.java.java_bin):'<span class="bad">missing – run install.ps1</span>'],
 ['Bio-Formats',S.java.bioformats_ok?'<span class="ok">ok</span> '+esc(S.java.bioformats_lib):'<span class="bad">missing – run install.ps1</span>'],
 ['Windows user',S.me]].map(([a,b])=>'<tr><td>'+a+'</td><td class="mono">'+(String(b).startsWith('<span')?b:esc(b))+'</td></tr>').join('');
let tmr;function check(){clearTimeout(tmr);tmr=setTimeout(async()=>{try{const j=await post('/api/check',{labdata_root:$('labdata_root').value,catalog_dir:$('catalog_dir').value});
 $('lab_info').innerHTML=j.labdata;$('cat_info').innerHTML=j.catalog}catch(e){}},300)}
['labdata_root','catalog_dir'].forEach(k=>$(k).addEventListener('input',check));check();
document.querySelectorAll('[data-pick]').forEach(b=>b.onclick=async()=>{const k=b.dataset.pick;b.disabled=true;
 $('msg').innerHTML='<span class="hint">A folder dialog opened (it may be behind the browser window).</span>';
 try{const j=await post('/api/pickfolder',{initial:$(k).value||$('labdata_root').value});if(j.path){$(k).value=j.path;check()}$('msg').innerHTML=''}
 catch(e){$('msg').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}finally{b.disabled=false}});
$('suggest').onclick=()=>{const l=$('labdata_root').value.replace(/[\\\/]+$/,'');if(l){$('catalog_dir').value=l+'\\MicroscopyCatalog';check()}};
$('save').onclick=async()=>{const body={labdata_root:$('labdata_root').value.trim(),catalog_dir:$('catalog_dir').value.trim(),
 auto_update_min:+$('auto_update_min').value,previews:$('previews').value,preview_budget_min:+$('preview_budget_min').value,autostart:$('autostart').checked};
 $('save').disabled=true;$('msg').innerHTML='Saving …';
 try{await post('/api/setup',body);$('msg').innerHTML='<span class="ok">Saved.</span> Restarting the server …';
  const t0=Date.now();await new Promise(r=>setTimeout(r,2500));
  while(Date.now()-t0<60000){try{const s=await (await fetch('/api/state')).json();if(s.status&&s.status!=='setup'){location.href='/';return}}catch(e){}await new Promise(r=>setTimeout(r,1000))}
  $('msg').innerHTML='<span class="warn">The server did not come back – see logs\\server.log in the .work folder.</span>'}
 catch(e){$('msg').innerHTML='<span class="bad">Not saved: '+esc(e.message)+'</span>';$('save').disabled=false}};
</script></body></html>
"""


def render(state: dict) -> bytes:
    js = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__STATE__", js).encode("utf-8")
