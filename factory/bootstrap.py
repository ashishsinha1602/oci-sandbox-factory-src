"""First start in a new tenancy: make the control database ready without a laptop.

    python bootstrap.py          # or: the worker runs this before polling

Idempotent, and safe on an existing install:
  1. If the SBX schema is missing, controldb.setup() creates the schema, the
     tables, the trigger and the APEX workspace (it prints the passwords it
     generated; keep them from the log).
  2. If the Sandbox Factory application is missing, the shipped export
     (apex_export_customized.sql, the page as last deployed) is installed.
  3. The assistant's prompts are (re)loaded from factory/prompts/*.txt.
Nothing is touched when everything is already there, so every worker can run
it at start.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import controldb  # noqa: E402
import install_status  # noqa: E402


def bootstrap() -> None:
    install_status.mark("worker started", "preparing the control database")
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    # One worker at a time, from the very first step: on a fresh install all
    # of them start together, and a worker that saw the schema already there
    # tried to install the application before the first one had created the
    # APEX workspace (ORA-20987). The lock belongs to this session and goes
    # when it closes, so the session stays open through setup.
    try:
        got = cur.var(int)
        cur.execute("""declare h varchar2(128); begin dbms_lock.allocate_unique('SBX_APP_INSTALL', h);
                       :r := dbms_lock.request(h, dbms_lock.x_mode, 900, false); end;""", r=got)
        if got.getvalue() not in (0, 4):
            print(f"bootstrap: install lock not taken (status {got.getvalue()}); going on without it", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: install lock unavailable ({e}); going on without it", flush=True)
    cur.execute("select count(*) from dba_users where username = :1", [controldb.SCHEMA])
    if not cur.fetchone()[0]:
        print("bootstrap: control schema missing; running controldb.setup()", flush=True)
        install_status.mark("creating control schema", "tables, trigger and the APEX workspace")
        controldb.setup([])                       # its own connection; ours keeps the lock
    else:
        # columns the newer code needs, on an install made by an older release
        for col in controldb.migrate(cur):
            print(f"bootstrap: column {col} added", flush=True)
        conn.commit()
    cur.execute("select application_id from apex_applications where workspace = :1 and application_name = :2",
                [controldb.WORKSPACE, "Sandbox Factory"])
    row = cur.fetchone()
    # The page this image ships, by fingerprint. An upgrade (a new release
    # image) must reach an existing install too, not only a fresh one, so the
    # application is replaced whenever the shipped page differs from the one
    # installed. The export removes and recreates app 112 on import.
    import hashlib
    export = HERE / "apex_export_customized.sql"
    shipped = hashlib.sha256(export.read_bytes()).hexdigest()[:16] if export.exists() else None
    installed = None
    try:
        cur.execute(f"select value from {controldb.SCHEMA}.factory_config where key = 'app_release'")
        r = cur.fetchone()
        installed = r[0] if r else None
    except Exception:  # noqa: BLE001  (no config table yet on a very old install)
        pass
    if not shipped:
        print("bootstrap: no apex_export_customized.sql shipped; skipping the APEX import", flush=True)
    elif not row or installed != shipped:
        import apex_customize
        print(f"bootstrap: {'installing' if not row else 'upgrading'} the Sandbox Factory application "
              f"({installed or 'none'} -> {shipped})", flush=True)
        install_status.mark("installing application", "importing the APEX application; a few minutes")
        apex_customize.install(export.read_text(encoding="utf-8"), 112)
        cur.execute(f"""merge into {controldb.SCHEMA}.factory_config c using (select 'app_release' key, :v value from dual) s
                        on (c.key = s.key) when matched then update set c.value = s.value
                        when not matched then insert (key, value) values (s.key, s.value)""", v=shipped)
        conn.commit()
    else:
        print(f"bootstrap: application {row[0]} present, release {shipped}", flush=True)
    conn.close()                                            # releases the install lock
    # the schema's run-time grants (DBMS_CLOUD, resource principal, network ACE),
    # healed on every start so an older install gets them too
    try:
        import sandbox_factory as sf
        c = controldb.connect("ADMIN")
        for line in controldb.runtime_grants(c, sf.config()["region"]):
            print("bootstrap: grant", line, flush=True)
        c.close()
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: grants not applied ({type(e).__name__}: {e})", flush=True)
    # Every step below is independent: one that fails is reported and the rest
    # still run (a failed prompt load once skipped the profile, templates and
    # prices of a fresh install, leaving it without chat).
    install_status.mark("loading prompts")
    try:
        import prompts
        for line in prompts.load():
            print("bootstrap: prompt", line, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: prompts not loaded ({type(e).__name__}: {e})", flush=True)
    # what this install is: region, registry, the chat models that answer here
    install_status.mark("refreshing profile", "region, registry and the models this region serves")
    try:
        import os
        import profile as tenancy_profile
        prof = tenancy_profile.refresh()
        if os.environ.get("SBX_APP_URL"):                 # set by the Terraform install
            c = controldb.connect("ADMIN")
            tenancy_profile.save(c.cursor(), {"app_url": os.environ["SBX_APP_URL"]})
            c.commit()
            c.close()
        print(f"bootstrap: profile region {prof['region']}, registry {prof['registry_prefix']}, "
              f"model {prof['genai_model'] or 'NONE ANSWERS - chat and Select AI are off'}", flush=True)
    except Exception as e:  # noqa: BLE001 - never stop a worker over it
        print(f"bootstrap: profile not refreshed ({type(e).__name__}: {e})", flush=True)
    # the assistant's catalogue of ready-made templates, with this install's
    # release registry filled in (factory/templates.txt)
    try:
        import profile as tenancy_profile
        c = controldb.connect("ADMIN")
        cur = c.cursor()
        reg = tenancy_profile.saved(cur).get("registry_prefix", "")
        text = (HERE / "templates.txt").read_text(encoding="utf-8").strip().replace("{R}", reg)
        tenancy_profile.save(cur, {"templates": text})
        c.commit()
        c.close()
        print("bootstrap: templates loaded", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: templates not loaded ({type(e).__name__}: {e})", flush=True)
    install_status.mark("refreshing prices", "Oracle's public price list")
    try:
        import profile as tenancy_profile
        tenancy_profile.refresh_prices()
        print("bootstrap: prices refreshed from Oracle's price list", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: prices not refreshed ({type(e).__name__}: {e})", flush=True)
    import os as _os
    install_status.mark("ready", "sign in at app_url" if _os.environ.get("SBX_APP_URL") else "application installed")


if __name__ == "__main__":
    bootstrap()
