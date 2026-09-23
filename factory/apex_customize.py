#!/usr/bin/env python3
"""Polish the wizard-generated request form and re-import the application.

The Create App wizard produces plain text fields for every column. This
script exports the app with APEX_EXPORT, rewrites the page-3 form items in
the export text (switches, select lists, defaults, read-only status/log,
no timestamp columns), and installs the result over the same application id.

    python apex_customize.py            # export, rewrite, import
    python apex_customize.py --dry-run  # export + rewrite, write the SQL, do not import
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import controldb

APP_NAME = "Sandbox Factory"
FORM_PAGE = 3
HERE = pathlib.Path(__file__).resolve().parent

YES_NO = ("P3_ENABLE_ADB", "P3_ENABLE_KAFKA", "P3_ENABLE_APP")
SELECT_LISTS = {
    "P3_ACTION": ("STATIC2:Create sandbox;CREATE,Deploy app from Git;DEPLOY,Destroy sandbox;DESTROY", "CREATE"),
    "P3_ADB_TIER": ("STATIC2:Always Free;free,Paid (ECPU, private endpoint);paid", "free"),
    "P3_KAFKA_MODE": ("STATIC2:Streaming (serverless, Kafka API);streaming,Managed cluster (billed hourly);cluster", "streaming"),
}
DEFAULTS = {
    "P3_TTL_DAYS": "7",
    "P3_APP_PORT": "8080",
    "P3_APP_IMAGE": "docker.io/library/nginx:alpine",
    "P3_STATUS": "QUEUED",
}
READ_ONLY = ("P3_STATUS", "P3_OUTPUTS", "P3_ERROR", "P3_LOG")
DROP = ("P3_CREATED_AT", "P3_STARTED_AT", "P3_FINISHED_AT")
LABELS = {
    "P3_REQUESTER": "Requester", "P3_SANDBOX_ID": "Sandbox id", "P3_ACTION": "Action",
    "P3_TTL_DAYS": "Lifetime (days)", "P3_ENABLE_ADB": "Autonomous Database", "P3_ADB_TIER": "ADB tier",
    "P3_ENABLE_KAFKA": "Kafka", "P3_KAFKA_MODE": "Kafka mode", "P3_ENABLE_APP": "Containerised app",
    "P3_APP_IMAGE": "Container image", "P3_GIT_URL": "Git URL (Dockerfile at repo root)", "P3_APP_PORT": "App port",
    "P3_REQUEST_TEXT": "Describe what you need (optional)", "P3_STATUS": "Status",
    "P3_OUTPUTS": "Outputs", "P3_ERROR": "Error", "P3_LOG": "Log",
}
HELP = {
    "P3_SANDBOX_ID": "2-20 chars, lowercase letters, digits, dashes. Becomes the compartment and stack name.",
    "P3_TTL_DAYS": "The sandbox is destroyed automatically after this many days.",
    "P3_GIT_URL": "Only for Deploy. The repository must have a Dockerfile at its root; it is built for ARM (A1) and pushed to your registry.",
    "P3_APP_IMAGE": "Only for Create with an app. Any public image; multi-arch or linux/arm64.",
}

ITEM_RE = re.compile(r"wwv_flow_imp_page\.create_page_item\(\n(.*?)\n\);", re.S)


def app_id() -> int:
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute("select application_id from apex_applications where workspace = :1 and application_name = :2",
                [controldb.WORKSPACE, APP_NAME])
    row = cur.fetchone()
    conn.close()
    if not row:
        raise SystemExit("application not found; run apex_builder.py first")
    return row[0]


def export_app(app: int) -> str:
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute("begin apex_util.set_workspace(p_workspace => :1); end;", [controldb.WORKSPACE])
    cur.execute("select contents from table(apex_export.get_application(p_application_id => :1))", [app])
    contents = cur.fetchone()[0]
    text = contents.read() if hasattr(contents, "read") else contents
    conn.close()
    return text


def set_param(body: str, name: str, value: str) -> str:
    """Set p_<name>=>value inside one create_page_item parameter list (replace or append)."""
    if name == "p_attributes":  # multi-line value ending in )).to_clob
        pat = re.compile(r"^,p_attributes=>wwv_flow_t_plugin_attributes\(.*?\)\)\.to_clob$", re.M | re.S)
    else:
        pat = re.compile(rf"^,{name}=>.*$", re.M)
    line = f",{name}=>{value}"
    return pat.sub(line, body, count=1) if pat.search(body) else body + "\n" + line


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def attrs(pairs: list[tuple[str, str]]) -> str:
    inner = ",\n".join(f"  {q(k)}, {q(v)}" for k, v in pairs)
    return f"wwv_flow_t_plugin_attributes(wwv_flow_t_varchar2(\n{inner})).to_clob"


def rewrite_item(body: str) -> str | None:
    name = re.search(r",p_name=>'([^']+)'", body).group(1)
    if name in DROP:
        return None
    if name in LABELS:
        body = set_param(body, "p_prompt", q(LABELS[name]))
    if name in HELP:
        body = set_param(body, "p_help_text", q(HELP[name]))
    if name in YES_NO:
        body = set_param(body, "p_display_as", "'NATIVE_YES_NO'")
        body = re.sub(r"^,p_cSize=>.*\n|^,p_cMaxlength=>.*\n", "", body, flags=re.M)
        body = set_param(body, "p_item_default", "'N'")
        body = set_param(body, "p_item_default_type", "'STATIC_TEXT_WITH_SUBSTITUTIONS'")
        body = set_param(body, "p_attributes", attrs([
            ("off_label", "No"), ("off_value", "N"), ("on_label", "Yes"), ("on_value", "Y"), ("use_defaults", "N")]))
    if name in SELECT_LISTS:
        lov, default = SELECT_LISTS[name]
        body = set_param(body, "p_display_as", "'NATIVE_SELECT_LIST'")
        body = re.sub(r"^,p_cSize=>.*\n|^,p_cMaxlength=>.*\n", "", body, flags=re.M)
        body = set_param(body, "p_lov", q(lov))
        body = set_param(body, "p_lov_display_null", "'NO'")
        body = set_param(body, "p_item_default", q(default))
        body = set_param(body, "p_item_default_type", "'STATIC_TEXT_WITH_SUBSTITUTIONS'")
        body = set_param(body, "p_attributes", attrs([("page_action_on_selection", "NONE")]))
    if name in DEFAULTS:
        body = set_param(body, "p_item_default", q(DEFAULTS[name]))
        body = set_param(body, "p_item_default_type", "'STATIC_TEXT_WITH_SUBSTITUTIONS'")
    if name == "P3_REQUESTER":
        body = set_param(body, "p_item_default", "'&APP_USER.'")
        body = set_param(body, "p_item_default_type", "'STATIC_TEXT_WITH_SUBSTITUTIONS'")
    if name in ("P3_APP_IMAGE", "P3_GIT_URL"):
        body = set_param(body, "p_display_as", "'NATIVE_TEXT_FIELD'")
        body = re.sub(r"^,p_cHeight=>.*\n", "", body, flags=re.M)
        body = set_param(body, "p_cSize", "60")
        body = set_param(body, "p_attributes", attrs([
            ("disabled", "N"), ("submit_when_enter_pressed", "N"), ("subtype", "TEXT"), ("trim_spaces", "BOTH")]))
    if name in READ_ONLY:
        body = set_param(body, "p_read_only_when_type", "'ALWAYS'")
    return body


def rewrite(text: str) -> str:
    start = text.find(f"prompt --application/pages/page_{FORM_PAGE:05d}")
    end = text.find("prompt --application/pages/page_", start + 10)
    if start < 0:
        raise SystemExit("form page not found in export")
    page = text[start:end]

    def repl(m):
        new = rewrite_item(m.group(1))
        return "" if new is None else f"wwv_flow_imp_page.create_page_item(\n{new}\n);"

    page = ITEM_RE.sub(repl, page)
    return text[:start] + page + text[end:]


def install(text: str, app: int) -> None:
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute(f"""
        begin
          apex_application_install.set_workspace(:ws);
          apex_application_install.set_application_id(:app);
          apex_application_install.set_schema(:schema);
          apex_application_install.set_application_alias('SANDBOX-FACTORY');
        end;""", ws=controldb.WORKSPACE, app=app, schema=controldb.SCHEMA)
    blocks = re.split(r"^/\s*$", text, flags=re.M)
    n = 0
    for block in blocks:
        lines = [l for l in block.splitlines() if not re.match(r"^(prompt|set |whenever|@)", l)]
        code = "\n".join(lines).strip()
        if not re.search(r"\bbegin\b", code, re.I):
            continue
        cur.execute(code)
        n += 1
    conn.commit()
    conn.close()
    print(f"installed {n} blocks into application {app}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    app = app_id()
    text = export_app(app)
    (HERE / "apex_export_original.sql").write_text(text, encoding="utf-8")
    new = rewrite(text)
    (HERE / "apex_export_customized.sql").write_text(new, encoding="utf-8")
    print(f"exported app {app}: {len(text)} chars -> customised {len(new)} chars")
    if a.dry_run:
        return
    install(new, app)


if __name__ == "__main__":
    main()
