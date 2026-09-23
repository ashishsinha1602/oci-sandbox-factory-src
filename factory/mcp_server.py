#!/usr/bin/env python3
"""MCP server for the Sandbox Factory.

Exposes the CLI's functions as tools so Claude (or any MCP client) can create,
deploy, list and destroy sandboxes from a prompt.

    python mcp_server.py            # stdio transport

Claude Code / Claude Desktop config:
    { "mcpServers": { "sandbox-factory": { "command": "python", "args": ["<this file>"] } } }
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os

from mcp.server.fastmcp import FastMCP

import sandbox_factory as sf

mcp = FastMCP("sandbox-factory")


def _args(**kw) -> argparse.Namespace:
    base = dict(
        owner=os.environ.get("SBX_OWNER", ""), team="personal", ttl=7, allowed_cidr="0.0.0.0/0",
        adb=False, adb_tier="free", adb_workload="OLTP",
        kafka=False, kafka_mode="streaming", topics="events",
        app=False, image="docker.io/library/nginx:alpine", shape="CI.Standard.A1.Flex", port=80,
        name="app", tag=None, env=None, keep_stack=False, dry_run=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _capture(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            result = fn(*a, **kw)
        except SystemExit as e:
            return f"{buf.getvalue()}\nERROR: {e}"
    text = buf.getvalue()
    return text if text.strip() else str(result)


@mcp.tool()
def create_sandbox(
    sandbox_id: str,
    owner: str,
    ttl_days: int = 7,
    adb: bool = False,
    adb_tier: str = "free",
    kafka: bool = False,
    kafka_mode: str = "streaming",
    app: bool = False,
    image: str = "docker.io/library/nginx:alpine",
    port: int = 80,
    team: str = "personal",
) -> str:
    """Create (or update) an auto-expiring OCI sandbox.

    sandbox_id: 2-20 chars, lowercase letters/digits/dashes.
    adb: Autonomous Database (adb_tier free = Always Free, paid = ECPU private endpoint).
    kafka: Kafka (kafka_mode streaming = serverless Kafka-compatible, cluster = managed brokers).
    app: run a container image with a public IP on `port`.
    Takes 3-8 minutes. Returns connection details.
    """
    return _capture(sf.cmd_create, _args(
        sandbox_id=sandbox_id, owner=owner, ttl=ttl_days, team=team,
        adb=adb, adb_tier=adb_tier, kafka=kafka, kafka_mode=kafka_mode,
        app=app, image=image, port=port,
    ))


@mcp.tool()
def deploy_app(
    sandbox_id: str,
    path: str,
    owner: str,
    port: int = 8080,
    ttl_days: int = 7,
    adb: bool = False,
    kafka: bool = False,
    name: str = "app",
    shape: str = "CI.Standard.A1.Flex",
) -> str:
    """Build the Dockerfile at `path`, push it to the tenancy registry, and run it in a sandbox.

    Also creates ADB / Kafka when asked; their connection details are injected as env vars.
    Needs an OCIR auth token in ~/.oci/ocir_token or OCIR_TOKEN.
    """
    return _capture(sf.cmd_deploy, _args(
        sandbox_id=sandbox_id, path=path, owner=owner, port=port, ttl=ttl_days,
        adb=adb, kafka=kafka, name=name, shape=shape,
    ))


@mcp.tool()
def list_sandboxes() -> str:
    """List every sandbox with owner and expiry date."""
    return _capture(sf.cmd_list, _args())


@mcp.tool()
def destroy_sandbox(sandbox_id: str) -> str:
    """Destroy a sandbox now (all its resources and its compartment)."""
    return _capture(sf.cmd_destroy, _args(sandbox_id=sandbox_id))


@mcp.tool()
def reap_sandboxes(dry_run: bool = True) -> str:
    """Destroy every sandbox past its expiry date. dry_run lists them without destroying."""
    return _capture(sf.cmd_reap, _args(dry_run=dry_run))


if __name__ == "__main__":
    mcp.run()
