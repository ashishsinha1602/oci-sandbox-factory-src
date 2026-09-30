"""The database panel: work with a sandbox's database from inside the Sandbox Factory page.

A paid sandbox database sits on a private endpoint, so its own web tools (SQL Developer
Web, APEX, REST) do not open from a browser, and a database-only sandbox has no gateway.
Nothing is made public for it. The page queues a request here (ownership checked in the
control database), the worker - already inside the private network - runs it against the
sandbox database, and the page reads the result back:

    sql     one statement; a query returns up to MAX_ROWS rows with column names
    tables  the ADMIN schema's tables and their row counts
    ask     a plain-English question answered by Select AI over the sandbox's data

    ensure(cur)  table + function in the control schema (every worker start; idempotent)
    serve()      the worker thread that answers queued requests
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import threading
import time
import traceback

import controldb

SCHEMA = controldb.SCHEMA
MAX_ROWS = 200
CALL_TIMEOUT_MS = 90_000

DDL = f"""
create table {SCHEMA}.db_runs (
  id          number generated always as identity primary key,
  sandbox_id  varchar2(64)  not null,
  requester   varchar2(255) not null,
  kind        varchar2(16)  not null,
  text        clob,
  status      varchar2(12)  default 'QUEUED' not null,
  result      clob,
  error       varchar2(4000),
  created_at  timestamp default systimestamp,
  finished_at timestamp)"""

# Called by the page (ajax.plsql: db_panel(action, x02, :APP_USER)). Only the owner of a
# built sandbox may queue a request for it, and only the owner reads the answer.
FUNCTION = f"""
create or replace function {SCHEMA}.db_panel(p_action varchar2, p_json clob, p_user varchar2) return clob is
  l_in  json_object_t := json_object_t.parse(nvl(p_json, '{{}}'));
  l_out json_object_t := json_object_t();
  l_sid varchar2(64) := l_in.get_string('sandbox_id');
  l_kind varchar2(16) := lower(l_in.get_string('kind'));
  l_id  number;
  l_n   number;
  l_st  varchar2(12); l_res clob; l_err varchar2(4000);
  l_text clob := l_in.get_clob('text');   -- JSON methods cannot be called inside SQL (ORA-40573)
  l_rid  number := l_in.get_number('id');
begin
  if p_action = 'dbrun' then
    if l_kind not in ('sql', 'tables', 'ask') then
      l_out.put('err', 'kind must be sql, tables or ask'); return l_out.to_clob;
    end if;
    select count(*) into l_n from (
      select action, status, row_number() over (order by id desc) rn
        from {SCHEMA}.sandbox_requests where sandbox_id = l_sid and requester = p_user)
     where rn = 1 and status = 'DONE' and action in ('CREATE', 'DEPLOY');
    if l_n = 0 then
      l_out.put('err', 'No running sandbox ' || l_sid || ' of yours.'); return l_out.to_clob;
    end if;
    insert into {SCHEMA}.db_runs (sandbox_id, requester, kind, text)
    values (l_sid, p_user, l_kind, l_text)
    returning id into l_id;
    commit;
    l_out.put('id', l_id);
  elsif p_action = 'dbres' then
    begin
      select status, result, error into l_st, l_res, l_err from {SCHEMA}.db_runs
       where id = l_rid and requester = p_user;
    exception when no_data_found then
      l_out.put('err', 'no such request'); return l_out.to_clob;
    end;
    l_out.put('status', l_st);
    if l_res is not null then l_out.put('result', json_object_t.parse(l_res)); end if;
    if l_err is not null then l_out.put('err', l_err); end if;
  else
    l_out.put('err', 'unknown action');
  end if;
  return l_out.to_clob;
end;"""


def ensure(cur) -> None:
    cur.execute("select count(*) from dba_tables where owner = :1 and table_name = 'DB_RUNS'", [SCHEMA])
    if not cur.fetchone()[0]:
        cur.execute(DDL)
        print("db panel: table db_runs created", flush=True)
    cur.execute(FUNCTION)
    cur.execute(f"select status from dba_objects where owner = :1 and object_name = 'DB_PANEL' and object_type = 'FUNCTION'", [SCHEMA])
    row = cur.fetchone()
    if not row or row[0] != "VALID":
        cur.execute(f"alter function {SCHEMA}.db_panel compile")


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if hasattr(v, "read"):
        s = v.read()
        return s[:4000] if isinstance(s, str) else f"<{len(s)} bytes>"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return str(v)


def _sandbox_db(conn, sandbox_id: str, requester: str) -> dict:
    cur = conn.cursor()
    cur.execute(f"""select outputs from {SCHEMA}.sandbox_requests
                     where sandbox_id = :1 and requester = :2 and status = 'DONE' and action in ('CREATE', 'DEPLOY')
                     order by id desc fetch first 1 rows only""", [sandbox_id, requester])
    row = cur.fetchone()
    out = json.loads(row[0].read() if row and hasattr(row[0], "read") else (row[0] if row else "{}")) or {}
    adb = out.get("adb") or {}
    if not (adb.get("connect_string") and adb.get("admin_password")):
        raise ValueError("this sandbox has no database")
    return adb


def run_one(kind: str, text: str, adb: dict) -> dict:
    import oracledb
    cs = adb["connect_string"]
    dsn = cs if cs.lstrip().startswith("(") else controldb._dsn(cs)   # TLS, no wallet: as the worker's adb_dsn
    with oracledb.connect(user="ADMIN", password=adb["admin_password"], dsn=dsn) as db:
        db.call_timeout = CALL_TIMEOUT_MS
        cur = db.cursor()
        if kind == "tables":
            cur.execute("""select table_name, num_rows from user_tables
                            where table_name not like 'DBTOOLS$%' and table_name not like 'SYS\\_%' escape '\\'
                              and table_name not like 'COPY$%' order by table_name""")
            rows = cur.fetchall()
            return {"columns": ["TABLE_NAME", "NUM_ROWS"], "rows": [[_jsonable(x) for x in r] for r in rows], "rowcount": len(rows)}
        if kind == "ask":
            q = (text or "").strip()
            if not q:
                raise ValueError("ask a question")
            cur.execute("""select dbms_cloud_ai.generate(prompt => :q, profile_name => 'SANDBOX_AI', action => 'narrate') from dual""", q=q)
            ans = cur.fetchone()[0]
            ans = ans.read() if hasattr(ans, "read") else ans
            sql = None
            try:
                cur.execute("""select dbms_cloud_ai.generate(prompt => :q, profile_name => 'SANDBOX_AI', action => 'showsql') from dual""", q=q)
                s = cur.fetchone()[0]
                sql = s.read() if hasattr(s, "read") else s
            except Exception:  # noqa: BLE001  the answer is what matters; the SQL is a bonus
                pass
            return {"answer": ans, "sql": sql}
        stmt = (text or "").strip().rstrip(";").strip()
        if not stmt:
            raise ValueError("write a SQL statement")
        cur.execute(stmt)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
            return {"columns": cols, "rows": [[_jsonable(x) for x in r] for r in rows[:MAX_ROWS]],
                    "rowcount": min(len(rows), MAX_ROWS), "truncated": len(rows) > MAX_ROWS}
        db.commit()
        return {"message": f"done: {cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0} row(s) affected"}


def serve_forever(poll_seconds: float = 1.5) -> None:
    """Answer queued database-panel requests, one thread per worker, beside the build loop."""
    name = threading.current_thread().name
    conn = None
    while True:
        try:
            if conn is None:
                conn = controldb.connect("ADMIN")
            cur = conn.cursor()
            cur.execute(f"select id from {SCHEMA}.db_runs where status = 'QUEUED' order by id fetch first 5 rows only")
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                time.sleep(poll_seconds)
                continue
            for rid in ids:
                cur.execute(f"update {SCHEMA}.db_runs set status = 'RUNNING' where id = :1 and status = 'QUEUED'", [rid])
                conn.commit()
                if cur.rowcount != 1:
                    continue                                   # the other worker took it
                cur.execute(f"select sandbox_id, requester, kind, text from {SCHEMA}.db_runs where id = :1", [rid])
                sid, who, kind, text = cur.fetchone()
                text = text.read() if hasattr(text, "read") else text
                try:
                    res = run_one(kind, text, _sandbox_db(conn, sid, who))
                    cur.execute(f"update {SCHEMA}.db_runs set status = 'DONE', result = :2, finished_at = systimestamp where id = :1",
                                [rid, json.dumps(res, default=str)])
                except Exception as e:  # noqa: BLE001  the user sees the database's own message
                    msg = re.sub(r"\s+Help: https://\S+", "", str(e)).strip()[:3900]
                    cur.execute(f"update {SCHEMA}.db_runs set status = 'FAILED', error = :2, finished_at = systimestamp where id = :1",
                                [rid, msg or type(e).__name__])
                conn.commit()
        except Exception as e:  # noqa: BLE001  never let the panel thread die
            print(f"db panel [{name}]: {type(e).__name__}: {str(e)[:200]}", flush=True)
            traceback.print_exc()
            try:
                conn and conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(10)


def start() -> None:
    threading.Thread(target=serve_forever, name="db-panel", daemon=True).start()
    print("db panel: answering database requests from the page", flush=True)
