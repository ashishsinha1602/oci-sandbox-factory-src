"""Record the product demo as a video, inside OCI.

Runs in a Playwright container instance (see ../record_demo_in_oci.py): logs in
to the Sandbox Factory as the demo user, types the demo prompts into the
assistant, confirms each plan, and records the browser as a .webm which is
uploaded to the reports bucket. No audio; add narration in an editor.

Environment: SF_URL, SF_USER, SF_PASSWORD, SF_PROMPTS (JSON list; entries
ending in " => create" click the plan's Create button), REPORT_BUCKET,
REPORT_NAMESPACE, SF_PART (part1 = the prompts, part2 = the finished cards).
"""
import base64
import json
import os
import time

from playwright.sync_api import sync_playwright

URL = os.environ["SF_URL"]
USER = os.environ["SF_USER"]
PASSWORD = os.environ["SF_PASSWORD"]
PART = os.environ.get("SF_PART", "part1")
PROMPTS = json.loads(os.environ.get("SF_PROMPTS") or "[]")
OUT = "/tmp/rec"
os.makedirs(OUT, exist_ok=True)


T0 = [0.0]      # set when the page (and so the video) starts
MCP_URLS = []   # MCP endpoints met on the tour
WAITS = []      # [start, end] seconds into the video spent waiting; the editor speeds these up


def wait_reply(pg, before, timeout=300000):
    # a new assistant bubble that is not the typing / "reading the repo" placeholder
    pg.wait_for_function(
        "n => { const m=document.querySelectorAll('#sf-msgs .sf-msg.ai'); return m.length>n && !m[m.length-1].querySelector('.sf-typing'); }",
        arg=before, timeout=timeout)
    pg.wait_for_timeout(1500)


def ask(pg, text, create=False, env=None):
    """env: "KEY=value;KEY2=value" typed into the Environment variables box
    under the proposal before Create, as a user would."""
    n = pg.locator("#sf-msgs .sf-msg.ai").count()
    pg.click("textarea#sf-chat-in")
    pg.type("textarea#sf-chat-in", text, delay=28)
    pg.wait_for_timeout(600)
    pg.keyboard.press("Enter")
    t = time.time() - T0[0] + 1.0          # keep the first second of the spinner at normal speed
    wait_reply(pg, n)
    WAITS.append([round(t, 2), round(time.time() - T0[0] - 1.5, 2)])
    pg.wait_for_timeout(4000)
    last = pg.locator("#sf-msgs .sf-msg.ai").last
    print("reply:", last.inner_text()[:160].replace("\n", " "), flush=True)
    if env:
        box = last.locator(".sf-envbox")
        if box.count():
            box.first.scroll_into_view_if_needed()
            if not box.first.evaluate("d => d.open"):
                box.first.locator("summary").click()
                pg.wait_for_timeout(900)
            ta = box.first.locator("textarea.sf-env")
            ta.click()
            ta.fill("")
            pg.type(".sf-envbox textarea.sf-env", "\n".join(env.split(";")), delay=24)
            pg.wait_for_timeout(1800)
        else:
            print("  (no Environment variables box shown)", flush=True)
    if create:
        btn = last.locator(".sf-act button.sf-btn:not(.sec)")
        if btn.count():
            btn.first.scroll_into_view_if_needed()
            btn.first.click()
            pg.wait_for_timeout(5000)
            print("  ->", last.locator(".sf-act").inner_text(), flush=True)
        else:
            print("  -> no Create button", flush=True)


MASK_JS = r"""
(() => {
  const DOTS = '••••••••';
  function secrets() {
    const out = new Set(__MASK__);
    const walk = (o, k) => {
      if (o && typeof o === 'object') { for (const [kk, v] of Object.entries(o)) walk(v, kk); return; }
      if (typeof o === 'string' && o.length >= 6 && /pass|secret|token|pwd/i.test(k || '')) out.add(o);
    };
    for (const r of (window.__sfRows || [])) { try { walk(r.outputs ? JSON.parse(r.outputs) : null, ''); } catch (e) {} }
    return [...out];
  }
  function scrub(root) {
    const list = secrets(); if (!list.length || !root) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      let t = n.nodeValue, c = t; for (const x of list) if (c.includes(x)) c = c.split(x).join(DOTS);
      if (c !== t) n.nodeValue = c;
    }
    for (const i of root.querySelectorAll ? root.querySelectorAll('input,textarea') : []) {
      if (i.type !== 'password' && list.some(x => (i.value || '').includes(x))) i.value = DOTS;
    }
  }
  new MutationObserver(() => scrub(document.body)).observe(document, {subtree: true, childList: true, characterData: true});
  document.addEventListener('DOMContentLoaded', () => scrub(document.body));
  setInterval(() => scrub(document.body), 200);
})();
"""


def show(pg, url, wait=6000, scroll=True):
    print("  open", url, flush=True)
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:  # noqa: BLE001  (a slow page still records what it drew)
        print(f"  ({type(e).__name__} loading {url})", flush=True)
    pg.wait_for_timeout(wait)
    if scroll:
        pg.mouse.wheel(0, 600)
        pg.wait_for_timeout(2500)


def grafana(pg, base, password):
    """Grafana behind the sandbox gateway: sign in as admin with the password
    the user gave as GF_SECURITY_ADMIN_PASSWORD, then the home page and the
    data-source gallery."""
    base = base.rstrip("/")
    show(pg, base + "/login", 4000, scroll=False)
    if pg.locator("input[name=user]").count():
        pg.fill("input[name=user]", "admin")
        pg.fill("input[name=password]", password or "admin")
        pg.wait_for_timeout(800)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(6000)
        skip = pg.get_by_role("button", name="Skip")
        if skip.count():
            skip.first.click()
            pg.wait_for_timeout(2500)
    show(pg, base + "/", 7000, scroll=False)
    show(pg, base + "/connections/datasources/new", 7000)


def airflow(pg, base, login):
    base = base.rstrip("/")
    show(pg, base + "/login/", 3000, scroll=False)
    if pg.locator("input[name=username]").count() and login:
        pg.fill("input[name=username]", login.get("user") or "admin")
        pg.fill("input[name=password]", login.get("password") or "")
        pg.wait_for_timeout(800)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(5000)
    show(pg, base + "/home", 5000)
    dag = pg.locator("a[href*='/dags/'][href*='/grid']").first
    if dag.count():
        dag.click()
        pg.wait_for_timeout(7000)
        graph = pg.get_by_role("button", name="Graph")
        if graph.count():
            graph.first.click()
            pg.wait_for_timeout(6000)


MCP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>MCP client</title><style>
body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0}
header{padding:18px 28px;background:#111827;border-bottom:1px solid #1f2937}
header h1{margin:0;font-size:20px}header p{margin:4px 0 0;color:#94a3b8;font-size:13px}
main{padding:18px 28px;display:grid;gap:14px}
.step{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:12px 16px}
.step h2{margin:0 0 8px;font-size:15px;color:#a5b4fc}.step h2 span{color:#94a3b8;font-weight:400;font-size:13px;margin-left:8px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12.5px/1.45 ui-monospace,Consolas,monospace;color:#cbd5e1;max-height:260px;overflow:hidden}
.req{color:#fbbf24}.ok{color:#34d399}
table{border-collapse:collapse;margin-top:6px}td,th{border:1px solid #334155;padding:5px 12px;text-align:left}th{color:#94a3b8}
</style></head><body><header><h1>An agent talking to the sandbox over MCP</h1><p>__URL__</p></header><main id="m"></main></body></html>"""


def mcp_scene(pg, url):
    """Drive the sandbox's MCP endpoint as an agent would and show each call."""
    import html
    import requests
    print("  mcp", url, flush=True)
    pg.set_content(MCP_PAGE.replace("__URL__", html.escape(url)))
    pg.wait_for_timeout(2500)
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def rpc(body):
        r = requests.post(url, json=body, headers=hdr, timeout=90)
        if r.headers.get("mcp-session-id"):
            hdr["Mcp-Session-Id"] = r.headers["mcp-session-id"]
        t = r.text
        if "data: " in t:
            t = [ln[6:] for ln in t.splitlines() if ln.startswith("data: ")][-1]
        return json.loads(t) if t.strip() else {}

    def add(title, sub, req, resp_html):
        block = (f'<div class="step"><h2>{html.escape(title)}<span>{html.escape(sub)}</span></h2>'
                 f'<pre class="req">&rarr; {html.escape(req)}</pre><pre class="ok">{resp_html}</pre></div>')
        pg.evaluate("h => { const m=document.getElementById('m'); m.insertAdjacentHTML('beforeend', h); window.scrollTo(0, document.body.scrollHeight); }", block)
        pg.wait_for_timeout(4500)

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "demo-agent", "version": "1"}}})
    info = (init.get("result") or {}).get("serverInfo") or {}
    add("1. initialize", "handshake", "initialize", html.escape(f"connected to {info.get('name')} {info.get('version')}"))
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = ((rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).get("result") or {}).get("tools") or [])
    add("2. tools/list", f"{len(tools)} tools", "tools/list",
        "<br>".join(f"<b>{html.escape(t['name'])}</b> &mdash; {html.escape((t.get('description') or '').split('.')[0][:90])}" for t in tools))
    q = "Which customers placed orders, and for which products?"
    sel = ((rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "select_schema", "arguments": {"question": q}}})
            .get("result") or {}).get("structuredContent") or {}).get("result") or {}
    picked = [e["object"] for e in (sel.get("explain") or []) if e.get("object", "").startswith("admin.")][:3]
    add("3. tools/call select_schema", "the server picks the tables a question needs", f'select_schema(question="{q}")',
        html.escape("tables: " + ", ".join(picked or [str(sel)[:200]])))
    # query the table the server picked first, whatever its columns are
    first = (picked or ["admin.customers"])[0]
    sql = f"select * from {first} fetch first 6 rows only"
    res = ((rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_query", "arguments": {"sql": sql}}})
            .get("result") or {}).get("structuredContent") or {}).get("result") or {}
    cols, rows = res.get("columns") or [], res.get("rows") or []
    table = ("<table><tr>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in cols) + "</tr>"
             + "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>" for r in rows) + "</table>")
    if not cols:
        raise RuntimeError(f"run_query returned no rows: {str(res)[:200]}")   # never film an error
    add("4. tools/call run_query", "one read-only SELECT, rows back", "run_query(sql=" + sql + ")",
        f"{len(rows)} rows" + table)
    pg.wait_for_timeout(4000)


def tour(pg, sid, o):
    print(f"tour {sid}", flush=True)
    app = o.get("app") or {}
    urls = [u for u in (app.get("urls") or ([app["url"]] if app.get("url") else []))]
    https = [u for u in urls if u.startswith("https://")]
    urls = https or urls
    logins = {(x.get("service") or "").lower(): x for x in (o.get("logins") or [])}
    for u in urls:
        if u.rstrip("/").endswith("/mcp"):
            continue       # an MCP endpoint answers JSON-RPC, not a page: played as a scene at the end
        names = " ".join(str(c) for c in (app.get("containers") or [])).lower()
        if "airflow" in names or "airflow" in logins:
            airflow(pg, u, logins.get("airflow"))
        elif "grafana" in names or "grafana" in sid.lower():
            grafana(pg, u, os.environ.get("SF_GRAFANA_PASSWORD"))
        else:
            show(pg, u, 8000)
    # MCP is played at the end, only where a container is actually named mcp,
    # at <gateway>/mcp (the first container has "/", the rest "/<name>").
    gw = next((u for u in urls if u.startswith("https://")), None)
    if gw and "mcp" in [str(c).lower() for c in (app.get("containers") or [])]:
        MCP_URLS.append(gw.rstrip("/") + "/mcp")
    seen = {str(c).lower() for c in (app.get("containers") or [])}
    for inst in o.get("app_instances") or []:
        if str(inst.get("name", "")).lower() in seen:
            continue       # the same service twice (a duplicate instance): show it once
        for u in (inst.get("urls") or [])[:1]:
            show(pg, u, 8000)
    # the tables the sandbox holds (the pipeline's gold tables), through ORDS
    lc = o.get("low_code") or {}
    pw = (o.get("adb") or {}).get("admin_password")
    if lc.get("rest_base") and pw:
        token = base64.b64encode(f"ADMIN:{pw}".encode()).decode()
        tables = list(lc.get("rest_tables") or [])
        if not tables:
            # created after the build (a pipeline's gold tables): ask ORDS what it publishes now
            try:
                r = pg.request.get(lc["rest_base"].rstrip("/") + "/metadata-catalog/",
                                   headers={"Authorization": "Basic " + token}, timeout=30000)
                # ORDS publishes each table under its lower-case alias
                tables = [i.get("name").lower() for i in (r.json().get("items") or []) if i.get("name")]
            except Exception as e:  # noqa: BLE001
                print(f"  metadata-catalog: {type(e).__name__}", flush=True)
        pg.context.set_extra_http_headers({"Authorization": "Basic " + token})
        try:
            for t in tables[:3]:
                show(pg, lc["rest_base"].rstrip("/") + "/" + t + "/", 5000)
        finally:
            pg.context.set_extra_http_headers({})
    for f in o.get("functions") or []:
        if f.get("url"):
            show(pg, f["url"], 4000, scroll=False)


def open_chat(pg):
    """Click the chat card until its panel opens: a click that lands before the
    page script has wired the cards does nothing."""
    for _ in range(10):
        pg.click(".sf-card[data-mode=chat]")
        try:
            pg.wait_for_selector("textarea#sf-chat-in", state="visible", timeout=3000)
            return
        except Exception:  # noqa: BLE001
            pg.wait_for_timeout(1000)
    raise RuntimeError("the chat panel did not open")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1366, "height": 860}, record_video_dir=OUT, record_video_size={"width": 1366, "height": 860})
    ctx.add_init_script(MASK_JS.replace("__MASK__", json.dumps([x for x in os.environ.get("SF_MASK", "").split(";") if len(x) >= 6])))
    pg = ctx.new_page()
    T0[0] = time.time()
    pg.goto(URL, wait_until="networkidle", timeout=90000)
    if pg.locator("#P9999_USERNAME").count():
        pg.fill("#P9999_USERNAME", USER)
        pg.fill("#P9999_PASSWORD", PASSWORD)
        pg.keyboard.press("Enter")
        pg.wait_for_load_state("networkidle", timeout=90000)
    pg.wait_for_selector(".sf-card[data-mode=chat]", timeout=60000)
    pg.wait_for_timeout(2500)
    if PART == "part1":
        open_chat(pg)   # the chat panel is hidden until the card is chosen
        pg.wait_for_timeout(1200)
        for q in PROMPTS:
            # "prompt => env K=V;K2=V2 => create": type the prompt, fill the env box, click Create
            parts = [x.strip() for x in q.split(" => ")]
            create = parts[-1] == "create"
            env = next((x[4:] for x in parts[1:] if x.startswith("env ")), None)
            ask(pg, parts[0], create=create, env=env)
        pg.mouse.wheel(0, 900)
        pg.wait_for_timeout(4000)
    else:
        pg.wait_for_timeout(2000)
        pg.mouse.wheel(0, 700)
        pg.wait_for_timeout(3000)
        for i in range(pg.locator(".sf-req button[data-open]").count()):
            pg.locator(".sf-req button[data-open]").nth(i).scroll_into_view_if_needed()
            pg.locator(".sf-req button[data-open]").nth(i).click()
            pg.wait_for_timeout(5000)
            pg.mouse.wheel(0, 500)
            pg.wait_for_timeout(2500)
            close = pg.locator("#sf-landing-close")
            if close.count():
                close.click()
                pg.wait_for_timeout(1500)
        pg.get_by_text("History", exact=False).first.click()
        pg.wait_for_timeout(3500)
        # Then open what each sandbox actually runs, in the same tab so it is
        # all one video: the app, Airflow (logged in) and its DAG, the tables
        # the pipeline wrote, read back through the database's REST API.
        rows = pg.evaluate("() => window.__sfRows || []")
        for r in rows:
            if r.get("status") != "DONE" or r.get("action") == "DESTROY" or not r.get("outputs"):
                continue
            try:
                tour(pg, r["sandbox_id"], json.loads(r["outputs"]))
            except Exception as e:  # noqa: BLE001
                print(f"tour {r.get('sandbox_id')}: {type(e).__name__}: {str(e)[:160]}", flush=True)
        # finish on the agent's view: MCP against each sandbox that has it
        for u in dict.fromkeys(MCP_URLS):
            try:
                mcp_scene(pg, u)
            except Exception as e:  # noqa: BLE001
                print(f"mcp {u}: {type(e).__name__}: {str(e)[:160]}", flush=True)
    ctx.close()
    b.close()

print("waits:", json.dumps(WAITS), flush=True)
files = [f for f in os.listdir(OUT) if f.endswith(".webm")]
print("recorded:", files, flush=True)
bucket, ns = os.environ.get("REPORT_BUCKET"), os.environ.get("REPORT_NAMESPACE")
if bucket and ns and files:
    import oci
    signer = oci.auth.signers.get_resource_principals_signer()
    osc = oci.object_storage.ObjectStorageClient({}, signer=signer)
    try:
        osc.get_bucket(ns, bucket)
    except Exception:  # noqa: BLE001  (a fresh install: no reports bucket yet)
        osc.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(name=bucket, compartment_id=os.environ["REPORT_COMPARTMENT"]))
    name = f"demo/{PART}-{time.strftime('%Y%m%d-%H%M%S')}.webm"
    with open(os.path.join(OUT, files[0]), "rb") as fh:
        osc.put_object(ns, bucket, name, fh, content_type="video/webm")
    print("uploaded:", name, flush=True)
