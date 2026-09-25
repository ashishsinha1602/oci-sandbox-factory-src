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


def wait_reply(pg, before, timeout=300000):
    # a new assistant bubble that is not the typing / "reading the repo" placeholder
    pg.wait_for_function(
        "n => { const m=document.querySelectorAll('#sf-msgs .sf-msg.ai'); return m.length>n && !m[m.length-1].querySelector('.sf-typing'); }",
        arg=before, timeout=timeout)
    pg.wait_for_timeout(1500)


def ask(pg, text, create=False):
    n = pg.locator("#sf-msgs .sf-msg.ai").count()
    pg.click("textarea#sf-chat-in")
    pg.type("textarea#sf-chat-in", text, delay=28)
    pg.wait_for_timeout(600)
    pg.keyboard.press("Enter")
    wait_reply(pg, n)
    pg.wait_for_timeout(4000)
    last = pg.locator("#sf-msgs .sf-msg.ai").last
    print("reply:", last.inner_text()[:160].replace("\n", " "), flush=True)
    if create:
        btn = last.locator(".sf-act button.sf-btn:not(.sec)")
        if btn.count():
            btn.first.scroll_into_view_if_needed()
            btn.first.click()
            pg.wait_for_timeout(5000)
            print("  ->", last.locator(".sf-act").inner_text(), flush=True)
        else:
            print("  -> no Create button", flush=True)


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


def tour(pg, sid, o):
    print(f"tour {sid}", flush=True)
    app = o.get("app") or {}
    urls = [u for u in (app.get("urls") or ([app["url"]] if app.get("url") else []))]
    https = [u for u in urls if u.startswith("https://")]
    urls = https or urls
    logins = {(x.get("service") or "").lower(): x for x in (o.get("logins") or [])}
    for u in urls:
        if u.rstrip("/").endswith("/mcp"):
            continue       # an MCP endpoint answers JSON-RPC, not a page
        if "airflow" in (app.get("containers") or []) or "airflow" in logins:
            airflow(pg, u, logins.get("airflow"))
        else:
            show(pg, u, 8000)
    for inst in o.get("app_instances") or []:
        for u in (inst.get("urls") or [])[:1]:
            show(pg, u, 8000)
    # the tables the sandbox holds (the pipeline's gold tables), through ORDS
    lc = o.get("low_code") or {}
    pw = (o.get("adb") or {}).get("admin_password")
    if lc.get("rest_base") and lc.get("rest_tables") and pw:
        token = base64.b64encode(f"ADMIN:{pw}".encode()).decode()
        pg.context.set_extra_http_headers({"Authorization": "Basic " + token})
        try:
            for t in lc["rest_tables"][:3]:
                show(pg, lc["rest_base"].rstrip("/") + "/" + t + "/", 5000)
        finally:
            pg.context.set_extra_http_headers({})
    for f in o.get("functions") or []:
        if f.get("url"):
            show(pg, f["url"], 4000, scroll=False)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1366, "height": 860}, record_video_dir=OUT, record_video_size={"width": 1366, "height": 860})
    pg = ctx.new_page()
    pg.goto(URL, wait_until="networkidle", timeout=90000)
    if pg.locator("#P9999_USERNAME").count():
        pg.fill("#P9999_USERNAME", USER)
        pg.fill("#P9999_PASSWORD", PASSWORD)
        pg.keyboard.press("Enter")
        pg.wait_for_load_state("networkidle", timeout=90000)
    pg.wait_for_selector(".sf-card[data-mode=chat]", timeout=60000)
    pg.wait_for_timeout(2500)
    if PART == "part1":
        pg.click(".sf-card[data-mode=chat]")   # the chat panel is hidden until the card is chosen
        pg.wait_for_selector("textarea#sf-chat-in", state="visible", timeout=30000)
        pg.wait_for_timeout(1200)
        for q in PROMPTS:
            create = q.endswith(" => create")
            ask(pg, q[:-len(" => create")] if create else q, create=create)
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
        pg.goto(URL, wait_until="networkidle", timeout=90000)
        pg.wait_for_timeout(3000)
    ctx.close()
    b.close()

files = [f for f in os.listdir(OUT) if f.endswith(".webm")]
print("recorded:", files, flush=True)
bucket, ns = os.environ.get("REPORT_BUCKET"), os.environ.get("REPORT_NAMESPACE")
if bucket and ns and files:
    import oci
    signer = oci.auth.signers.get_resource_principals_signer()
    osc = oci.object_storage.ObjectStorageClient({}, signer=signer)
    name = f"demo/{PART}-{time.strftime('%Y%m%d-%H%M%S')}.webm"
    with open(os.path.join(OUT, files[0]), "rb") as fh:
        osc.put_object(ns, bucket, name, fh, content_type="video/webm")
    print("uploaded:", name, flush=True)
