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
import re
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
                if v.display_name == f"{PREFIX}-secrets" and v.lifecycle_state == "ACTIVE":
                    km = oci.key_management.KmsManagementClient(**auth(), service_endpoint=v.management_endpoint)
                    for k in km.list_keys(compartment_id=fnd["compartments"]["control"]).data:
                        if k.display_name == f"{PREFIX}-secrets-key" and k.lifecycle_state == "ENABLED":
                            _SECRETS = {"vault_id": v.id, "key_id": k.id}
                    break
        except Exception as e:  # noqa: BLE001
            print(f"secrets vault lookup failed ({type(e).__name__}: {e}); Kafka gets no public endpoint", flush=True)
    return _SECRETS


# The install prefix (foundation var.prefix): names of the vault, key and
# compartments the factory looks up. "sbx" unless the install chose another.
PREFIX = os.environ.get("SBX_PREFIX", "sbx")


def in_oci() -> bool:
    return "signer" in auth()


def foundation_from_workers() -> dict | None:
    """Find sbx-control by name and copy SBX_FOUNDATION from a running worker.

    Needs `inspect compartments in tenancy` and `read compute-container-family
    in compartment sbx-control` for the calling principal (the DevOps runner's
    dynamic group has both). None when nothing is found; the caller then falls
    back to terraform output and fails the usual way.
    """
    try:
        idc = client(oci.identity.IdentityClient)
        name = os.environ.get("SBX_CONTROL_COMPARTMENT_NAME", f"{PREFIX}-control")
        comps = oci.pagination.list_call_get_all_results(
            idc.list_compartments, config()["tenancy"], compartment_id_in_subtree=True,
            access_level="ACCESSIBLE", lifecycle_state="ACTIVE").data
        ctl = next((c for c in comps if c.name == name), None)
        if not ctl:
            print(f"foundation: no compartment named {name} visible to this principal", flush=True)
            return None
        cc = client(oci.container_instances.ContainerInstanceClient)
        for x in cc.list_container_instances(compartment_id=ctl.id).data.items:
            if "worker" not in x.display_name.lower() or x.lifecycle_state != "ACTIVE":
                continue
            for k in cc.get_container_instance(x.id).data.containers:
                env = cc.get_container(k.container_id).data.environment_variables or {}
                if env.get("SBX_FOUNDATION"):
                    print(f"foundation: borrowed from worker {x.display_name}", flush=True)
                    return json.loads(env["SBX_FOUNDATION"])
        print(f"foundation: no running worker in {name} carries SBX_FOUNDATION", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"foundation: lookup through the workers failed ({type(e).__name__}: {e})", flush=True)
    return None


def foundation() -> dict:
    """OCIDs produced by the foundation stack. From SBX_FOUNDATION (JSON, set on the
    OCI-hosted worker), else cached in foundation.json, else borrowed from a running
    worker (on OCI), else terraform output."""
    if os.environ.get("SBX_FOUNDATION"):
        return json.loads(os.environ["SBX_FOUNDATION"])
    if FOUNDATION_JSON.exists():
        return json.loads(FOUNDATION_JSON.read_text())
    if in_oci():
        # A DevOps build runner or any other OCI principal with nothing
        # configured: the running workers carry the foundation in their
        # environment, so borrow it from one of them.
        borrowed = foundation_from_workers()
        if borrowed:
            return borrowed
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
    # Resource Manager allows only a few jobs at once per tenancy; with several
    # workers busy a new job is refused with 400 LimitExceeded. Wait for a slot.
    waited = 0
    while True:
        try:
            job = rm.create_job(rmm.CreateJobDetails(
                stack_id=stack_id,
                display_name=f"{label}-{operation.lower()}-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
                job_operation_details=details,
            )).data
            break
        except oci.exceptions.ServiceError as e:
            if e.status == 400 and e.code == "LimitExceeded" and waited < 1800:
                if waited == 0:
                    print(f"  Resource Manager is at its concurrent job limit; waiting for a slot", flush=True)
                time.sleep(60)
                waited += 60
                continue
            raise
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
# Things the variables builder decided on the tenancy's behalf, for the card.
NOTES: list[str] = []


def catalog_available(cfg: dict, fnd: dict) -> bool:
    """Is there a Data Catalog left in the region's limit (catalog-count)?

    The limits API reports no availability for this one, so count the
    catalogs the factory owns against the limit value.
    """
    try:
        lim = client(oci.limits.LimitsClient)
        vals = lim.list_limit_values(compartment_id=cfg["tenancy"], service_name="data-catalog", name="catalog-count").data
        limit = min((v.value for v in vals), default=None)
        if limit is None:
            return True
        dc = client(oci.data_catalog.DataCatalogClient)
        used = 0
        # Catalogs live in sbx-sandboxes; the worker may not list sbx-control, and one
        # compartment it cannot read must not turn the whole check into "assume free".
        # A catalog that is still DELETING holds its slot (create answers QuotaExceeded).
        for comp in (fnd["compartments"]["sandboxes"], fnd["compartments"]["control"]):
            try:
                used += sum(1 for c in dc.list_catalogs(compartment_id=comp).data
                            if c.lifecycle_state not in ("DELETED", "FAILED"))
            except oci.exceptions.ServiceError as e:
                if e.status not in (401, 403, 404):
                    raise
        ok = used < limit
        if not ok:
            NOTES.append(f"Built without a Data Catalog: this region's limit ({limit}) is used up. "
                         "Ask for a catalog-count increase, or destroy a sandbox that has one.")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"catalog limit check skipped ({type(e).__name__}); assuming one is available", flush=True)
        return True


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


def kafka_addon_reset(fnd: dict, sandbox_id: str) -> bool:
    """Undo a public add-on the provider lost track of, so a re-apply can make it.

    The Kafka add-on install can finish in the service (work request SUCCEEDED,
    add-on ACTIVE) while the Terraform provider reports "Work Request error"
    with no message and records nothing in the state. A second apply would then
    try to install it again and collide with the one that exists, so uninstall
    it first and let Terraform create it cleanly. True when there was one.
    """
    kc = client(oci.managed_kafka.KafkaClusterClient)
    comp = fnd["compartments"]["sandboxes"]
    clusters = [c for c in kc.list_kafka_clusters(compartment_id=comp).data.items
                if c.display_name == f"sbx-{sandbox_id}-kafka" and c.lifecycle_state == "ACTIVE"]
    if not clusters:
        return False
    found = False
    for a in kc.list_addons(kafka_cluster_id=clusters[0].id).data.items:
        found = True
        print(f"  kafka add-on {a.name} is {a.lifecycle_state} in the service but not in the state; uninstalling it for a clean re-apply", flush=True)
        wr = kc.uninstall_addon(clusters[0].id, a.name).headers.get("opc-work-request-id")
        started = time.time()
        while wr and time.time() - started < 1800:
            st = kc.get_work_request(wr).data.status
            if st in ("SUCCEEDED", "FAILED", "CANCELED"):
                print(f"  add-on uninstall {st} after {int(time.time() - started)}s", flush=True)
                break
            time.sleep(POLL_SECONDS)
    return found


def ui_first(containers: list) -> list:
    """The first container is served at "/" and the rest under "/<name>".

    A UI (Studio, a web app) only works at "/", and an MCP server is meant to
    answer at "/mcp", so whatever order a request lists them in, containers
    named mcp go after the others.
    """
    return sorted(containers, key=lambda c: (str(c.get("name", "")).lower() == "mcp"))


def db_links_through_gateway(outputs: dict) -> None:
    """Point a private database's web tools at the sandbox's gateway host.

    A paid database is on a private endpoint, so its SQL Developer Web, APEX
    and REST URLs do not open from a browser. The app gateway proxies /ords/*
    to it (modules/app), so rewrite those links to the gateway's HTTPS host.
    """
    adb = outputs.get("adb") if isinstance(outputs.get("adb"), dict) else None
    if not adb or adb.get("tier") != "paid":
        return
    urls = []
    app = outputs.get("app") if isinstance(outputs.get("app"), dict) else {}
    urls += [u for u in (app.get("urls") or [app.get("url")]) if u]
    for inst in outputs.get("app_instances") or []:
        urls += inst.get("urls") or []
    gw = next((u for u in urls if u.startswith("https://") and "apigateway" in u), None)
    if not gw:
        return
    host = gw.split("/")[2]
    def swap(u):
        return re.sub(r"^https://[^/]+", "https://" + host, u) if isinstance(u, str) and "/ords/" in u else u
    for k in ("sql_web_url", "apex_url"):
        adb[k] = swap(adb.get(k))
    for d in outputs.get("databases") or []:
        if isinstance(d, dict) and d.get("name") == "primary":
            d["sql_web_url"] = swap(d.get("sql_web_url"))
    lc = outputs.get("low_code")
    if isinstance(lc, dict) and lc.get("rest_base"):
        lc["rest_base"] = swap(lc["rest_base"])


def profile_model() -> str:
    """The chat model the tenancy profile found answering here (factory/profile.py),
    or SBX_GENAI_MODEL; empty when neither is known (a laptop without the DB)."""
    if os.environ.get("SBX_GENAI_MODEL"):
        return os.environ["SBX_GENAI_MODEL"]
    try:
        import controldb
        cur = controldb.connect().cursor()
        cur.execute(f"select value from {controldb.SCHEMA}.factory_config where key = 'genai_model'")
        row = cur.fetchone()
        return (row and row[0]) or ""
    except Exception:  # noqa: BLE001
        return ""


def kafka_public_addon(outputs: dict, sandbox_id: str) -> None:
    """Give a Kafka cluster its public SASL/SCRAM endpoint, through the SDK.

    Installs the PUBLICCONNECTIVITY add-on (or reuses one already there),
    waits for its work request, and records the public bootstrap in the
    outputs. Only for a cluster with a superuser (the vault is configured); a
    failure leaves the private endpoint working and says so on the card.
    """
    k = outputs.get("kafka") if isinstance(outputs.get("kafka"), dict) else None
    if not k or not k.get("cluster_id") or not k.get("superuser_secret_id") or k.get("public_bootstrap"):
        return
    m = oci.managed_kafka.models
    kc = client(oci.managed_kafka.KafkaClusterClient)
    cid, name = k["cluster_id"], f"{PREFIX}-{sandbox_id}-public"
    try:
        have = [a for a in kc.list_addons(kafka_cluster_id=cid).data.items if a.lifecycle_state not in ("DELETED", "FAILED")]
        if not have:
            print(f"installing the Kafka public endpoint ({name})", flush=True)
            r = kc.install_addon(m.InstallPublicConnectivityAddonDetails(
                name=name, addon_type="PUBLICCONNECTIVITY", authentication_mechanism="SASL",
                network_cidrs=k.get("public_cidrs") or ["0.0.0.0/0"],
                description=f"Public bootstrap for {sandbox_id}, SASL/SCRAM"), cid)
            wr, started = r.headers.get("opc-work-request-id"), time.time()
            while wr and time.time() - started < 3600:
                st = kc.get_work_request(wr).data.status
                if st in ("SUCCEEDED", "FAILED", "CANCELED"):
                    print(f"  public endpoint {st} after {int(time.time() - started)}s", flush=True)
                    break
                time.sleep(POLL_SECONDS)
            have = [a for a in kc.list_addons(kafka_cluster_id=cid).data.items if a.lifecycle_state == "ACTIVE"]
        if have:
            addon = kc.get_addon(cid, have[0].name).data
            if getattr(addon, "bootstrap_url", None):
                k["public_bootstrap"] = addon.bootstrap_url
                return
        outputs.setdefault("warnings", []).append("The Kafka public endpoint did not come up; the private bootstrap works from inside the sandbox network.")
    except Exception as e:  # noqa: BLE001
        outputs.setdefault("warnings", []).append(f"The Kafka public endpoint could not be installed ({type(e).__name__}); the private bootstrap works from inside the sandbox network.")


def build_variables(args, fnd: dict, cfg: dict, app_containers: list | None) -> dict:
    """Resource Manager variables are strings; lists/objects go as JSON."""
    gw = gateway_available(cfg)
    v = {
        "tenancy_ocid": cfg["tenancy"],
        "region": cfg["region"],
        "genai_model": profile_model(),
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
    if getattr(args, "enable_catalog", False) and catalog_available(cfg, fnd):
        v["enable_catalog"] = json.dumps(True)
    if getattr(args, "enable_aidp", False):
        v["enable_aidp"] = json.dumps(True)
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
    if getattr(args, "user_env", None):
        # names as the shell allows; values are never logged or output
        bad = [k for k in args.user_env if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", k)]
        if bad:
            raise SystemExit(f"environment variable names must be letters, digits and underscores: {', '.join(bad)}")
        v["user_env"] = json.dumps(args.user_env)
    if getattr(args, "app_instances", None):
        v["app_instances"] = json.dumps([{**i, "containers": ui_first(i.get("containers") or [])} for i in args.app_instances])
    if getattr(args, "adb_databases", None):
        v["adb_databases"] = json.dumps(args.adb_databases)
    if getattr(args, "nosql_tables", None):
        v["nosql_tables"] = json.dumps(args.nosql_tables)
    if app_containers is not None:
        v["app_containers"] = json.dumps(ui_first(app_containers))
        v["app_shape"] = args.shape
    return v


STARTER_FUNCTION = "https://github.com/ashishsinha1602/oci-sandbox-factory/tree/main/examples/hello-fn"

# An AWS Lambda handler runs unchanged as an OCI Function behind this adapter:
# the Fn FDK hands over (ctx, body), the adapter turns the body into the Lambda
# event, gives the handler a context with the fields Lambda code reads, and
# turns an API Gateway proxy reply ({statusCode, headers, body}) back into HTTP.
LAMBDA_SHIM = '''"""Runs an AWS Lambda handler as an OCI Function (generated by Sandbox Factory)."""
import importlib
import io
import json
import time

from fdk import response

_fn = getattr(importlib.import_module("{module}"), "{func}")


class LambdaContext:
    function_name = "{name}"
    function_version = "$LATEST"
    memory_limit_in_mb = 256
    log_group_name = log_stream_name = invoked_function_arn = ""

    def __init__(self, request_id):
        self.aws_request_id = request_id
        self._deadline = time.time() + 30

    def get_remaining_time_in_millis(self):
        return max(0, int((self._deadline - time.time()) * 1000))


def handler(ctx, data: io.BytesIO = None):
    raw = data.getvalue() if data else b""
    try:
        event = json.loads(raw or b"{{}}")
    except ValueError:
        event = {{"body": raw.decode(errors="replace")}}
    rid = ctx.CallID() if hasattr(ctx, "CallID") else ""
    out = _fn(event, LambdaContext(rid))
    status, headers = 200, {{"Content-Type": "application/json"}}
    if isinstance(out, dict) and "statusCode" in out:
        status = int(out.get("statusCode") or 200)
        headers.update(out.get("headers") or {{}})
        body = out.get("body", "")
        body = body if isinstance(body, str) else json.dumps(body, default=str)
    else:
        body = out if isinstance(out, str) else json.dumps(out, default=str)
    return response.Response(ctx, response_data=body, headers=headers, status_code=status)
'''
FUNCTION_DOCKERFILE = """FROM python:3.11-slim
WORKDIR /function
COPY . /function/
RUN pip install --no-cache-dir fdk==0.1.124{reqs}
ENV PYTHONPATH=/function
ENTRYPOINT ["fdk", "/function/{entry}", "handler"]
"""


def function_source(name: str, files: dict) -> dict:
    """Flat build folder for one function: the user's files plus, when they
    have no Dockerfile, a generated one (and the Lambda adapter if needed)."""
    flat = {}
    for path, text in files.items():
        base = path.replace("\\", "/").split("/")[-1]
        if base in flat and flat[base] != text:
            raise SystemExit(f"function {name}: two files named {base}; OCI Functions builds from one flat folder")
        flat[base] = text
    if "Dockerfile" in flat:
        return flat
    py = {k: v for k, v in flat.items() if k.endswith(".py")}
    fdk_native = next((k for k, v in py.items() if re.search(r"^def handler\(\s*ctx\b", v, re.M)), None)
    lam = None
    for k, v in py.items():
        m = re.search(r"^def (\w+)\(\s*event\s*,\s*context\s*\)", v, re.M)
        if m:
            lam = (k, m.group(1))
            break
    reqs = " -r requirements.txt" if "requirements.txt" in flat else ""
    if fdk_native:
        entry = fdk_native
    elif lam:
        entry = "sbx_lambda_adapter.py"
        flat[entry] = LAMBDA_SHIM.format(module=lam[0][:-3], func=lam[1], name=name)
        uses = [k for k, v in py.items() if re.search(r"^\s*(import|from)\s+(boto3|botocore)\b", v, re.M)]
        if uses:
            print(f"  function {name}: {', '.join(uses)} use boto3; AWS calls need OCI endpoints and "
                  f"credentials (Object Storage is S3-compatible) or the oci SDK", flush=True)
    else:
        raise SystemExit(f"function {name}: no Dockerfile, no Lambda handler (def x(event, context)) "
                         f"and no Fn handler (def handler(ctx, data)) in {', '.join(sorted(flat)) or 'the files'}")
    flat["Dockerfile"] = FUNCTION_DOCKERFILE.format(reqs=reqs, entry=entry)
    return flat


def schedule_problem(cron: str) -> str | None:
    """Why OCI Resource Scheduler could not run this cron, or None.

    The scheduler runs a schedule at most hourly; the sandbox stack turns a
    sub-hourly minute field (*/15, or 0,20,40) into one hourly schedule per
    minute, so only the minute field may be finer than an hour, and at most
    12 times an hour (every 5 minutes).
    """
    if not cron.strip():
        return None
    parts = cron.split()
    if len(parts) != 5:
        return "use five cron fields: minute hour day month weekday, e.g. */15 * * * * for every 15 minutes"
    minute = parts[0]
    if minute == "*":
        return "every minute is not possible; every 5 minutes (*/5 * * * *) is the most frequent"
    if minute.startswith("*/"):
        step = minute[2:]
        if not step.isdigit() or not 5 <= int(step) <= 59:
            return "the minute step must be 5 to 59, e.g. */15"
        return None
    mins = minute.split(",")
    if not all(m.isdigit() and int(m) < 60 for m in mins):
        return "the minute field must be */N or minutes such as 0,30"
    if len(mins) > 12:
        return "at most 12 runs an hour (every 5 minutes)"
    return None


def prepare_functions(functions: list | None, sandbox_id: str, cfg: dict) -> list | None:
    """Every function gets an image OCI Functions can actually pull.

    Functions pulls only from this tenancy's own registry in this region, so an
    image named anywhere else (Docker Hub, another tenancy, or one a model made
    up) fails at create time. Build it here instead, inside OCI: from the code
    folder the function names (git_url), or from the shipped hello-fn starter.
    """
    if not functions:
        return functions
    macros = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
    functions = [dict(f, schedule=macros.get((f.get("schedule") or "").strip(), (f.get("schedule") or "").strip()))
                 for f in functions]
    for f in functions:
        problem = schedule_problem(f.get("schedule") or "")
        if problem:
            raise SystemExit(f"function {f['name']}: schedule {f.get('schedule')!r}: {problem}")
    namespace = client(oci.object_storage.ObjectStorageClient).get_namespace().data
    region_key = next(r.key.lower() for r in client(oci.identity.IdentityClient).list_regions().data
                      if r.name == cfg["region"])
    registry = f"{region_key}.ocir.io"
    own = f"{registry}/{namespace}/"
    out = []
    for f in functions:
        f = dict(f)
        image = (f.get("image") or "").strip()
        src = (f.get("git_url") or "").strip()
        files = f.pop("files", None) or None
        if image.startswith(own) and not src and not files:
            out.append(f)
            continue
        if image and not src and not files:
            print(f"  function {f['name']}: {image} is not in this tenancy's registry, which is the only one "
                  f"OCI Functions pulls from; building the starter function instead", flush=True)
        import oci_build
        if not files and not src:
            src = os.environ.get("SBX_STARTER_FUNCTION", STARTER_FUNCTION)
        if src and not files:
            # a folder without a Dockerfile (plain Lambda code) is read and wrapped
            repo_url, sub = oci_build.split_tree_url(src)
            if oci_build.dockerfile_missing(repo_url, sub, None):
                files = oci_build.fetch_github_folder(repo_url, sub)
        tag = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        name = re.sub(r"[^a-z0-9-]+", "-", f["name"].lower()).strip("-") or "fn"
        f["image"] = f"{registry}/{namespace}/{image_repo(cfg, foundation()['compartments']['control'], sandbox_id, 'fn-' + name)}:{tag}"
        if files:
            folder = pathlib.Path(tempfile.mkdtemp(prefix=f"sbx-fn-{name}-"))
            for fname, text in function_source(f["name"], files).items():
                (folder / fname).write_text(text, encoding="utf-8")
            print(f"Building function {f['name']} from {len(files)} file(s) inside OCI (kaniko, linux/arm64)", flush=True)
            oci_build.build_in_oci(git_url=None, src_dir=str(folder), image=f["image"], registry=registry,
                                   namespace=namespace, sandbox_id=sandbox_id, platform="linux/arm64")
        else:
            print(f"Building function {f['name']} from {src} inside OCI (kaniko, linux/arm64)", flush=True)
            oci_build.build_in_oci(git_url=src, image=f["image"], registry=registry, namespace=namespace,
                                   sandbox_id=sandbox_id, platform="linux/arm64")
        f.pop("git_url", None)
        out.append(f)
    return out


def cmd_create(args, app_containers: list | None = None) -> dict:
    cfg = config()
    fnd = foundation()
    rm = client(oci.resource_manager.ResourceManagerClient)
    control = fnd["compartments"]["control"]
    if getattr(args, "functions", None):
        args.functions = prepare_functions(args.functions, args.sandbox_id, cfg)

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

    try:
        job = run_job(rm, stack_id, "APPLY", args.sandbox_id)
    except SystemExit:
        # Region limits are shared by every request in flight, so a check made
        # a minute ago can be stale by the time Terraform asks. When the apply
        # died on one of the two we know how to live without, re-apply once
        # without that piece and say so on the card.
        last = rm.list_jobs(stack_id=stack_id, sort_by="TIMECREATED", sort_order="DESC").data[0]
        log = rm.get_job_logs_content(last.id).data
        retry = {}
        if "gateway-count" in log and variables.get("app_gateway") != "false":
            retry.update(app_gateway="false", functions_gateway="false", app_public="true")
            NOTES.append("No API Gateway was left in this region's limit, so the app is on a public IP (HTTP) instead of an Oracle HTTPS hostname.")
        if "catalog-count" in log and variables.get("enable_catalog") == "true":
            retry["enable_catalog"] = "false"
            NOTES.append("Built without a Data Catalog: this region's limit is used up. Ask for a catalog-count increase, or destroy a sandbox that has one.")
        # The Kafka public add-on: the service finishes it, the provider says
        # "Work Request error" with no message and forgets it. Not a limit,
        # the same variables again once the orphan is gone.
        kafka_orphan = ("kafka_cluster_addon" in log
                        and ("Work Request error" in log or "409" in log or "already" in log.lower())
                        and kafka_addon_reset(fnd, args.sandbox_id))
        if not retry and not kafka_orphan:
            raise
        if retry:
            variables.update(retry)
            print(f"apply hit a region limit; re-applying without {', '.join(retry)}", flush=True)
            rm.update_stack(stack_id, rmm.UpdateStackDetails(variables=variables))
        else:
            print("re-applying for the Kafka public add-on", flush=True)
        job = run_job(rm, stack_id, "APPLY", args.sandbox_id)
    outputs = job_outputs(rm, job.id)
    db_links_through_gateway(outputs)
    kafka_public_addon(outputs, args.sandbox_id)
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


def empty_sandbox_buckets(sandbox_id: str) -> None:
    """Delete every object (and every old version) in the sandbox's buckets, so
    the destroy can remove them: Object Storage refuses a non-empty bucket
    (409 BucketNotEmpty), and a pipeline sandbox always has data in its bucket."""
    osc = client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    comp = foundation()["compartments"]["sandboxes"]
    for b in osc.list_buckets(ns, comp).data:
        if not b.name.startswith(f"{PREFIX}-{sandbox_id}-"):
            continue
        n = 0
        for page in oci.pagination.list_call_get_all_results_generator(osc.list_object_versions, "response", ns, b.name):
            for v in page.data.items:
                osc.delete_object(ns, b.name, v.name, version_id=v.version_id)
                n += 1
        for par in osc.list_preauthenticated_requests(ns, b.name).data:
            osc.delete_preauthenticated_request(ns, b.name, par.id)
        if n:
            print(f"  emptied bucket {b.name} ({n} object versions)", flush=True)


def destroy_stack(rm, stack, keep_stack: bool = False):
    label = stack.freeform_tags.get("sandbox_id", stack.display_name)
    # the Kafka public endpoint is installed through the SDK (not in the stack),
    # so remove it first or the cluster cannot be deleted
    try:
        kafka_addon_reset(foundation(), label)
    except Exception as e:  # noqa: BLE001
        print(f"  kafka add-on check skipped ({type(e).__name__})", flush=True)
    try:
        empty_sandbox_buckets(label)
    except Exception as e:  # noqa: BLE001
        print(f"  bucket clean-up skipped ({type(e).__name__}: {e})", flush=True)
    try:
        run_job(rm, stack.id, "DESTROY", label)
    except SystemExit:
        # Cloud resources detach and release on their own clock (a private
        # endpoint's VNIC, a bucket emptied a moment ago). A second pass a
        # couple of minutes later removes what the first could not.
        print("  destroy did not finish; waiting 120s and trying once more", flush=True)
        time.sleep(120)
        run_job(rm, stack.id, "DESTROY", label)
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
        # A build that failed before its stack existed (an image that would not
        # build, a refused name) has nothing in OCI, yet its card stayed FAILED
        # and every Destroy failed too. Nothing to destroy is a finished destroy.
        print(f"No stack for {args.sandbox_id}: nothing in OCI to destroy; the sandbox is gone", flush=True)
        return
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


def image_repo(cfg: dict, compartment_id: str, sandbox_id: str, name: str) -> str:
    """The repository path for an image this install builds, created public.

    Repository names are tenancy-wide, so the path carries the install prefix,
    and when a repository of that name exists somewhere this install cannot
    see (a retired install, another compartment) the control compartment's id
    is added: a second install must never fail on the first one's names.
    """
    first = f"{PREFIX}/{sandbox_id}/{name}"
    for repo in (first, f"{PREFIX}-{compartment_id[-6:]}/{sandbox_id}/{name}"):
        try:
            ensure_public_repo(cfg, compartment_id, repo)
            return repo
        except oci.exceptions.ServiceError as e:
            if e.status != 409:
                raise
            print(f"  repository {repo} exists elsewhere in the tenancy; using another name", flush=True)
    raise SystemExit(f"could not create an image repository for {sandbox_id}/{name}")


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

    repo = image_repo(cfg, fnd["compartments"]["control"], args.sandbox_id, args.name)
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
            import oci_build
            repo, sub = oci_build.split_tree_url(src)       # a folder link inside a repository
            repo, _, ref = repo.partition("#")
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="sbx-src-"))
            subprocess.run(["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [repo, str(tmp)], check=True)
            path = tmp / sub if sub else tmp
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
    p.add_argument("--adb-tier", choices=["free", "paid"], default="paid")
    p.add_argument("--adb-workload", choices=["OLTP", "DW", "AJD", "APEX"], default="OLTP")
    p.add_argument("--kafka", action="store_true")
    p.add_argument("--aidp", dest="enable_aidp", action="store_true", help="Oracle AI Data Platform instance (lakehouse) in the sandbox")
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
