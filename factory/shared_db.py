"""One database per install, a schema per sandbox (v1.2).

A sandbox that asks for a database no longer gets an Autonomous Database of
its own by default: it gets a schema in the install's shared database, with
the same things a private database gave it - its own user and password, SQL
Developer Web, REST for every table, an APEX workspace, Select AI over its own
tables - and none of the cost of an idle database per sandbox.

  Free Tier   the control database the install already has (Always Free, one
              of the two the tenancy allows); nothing new is created.
  standard    a paid, private database created by the foundation stack,
              reached through the foundation's one API Gateway
              (SBX_SHARED_DB_* in the worker environment). It is stopped when
              no sandbox uses it and started again when one does.

A sandbox that needs a database of its own (a RAG starter's 23ai vector store,
or "dedicated database" on the form) still gets one, exactly as before.
"""
import json
import os
import re
import secrets
import string
import time

import oracledb

import controldb

EDITION = os.environ.get("SBX_EDITION", "standard")
PROFILE = "SANDBOX_AI"
START_WAIT_SECONDS = 900


def _password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    core = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits)]
    core += [secrets.choice(alphabet) for _ in range(n - 4)]
    secrets.SystemRandom().shuffle(core)
    return "".join(core) + secrets.choice("#_-")


def info() -> dict | None:
    """The install's shared database, or None when every sandbox gets its own."""
    if os.environ.get("SBX_SHARED_DB_CONNECT"):
        web = os.environ.get("SBX_SHARED_DB_WEB", "").rstrip("/")
        return {"connect_string": os.environ["SBX_SHARED_DB_CONNECT"],
                "admin_password": os.environ.get("SBX_SHARED_DB_PASSWORD", ""),
                "db_name": os.environ.get("SBX_SHARED_DB_NAME", ""),
                "id": os.environ.get("SBX_SHARED_DB_ID", ""),
                "web": web,
                "private": os.environ.get("SBX_SHARED_DB_PRIVATE", "0") == "1",
                "where": "the install's shared database"}
    if EDITION == "free" and os.environ.get("SBX_CONTROL_CONNECT") and os.environ.get("SBX_ADMIN_PASSWORD"):
        # Free Tier: the control database doubles as the shared one (Always Free, public
        # endpoint behind its allow-list and passwords, like every Free Tier database).
        app_url = os.environ.get("SBX_APP_URL", "")
        web = app_url.split("/ords/")[0] if "/ords/" in app_url else ""
        service = os.environ["SBX_CONTROL_CONNECT"].split("/", 1)[-1]
        return {"connect_string": os.environ["SBX_CONTROL_CONNECT"],
                "admin_password": os.environ["SBX_ADMIN_PASSWORD"],
                "db_name": service.split("_")[0].split(".")[0].upper()[-14:] if service else "",
                "id": "",
                "web": web,
                "private": False,
                "where": "the factory's control database (Free Tier: the shared database)"}
    return None


def wanted(req: dict) -> bool:
    """Does this request take a schema in the shared database rather than a database of its own?"""
    if req.get("enable_adb") != "Y" or info() is None:
        return False
    if req.get("adb_dedicated") == "Y" or req.get("enable_rag") == "Y":
        return False               # a database of its own (RAG needs 23ai and its vector store)
    if req.get("adb_databases"):
        return False               # extra databases: the stack builds them all
    if req.get("seed_key") == "rag" or req.get("app_template") == "rag":
        return False               # the Knowledge base starter: its own 23ai database (the page sets no rag flag)
    seed = (req.get("seed_sql") or "").lower()
    if re.search(r"\bvector\b|dbms_vector|onnx", seed):
        return False               # AI Vector Search needs 23ai and a database of its own
    return True


def schema_name(sandbox_id: str) -> str:
    return "SBX_" + re.sub(r"[^A-Z0-9]", "_", sandbox_id.upper())[:60]


def alias(sandbox_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", sandbox_id.lower())[:60]


def _dsn(connect: str) -> str:
    return connect if connect.lstrip().startswith("(") else controldb._dsn(connect)


def _admin(shared: dict) -> oracledb.Connection:
    return oracledb.connect(user="ADMIN", password=shared["admin_password"], dsn=_dsn(shared["connect_string"]))


def _as(adb: dict) -> oracledb.Connection:
    return oracledb.connect(user=adb["admin_user"], password=adb["admin_password"], dsn=_dsn(adb["connect_string"]))


def _try(cur, sql: str, what: str, **binds) -> bool:
    try:
        cur.execute(sql, **binds) if binds else cur.execute(sql)
        return True
    except Exception as e:  # noqa: BLE001
        # the whole message: an ORA-06550 says what is wrong on its second line
        print(f"shared database: {what} not done ({type(e).__name__}: {' '.join(str(e).split())[:300]})", flush=True)
        return False


# ---------------------------------------------------------------- the database itself

def _db_client():
    import sandbox_factory as sf
    import oci
    return sf.client(oci.database.DatabaseClient)


def ensure_started(shared: dict) -> None:
    """The standard edition's shared database is stopped while no sandbox uses it."""
    if not shared.get("id"):
        return
    db = _db_client()
    state = db.get_autonomous_database(shared["id"]).data.lifecycle_state
    if state == "AVAILABLE":
        return
    if state == "STOPPED":
        print("shared database is stopped (no sandbox was using it); starting it", flush=True)
        db.start_autonomous_database(shared["id"])
    waited = 0
    while waited < START_WAIT_SECONDS:
        state = db.get_autonomous_database(shared["id"]).data.lifecycle_state
        if state == "AVAILABLE":
            time.sleep(20)          # the listener answers a moment after the state does
            return
        if state == "STOPPED":
            db.start_autonomous_database(shared["id"])
        time.sleep(15)
        waited += 15
    raise RuntimeError(f"the shared database did not start in {START_WAIT_SECONDS // 60} minutes (state {state})")


def stop_if_idle(shared: dict) -> bool:
    """After the last sandbox schema is gone: stop the database (storage only is billed)."""
    if not shared.get("id"):
        return False
    try:
        with _admin(shared) as db:
            cur = db.cursor()
            cur.execute("select count(*) from dba_users where username like 'SBX\\_%' escape '\\'")
            n = cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        print(f"shared database: could not count sandbox schemas ({type(e).__name__}); left running", flush=True)
        return False
    if n:
        return False
    try:
        c = _db_client()
        if c.get_autonomous_database(shared["id"]).data.lifecycle_state == "AVAILABLE":
            c.stop_autonomous_database(shared["id"])
            print("shared database: no sandbox uses it any more; stopped (storage only)", flush=True)
            return True
    except Exception as e:  # noqa: BLE001
        print(f"shared database: not stopped ({type(e).__name__}: {str(e)[:120]})", flush=True)
    return False


# ---------------------------------------------------------------- the register of schemas

def _registry(cur) -> None:
    """ADMIN.SBX_SCHEMAS: which sandbox owns which schema, and when it expires (the reaper reads it)."""
    cur.execute("select count(*) from dba_tables where owner = 'ADMIN' and table_name = 'SBX_SCHEMAS'")
    if cur.fetchone()[0] == 0:
        cur.execute("""create table admin.sbx_schemas (
                         sandbox_id  varchar2(200) primary key,
                         schema_name varchar2(128) not null,
                         owner       varchar2(200),
                         expires     varchar2(20),
                         created_at  timestamp default systimestamp)""")


def alive_ids() -> set[str]:
    """Sandboxes that hold a schema in the shared database (for reconcile: they are alive)."""
    shared = info()
    if shared is None:
        return set()
    try:
        with _admin(shared) as db:
            cur = db.cursor()
            _registry(cur)
            cur.execute("select sandbox_id from admin.sbx_schemas")
            return {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001  (a stopped database has no schemas in use)
        print(f"shared database: schema register not read ({type(e).__name__}); nothing reconciled", flush=True)
        return None


def reap(now_iso: str) -> list[str]:
    """Drop the schemas of expired sandboxes. Returns the sandbox ids removed."""
    shared = info()
    if shared is None:
        return []
    try:
        with _admin(shared) as db:
            cur = db.cursor()
            _registry(cur)
            cur.execute("select sandbox_id, expires from admin.sbx_schemas where expires is not null and expires < :n", n=now_iso)
            expired = [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        print(f"shared database: reaper could not read the schema register ({type(e).__name__})", flush=True)
        return []
    gone = []
    for sid in expired:
        print(f"{sid} expired (schema in the shared database)", flush=True)
        if destroy(sid):
            gone.append(sid)
    return gone


# The two hosts a schema user needs for Select AI with the resource principal: the model endpoint, and the
# identity endpoint the principal fetches its token from (ADMIN gets the latter from enable_resource_principal;
# a named user does not - ORA-24247 on every Select AI call from a shared schema, sbx14 2026-10-01).
def network_hosts(region: str) -> list[str]:
    return [f"inference.generativeai.{region}.oci.oraclecloud.com", f"auth.{region}.oraclecloud.com"]


def grant_network(cur, user: str, region: str) -> None:
    for host in network_hosts(region):
        _try(cur, """begin dbms_network_acl_admin.append_host_ace(host => :h,
                       ace => xs$ace_type(privilege_list => xs$name_list('http', 'connect', 'resolve'),
                                          principal_name => :p, principal_type => xs_acl.ptype_db)); end;""",
             f"network ACE for {host}", h=host, p=user)


def heal(region: str) -> int:
    """Every schema in the register gets the network grants it needs (schemas made before a fix). Returns the count."""
    shared = info()
    if shared is None:
        return 0
    try:
        with _admin(shared) as db:
            cur = db.cursor()
            _registry(cur)
            cur.execute("select schema_name from admin.sbx_schemas")
            users = [r[0] for r in cur.fetchall()]
            for u in users:
                grant_network(cur, u, region)
                for pkg in ("dbms_cloud_ai_agent", "dbms_cloud_ai"):
                    try:
                        cur.execute(f"grant execute on {pkg} to {u}")
                    except Exception:  # noqa: BLE001  not on every version
                        pass
            db.commit()
        if users:
            print(f"shared database: network grants checked for {len(users)} schema(s)", flush=True)
        return len(users)
    except Exception as e:  # noqa: BLE001
        print(f"shared database: heal skipped ({type(e).__name__}: {str(e)[:120]})", flush=True)
        return 0


def select_ai_profile(cur, user: str, region: str, model: str, compartment_id: str) -> bool:
    """The schema user's own Select AI profile, tested with a one-word call.

    No provider_endpoint: with one, DBMS_CLOUD_AI runs an ACL check that a non-ADMIN user fails
    (ORA-24247 whatever ACEs it holds); without it the call works. Only when the plain profile
    cannot reach the service (ORA-20404 on some databases) is it recreated with the endpoint.
    """
    base = {"provider": "oci", "credential_name": "OCI$RESOURCE_PRINCIPAL", "region": region, "model": model,
            "comments": "true", "oci_compartment_id": compartment_id, "oci_apiformat": "GENERIC",
            "object_list": [{"owner": user}]}
    variants = [("plain", base), ("with endpoint", {**base, "provider_endpoint": f"https://inference.generativeai.{region}.oci.oraclecloud.com"})]
    for label, attrs in variants:
        ok = _try(cur, """begin
                            begin dbms_cloud_ai.drop_profile(:n); exception when others then null; end;
                            dbms_cloud_ai.create_profile(profile_name => :n, attributes => :a);
                            dbms_cloud_ai.set_profile(:n);
                          end;""", f"Select AI profile ({label})", n=PROFILE, a=json.dumps(attrs))
        if not ok:
            continue
        try:
            cur.execute("select substr(dbms_cloud_ai.generate(prompt => 'Reply with the single word OK', profile_name => :n, action => 'chat'), 1, 20) from dual", n=PROFILE)
            answer = (cur.fetchone() or [""])[0] or ""
            # generate() returns a CLOB, and substr of a CLOB is a CLOB: python-oracledb hands
            # back a LOB object, and .strip() on it killed finish() before REST was enabled
            answer = answer.read() if hasattr(answer, "read") else str(answer)
        except Exception as e:  # noqa: BLE001
            print(f"Select AI test ({label}) failed for {user}: {str(e)[:140]}", flush=True)
            continue
        _try(cur, "begin dbms_cloud_ai.generate_synonyms(profile_name => :n, object_list => :o); end;",
             "AI catalogue", n=PROFILE, o=json.dumps([{"owner": user}]))
        print(f"Select AI enabled for {user} (model {model}, profile {label}; test said {answer.strip()[:20]!r})", flush=True)
        return True
    print(f"Select AI not working for {user}: no profile variant reached the service", flush=True)
    return False


def heal_profiles(region: str, model: str, compartment_id: str) -> int:
    """Schemas provisioned before the profile fix: recreate their Select AI profile as themselves (the control
    database holds each sandbox's schema password on its card). Returns how many were checked."""
    if EDITION == "free":
        return 0
    try:
        import controldb as cdb
        with cdb.connect("ADMIN") as con:
            c = con.cursor()
            c.execute("""select sandbox_id, outputs from (select r.*, row_number() over (partition by sandbox_id order by id desc) rn
                           from sbx.sandbox_requests r) where rn = 1 and status = 'DONE' and action <> 'DESTROY'""")
            rows = [(sid, (o.read() if hasattr(o, "read") else o)) for sid, o in c.fetchall()]
    except Exception as e:  # noqa: BLE001
        print(f"shared database: profile heal skipped ({type(e).__name__})", flush=True)
        return 0
    n = 0
    for sid, raw in rows:
        try:
            adb = (json.loads(raw) if raw else {}).get("adb") or {}
            if not adb.get("shared") or not adb.get("admin_password"):
                continue
            with _as(adb) as db:
                cur = db.cursor()
                try:
                    cur.execute("select substr(dbms_cloud_ai.generate(prompt => 'Reply with the single word OK', profile_name => :n, action => 'chat'), 1, 20) from dual", n=PROFILE)
                    cur.fetchone()
                    n += 1
                    continue                           # this schema's Select AI answers: nothing to do
                except Exception:  # noqa: BLE001
                    pass
                if select_ai_profile(cur, adb["admin_user"], region, model, compartment_id):
                    print(f"shared database: Select AI profile repaired for {sid}", flush=True)
                db.commit()
                n += 1
        except Exception as e:  # noqa: BLE001
            print(f"shared database: profile check skipped for {sid} ({type(e).__name__}: {str(e)[:100]})", flush=True)
    return n


# ---------------------------------------------------------------- one schema per sandbox

def provision(sandbox_id: str, region: str, owner: str = "", expires: str = "") -> dict:
    """Create (or, on a retry, refresh) the sandbox's schema. Returns the card's `adb` block."""
    shared = info()
    if shared is None:
        raise RuntimeError("this install has no shared database")
    ensure_started(shared)
    user, pw, al = schema_name(sandbox_id), _password(), alias(sandbox_id)
    web = shared["web"]
    with _admin(shared) as db:
        cur = db.cursor()
        _registry(cur)
        cur.execute("""merge into admin.sbx_schemas t using (select :sid sandbox_id from dual) s on (t.sandbox_id = s.sandbox_id)
                       when matched then update set schema_name = :sn, owner = :o, expires = :e
                       when not matched then insert (sandbox_id, schema_name, owner, expires) values (:sid, :sn, :o, :e)""",
                    sid=sandbox_id, sn=user, o=owner or "", e=expires or "")
        cur.execute("select count(*) from dba_users where username = :u", u=user)
        exists = cur.fetchone()[0] > 0
        if exists:
            cur.execute(f'alter user {user} identified by "{pw}" account unlock')
            print(f"schema {user} exists in {shared['where']}; password rotated", flush=True)
        else:
            cur.execute(f'create user {user} identified by "{pw}" default tablespace data quota unlimited on data')
            print(f"schema {user} created in {shared['where']}", flush=True)
        for g in ("connect", "resource", "create view", "create job", "create materialized view", "create synonym"):
            _try(cur, f"grant {g} to {user}", f"grant {g}")
        for pkg in ("dbms_cloud", "dbms_cloud_ai", "dbms_cloud_ai_agent", "dbms_cloud_repo", "dbms_vector", "dbms_vector_chain", "ctxsys.ctx_ddl"):
            try:
                cur.execute(f"grant execute on {pkg} to {user}")
            except Exception:  # noqa: BLE001  not every package exists on every version
                pass
        _try(cur, "grant read on directory data_pump_dir to " + user, "directory grant")
        # OCI as the database's own identity, for Select AI and DBMS_CLOUD: no key in the sandbox
        try:
            cur.execute(f"begin dbms_cloud_admin.enable_resource_principal(username => '{user}'); end;")
        except Exception as e:  # noqa: BLE001
            if "already" not in str(e).lower():
                print(f"shared database: resource principal for {user} not enabled ({type(e).__name__})", flush=True)
        grant_network(cur, user, region)
        # the schema's own SQL Developer Web and REST, at /ords/<alias>/
        _try(cur, """begin ords_admin.enable_schema(p_enabled => true, p_schema => :s, p_url_mapping_type => 'BASE_PATH',
                       p_url_mapping_pattern => :a, p_auto_rest_auth => true); end;""", "ORDS schema", s=user, a=al)
        # Select AI's profile is per user; this trigger makes every session of the schema pick its own up
        _try(cur, """begin
                       execute immediate q'[
                         create or replace trigger admin.sandbox_ai_logon
                         after logon on database
                         begin
                           dbms_cloud_ai.set_profile('SANDBOX_AI');
                         exception when others then null;
                         end;]';
                     end;""", "Select AI logon trigger")
        # an APEX workspace on the schema, signed in with the same user and password
        workspace = user
        apex_ok = _try(cur, """begin
                                 apex_instance_admin.add_workspace(p_workspace => :w, p_primary_schema => :s);
                               exception when others then
                                 if instr(lower(sqlerrm), 'already') = 0 and instr(lower(sqlerrm), 'exists') = 0 then raise; end if;
                               end;""", "APEX workspace", w=workspace, s=user)
        if apex_ok:
            made = _try(cur, """begin
                                  apex_util.set_security_group_id(apex_util.find_security_group_id(:w));
                                  apex_util.create_user(p_user_name => :u, p_web_password => :p, p_email_address => :m,
                                                        p_developer_privs => 'ADMIN:CREATE:DATA_LOADER:EDIT:HELP:MONITOR:SQL',
                                                        p_default_schema => :s, p_change_password_on_first_use => 'N');
                                  commit;
                                end;""", "APEX user", w=workspace, u=user, p=pw, m=f"{al}@sandbox.local", s=user)
            if not made:
                _try(cur, """begin
                               apex_util.set_security_group_id(apex_util.find_security_group_id(:w));
                               apex_util.edit_user(p_user_id => apex_util.get_user_id(:u), p_user_name => :u, p_web_password => :p,
                                                   p_change_password_on_first_use => 'N');
                               commit;
                             end;""", "APEX user password", w=workspace, u=user, p=pw)
        db.commit()
    adb = {"db_name": shared["db_name"], "admin_user": user, "admin_password": pw,
           "connect_string": shared["connect_string"], "tier": "shared", "shared": True,
           "private": shared["private"], "schema": user, "alias": al}
    if web:
        adb["sql_web_url"] = f"{web}/ords/{al}/_sdw/"
        adb["apex_url"] = f"{web}/ords/apex"
        adb["apex_workspace"] = workspace
        if shared["private"]:
            adb["gateway_url"] = web
    return adb


def finish(outputs: dict, seed_sql: str, region: str, model: str, compartment_id: str) -> None:
    """After the sandbox is up: sample data, Select AI, REST - all as the sandbox's own user."""
    adb = outputs.get("adb") or {}
    if not adb.get("shared"):
        return
    user = adb["admin_user"]
    seed_sql = (seed_sql or "").strip()
    with _as(adb) as db:
        cur = db.cursor()
        if seed_sql:
            import worker
            seed_sql = seed_sql.replace("{{EMBED_PARAMS}}", worker.embed_params())
            stmts = [x.strip().rstrip(";") for x in seed_sql.split(";") if x.strip()]
            if len(seed_sql) > 256 * 1024 or len(stmts) > 200:
                print("seed skipped: over the 256 KB / 200 statement limit", flush=True)
            else:
                done = 0
                for st in stmts:
                    try:
                        cur.execute(st)
                        done += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"seed statement failed ({type(e).__name__}): {st[:120]}", flush=True)
                db.commit()
                print(f"seeded {done}/{len(stmts)} statements into schema {user}", flush=True)
        # Select AI over this schema only (never another sandbox's)
        if EDITION != "free":
            select_ai_profile(cur, user, region, model, compartment_id)
        # REST for every table, now and later
        cur.execute("""select table_name from user_tables
                        where table_name not like 'DEF$%' and table_name not like 'SYS%' and table_name not like 'AQ$%'
                          and table_name not like 'MVIEW$%' and table_name not like 'LOGMNR%' and table_name not like 'SCHEDULER%'""")
        tables = [r[0] for r in cur.fetchall()]
        rested = 0
        for t in tables:
            if _try(cur, """begin ords.enable_object(p_enabled => true, p_schema => :s, p_object => :t, p_object_type => 'TABLE',
                              p_object_alias => lower(:t), p_auto_rest_auth => true); end;""", f"REST for {t}", s=user, t=t):
                rested += 1
        _try(cur, f"""begin
                        begin dbms_scheduler.drop_job('{user}.SANDBOX_REST_NEW', true); exception when others then null; end;
                        dbms_scheduler.create_job(
                          job_name        => '{user}.SANDBOX_REST_NEW',
                          job_type        => 'PLSQL_BLOCK',
                          job_action      => q'[begin
                            for t in (select table_name from user_tables
                                       where table_name not like 'DEF$%' and table_name not like 'SYS%'
                                         and table_name not in (select parsing_object from user_ords_enabled_objects)) loop
                              begin
                                ords.enable_object(p_enabled => true, p_schema => '{user}', p_object => t.table_name,
                                                   p_object_type => 'TABLE', p_object_alias => lower(t.table_name),
                                                   p_auto_rest_auth => true);
                              exception when others then null;
                              end;
                            end loop;
                            commit;
                          end;]',
                          repeat_interval => 'FREQ=MINUTELY;INTERVAL=2',
                          enabled         => true,
                          comments        => 'Sandbox Factory: publish new tables through ORDS');
                      end;""", "auto-publish job")
        db.commit()
    web = adb.get("sql_web_url", "").split("/ords/")[0]
    if web:
        import requests
        al = adb["alias"]
        ok = False
        for _ in range(18):
            try:
                r = requests.get(f"{web}/ords/{al}/metadata-catalog/", auth=(user, adb["admin_password"]), timeout=30)
                ok = r.status_code == 200
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                break
            time.sleep(20)
        if ok:
            outputs.setdefault("low_code", {})["rest_base"] = f"{web}/ords/{al}/"
            outputs["low_code"]["rest_tables"] = sorted(t.lower() for t in tables)
        else:
            outputs.setdefault("warnings", []).append(
                f"REST endpoints for the {user} tables did not come up; use SQL Developer Web or APEX instead.")
    print(f"ORDS auto-REST on for {rested}/{len(tables)} table(s) of {user} (basic auth as {user}); "
          f"APEX workspace {adb.get('apex_workspace', user)} is ready at the APEX URL", flush=True)


def destroy(sandbox_id: str) -> bool:
    """Drop the sandbox's schema, workspace and REST mapping. True when there was one."""
    shared = info()
    if shared is None:
        return False
    user = schema_name(sandbox_id)
    try:
        ensure_started(shared)
        with _admin(shared) as db:
            cur = db.cursor()
            cur.execute("select count(*) from dba_users where username = :u", u=user)
            if cur.fetchone()[0] == 0:
                return False
            _try(cur, "begin ords_admin.enable_schema(p_enabled => false, p_schema => :s); end;", "ORDS disable", s=user)
            _try(cur, f"alter user {user} account lock", "account lock")
            for attempt in range(4):
                # its sessions first (SQL Developer Web, an app, the panel), then the workspace and the schema
                cur.execute("select sid, serial# from v$session where username = :u", u=user)
                for sid, serial in cur.fetchall():
                    _try(cur, f"alter system kill session '{sid},{serial}' immediate", "session kill")
                try:
                    cur.execute("begin apex_instance_admin.remove_workspace(p_workspace => :w, p_drop_users => 'N', p_drop_tablespaces => 'N'); end;", w=user)
                except Exception as e:  # noqa: BLE001
                    if "ORA-20987" not in str(e) and "not exist" not in str(e).lower() and attempt < 3 and "ORA-01940" in str(e):
                        time.sleep(10)
                        continue
                    if "not exist" not in str(e).lower() and "ORA-20987" not in str(e):
                        print(f"shared database: APEX workspace not removed ({type(e).__name__}: {str(e)[:120]})", flush=True)
                try:
                    cur.execute(f"drop user {user} cascade")
                    break
                except Exception as e:  # noqa: BLE001
                    if "ORA-01940" in str(e) and attempt < 3:   # still connected: the kill takes a moment
                        time.sleep(10)
                        continue
                    raise
            _registry(cur)
            cur.execute("delete from admin.sbx_schemas where sandbox_id = :s", s=sandbox_id)
            db.commit()
        print(f"schema {user} dropped from {shared['where']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"shared database: schema {user} not removed ({type(e).__name__}: {str(e)[:160]})", flush=True)
        return True
    stop_if_idle(shared)
    return True
