"""End-to-end test of the Sandbox Factory against a live tenancy.

    python tests/e2e.py                 # every path except Kafka (25+ min extra)
    python tests/e2e.py --kafka
    python tests/e2e.py --keep          # leave the sandboxes for a look
    python tests/e2e.py --only web,data,lake

For each catalogue path it submits a request exactly as the page does (the SF
ajax process, as an APEX user, so the ownership trigger and quota run too),
waits for the worker, then verifies with real calls: HTTP on the app URL,
an object put/get in the bucket, a message through the queue, a row in NoSQL,
a Data Flow run, the function's public URL, Select AI on the database, the
knowledge-base answer and its cache, the Airflow login, Kafka produce and
consume. It also checks the assistant (chat widgets, planner, attached code)
and that a second user sees only their own sandboxes. Finally it destroys
everything it created and checks that nothing is left.

Writes tests/reports/e2e-<timestamp>.md and .json. Needs SBX_ADMIN_PASSWORD
and the laptop's ~/.oci/config (for the verification calls).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import traceback
import uuid

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "factory"))
os.chdir(ROOT / "factory")

import controldb  # noqa: E402
import oci  # noqa: E402
import requests  # noqa: E402
import sandbox_factory as sf  # noqa: E402

USER = "E2ETEST"
USER2 = "TEAMUSER2"
PREFIX = "e2e-"
AJAX = (ROOT / "factory" / "apex_home" / "ajax.plsql").read_text(encoding="utf-8").strip().rstrip("/").strip()
RESULTS: list[dict] = []
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.0f}s] {msg}", flush=True)


def record(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
    log(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------
# the page's own back end, as an APEX user
# --------------------------------------------------------------------------
class Factory:
    def __init__(self):
        self.conn = controldb.connect()
        self.cur = self.conn.cursor()
        self.cur.execute("alter session set current_schema=SBX")

    def ajax(self, user, action, payload, clob=None):
        src = AJAX.replace(":APP_USER", f"'{user}'")
        c = self.cur
        c.execute("begin apex_session.create_session(p_app_id=>112,p_page_id=>1,p_username=>:u); end;", u=user)
        c.execute("""declare nm owa.vc_arr; vl owa.vc_arr; begin nm(1):='x'; vl(1):='x'; owa.init_cgi_env(1,nm,vl); htp.init;
                     apex_application.g_x01:=:a; apex_application.g_x02:=:p; apex_application.g_clob_01:=:c; end;""",
                  a=action, p=json.dumps(payload), c=clob)
        c.execute(src)
        # The page reads a CLOB; so must the harness (a planner answer with a
        # cost table, or an attached-code reply, is longer than 32767 bytes).
        import oracledb
        v = c.var(oracledb.DB_TYPE_CLOB)
        c.execute("""declare l htp.htbuf_arr; n integer:=99999; s clob; begin dbms_lob.createtemporary(s, true); owa.get_page(l,n);
                     for i in 1..n loop dbms_lob.writeappend(s, length(l(i)), l(i)); end loop; :o:=s; end;""", o=v)
        self.conn.commit()
        body = v.getvalue()
        body = body.read() if hasattr(body, "read") else body
        return json.loads(body.split("\n\n", 1)[1])

    def submit(self, user, payload):
        r = self.ajax(user, "submit", payload)
        if "id" not in r:
            raise RuntimeError(f"submit refused: {r}")
        return r["id"]

    def status(self, rid):
        self.cur.execute("select status, error, outputs from sbx.sandbox_requests where id=:i", i=rid)
        st, err, out = self.cur.fetchone()
        out = out.read() if hasattr(out, "read") else out
        return st, err, (json.loads(out) if out else {})

    def wait(self, ids: dict, timeout=3600):
        """ids: {name: request id}. Returns {name: (status, error, outputs)}."""
        done, deadline = {}, time.time() + timeout
        while len(done) < len(ids) and time.time() < deadline:
            for name, rid in ids.items():
                if name in done:
                    continue
                st, err, out = self.status(rid)
                if st in ("DONE", "FAILED"):
                    done[name] = (st, err, out)
                    log(f"{name} (request {rid}): {st}" + (f" - {str(err)[:200]}" if err else ""))
            time.sleep(15)
        for name in ids:
            done.setdefault(name, ("TIMEOUT", "not finished in time", {}))
        return done


def base(name, **kw):
    p = {"sandbox_id": PREFIX + name, "action": "CREATE", "ttl_days": 1, "enable_adb": False, "enable_kafka": False,
         "enable_nosql": False, "enable_app": False, "app_port": 80, "text": f"e2e {name}"}
    p.update(kw)
    return p


AIRFLOW_ARGS = ('airflow db migrate && airflow users create --role Admin --username admin --password "$AIRFLOW_ADMIN_PASSWORD" '
                '--firstname Sandbox --lastname Admin --email admin@example.com; airflow scheduler & exec airflow webserver --port 8080')
AIRFLOW_ENV = {"AIRFLOW_ADMIN_PASSWORD": "{{GENERATE_PASSWORD}}", "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
               "AIRFLOW__WEBSERVER__WORKERS": "2", "AIRFLOW__WEBSERVER__EXPOSE_CONFIG": "False",
               "AIRFLOW__API__AUTH_BACKENDS": "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session"}
SPARK = """from pyspark.sql import SparkSession
import sys
spark = SparkSession.builder.appName("e2e").getOrCreate()
df = spark.createDataFrame([(i, i * i) for i in range(100)], ["n", "sq"])
df.write.mode("overwrite").parquet(sys.argv[1])
print("e2e spark ok", df.count())
"""


TICK_FN = """import datetime, os
import oci


def handler(event, context):
    signer = oci.auth.signers.get_resource_principals_signer()
    osc = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    name = "ticks/" + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S") + ".txt"
    osc.put_object(os.environ["OBJECT_NAMESPACE"], os.environ["DATA_BUCKET"], name, b"tick")
    return {"wrote": name}
"""
ICEBERG_SPARK = """from pyspark.sql import SparkSession
spark = SparkSession.builder.appName("e2e-iceberg").getOrCreate()
spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.e2e")
df = spark.createDataFrame([(i, i * i) for i in range(50)], ["n", "sq"])
df.writeTo("lake.e2e.squares").createOrReplace()
print("e2e iceberg rows", spark.table("lake.e2e.squares").count())
"""


def cases(fn_image: str | None):
    c = {
        "web": base("web", enable_app=True, app_image="docker.io/library/nginx:alpine", app_port=80),
        "data": base("data", enable_nosql=True, buckets=[{"name": "files"}], queues=[{"name": "jobs"}],
                     **({"functions": [{"name": "hello", "image": fn_image}]} if fn_image else {})),
        "lake": base("lake", buckets=[{"name": "data"}], enable_catalog=True,
                     dataflow_jobs=[{"name": "squares", "script": SPARK, "language": "PYTHON"}]),
        "db": base("db", enable_adb=True, adb_tier="paid", seed_key="sales", app_template="api", app_port=8080, enable_app=True),
        "rag": base("rag", enable_adb=True, adb_tier="paid", app_template="rag", app_port=8080, enable_app=True),
        "airflow": base("airflow", enable_app=True, app_port=8080,
                        containers=[{"name": "airflow", "image": "docker.io/apache/airflow:2.10.3", "port": 8080,
                                     "command": ["bash", "-c"], "args": [AIRFLOW_ARGS], "env": AIRFLOW_ENV}]),
        "kafka": base("kafka", enable_kafka=True, kafka_mode="cluster", ttl_days=1),
        # plain AWS Lambda code (no Dockerfile) and a function with no code at all:
        # both must become working OCI Functions built inside the tenancy
        "fn": base("fn", functions=[{"name": "lambda", "git_url": REPO_TREE + "/examples/hello-lambda"},
                                    {"name": "starter"}]),
        # a scheduled function written inline (the EventBridge + Lambda shape): every
        # run drops a file in the bucket, so the schedule is proven by the files
        "sched": base("sched", buckets=[{"name": "raw"}],
                      functions=[{"name": "tick", "schedule": "*/15 * * * *", "timeout_sec": 120,
                                  "files": {"func.py": TICK_FN, "requirements.txt": "oci"}}]),
        # Iceberg on Object Storage written by Data Flow (the Glue + Iceberg shape)
        "iceberg": base("iceberg", buckets=[{"name": "lake"}],
                        dataflow_jobs=[{"name": "ice", "script": ICEBERG_SPARK, "language": "PYTHON", "iceberg": True}]),
        # "just a Data Flow job": no bucket asked for, one is added for its script and logs
        "flow": base("flow", dataflow_jobs=[{"name": "squares", "script": SPARK, "language": "PYTHON"}]),
        # a pipeline folder sent as an app deploy must fail at once, in plain words
        "guard": base("guard", action="DEPLOY", enable_app=True, app_port=8080,
                      git_url=REPO_TREE + "/examples/telemetry-pipeline"),
    }
    return c


REPO_TREE = os.environ.get("SBX_REPO_TREE", "https://github.com/ashishsinha1602/oci-sandbox-factory/tree/main")
EXPECTED_FAIL = {"guard": "No Dockerfile"}


def call_function(f, body):
    r = None
    for _ in range(9):
        try:
            r = requests.post(f["url"], json=body, timeout=90)
            if r.status_code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(20)
    return r


def verify_sched(o, fnd):
    osc = sf.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    b = (o.get("buckets") or [{}])[0].get("name")
    fn = (o.get("functions") or [{}])[0]
    record("sched: function has a schedule", bool(fn.get("schedule")), str(fn.get("schedule")))
    ticks = []
    for _ in range(40):                       # up to 20 minutes: a */15 schedule fires at least once
        ticks = [x.name for x in osc.list_objects(ns, b, prefix="ticks/", fields="name").data.objects]
        if len(ticks) >= 1:
            break
        time.sleep(30)
    record("sched: Resource Scheduler invoked the function (it wrote to the bucket)", len(ticks) >= 1, f"{len(ticks)} tick file(s) in {b}")


def verify_iceberg(o, fnd):
    df = sf.client(oci.data_flow.DataFlowClient)
    job = (o.get("dataflow_jobs") or [{}])[0]
    osc = sf.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    b = (o.get("buckets") or [{}])[0].get("name")
    run = df.create_run(oci.data_flow.models.CreateRunDetails(compartment_id=fnd["compartments"]["sandboxes"], application_id=job["id"],
                                                              display_name="e2e iceberg")).data
    for _ in range(60):
        r = df.get_run(run.id).data
        if r.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED", "STOPPED"):
            break
        time.sleep(30)
    meta = [x.name for x in osc.list_objects(ns, b, prefix="iceberg/", fields="name").data.objects]
    record("iceberg: Data Flow writes an Iceberg table to Object Storage",
           r.lifecycle_state == "SUCCEEDED" and any(n.endswith(".metadata.json") for n in meta),
           f"run {r.lifecycle_state} {r.lifecycle_details or ''}; iceberg objects {len(meta)}")
    q = next((j for j in o.get("dataflow_jobs") or [] if j.get("query")), None)
    record("iceberg: a query application comes with the Iceberg job", q is not None, (q or {}).get("name"))
    if q:
        run = df.create_run(oci.data_flow.models.CreateRunDetails(
            compartment_id=fnd["compartments"]["sandboxes"], application_id=q["id"], display_name="e2e iceberg query",
            parameters=[oci.data_flow.models.ApplicationParameter(name="sql", value="SELECT count(*) AS n FROM lake.e2e.squares")])).data
        for _ in range(60):
            r = df.get_run(run.id).data
            if r.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED", "STOPPED"):
                break
            time.sleep(30)
        csv = [x.name for x in osc.list_objects(ns, b, prefix="query-results/", fields="name").data.objects if x.name.endswith(".csv")]
        body = osc.get_object(ns, b, csv[-1]).data.content.decode() if csv else ""
        record("iceberg: the query application reads the table back (50 rows)", r.lifecycle_state == "SUCCEEDED" and "50" in body,
               f"run {r.lifecycle_state} {r.lifecycle_details or ''}; result {body.strip()[:60]!r}")


def verify_fn(o):
    fns = {f.get("name", "").split("-")[-1]: f for f in o.get("functions") or [] if f.get("url")}
    lam = next((f for n, f in fns.items() if "lambda" in n or "lambda" in f.get("name", "")), None)
    st = next((f for n, f in fns.items() if "starter" in n or "starter" in f.get("name", "")), None)
    r = call_function(lam, {"name": "factory"}) if lam else None
    record("fn: AWS Lambda code runs as an OCI Function", r is not None and r.status_code == 200 and "hello factory" in r.text
           and "aws-lambda" in r.text, f"{(lam or {}).get('url')} -> {getattr(r, 'status_code', None)} {getattr(r, 'text', '')[:100]}")
    r = call_function(st, {"name": "factory"}) if st else None
    record("fn: a function with no code gets the starter", r is not None and r.status_code == 200 and "hello factory" in r.text,
           f"{(st or {}).get('url')} -> {getattr(r, 'status_code', None)} {getattr(r, 'text', '')[:100]}")


# --------------------------------------------------------------------------
# verifications
# --------------------------------------------------------------------------
def http_ok(url, **kw):
    for _ in range(6):
        try:
            r = requests.get(url, timeout=60, **kw)
            if r.status_code < 500:
                return r
        except Exception:  # noqa: BLE001
            pass
        time.sleep(20)
    return None


def verify_web(o):
    url = (o.get("app") or {}).get("url")
    r = http_ok(url)
    record("web: app URL answers over HTTPS", r is not None and r.status_code == 200, f"{url} -> {getattr(r, 'status_code', None)}")


def verify_data(o, fnd):
    osc = sf.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    b = (o.get("buckets") or [{}])[0].get("name")
    key = f"e2e/{uuid.uuid4().hex}.txt"
    osc.put_object(ns, b, key, b"hello bucket")
    got = osc.get_object(ns, b, key).data.content
    record("data: bucket put/get", got == b"hello bucket", b)
    q = (o.get("queues") or [{}])[0]
    qc = oci.queue.QueueClient(**sf.auth(), service_endpoint=q.get("messages_endpoint"))
    qc.put_messages(q["id"], oci.queue.models.PutMessagesDetails(messages=[oci.queue.models.PutMessagesDetailsEntry(content="ping")]))
    msgs = qc.get_messages(q["id"], timeout_in_seconds=10).data.messages
    record("data: queue put/get", any(m.content == "ping" for m in msgs), q.get("name"))
    t = (o.get("nosql") or {}).get("tables", [None])[0]
    nc = sf.client(oci.nosql.NosqlClient)
    tbl = nc.get_table(t, compartment_id=fnd["compartments"]["sandboxes"]).data
    cols = [c.name for c in tbl.schema.columns]
    pk = tbl.schema.primary_key[0]
    # only the key and any JSON column: every other column may stay null
    row = {pk: "e2e-1"}
    for c in tbl.schema.columns:
        if c.name != pk and c.type == "JSON":
            row[c.name] = {"e2e": True}
    nc.update_row(t, oci.nosql.models.UpdateRowDetails(compartment_id=fnd["compartments"]["sandboxes"], value=row))
    back = nc.get_row(t, key=[f"{pk}:e2e-1"], compartment_id=fnd["compartments"]["sandboxes"]).data.value
    record("data: NoSQL put/get row", back is not None and back.get(pk) == "e2e-1", f"{t} columns {cols}")
    fns = [f for f in o.get("functions") or [] if f.get("url")]
    if fns:
        r = None
        for _ in range(9):
            try:
                r = requests.post(fns[0]["url"], json={"name": "factory"}, timeout=90)
                if r.status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(20)
        detail = f"{fns[0]['url']} -> {getattr(r, 'status_code', None)} {getattr(r, 'text', '')[:80]}"
        if not (r is not None and r.status_code == 200) and fns[0].get("id") and fns[0].get("invoke_endpoint"):
            # The gateway hides the function's own error; invoke it directly so the
            # report says whether the function or the gateway route is at fault.
            try:
                fi = sf.client(oci.functions.FunctionsInvokeClient)
                fi.base_client.endpoint = fns[0]["invoke_endpoint"]
                d = fi.invoke_function(fns[0]["id"], invoke_function_body=json.dumps({"name": "factory"}).encode())
                detail += f" | direct invoke: {d.status} {d.data.content[:120]!r}"
            except Exception as e:  # noqa: BLE001
                detail += f" | direct invoke: {type(e).__name__}: {str(e)[:200]}"
        record("data: function public URL", r is not None and r.status_code == 200 and "hello factory" in r.text, detail)


def verify_lake(o, fnd, label="lake"):
    df = sf.client(oci.data_flow.DataFlowClient)
    job = (o.get("dataflow_jobs") or [{}])[0]
    osc = sf.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    b = (o.get("buckets") or [{}])[0].get("name")
    out = f"oci://{b}@{ns}/e2e-out/"
    run = df.create_run(oci.data_flow.models.CreateRunDetails(compartment_id=fnd["compartments"]["sandboxes"], application_id=job["id"],
                                                              display_name="e2e squares", arguments=[out])).data
    for _ in range(60):
        r = df.get_run(run.id).data
        if r.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED", "STOPPED"):
            break
        time.sleep(30)
    objs = [x.name for x in osc.list_objects(ns, b, prefix="e2e-out/", fields="name").data.objects]
    record(f"{label}: Data Flow run writes Parquet to the bucket", r.lifecycle_state == "SUCCEEDED" and any(n.endswith(".parquet") for n in objs),
           f"run {r.lifecycle_state} {r.lifecycle_details or ''}; objects {len(objs)}")
    cat = o.get("catalog") or {}
    if cat.get("id"):
        dc = sf.client(oci.data_catalog.DataCatalogClient)
        st = dc.get_catalog(cat["id"]).data.lifecycle_state
        record(f"{label}: Data Catalog is ACTIVE", st == "ACTIVE", cat.get("display_name"))


def verify_db(o):
    url = (o.get("app") or {}).get("url")
    r = http_ok(url + "/api/tables")
    tables = [t.get("table_name", t) for t in (r.json() if r is not None and r.status_code == 200 else [])]
    record("db: seeded tables visible through the API", bool(tables) and any("CUSTOMERS" in str(t).upper() for t in tables), f"{tables}")
    ans = None
    for _ in range(4):
        try:
            a = requests.post(url + "/api/ask", json={"question": "How many customers are there?"}, timeout=120)
            if a.status_code == 200:
                ans = a.json()
                break
            ans = a.text[:200]
        except Exception as e:  # noqa: BLE001
            ans = str(e)
        time.sleep(20)
    record("db: Select AI answers a plain-English question", isinstance(ans, dict) and bool(ans.get("answer")), str(ans)[:200])
    adb = o.get("adb") or {}
    record("db: ADMIN password on the card", bool(adb.get("admin_password")), adb.get("db_name"))
    lc = o.get("low_code") or {}
    if lc.get("rest_base") and adb.get("admin_password"):
        rr = requests.get(lc["rest_base"] + "customers/", auth=("ADMIN", adb["admin_password"]), timeout=60)
        record("db: ORDS REST endpoint (authenticated)", rr.status_code == 200 and "items" in rr.text, f"{lc['rest_base']}customers/ -> {rr.status_code}")


def verify_rag(o):
    url = (o.get("app") or {}).get("url")
    st = http_ok(url + "/api/status")
    emb = (st.json() if st is not None and st.status_code == 200 else {}).get("embedded", 0)
    record("rag: starter documents embedded in the database", emb and int(emb) > 0, str(st.json())[:150] if st is not None else "no status")
    a1 = a2 = None
    for _ in range(6):
        try:
            r = requests.post(url + "/api/ask", json={"question": "What does VECTOR_DISTANCE do?", "top_k": 4}, timeout=150)
            if r.status_code == 200:
                a1 = r.json()
                break
            a1 = r.text[:200]
        except Exception as e:  # noqa: BLE001
            a1 = str(e)
        time.sleep(30)
    record("rag: Ask the documents answers", isinstance(a1, dict) and bool(a1.get("answer")), str(a1)[:200])
    if isinstance(a1, dict):
        t = time.time()
        a2 = requests.post(url + "/api/ask", json={"question": "What does VECTOR_DISTANCE do?", "top_k": 4}, timeout=150).json()
        record("rag: second Ask is served from the cache", a2.get("cached") is True and time.time() - t < 5, f"cached={a2.get('cached')} in {time.time() - t:.1f}s")


def verify_airflow(o):
    url = (o.get("app") or {}).get("url")
    login = [l for l in o.get("logins") or [] if l.get("service") == "airflow"]
    record("airflow: generated admin login on the card", bool(login), str(login)[:80])
    if login:
        r = None
        for _ in range(8):
            try:
                r = requests.get(url + "/api/v1/dags", auth=("admin", login[0]["password"]), timeout=60)
                if r.status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(30)
        record("airflow: REST API accepts the generated login", r is not None and r.status_code == 200, f"{url} -> {getattr(r, 'status_code', None)}")


def verify_kafka(o):
    from kafka import KafkaConsumer, KafkaProducer
    k = o.get("kafka") or {}
    boot, user, pw = k.get("public_bootstrap"), k.get("username"), k.get("password")
    record("kafka: public bootstrap + superuser on the card", bool(boot and user and pw), f"{boot} {user}")
    if not (boot and user and pw):
        return
    common = dict(bootstrap_servers=boot, security_protocol="SASL_SSL", sasl_mechanism="SCRAM-SHA-512",
                  sasl_plain_username=user, sasl_plain_password=pw)
    topic = (k.get("topics") or ["events"])[0]
    msg = f"e2e {uuid.uuid4().hex[:8]}"
    p = KafkaProducer(**common, request_timeout_ms=30000)
    md = p.send(topic, msg.encode()).get(timeout=60)
    p.flush()
    p.close()
    c = KafkaConsumer(topic, **common, auto_offset_reset="earliest", consumer_timeout_ms=30000, group_id="e2e-" + uuid.uuid4().hex[:6])
    seen = [m.value.decode() for m in c]
    record("kafka: produce and consume from outside OCI", msg in seen, f"offset {md.offset}, {len(seen)} messages read")


def verify_ai(f: Factory):
    r = f.ajax(USER, "chat", {"model": "google.gemini-2.5-flash", "messages": [{"role": "user", "text": "What do I have running?"}]})
    m = re.search(r"\{[\s\S]*\}", r.get("raw", ""))
    j = json.loads(m.group(0)) if m else {}
    record("ai: chat answers as JSON with a sandboxes widget", isinstance(j.get("sandboxes"), list) and bool(j.get("reply")), str(j)[:200])
    r = f.ajax(USER, "plan", {"model": "google.gemini-2.5-flash", "text": "A database with sample sales data and a Python API on it for 2 days"})
    m = re.search(r"\{[\s\S]*\}", r.get("raw", ""))
    j = json.loads(m.group(0)) if m else {}
    record("ai: planner returns a build plan", j.get("enable_adb") is True and bool(j.get("summary")), str({k: j.get(k) for k in ('sandbox_id', 'enable_adb', 'enable_app')}))
    ex = ROOT / "examples" / "telemetry-pipeline"
    files = {str(p.relative_to(ex)).replace("\\", "/"): p.read_text(encoding="utf-8") for p in ex.rglob("*") if p.is_file()}
    digest = f"\n\nATTACHED CODE ({len(files)} files from examples/telemetry-pipeline):\nfiles: {', '.join(files)}\n" + "".join(
        f"\n=== {n} ===\n{files[n][:8000]}\n" for n in files if n.endswith((".py", ".md")))
    r = f.ajax(USER, "chat", {"model": "google.gemini-2.5-flash", "messages": [
        {"role": "user", "text": "I have Airflow writing to S3 and a Glue job building an Iceberg table. Move it to OCI."},
        {"role": "assistant", "text": "Please give me the code: a Git URL or a folder."},
        {"role": "user", "text": "here is the code"}]}, clob=digest)
    m = re.search(r"\{[\s\S]*\}", r.get("raw", ""))
    j = json.loads(m.group(0)) if m else {}
    w = ((j.get("action") or {}).get("workload")) or {}
    record("ai: attached code becomes a workload plan (DAG + Spark) with a cost table",
           bool(w.get("dags")) and bool(w.get("spark")) and bool((j.get("cost") or {}).get("items")), str(w)[:200])


def verify_isolation(f: Factory):
    mine = [r["sandbox_id"] for r in f.ajax(USER2, "status", {})]
    others = [r["sandbox_id"] for r in f.ajax(USER, "status", {})]
    record("users: the second user sees none of the first user's sandboxes", not set(mine) & set(others), f"{USER2}: {mine}")
    r = f.ajax(USER2, "submit", base("web", action="DESTROY"))
    record("users: the second user cannot destroy the first user's sandbox", "err" in r, str(r)[:120])


# --------------------------------------------------------------------------
def run_wave(f, fnd, todo, keep):
    """Submit one wave of sandboxes, verify them, destroy them. Returns {name: status}."""
    ids = {}
    for name, payload in todo.items():
        try:
            ids[name] = f.submit(USER, payload)
            log(f"submitted {name} as request {ids[name]}")
        except Exception as e:  # noqa: BLE001
            record(f"{name}: request accepted by the page's back end", False, str(e))
    for name in ids:
        record(f"{name}: request accepted by the page's back end", True)

    results = f.wait(ids, timeout=5400 if "kafka" in todo else 2400)
    outs = {}
    for name, (st, err, out) in results.items():
        if name in EXPECTED_FAIL:
            record(f"{name}: refused at once with a plain reason", st == "FAILED" and EXPECTED_FAIL[name] in str(err or ""),
                   f"{st}: {str(err or '')[:200]}")
            continue
        record(f"{name}: sandbox built by the worker", st == "DONE", err or st)
        if st == "DONE":
            outs[name] = out
            record(f"{name}: expiry stamped on the sandbox", bool((out.get("sandbox") or {}).get("expires")), (out.get("sandbox") or {}).get("expires"))
            if out.get("warnings"):
                # A region limit the factory worked around (public IP instead of a
                # gateway, no Data Catalog) is the tenancy's ceiling, not a defect:
                # seven sandboxes at once will always exceed a limit of five.
                limits = all(("limit" in w and ("Gateway" in w or "Catalog" in w)) for w in out["warnings"])
                record(f"{name}: built {'within region limits (fallback noted)' if limits else 'without warnings'}",
                       limits, "; ".join(out["warnings"])[:300])
    verifiers = {"web": lambda o: verify_web(o), "data": lambda o: verify_data(o, fnd), "lake": lambda o: verify_lake(o, fnd),
                 "db": verify_db, "rag": verify_rag, "airflow": verify_airflow, "kafka": verify_kafka,
                 "fn": verify_fn, "flow": lambda o: verify_lake(o, fnd, label="flow"),
                 "sched": lambda o: verify_sched(o, fnd), "iceberg": lambda o: verify_iceberg(o, fnd)}
    for name, o in outs.items():
        try:
            verifiers[name](o)
        except Exception as e:  # noqa: BLE001
            record(f"{name}: verification ran", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")

    if not keep:
        dids = {}
        for name in ids:
            try:
                dids[name] = f.submit(USER, base(name, action="DESTROY"))
            except Exception as e:  # noqa: BLE001
                record(f"{name}: destroy accepted", False, str(e))
        dres = f.wait(dids, timeout=2400)
        for name, (st, err, _) in dres.items():
            record(f"{name}: destroyed", st == "DONE", err or st)
        cc = sf.client(oci.container_instances.ContainerInstanceClient)
        left = [x.display_name for x in cc.list_container_instances(compartment_id=fnd["compartments"]["sandboxes"]).data.items
                if x.display_name.startswith("sbx-" + PREFIX) and x.lifecycle_state not in ("DELETED", "DELETING")]
        osc = sf.client(oci.object_storage.ObjectStorageClient)
        ns = osc.get_namespace().data
        lb = [b.name for b in osc.list_buckets(ns, fnd["compartments"]["sandboxes"]).data if b.name.startswith("sbx-" + PREFIX)]
        record("cleanup: no e2e container instances or buckets left", not left and not lb, f"instances {left} buckets {lb}")
        hist = [h["sandbox_id"] for h in f.ajax(USER, "history", {})]
        built = [n for n, (st, _, _) in results.items() if st == "DONE"]
        record("cleanup: destroyed sandboxes appear in History", all((PREFIX + n) in hist for n in built), str([h for h in hist if h.startswith(PREFIX)])[:200])

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kafka", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    fnd = sf.foundation()
    f = Factory()
    only = [x for x in a.only.split(",") if x]

    # a function image, built by the factory's own builder from the repo
    fn_image = None
    if not only or "data" in only:
        try:
            import oci_build
            import profile as tenancy_profile
            prof = tenancy_profile.refresh()                       # region key + namespace of THIS tenancy
            registry = f"{prof['region_key']}.ocir.io"
            fn_image = f"{registry}/{prof['namespace']}/sbx/hello-fn/app:e2e"
            oci_build.build_in_oci(git_url=os.environ.get("SBX_REPO_URL", "https://github.com/ashishsinha1602/oci-sandbox-factory.git"), image=fn_image,
                                   registry=registry, namespace=prof["namespace"],
                                   sandbox_id="hello-fn", platform="linux/arm64", dockerfile="Dockerfile", sub_path="examples/hello-fn")
            record("build: function image built inside OCI from the repo", True, fn_image)
        except BaseException as e:  # noqa: BLE001  (the builder exits via SystemExit on failure)
            record("build: function image built inside OCI from the repo", False, f"{type(e).__name__}: {e}")
            fn_image = None

    # tenancy limits decide what can be tested; say so in the report
    try:
        lim = sf.client(oci.limits.LimitsClient)
        ten = sf.config()["tenancy"]
        gw = lim.get_resource_availability("api-gateway", "gateway-count", ten).data
        record("preflight: API Gateway limit read (a full limit means apps get a public IP)", True, f"used {gw.used}, available {gw.available}")
        cat_ok = sf.catalog_available(sf.config(), fnd)
        record("preflight: Data Catalog limit read (a full limit means lake is built without one)", True, ("room left" if cat_ok else "; ".join(sf.NOTES)))
        sf.NOTES.clear()
    except Exception as e:  # noqa: BLE001
        cat_ok = True
        record("preflight: limits readable", False, f"{type(e).__name__}: {e}")
    todo = cases(fn_image)
    if not cat_ok:
        todo["lake"]["enable_catalog"] = False
    if not a.kafka:
        todo.pop("kafka")
    if only:
        todo = {k: v for k, v in todo.items() if k in only}

    for name, fn in (("ai", verify_ai), ("users", verify_isolation)):
        try:
            fn(f)
        except Exception as e:  # noqa: BLE001
            record(f"{name}: checks ran", False, f"{type(e).__name__}: {e}")

    # Waves: every case is one user's sandbox, and a region has only so many
    # gateways and catalogs, so at most WAVE run at once; the test user's
    # sandbox limit is raised for the run and put back afterwards.
    wave = int(os.environ.get("SBX_E2E_WAVE", "6"))
    cur = f.conn.cursor()
    cur.execute(f"select value from {controldb.SCHEMA}.factory_config where key = 'max_sandboxes_per_user'")
    row = cur.fetchone()
    cap_before = row[0] if row else None
    def set_cap(v):
        if v is None:
            cur.execute(f"delete from {controldb.SCHEMA}.factory_config where key = 'max_sandboxes_per_user'")
        else:
            cur.execute(f"""merge into {controldb.SCHEMA}.factory_config c using (select 'max_sandboxes_per_user' key, :v value from dual) s
                            on (c.key = s.key) when matched then update set c.value = s.value
                            when not matched then insert (key, value) values (s.key, s.value)""", v=str(v))
        f.conn.commit()
    names = list(todo)
    results = {}
    try:
        set_cap(max(int(cap_before or 3), wave + 1))
        for w0 in range(0, len(names), wave):
            part = {n: todo[n] for n in names[w0:w0 + wave]}
            log(f"wave {w0 // wave + 1}: {', '.join(part)}")
            results.update(run_wave(f, fnd, part, a.keep))
    finally:
        set_cap(cap_before)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M")
    rep = HERE / "reports"
    rep.mkdir(exist_ok=True)
    passed = sum(1 for r in RESULTS if r["ok"])
    md = [f"# Sandbox Factory end-to-end run {ts}", "", f"**{passed} / {len(RESULTS)} checks passed** in {int(time.time() - T0) // 60} min.", "",
          "| Check | Result | Detail |", "|---|---|---|"]
    md += [f"| {r['check']} | {'PASS' if r['ok'] else 'FAIL'} | {r['detail'].replace('|', '/').replace(chr(10), ' ')[:160]} |" for r in RESULTS]
    (rep / f"e2e-{ts}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (rep / f"e2e-{ts}.json").write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")
    if sf.in_oci():
        # Running inside OCI: the container is gone after the run, so the report
        # goes to a bucket in sbx-control as well.
        try:
            osc = sf.client(oci.object_storage.ObjectStorageClient)
            ns = osc.get_namespace().data
            bucket = "sbx-factory-reports"
            try:
                osc.get_bucket(ns, bucket)
            except oci.exceptions.ServiceError:
                osc.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(name=bucket, compartment_id=fnd["compartments"]["control"]))
            for ext in ("md", "json"):
                osc.put_object(ns, bucket, f"e2e-{ts}.{ext}", (rep / f"e2e-{ts}.{ext}").read_bytes())
            log(f"report uploaded to bucket {bucket}: e2e-{ts}.md")
        except Exception as e:  # noqa: BLE001
            log(f"report upload skipped ({type(e).__name__}: {e})")
    log(f"{passed}/{len(RESULTS)} passed; report tests/reports/e2e-{ts}.md")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
