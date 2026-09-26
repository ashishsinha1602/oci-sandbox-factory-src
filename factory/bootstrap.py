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


def bootstrap() -> None:
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute("select count(*) from dba_users where username = :1", [controldb.SCHEMA])
    if not cur.fetchone()[0]:
        print("bootstrap: control schema missing; running controldb.setup()", flush=True)
        conn.close()
        controldb.setup([])
        conn = controldb.connect("ADMIN")
        cur = conn.cursor()
    cur.execute("select application_id from apex_applications where workspace = :1 and application_name = :2",
                [controldb.WORKSPACE, "Sandbox Factory"])
    row = cur.fetchone()
    conn.close()
    if not row:
        export = HERE / "apex_export_customized.sql"
        if not export.exists():
            print("bootstrap: no apex_export_customized.sql shipped; skipping the APEX import", flush=True)
        else:
            import apex_customize
            print("bootstrap: installing the Sandbox Factory application", flush=True)
            apex_customize.install(export.read_text(encoding="utf-8"), 112)
    else:
        print(f"bootstrap: application {row[0]} present", flush=True)
    import prompts
    for line in prompts.load():
        print("bootstrap: prompt", line, flush=True)
    # what this install is: region, registry, the chat models that answer here
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
    try:
        import profile as tenancy_profile
        tenancy_profile.refresh_prices()
        print("bootstrap: prices refreshed from Oracle's price list", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: prices not refreshed ({type(e).__name__}: {e})", flush=True)


if __name__ == "__main__":
    bootstrap()
