#!/usr/bin/env python3
"""Request worker: turns rows in SBX.SANDBOX_REQUESTS into sandboxes.

The APEX app (or anything else) inserts a row with status QUEUED. This loop
claims it, runs the matching factory command, streams the log into the row,
and marks it DONE or FAILED with the outputs. Runs wherever Docker and the
OCI credentials live (your laptop today, a VM later).

    python worker.py            # poll forever
    python worker.py --once     # process what is queued, then exit
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

import oci

import app_templates
import controldb
import sandbox_factory as sf

POLL_SECONDS = 10
REAP_SECONDS = 1800  # destroy expired sandboxes every 30 minutes


class RowLog(io.TextIOBase):
    """stdout replacement that mirrors output into the request row's LOG column."""

    def __init__(self, conn, request_id):
        self.conn, self.request_id, self.buf = conn, request_id, []

    def write(self, s):
        sys.__stdout__.write(s)
        self.buf.append(s)
        if "\n" in s:
            self.flush_row()
        return len(s)

    def flush_row(self):
        cur = self.conn.cursor()
        cur.execute("update sbx.sandbox_requests set log = :1 where id = :2", ["".join(self.buf), self.request_id])
        self.conn.commit()



def kafka_credentials(outputs: dict, wait_seconds: int = 180) -> None:
    """Put the Kafka superuser's username and password on the outputs.

    The Kafka service writes the generated password into the sandbox's Vault
    secret when the superuser is enabled; the stack's placeholder is
    "pending" until it does. Read the latest version, parse it (JSON with a
    username when the service gives one), and hand the user everything a
    client needs.
    """
    import base64
    k = outputs["kafka"]
    sc = sf.client(oci.secrets.SecretsClient)
    deadline = time.time() + wait_seconds
    raw = "pending"
    while time.time() < deadline:
        b = sc.get_secret_bundle(k["superuser_secret_id"], stage="LATEST").data
        raw = base64.b64decode(b.secret_bundle_content.content).decode("utf-8", "replace").strip()
        if raw and raw != "pending":
            break
        time.sleep(10)
    if not raw or raw == "pending":
        raise RuntimeError("secret still holds the placeholder")
    username, password = "superuser", raw
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            username = j.get("username") or j.get("user") or username
            password = j.get("password") or password
    except ValueError:
        pass
    k["username"], k["password"] = username, password
    boot = k.get("public_bootstrap") or k.get("bootstrap_servers") or ""
    k["client_properties"] = (
        f"bootstrap.servers={boot}\n"
        "security.protocol=SASL_SSL\n"
        "sasl.mechanism=SCRAM-SHA-512\n"
        "sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required "
        f'username="{username}" password="{password}";')
    print("kafka: superuser credentials read from the vault", flush=True)


def adb_dsn(connect: str) -> str:
    """Autonomous Database only accepts TLS.

    connect_string is "host:port/service". Handed to oracledb as-is it opens a
    plain TCP connection, which the database resets (DPY-6005 / DPY-4011), so
    every seed, Select AI and REST step failed on paid, private-endpoint
    databases. Build an explicit TCPS descriptor - the same one the starter
    apps use. No wallet: these databases are created with mTLS off.
    """
    if connect.lstrip().startswith("("):
        return connect
    host_port, service = connect.split("/", 1)
    host, port = host_port.split(":")
    return (f"(description=(retry_count=5)(retry_delay=3)"
            f"(address=(protocol=tcps)(port={port})(host={host}))"
            f"(connect_data=(service_name={service}))"
            f"(security=(ssl_server_dn_match=yes)))")

def claim(conn):
    cur = conn.cursor()
    # The OCI-hosted worker cannot see a laptop's folders: it only takes requests whose
    # source is a Git URL or an image. A laptop worker (SBX_WORKER_KIND=laptop) takes everything.
    # Take the oldest queued request this worker is allowed to run. Several workers
    # run at once, so walk a window of candidates rather than fighting over min(id):
    # with a single candidate every extra worker would lose the skip-locked race and
    # idle, and the queue would drain one request at a time no matter how many run.
    if os.environ.get("SBX_WORKER_KIND", "oci" if sf.in_oci() else "laptop") == "oci":
        cur.execute("""select id from sbx.sandbox_requests where status = 'QUEUED'
                       and (git_url is null or lower(git_url) like 'http%' or lower(git_url) like 'git%' or lower(git_url) like 'ssh%')
                       order by id fetch first 25 rows only""")
    else:
        cur.execute("select id from sbx.sandbox_requests where status = 'QUEUED' order by id fetch first 25 rows only")
    candidates = [r[0] for r in cur.fetchall()]
    row = None
    for candidate in candidates:
        cur.execute("select id from sbx.sandbox_requests where id = :1 and status = 'QUEUED' for update skip locked", [candidate])
        row = cur.fetchone()
        if row:
            break
        conn.rollback()
    if not row:
        return None
    cur.execute("update sbx.sandbox_requests set status = 'RUNNING', started_at = systimestamp, worker_name = :w where id = :id",
                id=row[0], w=WORKER_NAME)
    conn.commit()
    cur.execute("""
        select id, requester, sandbox_id, action, ttl_days, enable_adb, adb_tier, enable_kafka,
               kafka_mode, enable_app, app_image, git_url, app_port, request_text, seed_sql, app_containers, app_files,
               seed_key, app_template, enable_nosql, adb_databases, functions, app_instances,
               buckets, queues, dataflow_jobs, enable_catalog, catalog_assets, enable_aidp, user_env, enable_rag
        from sbx.sandbox_requests where id = :1""", [row[0]])
    cols = [d[0].lower() for d in cur.description]
    row = dict(zip(cols, cur.fetchone()))
    for k in ("seed_sql", "app_containers", "app_files", "adb_databases",
              "functions", "app_instances", "buckets", "queues", "dataflow_jobs", "catalog_assets", "request_text", "user_env"):
        if hasattr(row.get(k), "read"):
            row[k] = row[k].read()
    return row


def finish(conn, request_id, ok: bool, outputs=None, error=None):
    cur = conn.cursor()
    cur.execute("""
        update sbx.sandbox_requests
        set status = :st, finished_at = systimestamp, outputs = :out, error = :err
        where id = :id""",
        st="DONE" if ok else "FAILED",
        out=json.dumps(outputs, indent=1) if outputs is not None else None,
        err=(error or "")[:4000] or None,
        id=request_id)
    conn.commit()


def rag_buckets(req: dict) -> list | None:
    """With RAG, documents live in a docs bucket of the sandbox's own; the
    database watches it. Added here, before the arguments are built."""
    buckets = json.loads(req["buckets"]) if req.get("buckets") else []
    if req.get("enable_rag") == "Y" and not any(b.get("name") == "docs" for b in buckets):
        buckets = buckets + [{"name": "docs", "public": False}]
    return buckets or None


EDITION = os.environ.get("SBX_EDITION", "standard")
# Free Tier edition on the Arm VM: applications run on the worker VM itself (host_apps.py)
FREE_APPS = EDITION == "free" and os.environ.get("SBX_FREE_APPS") == "1"


def wants_app(req: dict) -> bool:
    return req.get("enable_app") == "Y" or any(req.get(k) for k in ("git_url", "app_image", "app_files", "app_containers", "app_template"))


def free_injected_env(req: dict, outputs: dict, expires: str) -> dict:
    """What the sandbox stack injects into every container, rebuilt from the outputs (same names)."""
    fnd = sf.foundation(); cfg = sf.config()
    env = {"SANDBOX_ID": req["sandbox_id"], "SANDBOX_EXPIRES": expires,
           "SANDBOX_COMPARTMENT_OCID": fnd["compartments"]["sandboxes"], "OCI_REGION": cfg["region"]}
    adb = outputs.get("adb") or {}
    if adb.get("connect_string"):
        env.update({"ADB_DB_NAME": adb.get("db_name", ""), "ADB_CONNECT_STRING": adb["connect_string"], "ADB_ADMIN_PASSWORD": adb.get("admin_password", "")})
    buckets = outputs.get("buckets") or []
    if buckets:
        env["OBJECT_NAMESPACE"] = buckets[0].get("namespace", ""); env["DATA_BUCKET"] = buckets[0]["name"]
        for b in buckets:
            env["BUCKET_" + b["name"].replace("-", "_").upper()] = b["name"]
    nosql = outputs.get("nosql") or {}
    if nosql.get("tables"):
        env.update({"NOSQL_COMPARTMENT_OCID": nosql.get("compartment_id", ""), "NOSQL_TABLES": ",".join(nosql["tables"]), "NOSQL_REGION": cfg["region"]})
    if req.get("user_env"):
        env.update({str(k): str(v) for k, v in (json.loads(req["user_env"]) or {}).items()})
    return env


def free_app_deploy(req: dict, args) -> dict:
    """Free Tier: the non-app pieces through Resource Manager as usual, the containers on this VM."""
    import host_apps
    if not host_apps.available():
        raise RuntimeError("the worker VM's podman socket is not reachable; applications cannot run on this Free Tier install (reinstall, or pick the Arm VM)")
    containers = containers_for(req) or [{"name": "web", "image": req.get("app_image") or "", "port": int(req.get("app_port") or 80), "env": {}}]
    src = None
    built = materialise_app(req)                          # app_files / a template -> a folder with a Dockerfile
    if built:
        src = built
    elif req.get("git_url") and str(req["git_url"]).lower().startswith(("http://", "https://", "git@", "ssh://", "git://")):
        import oci_build
        repo, sub = oci_build.split_tree_url(req["git_url"].strip())
        repo, _, ref = repo.partition("#")
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sbx-src-"))
        subprocess.run(["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [repo, str(tmp)], check=True)
        src = tmp / sub if sub else tmp
    try:
        if src is not None:
            if not (pathlib.Path(src) / "Dockerfile").exists():
                raise ValueError(f"no Dockerfile in {src}")
            tag = f"localhost/sbx-{req['sandbox_id']}-{containers[0]['name']}:latest"
            print(f"building {tag} on the worker VM from {src}", flush=True)
            host_apps.build(str(src), tag)
            containers[0] = {**containers[0], "image": tag}
        elif not containers[0].get("image"):
            raise ValueError("an application needs an image, a Git repository or files with a Dockerfile")
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.ttl)).strftime("%Y-%m-%dT%H:%MZ")
        others = args.adb or args.nosql or args.buckets or getattr(args, "enable_rag", False)
        if others:
            args.app = False
            outputs = sf.cmd_create(args)
        else:
            outputs = {"sandbox": {"id": req["sandbox_id"], "owner": req["requester"], "expires": expires, "ttl_days": args.ttl}}
        env = free_injected_env(req, outputs, expires)
        outputs["app"] = host_apps.deploy(req["sandbox_id"], containers, env, req["requester"], expires)
        return outputs
    finally:
        if built:
            shutil.rmtree(built, ignore_errors=True)


# What an Oracle Cloud Free Tier account cannot have (not in Always Free). A
# request for any of it is declined up front with the free alternative, instead
# of failing minutes later inside Resource Manager with a service error.
FREE_CAN = ("an Always Free Autonomous Database (23ai, with REST and in-database document search), "
            "NoSQL tables and Object Storage buckets")


def free_edition_check(req: dict) -> None:
    """Free Tier edition: decline what Always Free does not include, and keep the database free."""
    if EDITION != "free":
        return
    wanted = []
    if req.get("enable_kafka") == "Y":
        wanted.append("Kafka")
    if FREE_APPS:
        if req.get("app_instances"):
            wanted.append("extra container instances (on Free Tier every container of a sandbox runs on the worker VM)")
    elif any(req.get(k) for k in ("git_url", "app_image", "app_files", "app_containers", "app_instances", "app_template")) or req.get("enable_app") == "Y":
        wanted.append("containerised apps (Container Instances)")
    if req.get("functions"):
        wanted.append("Functions")
    if req.get("dataflow_jobs"):
        wanted.append("Data Flow (Spark, Iceberg)")
    if req.get("queues"):
        wanted.append("Queue")
    if req.get("enable_catalog") == "Y":
        wanted.append("Data Catalog")
    if req.get("enable_aidp") == "Y":
        wanted.append("AI Data Platform")
    if req.get("adb_databases"):
        wanted.append("extra databases (Always Free allows two per tenancy, and the factory uses one)")
    if wanted:
        raise ValueError("Not available on an Oracle Cloud Free Tier account: " + ", ".join(wanted)
                         + ". This Free Tier install can build " + FREE_CAN
                         + ". Upgrade the tenancy to Pay As You Go and reinstall the standard edition for everything else.")
    if req.get("adb_tier") == "paid":
        req["adb_tier"] = "free"


def factory_args(req: dict) -> argparse.Namespace:
    return argparse.Namespace(
        sandbox_id=req["sandbox_id"], owner=req["requester"], team="hackathon",
        ttl=int(req["ttl_days"] or 3), allowed_cidr="0.0.0.0/0",
        adb=req["enable_adb"] == "Y" or req.get("enable_rag") == "Y", adb_tier=req["adb_tier"] or "paid", adb_workload="OLTP",
        kafka=req["enable_kafka"] == "Y", nosql=req.get("enable_nosql") == "Y", kafka_mode=req["kafka_mode"] or "cluster", topics="events",
        app=req["enable_app"] == "Y", image=req["app_image"] or None,
        shape="CI.Standard.A1.Flex", port=int(req["app_port"] or 80),
        name="app", tag=None, env=None, keep_stack=False, dry_run=False, path=None,
        adb_databases=json.loads(req["adb_databases"]) if req.get("adb_databases") else None,
        functions=json.loads(req["functions"]) if req.get("functions") else None,
        app_instances=json.loads(req["app_instances"]) if req.get("app_instances") else None,
        buckets=rag_buckets(req),
        queues=json.loads(req["queues"]) if req.get("queues") else None,
        dataflow_jobs=json.loads(req["dataflow_jobs"]) if req.get("dataflow_jobs") else None,
        enable_catalog=req.get("enable_catalog") == "Y",
        enable_aidp=req.get("enable_aidp") == "Y",
        catalog_assets=json.loads(req["catalog_assets"]) if req.get("catalog_assets") else None,
        user_env={str(k): str(v) for k, v in (json.loads(req["user_env"]) or {}).items()} if req.get("user_env") else None,
        enable_rag=req.get("enable_rag") == "Y",
    )


def containers_for(req: dict) -> list | None:
    """Explicit container list (planner/chat 'containers') plus SEED_SQL for every container."""
    seed = (req.get("seed_sql") or "").strip()
    env = {"SEED_SQL": base64.b64encode(seed.encode()).decode()} if seed else {}
    if req.get("app_containers"):
        items = json.loads(req["app_containers"])
        out = []
        for i, c in enumerate(items):
            cenv = {**env, **(c.get("env") or {})}
            # A container may ask for a secret to be minted for it, e.g. Airflow's
            # admin password. It is generated here, per sandbox, never taken from
            # the browser, and recorded so the landing page can show it.
            for k, v in list(cenv.items()):
                if v == "{{GENERATE_PASSWORD}}":
                    pw = secrets.token_urlsafe(14)
                    cenv[k] = pw
                    req.setdefault("_logins", []).append(
                        {"service": c.get("name") or f"app{i}", "user": "admin", "password": pw})
            item = {"name": c.get("name") or f"app{i}", "image": c["image"],
                    "port": c.get("port"), "env": cenv}
            # command/args were dropped here before, so anything that needs a
            # start command - Airflow runs a migrate, a user create and two
            # processes - could not be deployed as a plain image.
            if c.get("command"):
                item["command"] = c["command"]
            if c.get("args"):
                item["args"] = c["args"]
            out.append(item)
        return out
    if req["enable_app"] == "Y" and not req.get("git_url") and env:
        if not req["app_image"]:
            raise ValueError(
                "an app was requested with seed data but no image; name a container, "
                "give app_image, or use an app_template")
        return [{"name": "web", "image": req["app_image"], "port": int(req["app_port"] or 80), "env": env}]
    return None


def embed_params() -> str:
    """Parameters DBMS_VECTOR uses to embed text with OCI Generative AI.

    Seeds that build a vector corpus write {{EMBED_PARAMS}} and this fills in the
    region, credential and model, so the SQL itself carries no tenancy.
    """
    region = sf.config()["region"]
    cred = "GENAI_CRED" if os.environ.get("GENAI_USER_OCID") else "OCI$RESOURCE_PRINCIPAL"
    model = os.environ.get("EMBED_MODEL", "cohere.embed-multilingual-v3.0")
    comp = (sf.foundation().get("compartments") or {}).get("sandboxes", "")
    return json.dumps({
        "provider": "OCIGenAI",
        "credential_name": cred,
        **({"compartmentId": comp} if comp else {}),
        "url": f"https://inference.generativeai.{region}.oci.oraclecloud.com/20231130/actions/embedText",
        "model": model,
    })


def seed_database(outputs: dict, seed_sql: str) -> None:
    """Run SEED_SQL against the ADB this sandbox just created in OCI.

    Done here, not in an app container, so every OCI database gets its sample
    schema no matter what runs on top of it (MCP, agent, Grafana, nothing).
    """
    adb = outputs.get("adb") or {}
    connect, pw = adb.get("connect_string"), adb.get("admin_password")
    if not (connect and pw):
        print("seed skipped: no ADB connect string / password in outputs", flush=True)
        return
    import oracledb
    seed_sql = seed_sql.replace("{{EMBED_PARAMS}}", embed_params())
    if len(seed_sql) > 256 * 1024:
        print(f"seed skipped: {len(seed_sql)} bytes exceeds the 256 KB limit", flush=True)
        return
    stmts = [x.strip().rstrip(";") for x in seed_sql.split(";") if x.strip()]
    if not stmts:
        return
    if len(stmts) > 200:
        print(f"seed skipped: {len(stmts)} statements exceeds the 200 limit", flush=True)
        return
    with oracledb.connect(user="ADMIN", password=pw, dsn=adb_dsn(connect),
                          ssl_server_dn_match=True) as db:
        cur = db.cursor()
        done = 0
        for st in stmts:
            try:
                cur.execute(st)
                done += 1
            except Exception as e:  # noqa: BLE001 - one bad statement must not lose the rest
                print(f"seed statement failed ({type(e).__name__}): {st[:120]}", flush=True)
        db.commit()
    print(f"seeded {done}/{len(stmts)} statements into {adb.get('db_name')}", flush=True)


# Who this worker is, for the claim it writes on a request. A replaced worker
# comes back under the same name, so at start it can hand back the requests
# its predecessor was holding (see recover_own_claims).
WORKER_NAME = os.environ.get("WORKER_NAME") or socket.gethostname()

# A model the region actually serves on demand: llama-3.3-70b is listed but
# 404s in us-phoenix-1 (as do llama-4, command-a and grok-4); Gemini 2.5 Flash
# and grok-4.6 answer. Override per tenancy with SELECT_AI_MODEL.
SELECT_AI_MODEL = os.environ.get("SELECT_AI_MODEL", "")


def select_ai_model() -> str:
    """SELECT_AI_MODEL, else the model the tenancy profile found answering here."""
    if SELECT_AI_MODEL:
        return SELECT_AI_MODEL
    try:
        cur = controldb.connect().cursor()
        cur.execute(f"select value from {controldb.SCHEMA}.factory_config where key = 'genai_model'")
        row = cur.fetchone()
        return (row and row[0]) or "google.gemini-2.5-flash"
    except Exception:  # noqa: BLE001
        return "google.gemini-2.5-flash"


def _dedicated_genai_credential() -> tuple[str, dict] | None:
    """A DEDICATED, inference-only OCI key, if one is configured.

    Never the worker's own key: the sandbox ADMIN password is handed to the
    requester, so anything stored as a DBMS_CLOUD credential inside their
    database is effectively theirs. Only a principal whose policy is limited to
    `use generative-ai-family` may go in here.
    """
    user = os.environ.get("GENAI_USER_OCID")
    tenancy = os.environ.get("GENAI_TENANCY_OCID")
    fp = os.environ.get("GENAI_FINGERPRINT")
    key = os.environ.get("GENAI_KEY_FILE")
    if not (user and tenancy and fp and key):
        return None
    body = "".join(l for l in pathlib.Path(key).expanduser().read_text().splitlines()
                   if not l.startswith("-----"))
    return ("""
        begin
          begin dbms_cloud.drop_credential('GENAI_CRED'); exception when others then null; end;
          dbms_cloud.create_credential(
            credential_name => 'GENAI_CRED',
            user_ocid       => :user_ocid,
            tenancy_ocid    => :tenancy_ocid,
            private_key     => :private_key,
            fingerprint     => :fingerprint);
        end;
    """, {"user_ocid": user, "tenancy_ocid": tenancy,
          "private_key": body, "fingerprint": fp})


def enable_select_ai(outputs: dict, region: str, cfg: dict) -> None:
    """Turn on Oracle's own NL2SQL (Select AI) on the ADB this sandbox created.

    Gives every OCI database a `SELECT AI <question>` agent with no container:
    DBMS_CLOUD_AI drives OCI Generative AI and reads the live data dictionary,
    so the agent stays correct as the schema changes. Best effort - a sandbox is
    never failed because Select AI could not be configured.
    """
    adb = outputs.get("adb") or {}
    connect, pw = adb.get("connect_string"), adb.get("admin_password")
    if not (connect and pw):
        return
    import oracledb
    try:
        with oracledb.connect(user="ADMIN", password=pw, dsn=adb_dsn(connect),
                              ssl_server_dn_match=True) as db:
            cur = db.cursor()
            # Resource principal first: the database authenticates as itself and no
            # key material of any kind is stored in the sandbox.
            cred = "OCI$RESOURCE_PRINCIPAL"
            try:
                cur.execute("begin dbms_cloud_admin.enable_resource_principal(); end;")
            except Exception as e:  # noqa: BLE001
                dedicated = _dedicated_genai_credential()
                if not dedicated:
                    print(f"Select AI skipped: resource principal unavailable ({type(e).__name__}) and no "
                          f"dedicated GENAI_* credential configured. Refusing to store the worker's own "
                          f"OCI key in a sandbox database.", flush=True)
                    return
                sql, binds = dedicated
                cur.execute(sql, binds)
                cred = "GENAI_CRED"
            # Every non-Oracle schema in this database, so the agent covers whatever
            # the user creates - not just what the factory seeded into ADMIN.
            cur.execute("""
                select username from all_users
                 where oracle_maintained = 'N'
                    or username = 'ADMIN'
                 order by decode(username, 'ADMIN', 0, 1), username
            """)
            owners = [r[0] for r in cur.fetchall()] or ["ADMIN"]
            object_list = [{"owner": o} for o in owners]
            # With an explicit https endpoint the database opens the connection
            # itself, which a new database refuses (ORA-24247) until the host
            # has an ACE for the user asking.
            cur.execute("""
                begin
                  dbms_network_acl_admin.append_host_ace(
                    host => :h,
                    ace  => xs$ace_type(privilege_list => xs$name_list('http', 'connect', 'resolve'),
                                        principal_name => 'ADMIN', principal_type => xs_acl.ptype_db));
                end;
            """, h=f"inference.generativeai.{region}.oci.oraclecloud.com")
            cur.execute("""
                begin
                  begin dbms_cloud_ai.drop_profile('SANDBOX_AI'); exception when others then null; end;
                  dbms_cloud_ai.create_profile(
                    profile_name => 'SANDBOX_AI',
                    attributes   => :attrs);
                  dbms_cloud_ai.set_profile('SANDBOX_AI');
                end;
            """, attrs=json.dumps({
                "provider": "oci",
                "credential_name": cred,
                "region": region,
                "model": select_ai_model(),
                "comments": "true",
                # Spell the endpoint out, WITH the scheme: left to itself the
                # database builds ...oci.my$cloud_domain (ORA-20404), and a bare
                # host is taken for an object-store URI (ORA-20006). Proven on
                # both 19c and 23ai: only https://<host> reaches the service.
                "provider_endpoint": f"https://inference.generativeai.{region}.oci.oraclecloud.com",
                "oci_compartment_id": (outputs.get("sandbox") or {}).get("compartment_id") or sf.foundation()["compartments"]["sandboxes"],
                "oci_apiformat": "GENERIC",
                "object_list": object_list,
            }))
            # Persist the profile so every new session can just say SELECT AI ...
            cur.execute("""
                begin
                  execute immediate q'[
                    create or replace trigger admin.sandbox_ai_logon
                    after logon on database
                    begin
                      dbms_cloud_ai.set_profile('SANDBOX_AI');
                    exception when others then null;
                    end;]';
                exception when others then null;
                end;
            """)
            # Oracle's own AI cataloguing: GENERATE_SYNONYMS writes natural-language
            # aliases for tables and columns into the dictionary, which is what the
            # schemagate catalogue did by hand. Only on ADB versions that ship it.
            catalogued = False
            try:
                cur.execute("""
                    begin
                      dbms_cloud_ai.generate_synonyms(
                        profile_name => 'SANDBOX_AI',
                        object_list  => :objs);
                    end;
                """, objs=json.dumps(object_list))
                catalogued = True
            except Exception as e:  # noqa: BLE001 - not on every ADB version yet
                print(f"AI cataloguing not available here ({type(e).__name__}); Select AI falls back to comments", flush=True)
            # Schemas created after provisioning: refresh the profile and the
            # catalogue nightly so a database that grows later stays covered.
            try:
                cur.execute("""
                    begin
                      begin dbms_scheduler.drop_job('ADMIN.SANDBOX_AI_REFRESH', true); exception when others then null; end;
                      dbms_scheduler.create_job(
                        job_name   => 'ADMIN.SANDBOX_AI_REFRESH',
                        job_type   => 'PLSQL_BLOCK',
                        start_date => systimestamp + interval '1' hour,
                        repeat_interval => 'FREQ=HOURLY;INTERVAL=6',
                        enabled    => true,
                        job_action => q'[
                          declare
                            l_objs clob := '[';
                          begin
                            for u in (select username from all_users
                                       where oracle_maintained = 'N' or username = 'ADMIN') loop
                              l_objs := l_objs || case when length(l_objs) > 1 then ',' end
                                        || '{\"owner\":\"' || u.username || '\"}';
                            end loop;
                            l_objs := l_objs || ']';
                            dbms_cloud_ai.set_attribute('SANDBOX_AI', 'object_list', l_objs);
                            begin dbms_cloud_ai.generate_synonyms(profile_name => 'SANDBOX_AI', object_list => l_objs);
                            exception when others then null; end;
                          end;]');
                    end;
                """)
            except Exception as e:  # noqa: BLE001
                print(f"Select AI refresh job not scheduled ({type(e).__name__})", flush=True)
            db.commit()
        print(f"Select AI enabled on {adb.get('db_name')} over {len(owners)} schema(s) (model {select_ai_model()}"
              f"{', AI catalogue generated' if catalogued else ''}) - try: select ai what are my top customers", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Select AI setup skipped ({type(e).__name__}: {e})", flush=True)


def enable_low_code(outputs: dict) -> None:
    """Turn the new database into something you can build an app on immediately.

    Two things every Autonomous Database already ships, switched on for you:
      * ORDS auto-REST - every seeded table gets a working REST endpoint, no code.
      * an APEX workspace on the ADMIN schema - open the APEX URL and click
        Create App to get a low-code CRUD app over the same tables.
    Best effort: a sandbox is never failed because these could not be enabled.
    """
    adb = outputs.get("adb") or {}
    connect, pw = adb.get("connect_string"), adb.get("admin_password")
    if not (connect and pw):
        return
    import oracledb
    try:
        with oracledb.connect(user="ADMIN", password=pw, dsn=adb_dsn(connect),
                              ssl_server_dn_match=True) as db:
            cur = db.cursor()
            cur.execute("""
                begin
                  ords_admin.enable_schema(
                    p_enabled             => true,
                    p_schema              => 'ADMIN',
                    p_url_mapping_type    => 'BASE_PATH',
                    p_url_mapping_pattern => 'admin',
                    p_auto_rest_auth      => true);
                end;
            """)
            cur.execute("""
                select table_name from user_tables
                 where table_name not like 'DEF$%' and table_name not like 'SYS%'
                   and table_name not like 'AQ$%' and table_name not like 'MVIEW$%'
                   and table_name not like 'LOGMNR%' and table_name not like 'SCHEDULER%'
            """)
            tables = [r[0] for r in cur.fetchall()]
            # Tables that appear later (a pipeline's gold tables, anything the
            # user creates) are published by the database itself, every 2 min.
            try:
                cur.execute("""
                    begin
                      begin dbms_scheduler.drop_job('ADMIN.SANDBOX_REST_NEW', true); exception when others then null; end;
                      dbms_scheduler.create_job(
                        job_name        => 'ADMIN.SANDBOX_REST_NEW',
                        job_type        => 'PLSQL_BLOCK',
                        job_action      => q'[begin
                          for t in (select table_name from user_tables
                                     where table_name not like 'DEF$%' and table_name not like 'SYS%'
                                       and table_name not like 'AQ$%' and table_name not like 'MVIEW$%'
                                       and table_name not like 'LOGMNR%' and table_name not like 'SCHEDULER%'
                                       and table_name not in (select parsing_object from user_ords_enabled_objects)) loop
                            begin
                              ords.enable_object(p_enabled => true, p_schema => 'ADMIN', p_object => t.table_name,
                                                 p_object_type => 'TABLE', p_object_alias => lower(t.table_name),
                                                 p_auto_rest_auth => true);
                            exception when others then null;
                            end;
                          end loop;
                          commit;
                        end;]',
                        repeat_interval => 'FREQ=MINUTELY;INTERVAL=2',
                        enabled         => true,
                        comments        => 'Sandbox Factory: publish new ADMIN tables through ORDS');
                    end;
                """)
            except Exception as e:  # noqa: BLE001
                print(f"auto-publish job not created ({type(e).__name__}: {e})", flush=True)
            rested = 0
            for t in tables:
                try:
                    cur.execute("""
                        begin
                          ords.enable_object(
                            p_enabled        => true,
                            p_schema         => 'ADMIN',
                            p_object         => :t,
                            p_object_type    => 'TABLE',
                            p_object_alias   => lower(:t),
                            p_auto_rest_auth => true);
                        end;
                    """, t=t)
                    rested += 1
                except Exception:  # noqa: BLE001 - skip what will not REST
                    pass
            db.commit()
        if True:   # the schema is published even with no tables yet (a pipeline adds them)
            base = (adb.get("sql_web_url") or "").split("/ords/")[0]
            # Only promise a REST endpoint that answers. ORDS takes a moment to
            # publish a mapping, and some databases refuse to REST-enable ADMIN.
            ok = False
            if base:
                import requests
                for _ in range(4):
                    try:
                        r = requests.get(f"{base}/ords/admin/metadata-catalog/", auth=("ADMIN", pw), timeout=30)
                        ok = r.status_code == 200
                    except Exception:  # noqa: BLE001
                        ok = False
                    if ok:
                        break
                    time.sleep(20)
            if ok:
                outputs.setdefault("low_code", {})["rest_base"] = f"{base}/ords/admin/"
                outputs["low_code"]["rest_tables"] = sorted(t.lower() for t in tables)
            else:
                outputs.setdefault("warnings", []).append(
                    "REST endpoints for the ADMIN tables did not come up on this database; use SQL Developer Web or APEX instead.")
                rested = 0
        print(f"ORDS auto-REST on for {rested}/{len(tables)} table(s), authenticated (basic auth as ADMIN); "
              f"APEX is ready at the APEX URL", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Low-code setup skipped ({type(e).__name__}: {e})", flush=True)


def harvest_catalog(outputs: dict) -> None:
    """Ask the Data Catalog to harvest every bucket the stack registered.

    Registration is Terraform (the asset and its resource-principal connection);
    a harvest is a job run, which Terraform cannot express, so it is started
    here: one HARVEST job definition and one job per asset, run once now. The
    outcome is recorded on the card. A filename pattern is attached to the asset
    first: without one Oracle's harvester dies with a null pointer (DCAT-20001);
    with one it runs clean. Best effort: a sandbox is never failed for the catalog.
    """
    cat = outputs.get("catalog") or {}
    assets = [a for a in (cat.get("assets") or []) if a.get("key")]
    if not cat.get("id") or not assets:
        return
    import time as _t
    dc = sf.client(oci.data_catalog.DataCatalogClient)
    M = oci.data_catalog.models
    cfg = sf.config()
    compartment = (outputs.get("sandbox") or {}).get("compartment_id") or sf.foundation()["compartments"]["sandboxes"]
    result = {}
    for a in assets:
        name = a["name"]
        try:
            # the connection: the "Resource Principal" type whose parent is this asset's type
            conns = [c for c in dc.list_connections(cat["id"], a["key"]).data.items if c.lifecycle_state != "DELETED"]
            if conns:
                conn_key = conns[0].key
            else:
                rp = next(t for t in oci.pagination.list_call_get_all_results(dc.list_types, cat["id"], type_category="connection").data
                          if t.name == "Resource Principal" and dc.get_type(cat["id"], t.key).data.parent_type_key == a.get("type_key"))
                conn_key = dc.create_connection(cat["id"], a["key"], M.CreateConnectionDetails(
                    display_name=f"{name}-resource-principal", type_key=rp.key, is_default=True,
                    properties={"default": {"ociRegion": cfg["region"], "ociCompartment": compartment}})).data.key
            a["connection_key"] = conn_key
            # the pattern: first folder of the bucket = one logical entity (raw/, gold/, iceberg/ ...)
            pats = [p for p in dc.list_patterns(cat["id"], display_name=f"{name}-pattern").data.items
                    if p.lifecycle_state != "DELETED"]
            pat = pats[0] if pats else dc.create_pattern(cat["id"], M.CreatePatternDetails(
                display_name=f"{name}-pattern", expression="{bucketName:[^/]+}/{logicalEntity:[^/]+}/.*")).data
            try:
                dc.add_data_selector_patterns(cat["id"], a["key"], M.DataSelectorPatternDetails(items=[pat.key]))
            except oci.exceptions.ServiceError as e:  # already attached on a re-apply
                if e.status not in (409, 412):
                    raise
            jds = [j for j in dc.list_job_definitions(cat["id"], display_name=f"{name}-harvest").data.items
                   if j.lifecycle_state != "DELETED"]
            jd = jds[0] if jds else dc.create_job_definition(cat["id"], M.CreateJobDefinitionDetails(
                display_name=f"{name}-harvest", job_type="HARVEST", data_asset_key=a["key"],
                connection_key=a["connection_key"], is_incremental=True)).data
            jobs = [j for j in dc.list_jobs(cat["id"], display_name=f"{name}-harvest-job").data.items
                    if j.lifecycle_state != "DELETED"]
            job = jobs[0] if jobs else dc.create_job(cat["id"], M.CreateJobDetails(
                display_name=f"{name}-harvest-job", job_definition_key=jd.key)).data
            ex = dc.create_job_execution(cat["id"], job.key, M.CreateJobExecutionDetails()).data
            state = ex.lifecycle_state
            for _ in range(12):
                _t.sleep(10)
                e = dc.get_job_execution(cat["id"], job.key, ex.key).data
                state = e.lifecycle_state
                if state not in ("CREATED", "IN_PROGRESS", "INACTIVE"):
                    break
            err = str(getattr(e, "error_message", "") or "") if state == "FAILED" else ""
            result[name] = {"state": state, "job_key": job.key, "error": err[:200]}
            print(f"catalog: harvest of {name} {state}{(' - ' + err[:120]) if err else ''}", flush=True)
        except Exception as e:  # noqa: BLE001
            result[name] = {"state": "NOT_STARTED", "error": f"{type(e).__name__}: {str(e)[:160]}"}
            print(f"catalog: harvest of {name} not started ({type(e).__name__}: {str(e)[:120]})", flush=True)
    cat["harvest"] = result


def materialise_app(req: dict) -> pathlib.Path | None:
    """app_files is {filename: content} - a small app shipped with the request.

    Written to a temp folder and built exactly like a local folder deploy, so a
    Python starter needs no repository and nothing pushed to a registry.
    """
    raw = req.get("app_files")
    if not raw:
        return None
    files = json.loads(raw)
    if not files:
        return None
    if len(files) > 40:
        raise ValueError(f"app_files has {len(files)} entries; 40 is the limit")
    total = sum(len(v) for v in files.values())
    if total > 512 * 1024:
        raise ValueError(f"app_files is {total} bytes; 512 KB is the limit")
    root = pathlib.Path(tempfile.mkdtemp(prefix="sbx-app-"))
    for name, body in files.items():
        # keep everything inside the temp root
        dest = (root / name).resolve()
        if not str(dest).startswith(str(root.resolve())):
            raise ValueError(f"app_files entry escapes the build folder: {name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    if not (root / "Dockerfile").exists():
        raise ValueError("app_files needs a Dockerfile")
    print(f"app_files: {len(files)} file(s) written to {root}", flush=True)
    return root


def expand_templates(req: dict) -> None:
    """Turn the short keys the browser sends into the real seed SQL and app files.

    The page ships only a key ("react", "sales"): an APEX region source is capped
    at 32767 bytes and the full templates do not fit. Free-text seed_sql/app_files
    from chat still pass straight through and win over a key.
    """
    if not req.get("seed_sql"):
        seed = app_templates.seed_for(req.get("seed_key"))
        if seed:
            req["seed_sql"] = seed
            print(f"seed template '{req['seed_key']}' expanded ({len(seed)} chars)", flush=True)
    if not req.get("app_files"):
        app = app_templates.app_for(req.get("app_template"))
        if app:
            req["app_files"] = json.dumps(app)
            print(f"app template '{req['app_template']}' expanded ({len(app)} files)", flush=True)


def recorded_namespace(conn, sandbox_id: str) -> str | None:
    """The Object Storage namespace this sandbox recorded when it was created."""
    try:
        cur = conn.cursor()
        cur.execute("""select outputs from sbx.sandbox_requests
                        where sandbox_id = :s and status = 'DONE' and outputs is not null
                        order by id desc fetch first 3 rows only""", s=sandbox_id)
        for (out,) in cur.fetchall():
            data = json.loads(out.read() if hasattr(out, "read") else out)
            for b in (data.get("buckets") or []):
                if b.get("namespace"):
                    return b["namespace"]
    except Exception:  # noqa: BLE001 - cleanup must never block a destroy
        pass
    return None


def handle(req: dict, conn=None) -> dict:
    expand_templates(req)
    free_edition_check(req)
    args = factory_args(req)
    if req["action"] == "DESTROY" and conn is not None:
        args.os_namespace = recorded_namespace(conn, req["sandbox_id"])
    if req["action"] == "DESTROY":
        sf.cmd_destroy(args)
        if FREE_APPS:
            import host_apps
            try:
                if host_apps.remove(req["sandbox_id"]):
                    print(f"removed {req['sandbox_id']}'s containers from the worker VM", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"host containers not removed ({type(e).__name__}: {e})", flush=True)
        return {"destroyed": req["sandbox_id"]}
    if FREE_APPS and wants_app(req):
        return free_app_deploy(req, args)
    built = materialise_app(req)
    if built:
        try:
            args.path = str(built)
            args.app = True
            # app_files + containers: the files are built into an image that
            # runs as the first container, with that container's command, env
            # and generated secrets (Airflow with the user's DAGs, for one).
            return sf.cmd_deploy(args, app_containers=containers_for(req) if req.get("app_containers") else None)
        finally:
            shutil.rmtree(built, ignore_errors=True)
    if (req.get("functions") or req.get("app_instances") or req.get("buckets")
            or req.get("queues") or req.get("dataflow_jobs")
            or req.get("enable_catalog") == "Y" or req.get("enable_aidp") == "Y") and not req.get("app_files")             and not req.get("git_url") and req["enable_app"] != "Y":
        # Functions and extra instances are stack variables, not the primary app,
        # so a sandbox made only of them still goes through the plain create path.
        return sf.cmd_create(args)
    explicit = containers_for(req)
    if explicit and not req.get("git_url"):
        args.app = True
        return sf.cmd_create(args, app_containers=explicit)
    if req["action"] == "DEPLOY":
        if not req["git_url"]:
            raise ValueError("DEPLOY needs git_url")
        src = req["git_url"].strip()
        if src.lower().startswith(("http://", "https://", "git@", "ssh://", "git://")):
            # Git URL: sandbox_factory builds it (kaniko inside OCI, or local docker on a laptop)
            args.path = src
            args.app = True
            return sf.cmd_deploy(args)
        if not src.lower().startswith(("http://", "https://", "git@", "ssh://")):
            # a folder on the worker machine (the laptop running this worker)
            local = pathlib.Path(src).expanduser()
            if not (local / "Dockerfile").exists():
                raise ValueError(f"no Dockerfile in local folder {local}")
            print(f"building local folder {local}")
            args.path = str(local)
            args.app = True
            return sf.cmd_deploy(args)
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sbx-deploy-"))
        try:
            print(f"cloning {src}")
            subprocess.run(["git", "clone", "--depth", "1", src, str(tmp / "src")], check=True)
            args.path = str(tmp / "src")
            args.app = True
            return sf.cmd_deploy(args)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return sf.cmd_create(args)


def process_one(conn) -> bool:
    req = claim(conn)
    if not req:
        return False
    print(f"[{dt.datetime.now():%H:%M:%S}] request {req['id']}: {req['action']} {req['sandbox_id']} for {req['requester']}")
    log = RowLog(conn, req["id"])
    try:
        with contextlib.redirect_stdout(log):
            outputs = handle(req, conn)
            if req["action"] != "DESTROY" and isinstance(outputs, dict) and outputs.get("catalog"):
                # every sandbox with a catalog, database or not: register, connect, harvest
                try:
                    harvest_catalog(outputs)
                except Exception as e:  # noqa: BLE001  the catalog is a convenience on top, never a reason to fail the build
                    print(f"catalog: harvest step skipped ({type(e).__name__}: {str(e)[:160]})", flush=True)
            if req["action"] != "DESTROY" and isinstance(outputs, dict) and (outputs.get("adb") or {}).get("connect_string"):
                # Select AI first: it creates the credential that a seed needs in
                # order to embed anything with DBMS_VECTOR.
                # The infrastructure is already built and usable at this point.
                # Seeding and Select AI are conveniences on top, and the worker
                # cannot always reach a private endpoint, so a failure here must
                # not mark a working sandbox as failed - record it and move on.
                try:
                    cfg = sf.config()
                    if EDITION == "free":
                        # OCI Generative AI is not part of Always Free: no Select AI, say so on the card
                        outputs.setdefault("warnings", []).append(
                            "Select AI is not available on a Free Tier account (OCI Generative AI is not part of Always Free). "
                            "REST and in-database document search work.")
                    else:
                        enable_select_ai(outputs, cfg["region"], cfg)
                    seed = (req.get("seed_sql") or "").strip()
                    if seed:
                        seed_database(outputs, seed)
                    enable_low_code(outputs)
                    if req.get("enable_rag") == "Y":
                        import oracle_rag
                        docs = next((b for b in (outputs.get("buckets") or []) if str(b.get("name", "")).endswith("-docs")), None)
                        if docs:
                            outputs["rag"] = oracle_rag.enable(outputs, cfg["region"], docs["name"], docs.get("namespace") or "", select_ai_model())
                except Exception as e:  # noqa: BLE001
                    note = f"{type(e).__name__}: {e}"
                    print(f"sandbox is up; database setup did not finish ({note[:200]})", flush=True)
                    outputs.setdefault("warnings", []).append(
                        "The database is up, but its sample data, Select AI and REST endpoints were not set up. "
                        "Ask the factory to \"retry setup\" for this sandbox, or run the SQL yourself in SQL Developer Web. "
                        f"(detail: {note[:160]})")
        if isinstance(outputs, dict) and (outputs.get("kafka") or {}).get("superuser_secret_id"):
            try:
                kafka_credentials(outputs)
            except Exception as e:  # noqa: BLE001
                outputs.setdefault("warnings", []).append(
                    f"Kafka is up, but its superuser password could not be read from the vault ({type(e).__name__}: {e}). "
                    "Open the secret in the console to copy it.")
        if isinstance(outputs, dict) and req.get("_logins"):
            outputs["logins"] = req["_logins"]
        if isinstance(outputs, dict) and sf.NOTES:
            outputs.setdefault("warnings", []).extend(sf.NOTES)
            sf.NOTES.clear()
        finish(conn, req["id"], True, outputs)
        print(f"  request {req['id']} DONE")
    except SystemExit as e:
        log.flush_row()
        finish(conn, req["id"], False, error=str(e))
        print(f"  request {req['id']} FAILED: {e}")
    except Exception as e:  # noqa: BLE001
        if is_capacity_limit(e):
            # Resource Manager runs at most a handful of jobs at once per
            # tenancy and refuses the rest with LimitExceeded. That is not the
            # user's request failing - it is the queue being full. Put it back
            # and let the next free worker take it, instead of telling someone
            # their sandbox failed when it simply had to wait its turn.
            requeue(conn, req["id"])
            print(f"  request {req['id']} waiting: Resource Manager is at capacity, re-queued", flush=True)
            time.sleep(RETRY_BACKOFF_SECONDS)
            return True
        if _refused(e) and wait_for_permissions(after_bootstrap=True):
            # The worker's OWN access was refused (the probe says so; a resource
            # that is really gone would pass the probe and fail below as usual).
            # Not the user's request failing: it goes back to the queue, and the
            # worker has waited until its access works again.
            requeue(conn, req["id"])
            print(f"  request {req['id']} waiting: the worker's access was refused and has recovered; re-queued", flush=True)
            return True
        log.write(traceback.format_exc())
        finish(conn, req["id"], False, error=f"{type(e).__name__}: {e}")
        print(f"  request {req['id']} FAILED: {e}")
    return True


RETRY_BACKOFF_SECONDS = 20


def is_capacity_limit(e: Exception) -> bool:
    """A refusal that will clear on its own once running jobs finish."""
    text = str(e)
    return any(code in text for code in ("LimitExceeded", "TooManyRequests", "429"))


def requeue(conn, request_id: int) -> None:
    cur = conn.cursor()
    cur.execute("""update sbx.sandbox_requests
                      set status = 'QUEUED', started_at = null
                    where id = :i""", i=request_id)
    conn.commit()


def reconcile(conn):
    """Sandboxes destroyed outside the app (CLI, reaper) get a DESTROY/DONE row so the UI stops showing them."""
    fnd = sf.foundation()
    # sf.client() attaches the resource-principal signer. Building the client
    # from config() alone only works with an API key on a laptop; in OCI it
    # raised {'user': 'missing', 'key_file': 'missing', ...} every cycle.
    rm = sf.client(oci.resource_manager.ResourceManagerClient)
    live = {s.freeform_tags.get("sandbox_id") for s in rm.list_stacks(compartment_id=fnd["compartments"]["control"], lifecycle_state="ACTIVE").data
            if s.freeform_tags.get("managed_by") == "sandbox-factory"}
    if FREE_APPS:
        import host_apps
        live |= host_apps.alive_ids()
    cur = conn.cursor()
    cur.execute("""
        select sandbox_id, requester from (
          select sandbox_id, requester, action, status,
                 row_number() over (partition by sandbox_id order by id desc) rn
          from sbx.sandbox_requests)
        where rn = 1 and status = 'DONE' and action in ('CREATE', 'DEPLOY')""")
    for sandbox_id, requester in cur.fetchall():
        if sandbox_id not in live:
            cur.execute("""insert into sbx.sandbox_requests (requester, sandbox_id, action, ttl_days, status, started_at, finished_at, outputs, request_text)
                           values (:1, :2, 'DESTROY', 1, 'DONE', systimestamp, systimestamp, :3, 'destroyed outside the app (reaper or CLI)')""",
                        [requester, sandbox_id, json.dumps({"destroyed": sandbox_id})])
            print(f"reconcile: {sandbox_id} no longer exists; recorded as destroyed")
    conn.commit()


def recover_own_claims(conn) -> None:
    """Re-queue the requests a previous instance of THIS worker was running.

    A roll replaces the container under the same name, and whatever it was
    mid-way through (a Terraform job that Resource Manager finishes on its own)
    would otherwise sit at RUNNING for ever. The new instance knows it holds
    nothing, so anything still claimed under its name goes back to QUEUED and
    is picked up again; a CREATE re-applies the same stack idempotently.
    """
    cur = conn.cursor()
    cur.execute("""update sbx.sandbox_requests set status = 'QUEUED', started_at = null,
                   log = log || chr(10) || 'worker ' || :w || ' was replaced; queued again'
                   where status = 'RUNNING' and worker_name = :w""", w=WORKER_NAME)
    if cur.rowcount:
        print(f"recovered {cur.rowcount} request(s) left RUNNING by the previous {WORKER_NAME}", flush=True)
    conn.commit()


PERMISSION_WAIT_SECONDS = int(os.environ.get("SBX_PERMISSION_WAIT_SECONDS", "600"))


def _refused(e: Exception) -> bool:
    return isinstance(e, oci.exceptions.ServiceError) and e.status in (401, 403, 404) and \
        e.code in ("NotAuthorizedOrNotFound", "NotAuthenticated", "NotAuthorized", "DENIED")


def wait_for_permissions(after_bootstrap: bool = False) -> bool:
    """The worker's identity must be able to use Resource Manager and the registry
    before it takes requests.

    On a fresh install the dynamic group and its policy are created by the same
    apply that launches the workers, a minute or two earlier. A worker whose first
    calls reach IAM before that has propagated was seen (clean install sbx10,
    2026-09-29) to stay refused for good, token refreshes included, until it was
    restarted; every request then failed. So: probe with harmless reads, and while
    refused keep the requests queued, say so on the status page, take fresh
    credentials every 30 s, and after PERMISSION_WAIT_SECONDS exit so the
    container restart policy starts a clean process. Returns True if it had to wait.
    """
    import install_status
    ctrl = sf.foundation()["compartments"]["control"]
    t0, waited = time.time(), False
    while True:
        try:
            sf.client(oci.resource_manager.ResourceManagerClient).list_stacks(compartment_id=ctrl, limit=1)
            sf.client(oci.artifacts.ArtifactsClient).list_container_repositories(compartment_id=ctrl, limit=1)
            if waited:
                print(f"permissions: granted after {int(time.time() - t0)} s", flush=True)
                if after_bootstrap:            # at start-up, bootstrap marks ready when the application is in
                    try:
                        install_status.mark("ready", "sign in at app_url" if os.environ.get("SBX_APP_URL") else "application installed")
                    except Exception:  # noqa: BLE001
                        pass
            return waited
        except Exception as e:  # noqa: BLE001
            if not _refused(e):
                print(f"permissions: probe inconclusive ({type(e).__name__}: {str(e)[:160]}); carrying on", flush=True)
                return waited
            if not waited:
                try:
                    install_status.mark("waiting for permissions",
                                        "OCI is still applying the worker's access (a few minutes on a new install); requests stay queued")
                except Exception:  # noqa: BLE001
                    pass
            waited = True
            if time.time() - t0 > PERMISSION_WAIT_SECONDS:
                print(f"permissions: still refused after {PERMISSION_WAIT_SECONDS} s ({e.code}); exiting so the container restarts with a clean identity", flush=True)
                sys.exit(3)
            print(f"permissions: refused ({e.code} from {getattr(e, 'target_service', '?')}); fresh credentials in 30 s", flush=True)
            time.sleep(30)
            sf._AUTH = None                    # the next client gets a new signer and a new token


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    # The worker's own access first: until OCI has applied it, the status page
    # says so (never "ready") and every request waits in the queue.
    if sf.in_oci():
        wait_for_permissions()
    # A fresh tenancy: create the schema, install the application, load the
    # prompts. Idempotent, so every worker may run it.
    try:
        import bootstrap
        bootstrap.bootstrap()
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap skipped ({type(e).__name__}: {e})", flush=True)
    conn = controldb.connect("ADMIN")
    print(f"worker {WORKER_NAME} connected to control DB; polling sbx.sandbox_requests")
    recover_own_claims(conn)
    last_reap = 0.0
    last_prices = time.time()      # bootstrap has just refreshed them
    while True:
        if time.time() - last_reap > REAP_SECONDS:
            last_reap = time.time()
            try:
                print(f"[{dt.datetime.now():%H:%M:%S}] reaper: checking for expired sandboxes")
                sf.cmd_reap(argparse.Namespace(dry_run=False))
                if FREE_APPS:
                    import host_apps
                    host_apps.reap(time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()))
                reconcile(conn)
                if time.time() - last_prices > 86400:          # Oracle's price list, daily
                    last_prices = time.time()
                    import profile as tenancy_profile
                    tenancy_profile.refresh_prices()
            except Exception as e:  # noqa: BLE001
                print(f"reaper error: {e}", file=sys.stderr)
        try:
            worked = process_one(conn)
        except oracledb.Error as e:
            print(f"db error: {e}; reconnecting", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            conn = controldb.connect("ADMIN")
            worked = False
        if a.once and not worked:
            break
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import oracledb  # noqa: E402  (for the reconnect path)
    main()
