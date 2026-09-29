#!/usr/bin/env python3
"""Publish the public face of Sandbox Factory.

    python publish_public.py            # sync files, commit, push
    python publish_public.py --release v1.0.3

The source is private (ashishsinha1602/oci-sandbox-factory-src). The public
repository (ashishsinha1602/oci-sandbox-factory) holds only what an installer
needs: README, LICENSE, the prerequisites, the Terraform, the examples, and
the release zips the Deploy button downloads. Copyright (c) 2026 Ashish Sinha.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PUBLIC = "https://github.com/ashishsinha1602/oci-sandbox-factory.git"
FILES = {                                  # source path -> public path
    "public/README.md": "README.md",
    "LICENSE": "LICENSE",
    "docs/PREREQUISITES.md": "docs/PREREQUISITES.md",
    "docs/SECURITY.md": "docs/SECURITY.md",
    "docs/FREE-TIER.md": "docs/FREE-TIER.md",
}
TREES = ["foundation", "stacks/sandbox", "examples"]   # public: the Terraform users install, and the examples
SKIP = {".terraform", "terraform.tfstate", "terraform.tfstate.backup", "__pycache__", "dist"}
SKIP_SUFFIX = {".tfvars", ".tfstate", ".tfplan", ".log", ".zip"}


def run(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", help="also publish a GitHub release with the stack zips, e.g. v1.0.3")
    a = ap.parse_args()
    work = pathlib.Path(tempfile.mkdtemp(prefix="sbx-public-"))
    run("git", "-c", "protocol.version=1", "clone", "-q", PUBLIC, str(work))
    for p in work.iterdir():                      # the public tree is regenerated from scratch
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for src, dst in FILES.items():
        (work / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / src, work / dst)
    for tree in TREES:
        for f in (HERE / tree).rglob("*"):
            rel = f.relative_to(HERE)
            if not f.is_file() or any(part in SKIP for part in rel.parts) or f.suffix in SKIP_SUFFIX:
                continue
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, work / rel)
    ex = work / "foundation" / "personal.tfvars.example"
    if ex.exists():
        ex.write_text("\n".join(l for l in ex.read_text(encoding="utf-8").splitlines() if "Liberty" not in l) + "\n", encoding="utf-8")
    run("git", "add", "-A", cwd=work)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work).returncode:
        run("git", "-c", "user.name=Ashish Sinha", "-c", "user.email=89672346+ashishsinha1602@users.noreply.github.com",
            "commit", "-q", "-m", "Publish " + (a.release or "update"), cwd=work)
        run("git", "-c", "protocol.version=1", "push", "-q", "origin", "HEAD:main", cwd=work)
        print("public repository updated")
    else:
        print("public repository already current")
    if a.release:
        run(sys.executable, str(HERE / "release.py"))
        run("gh", "release", "create", a.release, str(HERE / "dist" / "sandbox-factory-foundation.zip"),
            str(HERE / "dist" / "sandbox-factory-sandbox.zip"), "--repo", "ashishsinha1602/oci-sandbox-factory",
            "--target", "main", "--title", a.release, "--notes", "Install with the Deploy to Oracle Cloud button in the README.")
        print("released", a.release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
