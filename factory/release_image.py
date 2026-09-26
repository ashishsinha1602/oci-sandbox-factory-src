"""Publish the worker image that every install runs.

    python release_image.py [git-ref]      # default: main

Builds factory/Dockerfile from the public repository inside OCI (kaniko,
linux/amd64 for the E4 workers) and pushes it to a PUBLIC repository in the
tenancy root, as <key>.ocir.io/<namespace>/sandbox-factory/worker:release and
:<date>. The root is deliberate: the repository must outlive any install, and
foundation/worker.tf's default worker_image points at it.
"""
import datetime as dt
import os
import sys

import oci
import oci_build
import sandbox_factory as sf

# The factory's source is private; the build clones it with SBX_GIT_TOKEN.
# Examples and the Terraform are public at github.com/ashishsinha1602/oci-sandbox-factory.
REPO_URL = os.environ.get("SBX_SOURCE_REPO", "https://github.com/ashishsinha1602/oci-sandbox-factory-src.git")


STARTERS = {   # shipped starter images: sandbox apps run on Arm (A1)
    "studio": "examples/schemagate-studio",
    "schemagate": "examples/schemagate-mcp",
}


def starters(ref: str = "main") -> None:
    """Publish the starter images (Studio, the schemagate MCP server) the same
    way: public, in the tenancy root, sandbox-factory/<name>:release."""
    cfg = sf.config()
    ns = sf.client(oci.object_storage.ObjectStorageClient).get_namespace().data
    key = next(r.key.lower() for r in sf.client(oci.identity.IdentityClient).list_regions().data if r.name == cfg["region"])
    registry = f"{key}.ocir.io"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    for name, folder in STARTERS.items():
        image = f"{registry}/{ns}/sandbox-factory/{name}:release"
        print(f"building {folder} -> {image} (+ :{stamp}, arm64)", flush=True)
        oci_build.build_in_oci(git_url=f"{REPO_URL}#{ref}", image=image, registry=registry, namespace=ns,
                               sandbox_id=f"release-{name}", platform="linux/arm64", dockerfile="Dockerfile",
                               sub_path=folder, repo_compartment=cfg["tenancy"], also_tags=[stamp])
        print(f"published {image}", flush=True)


def main(ref: str = "main") -> None:
    cfg = sf.config()
    ns = sf.client(oci.object_storage.ObjectStorageClient).get_namespace().data
    key = next(r.key.lower() for r in sf.client(oci.identity.IdentityClient).list_regions().data if r.name == cfg["region"])
    registry = f"{key}.ocir.io"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    image = f"{registry}/{ns}/sandbox-factory/worker:release"
    print(f"building {REPO_URL}#{ref} -> {image} (+ :{stamp})", flush=True)
    oci_build.build_in_oci(git_url=f"{REPO_URL}#{ref}", image=image, registry=registry, namespace=ns,
                           sandbox_id="release", platform="linux/amd64", dockerfile="factory/Dockerfile",
                           repo_compartment=cfg["tenancy"], also_tags=[stamp])
    print(f"published {image} and :{stamp}", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--starters"]:
        starters(*sys.argv[2:3])
    else:
        main(*sys.argv[1:2])
