"""Install progress for a fresh tenancy, readable from the outside.

Terraform finishes minutes before the application exists (the worker installs
it on its first start), and a user who opens the application URL in between sees
a 404 with no explanation. So the very first thing a worker does is publish
where it is:

    GET <ords>/admin/status/   ->  JSON {"phase": "installing application", "detail": "...", ...}
                                   or, for a browser, a page that refreshes itself and opens the
                                   application as soon as the phase is "ready"

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
        l_phase varchar2(60) := 'starting'; l_detail varchar2(400); l_at varchar2(40); l_app varchar2(400);
        l_html boolean := instr(nvl(owa_util.get_cgi_env('HTTP_ACCEPT'), ''), 'text/html') > 0;
      begin
        for r in (select phase, detail, updated_at, app_url from install_status where id = 1) loop
          l_phase := r.phase; l_detail := r.detail; l_app := r.app_url;
          l_at := to_char(r.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
        end loop;
        if l_html then
          if l_phase = 'ready' and l_app is not null then
            owa_util.redirect_url(l_app);
            return;
          end if;
          owa_util.mime_header('text/html', true);
          htp.p('<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="15"><title>Sandbox Factory is installing</title>'
             || '<style>body{font-family:system-ui,Segoe UI,sans-serif;background:#f6f5f2;color:#1b2a41;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}'
             || '.c{background:#fff;border:1px solid #e3e6ea;border-radius:14px;padding:32px 36px;max-width:560px}h1{font-size:22px;margin:0 0 10px}p{margin:8px 0;color:#5c6b7a}'
             || '.s{display:inline-block;width:14px;height:14px;border:2px solid #c74634;border-right-color:transparent;border-radius:50%;animation:r .8s linear infinite;vertical-align:-2px;margin-right:8px}@keyframes r{to{transform:rotate(360deg)}}'
             || 'b{color:#1b2a41}</style></head><body><div class="c"><h1><span class="s"></span>Sandbox Factory is installing</h1>'
             || '<p>The infrastructure is built. The worker is now setting up the application in the control database; this takes a few minutes (up to ten on the Free Tier edition).</p>'
             || '<p>Current step: <b>' || htf.escape_sc(l_phase) || '</b>' || case when l_detail is not null then ' &middot; ' || htf.escape_sc(l_detail) end || '</p>'
             || '<p>This page refreshes itself and opens the application when it is ready. Sign in with the admin user and password from the stack outputs.</p>'
             || '<p style="font-size:12px">last update ' || nvl(l_at, '-') || ' UTC</p></div></body></html>');
          return;
        end if;
        l.put('phase', l_phase); l.put('detail', l_detail); l.put('updated_at', l_at); l.put('app_url', l_app);
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
