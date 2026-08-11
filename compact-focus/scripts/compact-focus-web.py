#!/usr/bin/env python3
"""compact-focus-web — local web editor for the compaction ledger.

Stdlib-only micro-server: GET / serves the editor with the current
ledger.json embedded; POST /save writes the edited ledger (finalized) and
shuts the server down. Prints the URL on the first line of stdout so the
caller can open a browser at it. The terminal cannot host this interaction
(no TTY in the bang shell; curses limits); the browser can.

Usage: compact-focus-web.py <ledger.json>"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compaction ledger</title>
<style>
  :root { --bg:#101418; --panel:#171d23; --raise:#1e262e; --rule:#2a343e;
    --fg:#d6dee6; --dim:#7b8794; --green:#7dd58a; --yellow:#e5c07b;
    --red:#e06c75; --cyan:#6cb6d9;
    --mono: ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
  * { box-sizing: border-box; }
  body { background:var(--bg); color:var(--fg); font-family:var(--mono);
    font-size:14px; line-height:1.55; margin:0; padding:1.2rem; }
  .wrap { max-width:56rem; margin:0 auto; }
  h1 { font-size:1.05rem; margin:0 0 .2rem; }
  .sub { color:var(--dim); font-size:.85rem; margin:0 0 1rem; }
  section { background:var(--panel); border:1px solid var(--rule);
    border-radius:8px; padding: .7rem .9rem; margin-bottom: .8rem; }
  h2 { font-size:.85rem; letter-spacing:.05em; text-transform:uppercase;
    margin:.1rem 0 .5rem; }
  h2.keep{color:var(--green)} h2.summarize{color:var(--cyan)}
  h2.contested{color:var(--yellow)} h2.drop{color:var(--red)}
  .item { border-top:1px solid var(--rule); padding:.45rem 0; }
  .item:first-of-type { border-top:none; }
  .row { display:flex; gap:.6rem; align-items:baseline; }
  .catbtn { font:inherit; border:1px solid var(--rule); background:var(--raise);
    color:var(--fg); border-radius:4px; padding:.05rem .45rem; cursor:pointer;
    min-width:7.5rem; text-align:left; }
  .catbtn.keep{color:var(--green)} .catbtn.summarize{color:var(--cyan)}
  .catbtn.contested{color:var(--yellow)} .catbtn.drop{color:var(--red)}
  .lbl { flex:1; }
  .lbl[contenteditable]:focus { outline:1px solid var(--cyan); border-radius:3px; }
  .tag { color:var(--dim); font-size:.8rem; }
  .pct { color:var(--dim); white-space:nowrap; }
  .exp { background:none; border:none; color:var(--dim); font:inherit; cursor:pointer; }
  .children { margin:.3rem 0 0 2.2rem; display:none; }
  .children.open { display:block; }
  .child { display:flex; gap:.5rem; align-items:baseline; color:var(--cyan);
    font-size:.85rem; margin:.15rem 0; }
  .child .pct { min-width:3.5rem; }
  .note { width:100%; background:var(--raise); border:1px solid var(--rule);
    border-radius:4px; color:var(--dim); font:inherit; font-size:.82rem;
    margin-top:.3rem; padding:.15rem .45rem; }
  select,input[type=number] { font:inherit; background:var(--raise);
    color:var(--fg); border:1px solid var(--rule); border-radius:4px;
    padding:.1rem .3rem; }
  .cls { display:flex; gap:.7rem; align-items:center; margin:.25rem 0; }
  .cls .pct { min-width:4.5rem; }
  .constraints input[type=text] { width:100%; font:inherit;
    background:var(--raise); border:1px solid var(--rule); border-radius:4px;
    color:var(--fg); padding:.25rem .5rem; margin-top:.3rem; }
  .conline { color:var(--yellow); font-size:.85rem; }
  .savebar { position:sticky; bottom:0; background:var(--bg);
    padding:.7rem 0; display:flex; gap:1rem; align-items:center; }
  .save { font:inherit; font-weight:700; background:var(--green); color:#10241a;
    border:none; border-radius:6px; padding:.5rem 1.4rem; cursor:pointer; }
  .save:focus-visible,.catbtn:focus-visible { outline:2px solid var(--cyan); }
  .counts { color:var(--dim); font-size:.85rem; }
  .done { text-align:center; padding:4rem 1rem; }
  input[type=checkbox] { accent-color:var(--green); }
</style></head><body><div class="wrap" id="app"></div>
<script>
const S = __DATA__;
const CATS = ["keep","summarize","contested","drop"];
const TITLES = {keep:"Preserve — ongoing work, recent decisions, active files",
  summarize:"Summarize — completed, resolved, older",
  contested:"⚡ Contested — you decide",
  drop:"Remove — redundant, outdated (demoted, recoverable)"};
const clsStates = c => c.id === "todos" ? ["keep","drop"] : ["keep","summarize","drop"];
const esc = s => { const d = document.createElement("span"); d.textContent = s ?? ""; return d.innerHTML; };
function render() {
  const app = document.getElementById("app");
  let h = `<h1>⏸ Compaction ledger</h1>
    <p class="sub">Click a category button to cycle it · labels are editable in place · expand ▸ to check/uncheck the prompts behind an item (each with its share of the context window). Nothing is deleted — removed content is demoted, recoverable.</p>`;
  h += `<section><h2>Class rules</h2>`;
  S.classes.forEach((c, i) => {
    h += `<div class="cls"><select data-cls="${i}">` +
      clsStates(c).map(s => `<option ${s === c.state ? "selected" : ""}>${s}</option>`).join("") +
      `</select><span>${esc((c.label || c.id).replace("{n}", ""))}</span>` +
      (c.id === "first_n" ? `<input type="number" min="1" max="100" value="${c.n ?? 30}" data-n="${i}" style="width:4.5rem"> %` : "") +
      `<span class="pct">${c.pct ? "~" + c.pct + "%" : ""}</span></div>`;
  });
  h += `</section>`;
  CATS.forEach(cat => {
    const items = S.items.map((it, n) => [it, n]).filter(([it]) => it.cat === cat);
    if (!items.length && cat === "contested") return;
    h += `<section><h2 class="${cat}">${esc(TITLES[cat])}</h2>`;
    items.forEach(([it, n]) => {
      h += `<div class="item"><div class="row">
        <button class="catbtn ${it.cat}" data-cy="${n}" title="click to cycle category">${it.cat}</button>
        <span class="lbl" contenteditable data-lbl="${n}">${esc(it.label)}</span>
        <span class="pct">~${it.pct ?? "?"}%</span>
        ${it.children && it.children.length ? `<button class="exp" data-x="${n}">${it.expanded ? "▾" : "▸"} ${it.children.length}</button>` : ""}
      </div><div class="tag">${esc(it.tag)}</div>`;
      h += `<div class="children ${it.expanded ? "open" : ""}">` +
        (it.children || []).map((ch, ci) =>
          `<label class="child"><input type="checkbox" ${ch.checked ? "checked" : ""} data-ch="${n}.${ci}">
           <span class="pct">${ch.pct ?? "?"}%</span><span>${esc(ch.text)}</span></label>`).join("") + `</div>`;
      h += `<input class="note" placeholder="note…" value="${esc(it.note || "")}" data-note="${n}">`;
      h += `</div>`;
    });
    h += `</section>`;
  });
  h += `<section class="constraints"><h2>Must not be misinterpreted</h2>` +
    S.constraints.map(c => `<div class="conline">› ${esc(c)}</div>`).join("") +
    `<input type="text" id="newcon" placeholder="add a constraint and press enter…"></section>`;
  const counts = CATS.map(c => `${S.items.filter(i => i.cat === c).length} ${c}`).join(" · ");
  h += `<div class="savebar"><button class="save" id="save">Save → back to Claude</button><span class="counts">${counts}</span></div>`;
  app.innerHTML = h;
  app.querySelectorAll("[data-cy]").forEach(b => b.onclick = () => {
    const it = S.items[+b.dataset.cy];
    it.cat = CATS[(CATS.indexOf(it.cat) + 1) % 4];
    render();
  });
  app.querySelectorAll("[data-x]").forEach(b => b.onclick = () => {
    const it = S.items[+b.dataset.x]; it.expanded = !it.expanded; render();
  });
  app.querySelectorAll("[data-lbl]").forEach(el => el.onblur = () => {
    const it = S.items[+el.dataset.lbl];
    const v = el.textContent.trim();
    if (v && v !== it.label) { it.label = v; it.edited = true; }
  });
  app.querySelectorAll("[data-note]").forEach(el => el.onchange = () => {
    S.items[+el.dataset.note].note = el.value.trim();
  });
  app.querySelectorAll("[data-ch]").forEach(el => el.onchange = () => {
    const [n, ci] = el.dataset.ch.split(".");
    S.items[+n].children[+ci].checked = el.checked;
  });
  app.querySelectorAll("[data-cls]").forEach(el => el.onchange = () => {
    S.classes[+el.dataset.cls].state = el.value;
  });
  app.querySelectorAll("[data-n]").forEach(el => el.onchange = () => {
    const v = +el.value; if (v > 0 && v <= 100) S.classes[+el.dataset.n].n = v;
  });
  document.getElementById("newcon").onkeydown = e => {
    if (e.key === "Enter" && e.target.value.trim()) {
      S.constraints.push(e.target.value.trim()); render();
    }
  };
  document.getElementById("save").onclick = async () => {
    S.finalized = true;
    await fetch("/save", { method: "POST", body: JSON.stringify(S) });
    document.getElementById("app").innerHTML =
      `<div class="done"><h1>Saved ✓</h1><p class="sub">Return to Claude Code and say <b>done</b> — the compaction will use exactly this ledger.</p></div>`;
  };
}
render();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    ledger_path = None
    server_ref = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            with open(self.ledger_path) as f:
                data = f.read()
        except Exception:
            data = '{"items":[],"classes":[],"constraints":[]}'
        body = PAGE.replace("__DATA__", data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/save":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
            assert isinstance(data.get("items"), list)
            with open(self.ledger_path, "w") as f:
                json.dump(data, f, indent=1)
            self.send_response(200)
        except Exception:
            self.send_response(400)
        self.end_headers()
        threading.Thread(target=self.server_ref.shutdown, daemon=True).start()


def main():
    if len(sys.argv) != 2:
        print("usage: compact-focus-web.py <ledger.json>")
        sys.exit(1)
    Handler.ledger_path = sys.argv[1]
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    Handler.server_ref = srv
    print(f"http://127.0.0.1:{srv.server_address[1]}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
