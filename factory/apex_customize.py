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
import datetime as dt
import pathlib
import re
import sys

import hashlib
import json

import controldb
from apex_home_inject import rewrite_home, split_home

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
    "P3_APP_PORT": "80",
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
        raise SystemExit(
            "The live page looks different from what this script last deployed. "
            "Someone may have edited it in the APEX builder, and deploying would discard it. "
            "Recover their version with a flashback query on "
            "apex_application_page_regions (application_id=112, page_id=1), merge it into "
            "apex_home/home.html, then re-run with --force."
        )
    STATE.write_text(json.dumps({"markup_sha": now, "live_len": live_len}), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="deploy even if the live page changed since the last deploy")
    a = ap.parse_args()
    app = app_id()
    text = export_app(app)
    # Keep every export, not just the latest: the previous behaviour overwrote the
    # only copy of the live page on each run, so a mistaken deploy was unrecoverable
    # from disk.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = HERE / "apex_backups"
    backups.mkdir(exist_ok=True)
    (backups / f"app{app}-{stamp}.sql").write_text(text, encoding="utf-8")
    (HERE / "apex_export_original.sql").write_text(text, encoding="utf-8")
    guard_live_changes(text, a.force)
    new = rewrite_home(rewrite(text))
    (HERE / "apex_export_customized.sql").write_text(new, encoding="utf-8")
    print(f"exported app {app}: {len(text)} chars -> customised {len(new)} chars")
    if a.dry_run:
        return
    install(new, app)


if __name__ == "__main__":
    main()
