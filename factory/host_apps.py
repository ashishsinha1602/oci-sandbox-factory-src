"""Applications on the Free Tier worker VM.

Container Instances and API Gateway are not part of Always Free, so on the
Free Tier edition a sandbox's containers run on the worker VM itself, as one
podman pod per sandbox (the containers share localhost, like a Container
Instance), reachable at http://<vm public ip>:<port>. The worker talks to
podman on the host through its socket, mounted into the worker container.

    deploy(sandbox_id, containers, env, owner, expires) -> {"url", "urls", "containers", "host"}
    remove(sandbox_id)          pod + built images gone
    alive_ids()                 sandboxes that still have a pod (for reconcile)
    reap(now_iso)               pods past their expiry label, removed

Images from a Dockerfile are built here too (podman build), so nothing is
pushed to a registry. Everything is labelled managed_by=sandbox-factory.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import pathlib
import socket
import tarfile
import time
import urllib.parse
import urllib.request

SOCKET = os.environ.get("SBX_PODMAN_SOCKET", "/run/podman/podman.sock")
API = "/v4.0.0/libpod"
PORT_MIN, PORT_MAX = 8101, 8199
LABEL = "sandbox-factory"


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _call(method: str, path: str, body=None, content_type: str = "application/json", timeout: float = 600) -> tuple[int, bytes]:
    c = _UnixConn(SOCKET, timeout)
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    c.request(method, API + path, body=data, headers={"Content-Type": content_type} if data is not None else {})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def available() -> bool:
    if not os.path.exists(SOCKET):
        return False
    try:
        st, _ = _call("GET", "/_ping", timeout=10)
        return st == 200
    except Exception:  # noqa: BLE001
        return False


def _stream_error(raw: bytes) -> str | None:
    """podman streams JSON lines; an error is a line with an 'error' key."""
    for line in raw.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("error"):
            return str(j["error"])[:400]
    return None


def pull(image: str) -> None:
    st, raw = _call("POST", "/images/pull?reference=" + urllib.parse.quote(image, safe=""), body=b"", content_type="application/json", timeout=1200)
    err = _stream_error(raw)
    if st >= 300 or err:
        raise RuntimeError(f"pull {image}: HTTP {st} {err or raw[:300].decode(errors='replace')}")


def build(context_dir: str, tag: str) -> None:
    """podman build of a folder with a Dockerfile, tagged localhost/<tag>."""
    buf = io.BytesIO()
    root = pathlib.Path(context_dir)
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(root).as_posix())
    q = urllib.parse.urlencode({"t": tag, "dockerfile": "Dockerfile", "pull": "true", "rm": "true"})
    st, raw = _call("POST", "/build?" + q, body=buf.getvalue(), content_type="application/x-tar", timeout=3600)
    text = raw.decode(errors="replace")
    err = _stream_error(raw)
    # the stream is JSON lines {"stream": "..."}; the build's own words go to the request log
    steps = []
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("stream"):
            steps.append(str(j["stream"]).rstrip())
    for line in steps[-12:]:
        print("  " + line[:160], flush=True)
    if st >= 300 or err:
        raise RuntimeError(f"build {tag}: HTTP {st} {err or ''}\n" + "\n".join(steps[-10:]))
    # a build can answer 200 and still leave no image (a refused tag, a silent failure): check the store
    st2, _ = _call("GET", "/images/" + urllib.parse.quote(tag, safe="") + "/exists", timeout=60)
    if st2 != 204:
        raise RuntimeError(f"build {tag} finished (HTTP {st}) but the image is not in the store (exists -> HTTP {st2}). Build output:\n"
                           + "\n".join(steps[-15:]) + "\nraw tail: " + text[-800:])


def pods() -> list[dict]:
    st, raw = _call("GET", "/pods/json?" + urllib.parse.urlencode({"filters": json.dumps({"label": [f"managed_by={LABEL}"]})}), timeout=60)
    if st != 200:
        raise RuntimeError(f"pods: HTTP {st} {raw[:200]!r}")
    return json.loads(raw or b"[]") or []


def _used_ports() -> set[int]:
    used = set()
    for p in pods():
        for v in (p.get("Labels") or {}).get("ports", "").split(","):
            if v.strip().isdigit():
                used.add(int(v))
    return used


def _free_ports(n: int) -> list[int]:
    used = _used_ports()
    out = []
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used:
            out.append(port)
            if len(out) == n:
                return out
    raise RuntimeError(f"no free ports left on the worker VM ({PORT_MIN}-{PORT_MAX} all in use); destroy a sandbox first")


_IP = {}


def public_ip() -> str:
    """The worker VM's public address. The instance metadata names the VNIC but not
    its public IP, so the VNIC is read through the API (instance principal)."""
    if "ip" in _IP:
        return _IP["ip"]
    req = urllib.request.Request("http://169.254.169.254/opc/v2/vnics/", headers={"Authorization": "Bearer Oracle"})
    with urllib.request.urlopen(req, timeout=5) as r:
        vnics = json.load(r)
    ip = ""
    try:
        import oci
        import sandbox_factory as sf
        net = sf.client(oci.core.VirtualNetworkClient)
        for v in vnics:
            pub = net.get_vnic(v["vnicId"]).data.public_ip
            if pub:
                ip = pub
                break
    except Exception as e:  # noqa: BLE001
        print(f"public ip lookup failed ({type(e).__name__}: {str(e)[:120]}); using the private address", flush=True)
    _IP["ip"] = ip or next((v.get("privateIp") for v in vnics), "")
    return _IP["ip"]


def remove(sandbox_id: str, images: bool = True) -> bool:
    """The sandbox's pod, and (on destroy) the images built for it. A deploy
    replaces the pod but must keep the image it has just built."""
    name = f"sbx-{sandbox_id}"
    st, raw = _call("DELETE", f"/pods/{name}?force=true", timeout=300)
    gone = st in (200, 204)
    if st not in (200, 204, 404):
        raise RuntimeError(f"remove pod {name}: HTTP {st} {raw[:200]!r}")
    if not images:
        return gone
    # images built for this sandbox
    st, raw = _call("GET", "/images/json", timeout=60)
    for img in json.loads(raw or b"[]") if st == 200 else []:
        for t in img.get("Names") or img.get("RepoTags") or []:
            if f"/sbx-{sandbox_id}-" in t:
                _call("DELETE", "/images/" + urllib.parse.quote(t, safe="") + "?force=true", timeout=120)
    return gone


def deploy(sandbox_id: str, containers: list[dict], env: dict, owner: str, expires: str) -> dict:
    """One pod, one host port per container that exposes one; the first container is the sandbox's URL."""
    name = f"sbx-{sandbox_id}"
    remove(sandbox_id, images=False)                     # a re-run replaces the pod, keeps the image just built
    for c in containers:
        if not str(c["image"]).startswith("localhost/"):
            print(f"pulling {c['image']}", flush=True)
            pull(c["image"])
    ports = _free_ports(len([c for c in containers if c.get("port")]))
    mappings, assigned = [], {}
    for c in containers:
        if c.get("port"):
            hp = ports.pop(0)
            assigned[c["name"]] = hp
            mappings.append({"host_port": hp, "container_port": int(c["port"]), "protocol": "tcp"})
    labels = {"managed_by": LABEL, "sandbox_id": sandbox_id, "owner": owner or "", "expires": expires,
              "ports": ",".join(str(p) for p in assigned.values())}
    st, raw = _call("POST", "/pods/create", body={"name": name, "portmappings": mappings, "labels": labels}, timeout=120)
    if st not in (200, 201):
        raise RuntimeError(f"pod {name}: HTTP {st} {raw[:300]!r}")
    for c in containers:
        spec = {"name": f"{name}-{c['name']}", "image": c["image"], "pod": name,
                "env": {str(k): str(v) for k, v in {**env, **(c.get("env") or {})}.items()},
                "labels": labels, "restart_policy": "always"}
        if c.get("command"):
            spec["entrypoint"] = list(c["command"])
        if c.get("args"):
            spec["command"] = list(c["args"])
        st, raw = _call("POST", "/containers/create", body=spec, timeout=120)
        if st not in (200, 201):
            raise RuntimeError(f"container {spec['name']}: HTTP {st} {raw[:300]!r}")
        cid = json.loads(raw)["Id"]
        st, raw = _call("POST", f"/containers/{cid}/start", body=b"", timeout=120)
        if st not in (200, 204, 304):
            raise RuntimeError(f"start {spec['name']}: HTTP {st} {raw[:300]!r}")
        print(f"started {spec['name']} ({c['image']})" + (f" on port {assigned[c['name']]}" if c["name"] in assigned else ""), flush=True)
    ip = public_ip()
    urls = [f"http://{ip}:{p}" for p in assigned.values()]
    out = {"url": urls[0] if urls else None, "urls": urls, "host": ip, "pod": name,
           "containers": [{"name": c["name"], "image": c["image"], "port": c.get("port"),
                           "url": f"http://{ip}:{assigned[c['name']]}" if c["name"] in assigned else None} for c in containers],
           "where": "the Free Tier worker VM (podman); HTTP, no gateway"}
    # give the first container a moment, then say whether it answers
    if urls:
        time.sleep(5)
        try:
            with urllib.request.urlopen(urls[0], timeout=15) as r:
                out["first_response"] = r.status
        except Exception as e:  # noqa: BLE001  (many apps take longer; the card still shows the URL)
            out["first_response"] = f"not yet ({type(e).__name__})"
    return out


def alive_ids() -> set[str]:
    try:
        return {(p.get("Labels") or {}).get("sandbox_id") for p in pods() if (p.get("Labels") or {}).get("sandbox_id")}
    except Exception:  # noqa: BLE001
        return set()


def reap(now_iso: str) -> list[str]:
    gone = []
    for p in pods():
        lab = p.get("Labels") or {}
        if lab.get("expires") and lab["expires"] < now_iso and lab.get("sandbox_id"):
            print(f"{lab['sandbox_id']} expired {lab['expires']} (Free Tier VM)", flush=True)
            remove(lab["sandbox_id"])
            gone.append(lab["sandbox_id"])
    return gone
