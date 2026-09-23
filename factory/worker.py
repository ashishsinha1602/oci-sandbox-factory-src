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
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

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


def claim(conn):
    cur = conn.cursor()
    cur.execute("select min(id) from sbx.sandbox_requests where status = 'QUEUED'")
    candidate = cur.fetchone()[0]
    if candidate is None:
        return None
    cur.execute("select id from sbx.sandbox_requests where id = :1 and status = 'QUEUED' for update skip locked", [candidate])
    row = cur.fetchone()
    if not row:
        conn.rollback()
        return None
    cur.execute("update sbx.sandbox_requests set status = 'RUNNING', started_at = systimestamp where id = :1", [row[0]])
    conn.commit()
    cur.execute("""
        select id, requester, sandbox_id, action, ttl_days, enable_adb, adb_tier, enable_kafka,
               kafka_mode, enable_app, app_image, git_url, app_port, request_text
        from sbx.sandbox_requests where id = :1""", [row[0]])
    cols = [d[0].lower() for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


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


def factory_args(req: dict) -> argparse.Namespace:
    return argparse.Namespace(
        sandbox_id=req["sandbox_id"], owner=req["requester"], team="hackathon",
        ttl=int(req["ttl_days"] or 7), allowed_cidr="0.0.0.0/0",
        adb=req["enable_adb"] == "Y", adb_tier=req["adb_tier"] or "free", adb_workload="OLTP",
        kafka=req["enable_kafka"] == "Y", kafka_mode=req["kafka_mode"] or "streaming", topics="events",
        app=req["enable_app"] == "Y", image=req["app_image"] or "docker.io/library/nginx:alpine",
        shape="CI.Standard.A1.Flex", port=int(req["app_port"] or 80),
        name="app", tag=None, env=None, keep_stack=False, dry_run=False, path=None,
    )


def handle(req: dict) -> dict:
    args = factory_args(req)
    if req["action"] == "DESTROY":
        sf.cmd_destroy(args)
        return {"destroyed": req["sandbox_id"]}
    if req["action"] == "DEPLOY":
        if not req["git_url"]:
            raise ValueError("DEPLOY needs git_url")
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sbx-deploy-"))
        try:
            print(f"cloning {req['git_url']}")
            subprocess.run(["git", "clone", "--depth", "1", req["git_url"], str(tmp / "src")], check=True)
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
            outputs = handle(req)
        finish(conn, req["id"], True, outputs)
        print(f"  request {req['id']} DONE")
    except SystemExit as e:
        log.flush_row()
        finish(conn, req["id"], False, error=str(e))
        print(f"  request {req['id']} FAILED: {e}")
    except Exception as e:  # noqa: BLE001
        log.write(traceback.format_exc())
        finish(conn, req["id"], False, error=f"{type(e).__name__}: {e}")
        print(f"  request {req['id']} FAILED: {e}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    conn = controldb.connect("ADMIN")
    print("worker connected to control DB; polling sbx.sandbox_requests")
    last_reap = 0.0
    while True:
        if time.time() - last_reap > REAP_SECONDS:
            last_reap = time.time()
            try:
                print(f"[{dt.datetime.now():%H:%M:%S}] reaper: checking for expired sandboxes")
                sf.cmd_reap(argparse.Namespace(dry_run=False))
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
