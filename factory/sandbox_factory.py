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
import tempfile
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

_AUTH: dict | None = None


def _metadata_reachable(timeout: float = 0.4) -> bool:
    """Is the OCI instance metadata service one hop away? Cheap yes/no."""
    import socket
    try:
        with socket.create_connection(("169.254.169.254", 80), timeout=timeout):
            return True
    except OSError:
        return False


def auth() -> dict:
    """Keyword args for any OCI client.

    Inside OCI (Container Instance, VM, Function) the resource principal is used:
    no key file, the identity is the instance itself. Elsewhere ~/.oci/config.
    """
    global _AUTH
    if _AUTH is None:
        if os.environ.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
            # Functions
            signer = oci.auth.signers.get_resource_principals_signer()
            _AUTH = {"config": {"region": signer.region, "tenancy": signer.tenancy_id}, "signer": signer}
        else:
            # Container Instances and VMs authenticate as the instance itself.
            # This is an INSTANCE principal, not a resource principal - the worker
            # container has no ~/.oci/config, so without this it fell back to the
            # config file and every OCI call failed with key_file/user "missing".
            errors = []
            if not _metadata_reachable():
                # Off an OCI instance the SDK still retries 169.254.169.254 for
                # tens of seconds before giving up, which makes every local
                # command crawl. Probe once, cheaply, and skip straight to the
                # config file.
                _AUTH = {"config": oci.config.from_file(profile_name=os.environ.get("OCI_CLI_PROFILE", "DEFAULT"))}
                return _AUTH
            for label, make in (
                ("instance-principal", oci.auth.signers.InstancePrincipalsSecurityTokenSigner),
                ("resource-principal", oci.auth.signers.get_resource_principals_signer),
            ):
                try:
                    signer = make()
                    _AUTH = {"config": {"region": signer.region, "tenancy": signer.tenancy_id}, "signer": signer}
                    print(f"auth: using {label}", flush=True)
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{label}: {type(e).__name__}: {e}")
            else:
                for e in errors:
                    print(f"auth: {e}", flush=True)
                _AUTH = {"config": oci.config.from_file(profile_name=os.environ.get("OCI_CLI_PROFILE", "DEFAULT"))}
    return _AUTH


def config() -> dict:
    return auth()["config"]


def client(cls):
    return cls(**auth())


_SECRETS = None


def secrets_vault(fnd: dict) -> dict:
    """The shared Vault + key in sbx-control that per-sandbox secrets live in
    (the Kafka superuser password, for one). Looked up by name so no tenancy
    id needs to be configured anywhere; empty when the tenancy has none, in
    which case a Kafka cluster is built without a public endpoint."""
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = {"vault_id": "", "key_id": ""}
        try:
            kv = client(oci.key_management.KmsVaultClient)
            for v in kv.list_vaults(compartment_id=fnd["compartments"]["control"]).data:
                if v.display_name == "sbx-secrets" and v.lifecycle_state == "ACTIVE":
                    km = oci.key_management.KmsManagementClient(**auth(), service_endpoint=v.management_endpoint)
                    for k in km.list_keys(compartment_id=fnd["compartments"]["control"]).data:
                        if k.display_name == "sbx-secrets-key" and k.lifecycle_state == "ENABLED":
                            _SECRETS = {"vault_id": v.id, "key_id": k.id}
                    break
        except Exception as e:  # noqa: BLE001
            print(f"secrets vault lookup failed ({type(e).__name__}: {e}); Kafka gets no public endpoint", flush=True)
    return _SECRETS


def in_oci() -> bool:
    return "signer" in auth()


def foundation() -> dict:
    """OCIDs produced by the foundation stack. From SBX_FOUNDATION (JSON, set on the
    OCI-hosted worker), else cached in foundation.json, else terraform output."""
    if os.environ.get("SBX_FOUNDATION"):
        return json.loads(os.environ["SBX_FOUNDATION"])
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


def assert_owner(stack, owner: str | None, action: str) -> None:
    """A sandbox may only be changed by the person who created it.

    Every stack carries an `owner` freeform tag. Checking it here, rather than only
    in the APEX page, covers every route to these commands - the MCP server and the
    CLI never touch SBX.SANDBOX_REQUESTS and so never see that guard. Pass
    owner=None (the reaper, an administrator) to skip the check deliberately.
    """
    if stack is None or owner is None:
        return
    if not owner.strip():
        raise SystemExit(
            f"Refusing to {action} {stack.display_name}: the caller is not identified. "
            "Set SBX_OWNER to the person acting."
        )
    actual = (stack.freeform_tags or {}).get("owner")
    if actual and actual.casefold() != owner.casefold():
        raise SystemExit(
            f"Sandbox {stack.display_name} belongs to {actual}, not {owner}; refusing to {action} it."
        )


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


def adb_passwords_from_state(rm, stack_id: str) -> dict:
    """ADMIN passwords by module path: 'module.adb' for the primary database,
    'module.adb_extra["name"]' for each extra one. Sensitive outputs are masked
    in job outputs, so the state is the only place to read them from."""
    try:
        state = json.loads(rm.get_stack_tf_state(stack_id).data.content.decode())
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in state.get("resources", []):
        if r.get("type") == "random_password" and r.get("name") == "admin":
            # count-based modules are addressed as module.adb[0]; normalise so the
            # primary database is always "module.adb".
            mod = (r.get("module") or "").replace("module.adb[0]", "module.adb")
            out[mod] = r["instances"][0]["attributes"].get("result")
    return out


def adb_password_from_state(rm, stack_id: str) -> str | None:
    return adb_passwords_from_state(rm, stack_id).get("module.adb")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_GATEWAY_OK = None


def gateway_available(cfg: dict) -> bool:
    """Is there an API Gateway left in this region's limit (gateway-count)?

    A tenancy's default is small (5). When it is used up, a sandbox that
    asked for a gateway would fail its apply, so the app is exposed on a public
    IP instead and the card says so. Unknown (no permission, API error) counts
    as available: the apply, not this check, has the last word.
    """
    global _GATEWAY_OK
    if _GATEWAY_OK is None:
        try:
            lim = client(oci.limits.LimitsClient)
            av = lim.get_resource_availability("api-gateway", "gateway-count", cfg["tenancy"]).data
            _GATEWAY_OK = (av.available or 0) > 0
            if not _GATEWAY_OK:
                print("no API Gateway left in this region's limit (gateway-count); the app gets a public IP instead", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"gateway limit check skipped ({type(e).__name__}); assuming one is available", flush=True)
            _GATEWAY_OK = True
    return _GATEWAY_OK


def build_variables(args, fnd: dict, cfg: dict, app_containers: list | None) -> dict:
    """Resource Manager variables are strings; lists/objects go as JSON."""
    gw = gateway_available(cfg)
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
        "secrets_vault_id": secrets_vault(fnd)["vault_id"],
        "secrets_key_id": secrets_vault(fnd)["key_id"],
        "enable_adb": json.dumps(bool(args.adb)),
        "adb_tier": args.adb_tier,
        "adb_workload": args.adb_workload,
        "enable_kafka": json.dumps(bool(args.kafka)),
        "kafka_mode": args.kafka_mode,
        "kafka_topics": json.dumps(args.topics.split(",")),
        "enable_nosql": json.dumps(bool(getattr(args, "nosql", False))),
        # A paid database is reachable only on its private endpoint, which lives in
        # the private subnet. Put the app there too, beside it - the API Gateway
        # still gives the sandbox a public HTTPS URL, so nothing is lost.
        # Behind a gateway the app can sit in the private subnet (it must, next
        # to a paid database). Without one it needs a public IP to be reachable.
        "app_public": json.dumps((not (bool(args.adb) and args.adb_tier == "paid")) or not gw),
        "app_gateway": json.dumps(gw),
        "functions_gateway": json.dumps(gw),
        "enable_app": json.dumps(app_containers is not None),
    }
    if getattr(args, "enable_catalog", False):
        v["enable_catalog"] = json.dumps(True)
    if getattr(args, "catalog_assets", None):
        v["catalog_assets"] = json.dumps(args.catalog_assets)
    if getattr(args, "buckets", None):
        v["buckets"] = json.dumps(args.buckets)
    if getattr(args, "queues", None):
        v["queues"] = json.dumps(args.queues)
    if getattr(args, "dataflow_jobs", None):
        v["dataflow_jobs"] = json.dumps(args.dataflow_jobs)
    if getattr(args, "functions", None):
        v["functions"] = json.dumps(args.functions)
    if getattr(args, "app_instances", None):
        v["app_instances"] = json.dumps(args.app_instances)
    if getattr(args, "adb_databases", None):
        v["adb_databases"] = json.dumps(args.adb_databases)
    if getattr(args, "nosql_tables", None):
        v["nosql_tables"] = json.dumps(args.nosql_tables)
    if app_containers is not None:
        v["app_containers"] = json.dumps(app_containers)
        v["app_shape"] = args.shape
    return v


def cmd_create(args, app_containers: list | None = None) -> dict:
    cfg = config()
    fnd = foundation()
    rm = client(oci.resource_manager.ResourceManagerClient)
    control = fnd["compartments"]["control"]

    if args.app and app_containers is None:
        app_containers = [{"name": "web", "image": args.image, "port": args.port, "env": {}}]

    variables = build_variables(args, fnd, cfg, app_containers)
    # A full timestamp, not a date. Comparing dates made a 3-day sandbox built
    # just after midnight live until the start of the fourth day after - close to
    # four days. ISO strings sort correctly, so the reaper compares them as-is.
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.ttl)).strftime("%Y-%m-%dT%H:%MZ")
    tags = {"sandbox_id": args.sandbox_id, "owner": args.owner, "expires": expires, "managed_by": "sandbox-factory"}

    existing = find_stack(rm, control, args.sandbox_id)
    assert_owner(existing, getattr(args, "owner", None), "update")
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
    pws = adb_passwords_from_state(rm, stack_id)
    if isinstance(outputs.get("adb"), dict):
        # Sensitive outputs are masked in the job outputs; read the password from the state
        # so the requester can log in to SQL Developer Web / connect a client.
        pw = pws.get("module.adb")
        if pw:
            outputs["adb"]["admin_user"] = "ADMIN"
            outputs["adb"]["admin_password"] = pw
    # Every extra database has its own ADMIN password; a user who cannot see it
    # cannot log in, so it goes on the card and the landing page like the primary.
    for d in outputs.get("databases") or []:
        if isinstance(d, dict):
            pw = pws.get("module.adb") if d.get("name") == "primary" else pws.get('module.adb_extra["%s"]' % d.get("name"))
            if pw:
                d["admin_user"] = "ADMIN"
                d["admin_password"] = pw
    print(json.dumps({k: v for k, v in outputs.items() if k != "adb_admin_password"}, indent=2))
    return outputs


def cmd_list(args) -> list:
    cfg = config()
    fnd = foundation()
    rm = client(oci.resource_manager.ResourceManagerClient)
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


def empty_buckets(sandbox_id: str, namespace: str | None = None) -> None:
    """Delete every object in this sandbox's buckets before Terraform runs.

    Object Storage refuses to delete a bucket that still holds objects, so a
    sandbox that wrote anything would fail to destroy and outlive its TTL. The
    lifecycle policy expires objects after a day; this covers what is newer.
    """
    try:
        fnd = foundation()
        osc = client(oci.object_storage.ObjectStorageClient)
        # Prefer the namespace the sandbox already recorded in its outputs.
        # get_namespace() answers 404 NamespaceNotFound for this worker's
        # instance principal whatever compartment it is asked for, and the
        # sandbox knows its own namespace anyway, so do not depend on the call.
        ns = namespace
        if not ns:
            for scope in (config().get("tenancy"), fnd["compartments"]["control"], None):
                try:
                    ns = osc.get_namespace(compartment_id=scope).data if scope else osc.get_namespace().data
                    break
                except Exception:  # noqa: BLE001 - try the next scope
                    continue
        if not ns:
            print("  bucket cleanup skipped: object storage namespace unknown")
            return
        prefix = f"sbx-{sandbox_id}-"
        for comp in {fnd["compartments"]["control"], fnd["compartments"]["sandboxes"]}:
            for b in osc.list_buckets(ns, comp).data:
                if not b.name.startswith(prefix):
                    continue
                removed = 0
                start = None
                while True:
                    page = osc.list_objects(ns, b.name, start=start, limit=1000).data
                    for o in page.objects:
                        osc.delete_object(ns, b.name, o.name)
                        removed += 1
                    start = page.next_start_with
                    if not start:
                        break
                if removed:
                    print(f"  emptied {removed} object(s) from {b.name}")
    except Exception as e:  # noqa: BLE001 - never block a destroy on cleanup
        print(f"  bucket cleanup skipped ({type(e).__name__}: {e})")


def cmd_destroy(args):
    cfg = config()
    fnd = foundation()
    rm = client(oci.resource_manager.ResourceManagerClient)
    stack = find_stack(rm, fnd["compartments"]["control"], args.sandbox_id)
    if not stack:
        raise SystemExit(f"No sandbox named {args.sandbox_id}")
    assert_owner(stack, getattr(args, "owner", None), "destroy")
    empty_buckets(args.sandbox_id, getattr(args, "os_namespace", None))
    print(f"Destroying {stack.display_name}")
    destroy_stack(rm, stack, keep_stack=args.keep_stack)


def cmd_reap(args):
    """Destroy every sandbox whose expires tag is before today."""
    cfg = config()
    fnd = foundation()
    rm = client(oci.resource_manager.ResourceManagerClient)
    # Tags written before timestamps were introduced hold a bare date; "2026-09-28"
    # sorts before "2026-09-28T..." so those are reaped at the start of that day,
    # which is early rather than late - the safe direction for spend.
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
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
    user = os.environ.get("OCIR_USER") or client(oci.identity.IdentityClient).get_user(cfg["user"]).data.name
    registry = f"{region_key}.ocir.io"
    tool = container_tool()
    subprocess.run(
        [tool, "login", registry, "-u", f"{namespace}/{user}", "--password-stdin"],
        input=token, text=True, check=True, capture_output=True,
    )
    return registry


def ensure_public_repo(cfg: dict, compartment_id: str, repo: str):
    """A public OCIR repo lets Container Instances pull without a pull secret."""
    art = client(oci.artifacts.ArtifactsClient)
    existing = art.list_container_repositories(compartment_id=compartment_id, display_name=repo).data.items
    if existing:
        return existing[0]
    return art.create_container_repository(oci.artifacts.models.CreateContainerRepositoryDetails(
        compartment_id=compartment_id, display_name=repo, is_public=True,
        freeform_tags={"managed_by": "sandbox-factory"},
    )).data


def cmd_deploy(args, app_containers: list | None = None):
    """Build an image and run it in a sandbox.

    args.path is a local folder (laptop worker, Docker) or a Git URL (any worker:
    built inside OCI by a kaniko container, see oci_build.py). SBX_BUILD_MODE=kaniko
    forces the OCI build even for a laptop run.
    """
    cfg = config()
    fnd = foundation()
    src = str(args.path)
    is_git = src.lower().startswith(("http://", "https://", "git@", "ssh://", "git://"))
    if not is_git:
        path = pathlib.Path(src).resolve()
        if not (path / "Dockerfile").exists():
            raise SystemExit(f"No Dockerfile in {path}")

    namespace = client(oci.object_storage.ObjectStorageClient).get_namespace().data
    region_key = next(r.key.lower() for r in client(oci.identity.IdentityClient).list_regions().data
                      if r.name == cfg["region"])  # us-phoenix-1 -> phx
    registry = f"{region_key}.ocir.io"

    repo = f"sbx/{args.sandbox_id}/{args.name}"
    ensure_public_repo(cfg, fnd["compartments"]["control"], repo)
    tag = args.tag or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    image = f"{registry}/{namespace}/{repo}:{tag}"
    platform = "linux/arm64" if args.shape.startswith("CI.Standard.A1") else "linux/amd64"

    # kaniko is not just for Git sources. An app_template expands into a local
    # folder, and the OCI worker has no docker, so gating kaniko on is_git left
    # every template build dying with "Need docker or podman on PATH".
    use_kaniko = in_oci() or os.environ.get("SBX_BUILD_MODE") == "kaniko" or not shutil.which("docker")
    if use_kaniko:
        import oci_build
        where = src if is_git else f"{path} ({len(list(path.iterdir()))} files)"
        print(f"Building {image} from {where} inside OCI (kaniko, {platform})")
        oci_build.build_in_oci(git_url=src if is_git else None,
                               src_dir=None if is_git else str(path),
                               image=image, registry=registry, namespace=namespace,
                               sandbox_id=args.sandbox_id, platform=platform)
    else:
        if is_git:
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="sbx-src-"))
            subprocess.run(["git", "clone", "--depth", "1", src, str(tmp)], check=True)
            path = tmp
        ocir_login(cfg, region_key, namespace)
        tool = container_tool()
        print(f"Building {image} for {platform}")
        if tool == "docker":
            subprocess.run(["docker", "buildx", "build", "--platform", platform, "-t", image, "--push", str(path)], check=True)
        else:
            subprocess.run(["podman", "build", "--platform", platform, "-t", image, str(path)], check=True)
            subprocess.run(["podman", "push", image], check=True)

    env = dict(kv.split("=", 1) for kv in (args.env or []))
    if app_containers:
        # The request described the containers (start command, env, generated
        # passwords); the image just built is the first one's image.
        containers = [dict(c) for c in app_containers]
        containers[0] = {**containers[0], "image": image, "env": {**env, **(containers[0].get("env") or {})}}
    else:
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
    p.add_argument("--ttl", type=int, default=3, choices=range(1, 31), metavar="1-30", help="days until auto-destroy (max 30)")
    p.add_argument("--allowed-cidr", default="0.0.0.0/0")
    p.add_argument("--adb", action="store_true")
    p.add_argument("--adb-tier", choices=["free", "paid"], default="free")
    p.add_argument("--adb-workload", choices=["OLTP", "DW", "AJD", "APEX"], default="OLTP")
    p.add_argument("--kafka", action="store_true")
    p.add_argument("--nosql", action="store_true", help="add OCI NoSQL tables to the sandbox")
    p.add_argument("--kafka-mode", choices=["streaming", "cluster"], default="cluster")
    p.add_argument("--topics", default="events", help="comma-separated")
    p.add_argument("--shape", default="CI.Standard.A1.Flex")
    p.add_argument("--port", type=int, default=80)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create or update a sandbox")
    add_sandbox_options(c)
    c.add_argument("--app", action="store_true")
    c.add_argument("--image", default=None, help="container image; omit when using --app-template or explicit containers")
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
