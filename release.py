#!/usr/bin/env python3
"""Build the Resource Manager stack zips that people install from.

Resource Manager takes a zip of Terraform plus a schema.yaml and renders the
schema as a form. Two zips ship:

    sandbox-factory-foundation.zip   one-time tenancy setup
    sandbox-factory-sandbox.zip      one sandbox (also what the worker uploads)

    python release.py                       # build into dist/
    python release.py --check               # verify contents, build nothing

Attach both to a GitHub release, then the "Deploy to Oracle Cloud" button in
the README points at the foundation zip's download URL.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
DIST = HERE / "dist"

STACKS = {
    "sandbox-factory-foundation": HERE / "foundation",
    "sandbox-factory-sandbox": HERE / "stacks" / "sandbox",
}

# Never ship state, credentials, or a developer's own variable values.
EXCLUDE_NAMES = {
    ".terraform", ".terraform.lock.hcl", "terraform.tfstate",
    "terraform.tfstate.backup", "dist", "__pycache__",
}
EXCLUDE_SUFFIXES = {".tfvars", ".tfplan", ".tfstate", ".log", ".zip"}
KEEP_ANYWAY = {"personal.tfvars.example"}


def wanted(path: pathlib.Path, root: pathlib.Path) -> bool:
    rel = path.relative_to(root)
    if any(part in EXCLUDE_NAMES or part.startswith(".terraform") for part in rel.parts):
        return False
    if path.name in KEEP_ANYWAY:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return path.is_file()


def files_for(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*") if wanted(p, root))


def check(name: str, root: pathlib.Path) -> list[str]:
    problems = []
    if not (root / "schema.yaml").exists():
        problems.append(f"{name}: no schema.yaml - Resource Manager will not render a form")
    if not any(p.suffix == ".tf" for p in files_for(root)):
        problems.append(f"{name}: no .tf files")
    for p in files_for(root):
        if p.suffix in {".tfvars", ".tfstate"}:
            problems.append(f"{name}: would ship {p.name}")
    return problems


def build(name: str, root: pathlib.Path) -> pathlib.Path:
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{name}.zip"
    members = files_for(root)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in members:
            z.write(p, p.relative_to(root).as_posix())
    print(f"{out.relative_to(HERE)}  {len(members)} files  {out.stat().st_size // 1024} KB")
    return out


def check_app_export() -> list[str]:
    """The APEX application that ships in the worker image must be the page in
    the repository: a fresh install once got an export older than today's
    page (old model picker, no computed prices, no build-intent guard)."""
    import re
    problems = []
    exp = (HERE / "factory" / "apex_export_customized.sql").read_text(encoding="utf-8")
    blob = "".join(re.findall(r"g_varchar2_table\(\d+\)\s*:=\s*'([0-9A-F]+)'", exp))
    js = (HERE / "factory" / "apex_home" / "sf.js").read_bytes().replace(bytes([13, 10]), bytes([10]))
    crlf = js.replace(bytes([10]), bytes([13, 10]))
    if js.hex().upper() not in blob and crlf.hex().upper() not in blob:
        problems.append("factory/apex_export_customized.sql does not contain the current apex_home/sf.js - re-run apex_customize.py and commit the export")
    ajax = (HERE / "factory" / "apex_home" / "ajax.plsql").read_text(encoding="utf-8")
    for marker in re.findall(r"^\s*function (\w+)", ajax, re.M)[:50]:
        if marker not in exp:
            problems.append(f"export is missing ajax function {marker}")
    return problems


def check_columns() -> list[str]:
    """Every column the page's server code writes must be one the control
    database creates on a fresh install (controldb.DDL + COLUMNS). A column
    known only to an upgrade path broke every fresh install on 2026-09-26."""
    import re
    sys.path.insert(0, str(HERE / "factory"))
    import controldb
    known = set(re.findall(r"^\s*(\w+)\s+(?:number|varchar2|clob|timestamp|date)", controldb.DDL, re.M | re.I))
    known |= {c for c, _ in controldb.COLUMNS}
    ajax = (HERE / "factory" / "apex_home" / "ajax.plsql").read_text(encoding="utf-8")
    used = set()
    for m in re.finditer(r"insert into sandbox_requests\s*\(([^)]*)\)", ajax, re.I):
        used |= {c.strip().lower() for c in m.group(1).split(",")}
    return [f"ajax.plsql writes column {c} that a fresh control database does not have" for c in sorted(used - {k.lower() for k in known})]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify only, build nothing")
    args = ap.parse_args()

    problems = [p for name, root in STACKS.items() for p in check(name, root)] + check_app_export() + check_columns()
    if problems:
        for p in problems:
            print("PROBLEM:", p, file=sys.stderr)
        return 1
    print("both stacks look shippable; the shipped application matches the page source")
    if args.check:
        return 0
    for name, root in STACKS.items():
        build(name, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
