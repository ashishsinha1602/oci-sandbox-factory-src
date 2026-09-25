#!/usr/bin/env python3
"""Control database: connection helper + one-time setup.

The control ATP (foundation/control_adb.tf) hosts the APEX front end and the
sandbox_requests queue. Credentials are read from the foundation stack's
outputs at runtime; nothing is written to disk.

    python controldb.py setup      # schema SBX, tables, APEX workspace SBX
    python controldb.py sql "select count(*) from sbx.sandbox_requests"
    python controldb.py users alice@example.com   # APEX end-user accounts for the app
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import string
import subprocess
import sys

import oracledb

FOUNDATION_DIR = pathlib.Path(__file__).resolve().parent.parent / "foundation"
SCHEMA = "SBX"
WORKSPACE = "SBX"
WORKSPACE_ADMIN = "SBXADMIN"


def _tf_output(name: str, raw: bool = False) -> str:
    tf = shutil.which("terraform") or str(pathlib.Path.home() / "bin" / "terraform.exe")
    cmd = [tf, "output", "-raw" if raw else "-json", name]
    return subprocess.check_output(cmd, cwd=FOUNDATION_DIR, text=True).strip()


def control_info() -> dict:
    """Control ADB details. From SBX_CONTROL_CONNECT (host:port/service) on the
    OCI-hosted worker, else from the foundation stack's outputs."""
    if os.environ.get("SBX_CONTROL_CONNECT"):
        return {"connect_string": os.environ["SBX_CONTROL_CONNECT"]}
    return json.loads(_tf_output("control_adb"))


def _dsn(connect_string: str) -> str:
    host_port, service = connect_string.split("/", 1)
    host, port = host_port.split(":")
    return (
        f"(description=(retry_count=5)(retry_delay=3)"
        f"(address=(protocol=tcps)(port={port})(host={host}))"
        f"(connect_data=(service_name={service}))"
        f"(security=(ssl_server_dn_match=yes)))"
    )


def connect(user: str = "ADMIN") -> oracledb.Connection:
    """Connect to the control ATP. ADMIN password comes from terraform output;
    the SBX password from SBX_DB_PASSWORD (worker) or is not needed."""
    info = control_info()
    if user.upper() == "ADMIN":
        password = os.environ.get("SBX_ADMIN_PASSWORD") or _tf_output("control_adb_admin_password", raw=True)
    else:
        password = os.environ["SBX_DB_PASSWORD"]
    return oracledb.connect(user=user, password=password, dsn=_dsn(info["connect_string"]))


def _password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "S" + "".join(secrets.choice(alphabet) for _ in range(n - 2)) + "9#"


DDL = f"""
create table {SCHEMA}.sandbox_requests (
  id            number generated always as identity primary key,
  created_at    timestamp default systimestamp not null,
  requester     varchar2(200) not null,
  sandbox_id    varchar2(20)  not null,
  action        varchar2(10)  default 'CREATE' not null
                check (action in ('CREATE','DEPLOY','DESTROY')),
  ttl_days      number(2)     default 3 not null
                constraint sandbox_requests_ttl_ck check (ttl_days between 1 and 3),
  enable_adb    varchar2(1)   default 'N' not null check (enable_adb in ('Y','N')),
  adb_tier      varchar2(4)   default 'free' check (adb_tier in ('free','paid')),
  enable_kafka  varchar2(1)   default 'N' not null check (enable_kafka in ('Y','N')),
  enable_nosql  varchar2(1)   default 'N' not null check (enable_nosql in ('Y','N')),
  kafka_mode    varchar2(9)   default 'streaming' check (kafka_mode in ('streaming','cluster')),
  enable_app    varchar2(1)   default 'N' not null check (enable_app in ('Y','N')),
  app_image     varchar2(500),
  git_url       varchar2(500),
  app_port      number(5)     default 8080,
  request_text  varchar2(4000),
  status        varchar2(10)  default 'QUEUED' not null
                check (status in ('QUEUED','RUNNING','DONE','FAILED')),
  started_at    timestamp,
  finished_at   timestamp,
  outputs       clob,
  error         varchar2(4000),
  log           clob,
  seed_sql      clob,
  app_containers clob,
  app_files     clob,
  adb_databases clob,
  functions     clob,
  app_instances clob,
  buckets       clob,
  queues        clob,
  dataflow_jobs clob,
  enable_catalog varchar2(1) default 'N' not null check (enable_catalog in ('Y','N')),
  catalog_assets clob,
  seed_key      varchar2(40),
  app_template  varchar2(40)
)
"""

# Ownership and quota, enforced by the database so every route is covered:
# the APEX page, the MCP server, the CLI and anything added later.
OWNER_TRIGGER = f"""
create or replace trigger {SCHEMA}.sandbox_requests_owner_biu
before insert on {SCHEMA}.sandbox_requests
for each row
declare
  l_owner {SCHEMA}.sandbox_requests.requester%type;
  l_live  number;
  l_cap   number;
begin
  if not regexp_like(:new.sandbox_id, '^[a-z][a-z0-9-]{{1,19}}$') then
    raise_application_error(-20010, 'Bad sandbox name: '||:new.sandbox_id);
  end if;

  select min(requester) into l_owner
    from {SCHEMA}.sandbox_requests where sandbox_id = :new.sandbox_id;

  if l_owner is not null and l_owner <> :new.requester then
    raise_application_error(-20011,
      'Sandbox '||:new.sandbox_id||' belongs to '||l_owner||', not '||:new.requester);
  end if;

  if :new.action = 'DESTROY' and l_owner is null then
    raise_application_error(-20012, 'No sandbox '||:new.sandbox_id||' owned by '||:new.requester);
  end if;

  if :new.action <> 'DESTROY' and l_owner is null then
    begin
      select to_number(value) into l_cap from {SCHEMA}.factory_config where key = 'max_sandboxes_per_user';
    exception when no_data_found then l_cap := 3;
    end;
    select count(*) into l_live from (
      select sandbox_id, action, status,
             row_number() over (partition by sandbox_id order by id desc) rn
        from {SCHEMA}.sandbox_requests where requester = :new.requester)
     where rn = 1 and not (action = 'DESTROY' and status = 'DONE');
    if l_live >= l_cap then
      raise_application_error(-20013,
        :new.requester||' already has '||l_live||' sandboxes; the limit is '||l_cap);
    end if;
  end if;
end;
"""

# APEX-friendly view of live sandboxes (latest DONE request per sandbox that is not destroyed).
VIEW = f"""
create or replace view {SCHEMA}.sandboxes_v as
select r.sandbox_id, r.requester, r.finished_at, r.ttl_days,
       r.finished_at + r.ttl_days as expires_at,
       r.enable_adb, r.enable_kafka, r.enable_nosql, r.enable_app, r.outputs
from {SCHEMA}.sandbox_requests r
where r.status = 'DONE'
  and r.action in ('CREATE','DEPLOY')
  and r.id = (select max(id) from {SCHEMA}.sandbox_requests x
              where x.sandbox_id = r.sandbox_id and x.status = 'DONE')
"""


def setup(argv: list[str]) -> None:
    conn = connect("ADMIN")
    cur = conn.cursor()
    schema_pw = _password()

    def exists(sql, *binds):
        cur.execute(sql, binds)
        return cur.fetchone()[0] > 0

    if not exists("select count(*) from dba_users where username = :1", SCHEMA):
        cur.execute(f'create user {SCHEMA} identified by "{schema_pw}" quota unlimited on data')
        print(f"schema {SCHEMA} created")
    else:
        cur.execute(f'alter user {SCHEMA} identified by "{schema_pw}"')
        print(f"schema {SCHEMA} exists; password rotated")
    cur.execute(f"grant connect, resource, create view to {SCHEMA}")

    if not exists("select count(*) from dba_tables where owner = :1 and table_name = 'SANDBOX_REQUESTS'", SCHEMA):
        cur.execute(DDL)
        print("table sandbox_requests created")
    else:
        # Columns added after the first deployment of a control database: setup only
        # runs the DDL for a brand new table, so bring an existing one up to date.
        for col, kind in (("seed_sql", "clob"), ("app_containers", "clob"), ("app_files", "clob"),
                          ("seed_key", "varchar2(40)"), ("app_template", "varchar2(40)"),
                          ("enable_nosql", "varchar2(1) default 'N' not null"),
                          ("adb_databases", "clob"), ("functions", "clob"),
                          ("app_instances", "clob"), ("buckets", "clob"),
                          ("queues", "clob"), ("dataflow_jobs", "clob"),
                          ("enable_catalog", "varchar2(1) default 'N' not null"),
                          ("catalog_assets", "clob")):
            if not exists("select count(*) from dba_tab_columns where owner = :1 and table_name = 'SANDBOX_REQUESTS' and column_name = :2",
                          SCHEMA, col.upper()):
                cur.execute(f"alter table {SCHEMA}.sandbox_requests add ({col} {kind})")
                print(f"column {col} added to sandbox_requests")
    cur.execute(VIEW)
    print("view sandboxes_v created")
    cur.execute(OWNER_TRIGGER)
    print("trigger sandbox_requests_owner_biu created (ownership + per-user quota)")
    if not exists("select count(*) from dba_tables where owner = :1 and table_name = 'FACTORY_CONFIG'", SCHEMA):
        cur.execute(f"create table {SCHEMA}.factory_config (key varchar2(64) primary key, value varchar2(4000))")
    cur.execute(f"""merge into {SCHEMA}.factory_config c
        using (select 'compartment_ocid' key, json_value(cloud_identity, '$.COMPARTMENT_OCID') value from v$pdbs where rownum = 1) s
        on (c.key = s.key) when matched then update set c.value = s.value
        when not matched then insert (key, value) values (s.key, s.value)""")
    print("factory_config: compartment_ocid set")
    # Tenancy-specific settings the APEX page reads at runtime, so the page itself
    # carries no region or registry. Override either with an env var at setup time.
    for key, val in (("registry_prefix", os.environ.get("SBX_REGISTRY_PREFIX", "")),
                     ("genai_region", os.environ.get("SBX_GENAI_REGION", ""))):
        if val:
            cur.execute(f"""merge into {SCHEMA}.factory_config c using (select :k key, :v value from dual) s
                on (c.key = s.key) when matched then update set c.value = s.value
                when not matched then insert (key, value) values (s.key, s.value)""", k=key, v=val)
            print(f"factory_config: {key} = {val}")

    # APEX workspace on the SBX schema + a workspace admin who must change password on first login.
    cur.execute("select count(*) from apex_workspaces where workspace = :1", [WORKSPACE])
    if cur.fetchone()[0] == 0:
        cur.execute(f"begin apex_instance_admin.add_workspace(p_workspace => '{WORKSPACE}', p_primary_schema => '{SCHEMA}'); end;")
        print(f"APEX workspace {WORKSPACE} created")
    admin_pw = _password()
    cur.execute(f"""
        begin
          apex_util.set_workspace(p_workspace => '{WORKSPACE}');
          if apex_util.get_user_id('{WORKSPACE_ADMIN}') is null then
            apex_util.create_user(
              p_user_name                   => '{WORKSPACE_ADMIN}',
              p_email_address               => :email,
              p_web_password                => :pw,
              p_developer_privs             => 'ADMIN:CREATE:DATA_LOADER:EDIT:HELP:MONITOR:SQL',
              p_default_schema              => '{SCHEMA}',
              p_change_password_on_first_use => 'N');
          else
            apex_util.reset_password(p_user_name => '{WORKSPACE_ADMIN}', p_old_password => null,
                                     p_new_password => :pw, p_change_password_on_first_use => false);
          end if;
        end;""", email=os.environ.get("SBX_OWNER", "admin@example.com"), pw=admin_pw)
    conn.commit()
    print(f"APEX workspace admin {WORKSPACE_ADMIN} ready")
    # Hand the two generated passwords to the caller via env-style lines on stdout
    # only when asked; by default they are kept in this process (see apex_builder.py).
    if "--print-passwords" in argv:
        print(f"SBX_DB_PASSWORD={schema_pw}")
        print(f"SBX_APEX_ADMIN_PASSWORD={admin_pw}")
    else:
        pathlib.Path(os.environ.get("SBX_SECRETS_FILE", os.devnull)).write_text(
            json.dumps({"SBX_DB_PASSWORD": schema_pw, "SBX_APEX_ADMIN_PASSWORD": admin_pw}))
    conn.close()


def add_users(argv: list[str]) -> None:
    """Create APEX end-user accounts for the Sandbox Factory app.

        python controldb.py users alice@example.com bob@example.com
    Each user gets a one-time password (printed) and must change it on first login.
    """
    conn = connect("ADMIN")
    cur = conn.cursor()
    for email in argv:
        user = email.split("@")[0].upper()
        pw = _password()
        cur.execute(f"""
            begin
              apex_util.set_workspace(p_workspace => '{WORKSPACE}');
              if apex_util.get_user_id(:u) is null then
                apex_util.create_user(
                  p_user_name => :u, p_email_address => :e, p_web_password => :pw,
                  p_developer_privs => null, p_default_schema => '{SCHEMA}',
                  p_change_password_on_first_use => 'Y');
              else
                apex_util.reset_password(p_user_name => :u, p_old_password => null,
                                         p_new_password => :pw, p_change_password_on_first_use => true);
              end if;
            end;""", u=user, e=email, pw=pw)
        print(f"{user:<20} {email:<40} one-time password: {pw}")
    conn.commit()
    conn.close()


def run_sql(argv: list[str]) -> None:
    conn = connect("ADMIN")
    cur = conn.cursor()
    cur.execute(argv[0])
    if cur.description:
        for row in cur.fetchall():
            print(row)
    else:
        conn.commit()
        print("ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    {"setup": setup, "sql": run_sql, "users": add_users}[cmd](sys.argv[2:])
