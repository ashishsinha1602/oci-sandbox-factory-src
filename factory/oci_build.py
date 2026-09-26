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
import re
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


def dockerfile_missing(repo: str, sub_path: str | None, dockerfile: str | None) -> str | None:
    """A plain sentence when a GitHub folder has no Dockerfile, else None.

    Without this, kaniko starts, fails to find one and prints its whole usage
    text, which is all the user saw. Checked through GitHub's public API;
    other hosts, and any error reaching GitHub, fall through to the build.
    """
    import urllib.request
    m = re.match(r"^https?://github\.com/([^/]+)/([^/#]+?)(?:\.git)?(?:#(.+))?$", repo)
    if not m:
        return None
    owner, name, ref = m.groups()
    # kaniko reads --dockerfile relative to the context (the sub path), so a
    # Dockerfile given as "factory/Dockerfile" lives in <sub_path>/factory/
    target = "/".join(p.strip("/") for p in (sub_path or "", dockerfile or "Dockerfile") if p and p.strip("/"))
    folder, _, dockerfile = target.rpartition("/")
    api = f"https://api.github.com/repos/{owner}/{name}/contents/{folder}" + (f"?ref={ref}" if ref else "")
    try:
        req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "sandbox-factory"})
        with urllib.request.urlopen(req, timeout=15) as r:
            entries = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001  (rate limit, private repo, network: let kaniko decide)
        return None
    if not isinstance(entries, list):
        return None
    names = {e.get("name") for e in entries}
    want = (dockerfile or "Dockerfile").split("/")[-1]
    if want in names:
        return None
    base = re.sub(r"\.git$", "", repo.split("#")[0])
    where = f"{base}/{folder}".rstrip("/")
    py = [n for n in names if n and n.endswith(".py")] or [n for n in names if n in ("dags", "spark", "jobs")]
    hint = (" It looks like a data pipeline (DAGs or Spark jobs): ask the assistant to build it as a pipeline, "
            "not to deploy it as an app.") if py or {"dags", "spark"} & names else " Add a Dockerfile to deploy it as an app."
    return f"No {want} in {where}, so there is no app image to build.{hint}"


def fetch_github_folder(repo: str, sub_path: str | None) -> dict:
    """The text files of one GitHub folder (not its subfolders), as {name: text}."""
    import urllib.request
    m = re.match(r"^https?://github\.com/([^/]+)/([^/#]+?)(?:\.git)?(?:#(.+))?$", repo)
    if not m:
        raise SystemExit(f"cannot read {repo}: only GitHub folders can be read without a Dockerfile")
    owner, name, ref = m.groups()
    folder = (sub_path or "").strip("/")
    api = f"https://api.github.com/repos/{owner}/{name}/contents/{folder}" + (f"?ref={ref}" if ref else "")
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "sandbox-factory"}
    with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=20) as r:
        entries = json.loads(r.read().decode())
    files = {}
    for e in entries if isinstance(entries, list) else []:
        if e.get("type") == "file" and e.get("size", 0) < 200000 and re.search(r"\.(py|txt|json|ya?ml|cfg|toml)$|^Dockerfile$", e["name"]):
            with urllib.request.urlopen(urllib.request.Request(e["download_url"], headers=hdr), timeout=20) as r:
                files[e["name"]] = r.read().decode("utf-8", errors="replace")
    if not files:
        raise SystemExit(f"no code files in {repo}/{folder}")
    return files


def split_tree_url(url: str) -> tuple[str, str | None]:
    """A folder link is what people paste: turn it into repo#branch + sub path.

    https://github.com/o/r/tree/main/examples/app -> ("https://github.com/o/r#main", "examples/app")
    GitLab (/-/tree/) and Gitea (/src/branch/) links work the same way; a plain
    repository URL comes back unchanged with no sub path.
    """
    m = re.match(r"^(https?://[^/]+/[^/]+/[^/]+?)(?:\.git)?/(?:-/tree|-/blob|src/branch|tree|blob)/([^/]+)(?:/(.*?))?/?$", url.strip())
    if not m:
        return url.strip(), None
    repo, ref, sub = m.groups()
    return f"{repo}#{ref}", (sub or None)


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
                 platform: str = "linux/arm64", src_dir: str | None = None,
                 dockerfile: str | None = None, sub_path: str | None = None,
                 repo_compartment: str | None = None, also_tags: list | None = None) -> None:
    """Build an image inside OCI with kaniko.

    dockerfile is relative to the context root; kaniko looks for "Dockerfile"
    there unless told otherwise, so a repository that keeps its Dockerfile in a
    subdirectory needs it named explicitly.
    """
    if git_url:
        # "https://github.com/o/r/tree/main/examples/app" is a folder inside a
        # repository: clone the repository at that branch, build that folder.
        git_url, tree_path = split_tree_url(git_url)
        sub_path = sub_path or tree_path
        missing = dockerfile_missing(git_url, sub_path, dockerfile)
        if missing:
            raise SystemExit(missing)
    fnd = sf.foundation()
    ci = sf.client(oci.container_instances.ContainerInstanceClient)
    idc = sf.client(oci.identity.IdentityClient)
    control = fnd["compartments"]["control"]
    ad = idc.list_availability_domains(control).data[0].name
    user, token = _token()

    # Create the repository in sbx-control before pushing. A push to a
    # repository that does not exist yet auto-creates it in the tenancy root,
    # where neither the workers nor the sandbox services may read it.
    repo = image.split("/", 2)[2].rsplit(":", 1)[0]
    sf.ensure_public_repo(sf.config(), repo_compartment or control, repo)
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
                       f"--destination={image}", "--cache=false", "--snapshot-mode=redo"]
                      + [f"--destination={image.rsplit(':', 1)[0]}:{t}" for t in (also_tags or [])]
                      + ([f"--dockerfile={dockerfile}"] if dockerfile else [])
                      # a project inside a repository: COPY paths are relative to it
                      + ([f"--context-sub-path={sub_path}"] if sub_path else []),
            volume_mounts=[cim.CreateVolumeMountDetails(volume_name="docker-config", mount_path="/kaniko/.docker")]
                          + ([cim.CreateVolumeMountDetails(volume_name="source", mount_path="/workspace")] if src_dir else []),
        )],
    )
    inst = ci.create_container_instance(details).data
    print(f"  build container {name} starting", flush=True)
    started = time.time()
    state = inst.lifecycle_state
    container_id, log = None, ""
    while time.time() - started < TIMEOUT_SECONDS:
        inst = ci.get_container_instance(inst.id).data
        state = inst.lifecycle_state
        if state in ("INACTIVE", "FAILED", "DELETED"):
            break
        print(f"  ... build {state.lower()} {int(time.time() - started)}s", flush=True)
        # Logs can only be read while the container is up (409 once it has
        # exited), so keep the latest copy from each poll for the failure report.
        if state == "ACTIVE":
            try:
                container_id = container_id or ci.list_containers(compartment_id=control, container_instance_id=inst.id).data.items[0].id
                log = ci.retrieve_logs(container_id).data.content.decode(errors="replace") or log
            except Exception:  # noqa: BLE001
                pass
        time.sleep(POLL_SECONDS)

    container = ci.list_containers(compartment_id=control, container_instance_id=inst.id).data.items[0]
    detail = ci.get_container(container.id).data
    try:
        log = ci.retrieve_logs(container.id).data.content.decode(errors="replace") or log
    except Exception as e:  # noqa: BLE001
        if not log:
            print(f"  (no build log: {e})")
    if log:
        lines = log.splitlines()
        # kaniko prints its whole usage after a bad flag or context, which
        # pushes the one line that says what went wrong off the tail: show
        # every error line first, then the tail.
        errors = [l for l in lines if "error" in l.lower() and "--" not in l.split("stderr F", 1)[-1][:12]]
        if errors:
            print("\n".join(errors[:10]), flush=True)
        print("\n".join(l for l in lines[-25:] if l not in errors), flush=True)
    try:
        ci.delete_container_instance(inst.id)
    except Exception:  # noqa: BLE001
        pass
    if state != "INACTIVE" or (detail.exit_code or 0) != 0:
        raise SystemExit(f"image build failed (instance {state}, exit code {detail.exit_code})")
    print(f"  built and pushed {image} in {int(time.time() - started)}s", flush=True)
