"""What the OCI DevOps build pipeline runs on every push to main.

    python factory/ci_build_and_roll.py

1. Builds the worker image inside OCI (kaniko in a container instance) from the
   commit being built, tagged with the build run hash so each build is unique.
2. Replaces the running workers with it.

The build runner authenticates as itself (resource principal, dynamic group
sbx-devops-dg). No laptop, no key.
"""
import datetime as dt
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

import oci  # noqa: E402
import oci_build  # noqa: E402
import sandbox_factory as sf  # noqa: E402

fnd = sf.foundation()
ns = sf.client(oci.object_storage.ObjectStorageClient).get_namespace().data
region_key = {"us-phoenix-1": "phx", "us-ashburn-1": "iad"}.get(sf.config()["region"], sf.config()["region"].split("-")[1][:3])
tag = os.environ.get("OCI_BUILD_RUN_ID", "")[-12:] or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
image = f"{region_key}.ocir.io/{ns}/sbx/factory/worker:{tag}"
git_url = os.environ.get("OCI_PRIMARY_SOURCE_URL") or "https://github.com/ashishsinha1602/oci-sandbox-factory.git"
print(f"building {image} from {git_url}", flush=True)
oci_build.build_in_oci(git_url=git_url, image=image, registry=f"{region_key}.ocir.io", namespace=ns,
                       sandbox_id="worker", platform="linux/arm64", dockerfile="factory/Dockerfile")
print("rolling workers", flush=True)
subprocess.run([sys.executable, "-u", str(HERE / "roll_workers.py"), image], check=True)
print("done", image, flush=True)
