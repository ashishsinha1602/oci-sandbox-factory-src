"""One image tag for both architectures.

The workers run on x86 Container Instances (standard edition) and, in the Free
Tier edition, on an Always Free Arm VM. kaniko builds one architecture per run,
so the release builds the image twice and then publishes a manifest list that
points at both, under the tags every install pulls (:release and :<stamp>).
podman/docker pick the right architecture from it by themselves.

Uses the registry's HTTP API with the same login the builds push with
(OCIR_USER + OCIR_TOKEN, <namespace>/<user>).
"""
from __future__ import annotations

import base64
import json
import os

import requests

MANIFEST_TYPES = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"


def _auth(registry: str, repo: str, namespace: str) -> dict:
    user = os.environ["OCIR_USER"]
    token = os.environ.get("OCIR_TOKEN") or open(os.path.expanduser("~/.oci/ocir_token"), encoding="utf-8").read().strip()
    login = user if "/" in user else f"{namespace}/{user}"
    basic = {"Authorization": "Basic " + base64.b64encode(f"{login}:{token}".encode()).decode()}
    r = requests.get(f"https://{registry}/v2/", headers=basic, timeout=60)
    if r.status_code == 200:
        return basic
    www = r.headers.get("WWW-Authenticate", "")
    realm = www.split('realm="')[1].split('"')[0]
    service = www.split('service="')[1].split('"')[0]
    t = requests.get(realm, params={"service": service, "scope": f"repository:{repo}:pull,push"}, headers=basic, timeout=60)
    t.raise_for_status()
    return {"Authorization": "Bearer " + t.json()["token"]}


def publish(registry: str, namespace: str, repo: str, sources: dict[str, str], tags: list[str]) -> str:
    """sources = {"amd64": "<tag>", "arm64": "<tag>"} already in <registry>/<repo>; returns the list digest."""
    h = _auth(registry, repo, namespace)
    manifests = []
    for arch, tag in sources.items():
        r = requests.get(f"https://{registry}/v2/{repo}/manifests/{tag}", headers={**h, "Accept": MANIFEST_TYPES}, timeout=60)
        r.raise_for_status()
        manifests.append({"mediaType": r.headers["Content-Type"].split(";")[0], "size": len(r.content),
                          "digest": r.headers["Docker-Content-Digest"], "platform": {"architecture": arch, "os": "linux"}})
    oci_index = all(m["mediaType"] == "application/vnd.oci.image.manifest.v1+json" for m in manifests)
    body = {"schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json" if oci_index else "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": manifests}
    digest = ""
    for tag in tags:
        r = requests.put(f"https://{registry}/v2/{repo}/manifests/{tag}", headers={**h, "Content-Type": body["mediaType"]},
                         data=json.dumps(body), timeout=60)
        r.raise_for_status()
        digest = r.headers.get("Docker-Content-Digest", "")
        back = requests.get(f"https://{registry}/v2/{repo}/manifests/{tag}", headers={**h, "Accept": body["mediaType"]}, timeout=60)
        archs = sorted(m["platform"]["architecture"] for m in back.json().get("manifests", []))
        if archs != sorted(sources):
            raise RuntimeError(f"{repo}:{tag} read back with {archs}, expected {sorted(sources)}")
        print(f"published {registry}/{repo}:{tag} for {', '.join(archs)}", flush=True)
    return digest
