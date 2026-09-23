#!/usr/bin/env python3
"""Build the Sandbox Factory APEX application through the App Builder wizard.

APEX has no public API for creating applications, so this drives the builder
UI with Playwright (verified on APEX 26.1 / Autonomous Database):

  1. sign in to the database SSO page as the SBX schema user
  2. pick workspace SBX
  3. App Builder > Create > Use Create App Wizard
  4. add "Requests" (interactive report + form on SANDBOX_REQUESTS)
     and "Sandboxes" (interactive report on SANDBOXES_V)
  5. save the wizard's blueprint JSON next to this file, create the app

    SBX_SECRETS_FILE=<json written by controldb.py setup> python apex_builder.py [--headed]

The blueprint (apex_blueprint.json) can be pasted into the wizard's
"View Blueprint" dialog on any other instance to recreate the app by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re

from playwright.sync_api import sync_playwright

import controldb

APP_NAME = "Sandbox Factory"
HERE = pathlib.Path(__file__).resolve().parent
SHOTS = pathlib.Path(os.environ.get("SBX_SHOTS", HERE / "shots"))
SHOTS.mkdir(exist_ok=True)


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def login(page, apex_url, db_user, db_password):
    page.goto(apex_url, wait_until="networkidle")
    page.fill("#username-field", db_user)
    page.fill("#password-field", db_password)
    page.click("#sign-in-submit")
    page.wait_for_url(re.compile(r"select-workspace|workspace/home"), timeout=60000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    if "select-workspace" in page.url:
        page.get_by_text(re.compile(rf"^{controldb.WORKSPACE}$")).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)


def frame(page, key, tries=20):
    for _ in range(tries):
        found = [f for f in page.frames if key in f.url]
        if found:
            return found[-1]
        page.wait_for_timeout(500)
    raise RuntimeError(f"no frame matching {key}")


def dialog_open(page):
    return page.locator(".ui-dialog:visible").count() > 0


def pick_table(page, fr, table, item="#P1020_TABLE"):
    """The table picker is a read-only popup LOV; click the row with the exact name."""
    for _ in range(3):
        fr.click(item + "_lov_btn")
        page.wait_for_timeout(2500)
        for f in page.frames:
            rows = [r for r in f.query_selector_all("[role=dialog] [role=option]")
                    if r.is_visible() and r.evaluate("e=>e.textContent.trim()") == table]
            if rows:
                rows[0].click()
                break
        page.wait_for_timeout(1500)
        if fr.eval_on_selector(item, "e=>e.value") == table:
            return
    raise RuntimeError(f"could not pick {table}")


def finish_dialog(page):
    """Press the forward button of whatever wizard dialog is open until it closes."""
    for _ in range(4):
        if not dialog_open(page):
            return
        frs = [f for f in page.frames if "/create-app/" in f.url and "create-application" not in f.url]
        if not frs:
            page.wait_for_timeout(1000)
            continue
        fr = frs[-1]
        for name in ("Add Page", "Next", "Create Page", "Finish"):
            btn = fr.get_by_role("button", name=re.compile(rf"^{name}$"))
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(4000)
                break
        else:
            return


def add_report_page(page, name, table, include_form):
    page.get_by_role("link", name=re.compile("^Add Page$")).first.click()
    page.wait_for_timeout(3000)
    frame(page, "create-page-wizard").get_by_text(re.compile("^Interactive Report$")).first.click()
    page.wait_for_timeout(3000)
    fr = frame(page, "create-re")
    fr.fill("#P1020_PAGE_NAME", name)
    pick_table(page, fr, table)
    if include_form:
        cb = fr.locator("#P1020_INCLUDE_FORM")
        for _ in range(20):  # enabled once the wizard has found the primary key
            if cb.is_enabled():
                break
            page.wait_for_timeout(500)
        if cb.is_enabled() and not cb.is_checked():
            cb.click()
    fr.click("#P1_NEXT")
    page.wait_for_timeout(4000)
    finish_dialog(page)


def existing_app_id():
    conn = controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute("select application_id from apex_applications where workspace = :1 and application_name = :2",
                [controldb.WORKSPACE, APP_NAME])
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    app_id = existing_app_id()
    info = controldb.control_info()
    if app_id:
        print(f"app already exists: {app_id}")
        print(f"URL: {info['ords_url']}r/{controldb.WORKSPACE.lower()}/sandbox-factory")
        return

    secrets = json.loads(pathlib.Path(os.environ["SBX_SECRETS_FILE"]).read_text())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        page = browser.new_context(viewport={"width": 1400, "height": 1200}).new_page()
        login(page, info["apex_url"], controldb.SCHEMA, secrets["SBX_DB_PASSWORD"])

        page.get_by_role("link", name=re.compile("^App Builder")).first.click()
        page.wait_for_load_state("networkidle")
        page.get_by_role("link", name=re.compile("^Create$")).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        page.get_by_role("link", name=re.compile("Use Create App Wizard")).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)

        page.fill("#P1_APP_NAME", APP_NAME)
        add_report_page(page, "Requests", "SANDBOX_REQUESTS", include_form=True)
        add_report_page(page, "Sandboxes", "SANDBOXES_V", include_form=False)
        shot(page, "pages")

        page.get_by_role("link", name=re.compile("View Blueprint")).first.click()
        page.wait_for_timeout(3000)
        bp = frame(page, "application-blueprint")
        (HERE / "apex_blueprint.json").write_text(bp.eval_on_selector("textarea", "e=>e.value"))
        bp.get_by_role("button", name=re.compile("^Cancel$")).click()
        page.wait_for_timeout(1500)

        page.click("#CREATE_APP")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(8000)
        shot(page, "created")
        browser.close()

    app_id = existing_app_id()
    print(f"created app {app_id}")
    print(f"URL: {info['ords_url']}r/{controldb.WORKSPACE.lower()}/sandbox-factory")


if __name__ == "__main__":
    main()
