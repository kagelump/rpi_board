#!/usr/bin/env python3
"""Local HTTP API and dashboard for historic generation records."""
from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))

from scripts.history.store import GenerationStore


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weather Board Generation History</title>
<style>
:root{color-scheme:light;--ink:#171717;--paper:#f4f1e8;--card:#fffdf7;--red:#c51f1f;--yellow:#efb500;--muted:#6d685f;--line:#d6d0c4}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
header{padding:22px 28px;background:var(--ink);color:white;display:flex;align-items:end;justify-content:space-between}h1{margin:0;font-size:24px}header p{margin:3px 0 0;color:#cbc7bd}
main{padding:20px;display:grid;grid-template-columns:minmax(340px,43%) minmax(420px,57%);gap:18px;max-width:1600px;margin:auto}.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}.toolbar{padding:12px;display:flex;gap:8px;border-bottom:1px solid var(--line);flex-wrap:wrap}input,select,button{font:inherit;padding:7px 9px;border:1px solid #aaa;border-radius:5px;background:white}button{cursor:pointer;background:var(--ink);color:white}.stats{display:flex;gap:18px}.stat b{font-size:22px;display:block}.stat span{font-size:11px;color:#cbc7bd;text-transform:uppercase}
#runs{max-height:calc(100vh - 190px);overflow:auto}.run{padding:13px 15px;border-bottom:1px solid var(--line);cursor:pointer}.run:hover,.run.active{background:#f8eecb}.run-top{display:flex;justify-content:space-between;gap:8px}.run h3{margin:0;font-size:16px}.meta{color:var(--muted);font-size:12px}.badge{font-size:11px;padding:2px 7px;border-radius:20px;background:#ddd}.succeeded{background:#d5ecd9}.degraded{background:#ffe2a9}.failed{background:#f5c1c1}.legacy{background:#ddd}
#detail{padding:18px;max-height:calc(100vh - 128px);overflow:auto}.empty{color:var(--muted);padding:40px;text-align:center}.hero-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.artifact img{width:100%;border:1px solid var(--line);background:white}.artifact small{display:block;color:var(--muted)}h2{margin:0 0 4px}h3.section{margin:22px 0 8px;border-bottom:3px solid var(--yellow);padding-bottom:5px}.timeline{border-left:3px solid var(--ink);margin-left:7px;padding-left:14px}.stage{margin:10px 0}.stage b{display:inline-block;min-width:170px}.log{font-family:ui-monospace,monospace;font-size:12px;padding:7px;border-bottom:1px solid #eee}.log .warn,.log .error{color:var(--red)}details{border:1px solid var(--line);border-radius:6px;margin:7px 0;background:white}summary{cursor:pointer;padding:9px 11px;font-weight:600}pre{white-space:pre-wrap;word-break:break-word;margin:0;padding:12px;background:#191919;color:#f3f0e8;max-height:460px;overflow:auto;font-size:12px}.prompt{background:#fff8df;padding:10px;border-left:4px solid var(--yellow)}a{color:#8d1515}@media(max-width:900px){main{grid-template-columns:1fr}.panel,#runs,#detail{max-height:none}.stats{display:none}}
</style>
</head>
<body><header><div><h1>Generation History</h1><p>Weather, prompts, model activity, styles and immutable output artifacts</p></div><div class="stats" id="stats"></div></header>
<main><section class="panel"><div class="toolbar"><input id="date" type="date"><select id="status"><option value="">All statuses</option><option>succeeded</option><option>degraded</option><option>failed</option><option>legacy</option></select><input id="style" placeholder="Style"><button id="refresh">Refresh</button></div><div id="runs"></div></section><section class="panel"><div id="detail" class="empty">Select a generation run.</div></section></main>
<script>
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=s=>s?new Date(s).toLocaleString():'';let active=null;
async function api(path){const r=await fetch(path);if(!r.ok)throw Error(await r.text());return r.json()}
async function stats(){const s=await api('/api/stats');$('#stats').innerHTML=`<div class=stat><b>${s.runs||0}</b><span>runs</span></div><div class=stat><b>${s.degraded||0}</b><span>degraded</span></div><div class=stat><b>${s.failed||0}</b><span>failed</span></div>`}
async function runs(){const q=new URLSearchParams({limit:'200'});if($('#date').value)q.set('target_date',$('#date').value);if($('#status').value)q.set('status',$('#status').value);if($('#style').value)q.set('style',$('#style').value);const d=await api('/api/runs?'+q);$('#runs').innerHTML=d.items.map(r=>`<article class="run ${r.id===active?'active':''}" data-id="${r.id}"><div class=run-top><h3>${esc(r.target_date||fmt(r.started_at))}</h3><span class="badge ${r.status}">${r.status}</span></div><div>${esc(r.summary.headline||r.selected_style||'Generation run')}</div><div class=meta>${esc(r.daypart_role||'')} · ${esc(r.selected_style||'style unknown')} · ${fmt(r.started_at)} · ${r.artifact_count||0} artifacts</div></article>`).join('')||'<div class=empty>No matching runs.</div>';document.querySelectorAll('.run').forEach(el=>el.onclick=()=>detail(el.dataset.id))}
function pretty(v){return esc(JSON.stringify(v,null,2))}
async function detail(id){active=id;await runs();const r=await api('/api/runs/'+id);const arts=r.artifacts.filter(a=>a.mime_type.startsWith('image/')).map(a=>`<div class=artifact><a href="/api/artifacts/${a.id}" target=_blank><img loading=lazy src="/api/artifacts/${a.id}"></a><small>${esc(a.kind)} · ${a.width||'?'}×${a.height||'?'} · ${(a.byte_size/1024).toFixed(0)} KB</small></div>`).join('');const stages=r.stages.map(s=>`<div class=stage><b>${esc(s.stage_name)}</b><span class="badge ${s.status}">${s.status}</span> <span class=meta>${s.duration_ms??'?'} ms ${s.exit_code!=null?'· exit '+s.exit_code:''}</span>${s.message?'<div>'+esc(s.message)+'</div>':''}</div>`).join('');const logs=r.logs.map(l=>`<div class=log><span class="${l.level}">${esc(l.level.toUpperCase())}</span> ${esc(l.component)} / ${esc(l.event_type)} — ${esc(l.message||'')} ${Object.keys(l.data||{}).length?'<details><summary>data</summary><pre>'+pretty(l.data)+'</pre></details>':''}</div>`).join('');const snaps=r.snapshots.map(s=>`<details data-snapshot="${s.id}"><summary>${esc(s.kind)} <span class=meta>${(s.byte_size/1024).toFixed(1)} KB · ${s.sha256.slice(0,12)}</span></summary><pre>Loading…</pre></details>`).join('');$('#detail').className='';$('#detail').innerHTML=`<h2>${esc(r.target_date||'Generation run')}</h2><div class=meta>${esc(r.id)} · ${fmt(r.started_at)} → ${fmt(r.completed_at)}</div><p><span class="badge ${r.status}">${r.status}</span> <b>${esc(r.selected_style||'No recorded style')}</b> · ${esc(r.image_provider||'provider unknown')} · ${esc(r.brief_source||'brief source unknown')}</p>${r.summary.headline?`<h3>${esc(r.summary.headline)}</h3>`:''}${r.summary.illustration_prompt?`<div class=prompt><b>Illustration direction</b><br>${esc(r.summary.illustration_prompt)}</div>`:''}<h3 class=section>Artifacts</h3><div class=hero-grid>${arts||'<span class=meta>No saved images.</span>'}</div><h3 class=section>Pipeline</h3><div class=timeline>${stages||'<span class=meta>No stage data.</span>'}</div><h3 class=section>Generation events</h3>${logs||'<span class=meta>No detailed events.</span>'}<h3 class=section>Input snapshots</h3>${snaps||'<span class=meta>No snapshots.</span>'}`;document.querySelectorAll('details[data-snapshot]').forEach(el=>el.addEventListener('toggle',async()=>{if(!el.open||el.dataset.loaded)return;const s=await api('/api/snapshots/'+el.dataset.snapshot);el.querySelector('pre').innerHTML=pretty(s.payload);el.dataset.loaded='1'}))}
$('#refresh').onclick=()=>{runs();stats()};stats();runs();
</script></body></html>"""


class HistoryRequestHandler(BaseHTTPRequestHandler):
    store: GenerationStore

    def log_message(self, format, *args):
        print(f"[history-server] {self.address_string()} {format % args}")

    def _json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self):
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                data = DASHBOARD_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/health":
                self._json({"ok": True, "schema_version": 1})
                return
            if path == "/api/stats":
                self._json(self.store.stats())
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                self._json(self.store.list_runs(
                    limit=int(query.get("limit", [50])[0]),
                    offset=int(query.get("offset", [0])[0]),
                    target_date=query.get("target_date", [None])[0],
                    status=query.get("status", [None])[0],
                    style=query.get("style", [None])[0],
                ))
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1:3] == ["api", "runs"]:
                result = self.store.get_run(parts[3])
                self._json(result) if result else self._not_found()
                return
            if len(parts) == 4 and parts[1:3] == ["api", "snapshots"]:
                result = self.store.get_snapshot(parts[3])
                self._json(result) if result else self._not_found()
                return
            if len(parts) == 4 and parts[1:3] == ["api", "artifacts"]:
                result = self.store.get_artifact(parts[3])
                if not result:
                    self._not_found(); return
                metadata, artifact_path = result
                if not artifact_path.exists():
                    self._not_found(); return
                data = artifact_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", metadata["mime_type"])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            self._not_found()
        except (ValueError, TypeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            self._json({"error": f"internal error: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def make_server(host: str, port: int, store: GenerationStore | None = None):
    store = store or GenerationStore()
    handler = type("ConfiguredHistoryHandler", (HistoryRequestHandler,), {"store": store})
    return ThreadingHTTPServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(description="Serve the weather-board history dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--records")
    parser.add_argument("--artifacts-dir")
    args = parser.parse_args()
    store = GenerationStore(args.records, args.artifacts_dir)
    server = make_server(args.host, args.port, store)
    print(f"History dashboard: http://{args.host}:{server.server_port}")
    print(f"Record ledger: {store.record_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
