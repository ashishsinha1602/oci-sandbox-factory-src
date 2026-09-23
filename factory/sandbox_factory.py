#!/usr/bin/env python3
"""Sandbox Factory: create, list, destroy, reap and deploy sandboxes.

Every sandbox is one OCI Resource Manager stack built from ../stacks/sandbox.
Resource Manager runs Terraform, keeps the state, and shows the same form in
the console. This CLI is the thin layer a form, a chat agent or a cron job
calls.

    python sandbox_factory.py create demo1 --adb --kafka --app
    python sandbox_factory.py deploy demo2 ./myapp --port 8080
    python sandbox_factory.py list
    python sandbox_factory.py destroy demo1
    python sandbox_factory.py reap            # destroy everything past sbx.expires

Auth: ~/.oci/config (OCI_CLI_PROFILE selects the profile). Foundation OCIDs
come from foundation.json next to this file, generated from the foundation
stack's outputs on first run.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import zipfile

import oci
from oci.resource_manager import models as rmm

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACK_DIR = ROOT / "stacks" / "sandbox"
FOUNDATION_DIR = ROOT / "foundation"
FOUNDATION_JSON = pathlib.Path(__file__).resolve().parent / "foundation.json"
TF_VERSION = "1.5.x"
POLL_SECONDS = 15
ZIP_EXCLUDE = (".terraform", ".terraform.lock.hcl", ".tfstate", ".tfplan", ".tfvars", "__pycache__")


# ---------------------------------------------------------------------------
# OCI plumbing
# ---------------------------------------------------------------------------

def config() -> dict:
    return oci.config.from_file(profile_name=os.environ.get("OCI_CLI_PROFILE", "DEFAULT"))


def foundation() -> dict:
    """OCIDs produced by the foundation stack. Cached in foundation.json."""
    if FOUNDATION_JSON.exists():
        return json.loads(FOUNDATION_JSON.read_text())
    tf = shutil.which("terraform") or str(pathlib.Path.home() / "bin" / "terraform.exe")
    raw = subprocess.check_output([tf, "output", "-json"], cwd=FOUNDATION_DIR, text=True)
    out = json.loads(raw)
    data = {
        "compartments": out["compartments"]["value"],
        "network": out["network"]["value"],
        "tag_namespace": out["tag_namespace"]["value"],
    }
    FOUNDATION_JSON.write_text(json.dumps(data, indent=2))
    return data


def zip_stack_b64() -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in STACK_DIR.rglob("*"):
            rel = path.relative_to(STACK_DIR)
            if path.is_dir() or any(part in ZIP_EXCLUDE or part.endswith(ZIP_EXCLUDE) for part in rel.parts):
                continue
            z.write(path, rel.as_posix())
    return base64.b64encode(buf.getvalue()).decode()


def stack_name(sandbox_id: str) -> str:
    return f"sbx-{sandbox_id}"


def find_stack(rm, control_compartment: str, sandbox_id: str):
    stacks = rm.list_stacks(
        compartment_id=control_compartment,
        display_name=stack_name(sandbox_id),
        lifecycle_state="ACTIVE",
    ).data
    return stacks[0] if stacks else None


def run_job(rm, stack_id: str, operation: str, label: str) -> oci.resource_manager.models.Job:
    details = {
        "PLAN": rmm.CreatePlanJobOperationDetails(),
        "APPLY": rmm.CreateApplyJobOperationDetails(execution_plan_strategy="AUTO_APPROVED"),
        "DESTROY": rmm.CreateDestroyJobOperationDetails(execution_plan_strategy="AUTO_APPROVED"),
    }[operation]
    job = rm.create_job(rmm.CreateJobDetails(
        stack_id=stack_id,
        display_name=f"{label}-{operation.lower()}-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
        job_operation_details=details,
    )).data
    print(f"  {operation} job {job.id.split('.')[-1][-8:]} started", flush=True)
    started = time.time()
    while True:
        job = rm.get_job(job.id).data
        if job.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED"):
            break
        print(f"  ... {job.lifecycle_state.lower()} {int(time.time() - started)}s", flush=True)
        time.sleep(POLL_SECONDS)
    print(f"  {operation} {job.lifecycle_state} after {int(time.time() - started)}s")
    if job.lifecycle_state != "SUCCEEDED":
        log = rm.get_job_logs_content(job.id).data
        print("\n".join(log.splitlines()[-40:]), file=sys.stderr)
        raise SystemExit(f"{operation} failed")
    return job


def job_outputs(rm, job_id: str) -> dict:
    items = rm.list_job_outputs(job_id).data.items
    out = {}
    for o in items:
        if o.is_sensitive:
            out[o.output_name] = "<sensitive>"
            continue
        try:
            out[o.output_name] = json.loads(o.output_value)
        except (TypeError, ValueError):
            out[o.output_name] = o.output_value
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def build_variables(args, fnd: dict, cfg: dict, app_containers: list | None) -> dict:
    """Resource Manager variables are strings; lists/objects go as JSON."""
    v = {
        "tenancy_ocid": cfg["tenancy"],
        "region": cfg["region"],
        "compartment_ocid": fnd["compartments"]["control"],
        "sandboxes_compartment_ocid": fnd["compartments"]["sandboxes"],
        "vcn_id": fnd["network"]["vcn_id"],
        "public_subnet_id": fnd["network"]["public_subnet_id"],
        "private_subnet_id": fnd["network"]["private_subnet_id"],
        "tag_namespace": fnd["tag_namespace"],
        "sandbox_id": args.sandbox_id,
        "owner": args.owner,
        "team": args.team,
        "ttl_days": str(args.ttl),
        "allowed_cidr": args.allowed_cidr,
        "enable_adb": json.dumps(bool(args.adb)),
        "adb_tier": args.adb_tier,
        "adb_workload": args.adb_workload,
        "enable_kafka": json.dumps(bool(args.kafka)),
        "kafka_mode": args.kafka_mode,
        "kafka_topics": json.dumps(args.topics.split(",")),
        "enable_app": json.dumps(app_containers is not None),
    }
    if app_containers is not None:
        v["app_containers"] = json.dumps(app_containers)
        v["app_shape"] = args.shape
    return v


def cmd_create(args, app_containers: list | None = None) -> dict:
    cfg = config()
    fnd = foundation()
    rm = oci.resource_manager.ResourceManagerClient(cfg)
    control = fnd["compartments"]["control"]

    if args.app and app_containers is None:
        app_containers = [{"name": "web", "image": args.image, "port": args.port, "env": {}}]

    variables = build_variables(args, fnd, cfg, app_containers)
    expires = (dt.date.today() + dt.timedelta(days=args.ttl)).isoformat()
    tags = {"sandbox_id": args.sandbox_id, "owner": args.owner, "expires": expires, "managed_by": "sandbox-factory"}

    existing = find_stack(rm, control, args.sandbox_id)
    if existing:
        print(f"Updating stack {existing.display_name}")
        rm.update_stack(existing.id, rmm.UpdateStackDetails(
            config_source=rmm.UpdateZipUploadConfigSourceDetails(zip_file_base64_encoded=zip_stack_b64()),
            variables=variables,
            freeform_tags=tags,
        ))
        stack_id = existing.id
    else:
        print(f"Creating stack {stack_name(args.sandbox_id)}")
        stack = rm.create_stack(rmm.CreateStackDetails(
            compartment_id=control,
            display_name=stack_name(args.sandbox_id),
            description=f"Sandbox {args.sandbox_id} for {args.owner}. Expires {expires}.",
            config_source=rmm.CreateZipUploadConfigSourceDetails(zip_file_base64_encoded=zip_stack_b64()),
            variables=variables,
            terraform_version=TF_VERSION,
            freeform_tags=tags,
        )).data
        stack_id = stack.id

    job = run_job(rm, stack_id, "APPLY", args.sandbox_id)
    outputs = job_outputs(rm, job.id)
    print(json.dumps(outputs, indent=2))
    return outputs


def cmd_list(args) -> list:
    cfg = config()
    fnd = foundation()
    rm = oci.resource_manager.ResourceManagerClient(cfg)
    rows = []
    for s in rm.list_stacks(compartment_id=fnd["compartments"]["control"], lifecycle_state="ACTIVE").data:
        if s.freeform_tags.get("managed_by") != "sandbox-factory":
            continue
        rows.append({
            "sandbox_id": s.freeform_tags.get("sandbox_id"),
            "owner": s.freeform_tags.get("owner"),
            "expires": s.freeform_tags.get("expires"),
            "created": s.time_created.date().isoformat(),
            "stack_id": s.id,
        })
    rows.sort(key=lambda r: r["expires"] or "")
    if not rows:
        print("No sandboxes.")
    for r in rows:
        print(f"{r['sandbox_id']:<20} {r['owner']:<32} expires {r['expires']}  created {r['created']}")
    return rows


def destroy_stack(rm, stack, keep_stack: bool = False):
    run_job(rm, stack.id, "DESTROY", stack.freeform_tags.get("sandbox_id", stack.display_name))
    if not keep_stack:
        rm.delete_stack(stack.id)
        print(f"  stack {stack.display_name} deleted")


def cmd_destroy(args):
    cfg = config()
    fnd = foundation()
    rm = oci.resource_manager.ResourceManagerClient(cfg)
    stack = find_stack(rm, fnd["compartments"]["control"], args.sandbox_id)
    if not stack:
        raise SystemExit(f"No sandbox named {args.sandbox_id}")
    print(f"Destroying {stack.display_name}")
    destroy_stack(rm, stack, keep_stack=args.keep_stack)


def cmd_reap(args):
    """Destroy every sandbox whose expires tag is before today."""
    cfg = config()
    fnd = foundation()
    rm = oci.resource_manager.ResourceManagerClient(cfg)
    today = dt.date.today().isoformat()
    victims = []
    for s in rm.list_stacks(compartment_id=fnd["compartments"]["control"], lifecycle_state="ACTIVE").data:
        exp = s.freeform_tags.get("expires")
        if s.freeform_tags.get("managed_by") == "sandbox-factory" and exp and exp < today:
            victims.append(s)
    if not victims:
        print("Nothing expired.")
        return
    for s in victims:
        print(f"{s.freeform_tags['sandbox_id']} expired {s.freeform_tags['expires']}")
        if not args.dry_run:
            destroy_stack(rm, s)


# ---------------------------------------------------------------------------
# deploy: local Dockerfile -> OCIR -> sandbox
# ---------------------------------------------------------------------------

def container_tool() -> str:
    for tool in ("docker", "podman"):
        if shutil.which(tool):
            return tool
    raise SystemExit("Need docker or podman on PATH.")


def ocir_login(cfg: dict, region_key: str, namespace: str) -> str:
    """Log the container tool into OCIR. Token from OCIR_TOKEN or ~/.oci/ocir_token."""
    token = os.environ.get("OCIR_TOKEN")
    token_file = pathlib.Path.home() / ".oci" / "ocir_token"
    if not token and token_file.exists():
        token = token_file.read_text().strip()
    if not token:
        raise SystemExit(
            "No OCIR auth token. Create one in the console (Profile > Auth tokens) and put it in\n"
            f"  {token_file}   or   OCIR_TOKEN env var"
        )
    idc = oci.identity.IdentityClient(cfg)
    user = idc.get_user(cfg["user"]).data.name
    registry = f"{region_key}.ocir.io"
    tool = container_tool()
    subprocess.run(
        [tool, "login", registry, "-u", f"{namespace}/{user}", "--password-stdin"],
        input=token, text=True, check=True, capture_output=True,
    )
    return registry


def ensure_public_repo(cfg: dict, compartment_id: str, repo: str):
    """A public OCIR repo lets Container Instances pull without a pull secret."""
    art = oci.artifacts.ArtifactsClient(cfg)
    existing = art.list_container_repositories(compartment_id=compartment_id, display_name=repo).data.items
    if existing:
        return existing[0]
    return art.create_container_repository(oci.artifacts.models.CreateContainerRepositoryDetails(
        compartment_id=compartment_id, display_name=repo, is_public=True,
        freeform_tags={"managed_by": "sandbox-factory"},
    )).data


def cmd_deploy(args):
    cfg = config()
    fnd = foundation()
    path = pathlib.Path(args.path).resolve()
    if not (path / "Dockerfile").exists():
        raise SystemExit(f"No Dockerfile in {path}")

    namespace = oci.object_storage.ObjectStorageClient(cfg).get_namespace().data
    region_key = cfg["region"].split("-")[1][:3]  # us-phoenix-1 -> phx
    registry = ocir_login(cfg, region_key, namespace)

    repo = f"sbx/{args.sandbox_id}/{args.name}"
    ensure_public_repo(cfg, fnd["compartments"]["control"], repo)
    tag = args.tag or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    image = f"{registry}/{namespace}/{repo}:{tag}"

    platform = "linux/arm64" if args.shape.startswith("CI.Standard.A1") else "linux/amd64"
    tool = container_tool()
    print(f"Building {image} for {platform}")
    if tool == "docker":
        subprocess.run(["docker", "buildx", "build", "--platform", platform, "-t", image, "--push", str(path)], check=True)
    else:
        subprocess.run(["podman", "build", "--platform", platform, "-t", image, str(path)], check=True)
        subprocess.run(["podman", "push", image], check=True)

    env = dict(kv.split("=", 1) for kv in (args.env or []))
    containers = [{"name": args.name, "image": image, "port": args.port, "env": env}]
    args.app = True
    return cmd_create(args, app_containers=containers)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_sandbox_options(p):
    p.add_argument("sandbox_id")
    p.add_argument("--owner", default=os.environ.get("SBX_OWNER", ""), help="email; SBX_OWNER env")
    p.add_argument("--team", default="personal")
    p.add_argument("--ttl", type=int, default=7, help="days until auto-destroy")
    p.add_argument("--allowed-cidr", default="0.0.0.0/0")
    p.add_argument("--adb", action="store_true")
    p.add_argument("--adb-tier", choices=["free", "paid"], default="free")
    p.add_argument("--adb-workload", choices=["OLTP", "DW", "AJD", "APEX"], default="OLTP")
    p.add_argument("--kafka", action="store_true")
    p.add_argument("--kafka-mode", choices=["streaming", "cluster"], default="streaming")
    p.add_argument("--topics", default="events", help="comma-separated")
    p.add_argument("--shape", default="CI.Standard.A1.Flex")
    p.add_argument("--port", type=int, default=80)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create or update a sandbox")
    add_sandbox_options(c)
    c.add_argument("--app", action="store_true")
    c.add_argument("--image", default="docker.io/library/nginx:alpine")
    c.set_defaults(fn=cmd_create)

    d = sub.add_parser("deploy", help="build a local Dockerfile, push to OCIR, run it in a sandbox")
    add_sandbox_options(d)
    d.add_argument("path", help="folder with a Dockerfile")
    d.add_argument("--name", default="app")
    d.add_argument("--tag")
    d.add_argument("--env", action="append", help="KEY=VALUE, repeatable")
    d.set_defaults(fn=cmd_deploy)

    l = sub.add_parser("list", help="list sandboxes")
    l.set_defaults(fn=cmd_list)

    x = sub.add_parser("destroy", help="destroy a sandbox now")
    x.add_argument("sandbox_id")
    x.add_argument("--keep-stack", action="store_true")
    x.set_defaults(fn=cmd_destroy)

    r = sub.add_parser("reap", help="destroy every sandbox past its expiry")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_reap)

    args = ap.parse_args(argv)
    if getattr(args, "owner", None) == "" and args.cmd in ("create", "deploy"):
        ap.error("--owner is required (or set SBX_OWNER)")
    args.fn(args)


if __name__ == "__main__":
    main()
