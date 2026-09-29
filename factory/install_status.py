"""Install progress for a fresh tenancy, readable from the outside.

Terraform finishes minutes before the application exists (the worker installs
it on its first start), and a user who opens the application URL in between sees
a 404 with no explanation. So the very first thing a worker does is publish
where it is:

    GET <ords>/admin/status/   ->  {"phase": "installing application", "detail": "...",
                                    "updated_at": "...", "app_url": "..."}

One row in ADMIN.INSTALL_STATUS, served by a public ORDS handler in the ADMIN
schema. `mark()` never raises: progress reporting must not stop a bootstrap.
"""
from __future__ import annotations

import os
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_ready = False

DDL = """
begin
  execute immediate 'create table install_status (
      id number default 1 primary key, phase varchar2(60), detail varchar2(400),
      updated_at timestamp, app_url varchar2(400))';
exception when others then if sqlcode <> -955 then raise; end if;
end;"""

ORDS = """
begin
  begin
    ords_admin.enable_schema(p_enabled => true, p_schema => 'ADMIN', p_url_mapping_type => 'BASE_PATH',
                             p_url_mapping_pattern => 'admin', p_auto_rest_auth => true);
  exception when others then null;   -- already enabled, or a database that refuses: the handler below still tries
  end;
  ords.define_module(p_module_name => 'status', p_base_path => '/', p_items_per_page => 0, p_status => 'PUBLISHED',
                     p_comments => 'Sandbox Factory install progress (public, read-only)');
  ords.define_template(p_module_name => 'status', p_pattern => 'status/');
  ords.define_handler(p_module_name => 'status', p_pattern => 'status/', p_method => 'GET',
    p_source_type => 'plsql/block',
    p_source => q'[
      declare
        l json_object_t := json_object_t();
      begin
        for r in (select phase, detail, updated_at, app_url from install_status where id = 1) loop
          l.put('phase', r.phase);
          l.put('detail', r.detail);
          l.put('updated_at', to_char(r.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"'));
          l.put('app_url', r.app_url);
        end loop;
        if not l.has('phase') then l.put('phase', 'starting'); end if;
        owa_util.mime_header('application/json', true);
        htp.p(l.to_string);
      end;]');
  commit;
end;"""


def mark(phase: str, detail: str = "") -> None:
    """Record the current bootstrap phase (creates the table and endpoint on first use)."""
    global _ready
    try:
        import controldb
        con = controldb.connect("ADMIN")
        cur = con.cursor()
        if not _ready:
            cur.execute(DDL)
            cur.execute("select count(*) from user_ords_modules where name = 'status'")
            if not cur.fetchone()[0]:
                cur.execute(ORDS)
            _ready = True
        cur.execute("""merge into install_status s using (select 1 id from dual) d on (s.id = d.id)
                       when matched then update set phase = :p, detail = :d, updated_at = systimestamp at time zone 'UTC', app_url = :u
                       when not matched then insert (id, phase, detail, updated_at, app_url)
                            values (1, :p, :d, systimestamp at time zone 'UTC', :u)""",
                    p=phase[:60], d=(detail or "")[:400], u=os.environ.get("SBX_APP_URL", "")[:400])
        con.commit()
        con.close()
        print(f"status: {phase}" + (f" ({detail})" if detail else ""), flush=True)
    except Exception as e:  # noqa: BLE001 - progress reporting never stops a bootstrap
        print(f"status: could not record '{phase}' ({type(e).__name__}: {str(e)[:120]})", flush=True)
