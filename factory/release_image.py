"""Publish the worker image that every install runs.

    python release_image.py [git-ref]      # default: main

Builds factory/Dockerfile inside OCI (kaniko) twice, for linux/amd64 (the E4
Container Instance workers) and linux/arm64 (the Free Tier edition's Always
Free Arm VM), and publishes them as ONE multi-architecture tag in a PUBLIC
repository in the tenancy root: <key>.ocir.io/<namespace>/sandbox-factory/worker:release
and :<date>. The root is deliberate: the repository must outlive any install, and
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
    """Build the worker for x86 (Container Instances) and Arm (the Free Tier VM),
    then publish :release and :<stamp> as one multi-architecture tag."""
    import registry_manifest
    cfg = sf.config()
    ns = sf.client(oci.object_storage.ObjectStorageClient).get_namespace().data
    key = next(r.key.lower() for r in sf.client(oci.identity.IdentityClient).list_regions().data if r.name == cfg["region"])
    registry = f"{key}.ocir.io"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    repo = f"{ns}/sandbox-factory/worker"
    arch_tags = {}
    for platform, arch in (("linux/amd64", "amd64"), ("linux/arm64", "arm64")):
        image = f"{registry}/{repo}:{stamp}-{arch}"
        print(f"building {REPO_URL}#{ref} -> {image}", flush=True)
        oci_build.build_in_oci(git_url=f"{REPO_URL}#{ref}", image=image, registry=registry, namespace=ns,
                               sandbox_id=f"release-{arch}", platform=platform, dockerfile="factory/Dockerfile",
                               repo_compartment=cfg["tenancy"])
        arch_tags[arch] = f"{stamp}-{arch}"
    registry_manifest.publish(registry, ns, repo, arch_tags, ["release", stamp])
    print(f"published {registry}/{repo}:release and :{stamp} (amd64 + arm64)", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--starters"]:
        starters(*sys.argv[2:3])
    else:
        main(*sys.argv[1:2])
