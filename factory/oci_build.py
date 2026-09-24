"""Build a container image inside OCI with kaniko, no Docker daemon anywhere.

A short-lived Container Instance (ARM, in sbx-control's private subnet) runs
gcr.io/kaniko-project/executor with a Git context and pushes the result to
OCI Container Registry. The registry credential is the same auth token the
laptop path uses (OCIR_TOKEN + OCIR_USER), mounted as /kaniko/.docker/config.json.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import time

import oci
from oci.container_instances import models as cim

import sandbox_factory as sf

KANIKO_IMAGE = "gcr.io/kaniko-project/executor:latest"
POLL_SECONDS = 15
TIMEOUT_SECONDS = 1500


def _token() -> tuple[str, str]:
    token = os.environ.get("OCIR_TOKEN")
    token_file = pathlib.Path.home() / ".oci" / "ocir_token"
    if not token and token_file.exists():
        token = token_file.read_text().strip()
    user = os.environ.get("OCIR_USER")
    if not user:
        cfg = sf.config()
        user = sf.client(oci.identity.IdentityClient).get_user(cfg["user"]).data.name
    if not token:
        raise SystemExit("OCIR_TOKEN (or ~/.oci/ocir_token) is required to push the built image")
    return user, token


def git_context(url: str) -> str:
    """kaniko wants git://host/org/repo.git[#refs/heads/branch]."""
    u = url.strip()
    if u.startswith("git://"):
        return u
    ref = ""
    if "#" in u:
        u, ref = u.split("#", 1)
        ref = "#refs/heads/" + ref if not ref.startswith("refs/") else "#" + ref
    for prefix in ("https://", "http://", "ssh://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("git@"):
        u = u[4:].replace(":", "/", 1)
    if not u.endswith(".git"):
        u += ".git"
    return "git://" + u + ref


def _source_volume(src_dir: str):
    """The build context as a config-file volume kaniko can read.

    A container config volume holds a flat set of files - "/" is not allowed in
    a file name - which is enough for the starter templates. Anything with
    subdirectories has to come in as a Git URL instead.
    """
    files, total = [], 0
    for p in sorted(pathlib.Path(src_dir).iterdir()):
        if not p.is_file():
            raise SystemExit(
                f"{p.name} is a directory; a local build context must be flat. "
                "Use a Git URL for a project with subdirectories.")
        data = p.read_bytes()
        total += len(data)
        files.append(cim.ContainerConfigFile(
            file_name=p.name, data=base64.b64encode(data).decode()))
    if not any(f.file_name == "Dockerfile" for f in files):
        raise SystemExit(f"no Dockerfile in {src_dir}")
    print(f"  shipping {len(files)} source file(s), {total/1024:.1f} KB, to the builder", flush=True)
    return cim.CreateContainerConfigFileVolumeDetails(name="source", configs=files)


def build_in_oci(git_url: str | None, image: str, registry: str, namespace: str, sandbox_id: str,
                 platform: str = "linux/arm64", src_dir: str | None = None) -> None:
    fnd = sf.foundation()
    ci = sf.client(oci.container_instances.ContainerInstanceClient)
    idc = sf.client(oci.identity.IdentityClient)
    control = fnd["compartments"]["control"]
    ad = idc.list_availability_domains(control).data[0].name
    user, token = _token()

    docker_config = json.dumps({"auths": {registry: {"auth": base64.b64encode(f"{namespace}/{user}:{token}".encode()).decode()}}})
    shape = "CI.Standard.A1.Flex" if platform.endswith("arm64") else "CI.Standard.E4.Flex"
    name = f"sbx-build-{sandbox_id}-{dt.datetime.now(dt.timezone.utc):%H%M%S}"

    details = cim.CreateContainerInstanceDetails(
        compartment_id=control,
        availability_domain=ad,
        display_name=name,
        shape=shape,
        # 1 OCPU / 4 GB, not 2 / 8. The A1 core quota on compartment sbx is 4 and
        # the worker plus a live sandbox app already hold 3, so a 2-core builder
        # is refused with QuotaExceeded: standard-a1-core-count. Builds are
        # transient, so the smaller builder is the right trade; override with
        # SBX_BUILD_OCPUS / SBX_BUILD_MEMORY_GB if a build needs more.
        shape_config=cim.CreateContainerInstanceShapeConfigDetails(
            ocpus=float(os.environ.get("SBX_BUILD_OCPUS", "1")),
            memory_in_gbs=float(os.environ.get("SBX_BUILD_MEMORY_GB", "4")),
        ),
        container_restart_policy="NEVER",
        freeform_tags={"managed_by": "sandbox-factory", "sandbox_id": sandbox_id, "role": "build"},
        vnics=[cim.CreateContainerVnicDetails(subnet_id=fnd["network"]["private_subnet_id"], is_public_ip_assigned=False)],
        volumes=[cim.CreateContainerConfigFileVolumeDetails(
            name="docker-config",
            configs=[cim.ContainerConfigFile(file_name="config.json", data=base64.b64encode(docker_config.encode()).decode())],
        )] + ([_source_volume(src_dir)] if src_dir else []),
        containers=[cim.CreateContainerDetails(
            display_name="kaniko",
            image_url=KANIKO_IMAGE,
            arguments=[f"--context={'dir:///workspace' if src_dir else git_context(git_url)}",
                       f"--destination={image}", "--cache=false", "--snapshot-mode=redo"],
            volume_mounts=[cim.CreateVolumeMountDetails(volume_name="docker-config", mount_path="/kaniko/.docker")]
                          + ([cim.CreateVolumeMountDetails(volume_name="source", mount_path="/workspace")] if src_dir else []),
        )],
    )
    inst = ci.create_container_instance(details).data
    print(f"  build container {name} starting", flush=True)
    started = time.time()
    state = inst.lifecycle_state
    while time.time() - started < TIMEOUT_SECONDS:
        inst = ci.get_container_instance(inst.id).data
        state = inst.lifecycle_state
        if state in ("INACTIVE", "FAILED", "DELETED"):
            break
        print(f"  ... build {state.lower()} {int(time.time() - started)}s", flush=True)
        time.sleep(POLL_SECONDS)

    container = ci.list_containers(compartment_id=control, container_instance_id=inst.id).data.items[0]
    detail = ci.get_container(container.id).data
    try:
        log = ci.retrieve_logs(container.id).data.content.decode(errors="replace")
        print("\n".join(log.splitlines()[-25:]), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  (no build log: {e})")
    try:
        ci.delete_container_instance(inst.id)
    except Exception:  # noqa: BLE001
        pass
    if state != "INACTIVE" or (detail.exit_code or 0) != 0:
        raise SystemExit(f"image build failed (instance {state}, exit code {detail.exit_code})")
    print(f"  built and pushed {image} in {int(time.time() - started)}s", flush=True)
