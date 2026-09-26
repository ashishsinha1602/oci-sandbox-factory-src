"""Record the product demo inside OCI.

    SF_DEMO_PASSWORD=... python record_demo_in_oci.py [part1|part2] [--user TEAMUSER2] [--out demo.webm]

Starts one Playwright container instance in sbx-control (identity: the
workers' dynamic group, so it can write the reports bucket), which drives a
real Chromium against the live Sandbox Factory as the demo user, types the
demo prompts, confirms each plan and records the browser. The .webm is
uploaded to bucket sbx-factory-reports under demo/, and downloaded to --out
when given. Nothing runs on the machine that launches it.
"""
import argparse
import base64
import json
import os
import pathlib
import sys
import time

import oci
import sandbox_factory as sf

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python:v1.49.0-noble"
BUCKET = os.environ.get("SBX_PREFIX", "sbx") + "-factory-reports"   # bucket names are tenancy-wide: one per install
GRAFANA_PASSWORD = os.environ.get("SF_GRAFANA_PASSWORD", "Grafana-Demo-2026")
PROMPTS = [
    "What do I have running?",
    "Deploy Grafana for my team from the public image grafana/grafana on port 3000, for 2 days. It reads its admin password from GF_SECURITY_ADMIN_PASSWORD."
    f" => env GF_SECURITY_ADMIN_PASSWORD={GRAFANA_PASSWORD} => create",
    "I have Airflow writing to S3 and Glue building Iceberg tables, about 1 GB a day. Move it to OCI.",
    "here is the code https://github.com/ashishsinha1602/oci-sandbox-factory/tree/main/examples/telemetry-pipeline => create",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("part", nargs="?", default="part1", choices=["part1", "part2"])
    ap.add_argument("--user", default=os.environ.get("SF_DEMO_USER", "TEAMUSER2"))
    ap.add_argument("--url", default=os.environ.get("SF_URL"))
    ap.add_argument("--out", help="download the recording to this file")
    a = ap.parse_args()
    password = os.environ.get("SF_DEMO_PASSWORD")
    if not password:
        sys.exit("SF_DEMO_PASSWORD is not set (the demo user's APEX password)")
    fnd = sf.foundation()
    cfg = sf.config()
    auth = sf.auth()
    cc = oci.container_instances.ContainerInstanceClient(**auth)
    idc = oci.identity.IdentityClient(**auth)
    osc = oci.object_storage.ObjectStorageClient(**auth)
    ns = osc.get_namespace().data
    ctrl = fnd["compartments"]["control"]
    url = a.url
    if not url:
        # the control database's APEX URL, as the workers know it
        import controldb
        cur = controldb.connect().cursor()
        cur.execute("select value from sbx.factory_config where key = 'app_url'")
        row = cur.fetchone()
        url = row and row[0]
    if not url:
        sys.exit("pass --url (the Sandbox Factory application URL) or set factory_config.app_url")

    m = oci.container_instances.models
    ad = idc.list_availability_domains(compartment_id=cfg["tenancy"]).data[0].name
    name = f"sbx-demo-recorder-{time.strftime('%H%M%S')}"
    script = (pathlib.Path(__file__).parent / "demo" / "record_demo.py").read_text(encoding="utf-8")
    inst = cc.create_container_instance(m.CreateContainerInstanceDetails(
        compartment_id=ctrl, availability_domain=ad, display_name=name, shape="CI.Standard.E4.Flex",
        shape_config=m.CreateContainerInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=4),
        container_restart_policy="NEVER", freeform_tags={"managed_by": "sandbox-factory", "role": "demo"},
        vnics=[m.CreateContainerVnicDetails(subnet_id=fnd["network"]["private_subnet_id"], display_name=name, is_public_ip_assigned=False)],
        volumes=[m.CreateContainerConfigFileVolumeDetails(
            name="script", configs=[m.ContainerConfigFile(file_name="record_demo.py", data=base64.b64encode(script.encode()).decode())])],
        containers=[m.CreateContainerDetails(
            display_name="recorder", image_url=PLAYWRIGHT_IMAGE,
            environment_variables={"SF_URL": url, "SF_USER": a.user, "SF_PASSWORD": password, "SF_PART": a.part,
                                   "SF_PROMPTS": json.dumps(PROMPTS), "REPORT_BUCKET": BUCKET, "REPORT_NAMESPACE": ns,
                                   "REPORT_COMPARTMENT": ctrl, "SF_GRAFANA_PASSWORD": GRAFANA_PASSWORD, "SF_MASK": GRAFANA_PASSWORD},
            volume_mounts=[m.CreateVolumeMountDetails(volume_name="script", mount_path="/workspace")],
            command=["bash", "-c", "pip install --quiet oci requests playwright==1.49.0 && python -u /workspace/record_demo.py"])])).data
    print(f"started {name}", flush=True)
    seen, uploaded, waits = 0, None, "[]"
    while True:
        ci = cc.get_container_instance(inst.id).data
        try:
            log = cc.retrieve_logs(ci.containers[0].container_id).data.content.decode("utf-8", "replace")
            lines = log.splitlines()
            for line in lines[seen:]:
                text = line.split(" ", 2)[-1] if line.startswith("20") else line
                text = text[2:] if text.startswith(("F ", "P ")) else text
                print(text, flush=True)
                if text.startswith("uploaded: "):
                    uploaded = text.split(": ", 1)[1].strip()
                if text.startswith("waits: "):
                    waits = text.split(": ", 1)[1].strip()
            seen = len(lines)
        except Exception:  # noqa: BLE001
            pass
        if ci.lifecycle_state in ("INACTIVE", "FAILED", "DELETED"):
            break
        time.sleep(20)
    code = getattr(cc.get_container(ci.containers[0].container_id).data, "exit_code", None)
    cc.delete_container_instance(inst.id)
    print(f"recorder finished with exit code {code}; video: {uploaded or 'none'}", flush=True)
    if uploaded and a.out:
        body = osc.get_object(ns, BUCKET, uploaded).data.content
        pathlib.Path(a.out).write_bytes(body)
        # seconds spent waiting on the assistant, for the editor to speed up
        pathlib.Path(a.out + ".waits.json").write_text(waits, encoding="utf-8")
        print(f"downloaded {len(body)} bytes to {a.out}", flush=True)
    sys.exit(0 if code == 0 and uploaded else 1)


if __name__ == "__main__":
    main()
