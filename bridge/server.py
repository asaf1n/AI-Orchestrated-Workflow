"""MCP tools for authenticated remote LLM controllers.

Run on loopback. Publish only behind an authenticated TLS reverse proxy.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from bridge.agy_bridge import AgyBridge, BridgeError


def build_server():
    # Optional dependency is imported only when the MCP server starts.
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    workspace = os.environ.get("ORCHESTRATE_WORKSPACE")
    if not workspace:
        raise RuntimeError("ORCHESTRATE_WORKSPACE must identify an approved project")
    state_dir = os.environ.get(
        "ORCHESTRATE_STATE_DIR", str(Path.home() / ".local/state/ai-orchestrated-workflow")
    )
    gateway = AgyBridge(
        workspace=workspace,
        state_dir=state_dir,
        agy_binary=os.environ.get("AGY_BIN", "agy"),
        model=os.environ.get("AGY_MODEL") or None,
        timeout_seconds=int(os.environ.get("AGY_TIMEOUT_SECONDS", "1200")),
    )
    server = MCPServer("AI-Orchestrated-Workflow")

    def _run(fn, *args):
        try:
            return fn(*args)
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def agy_dispatch_goal(plan: str, goal: str, action: str = "plan") -> dict:
        """Start the sole agy orchestration session for a named plan.

        action: plan (default), run (approved plan only), resume or status.
        Execution remains subject to the workflow's explicit permissions.
        """
        return _run(gateway.dispatch_goal, plan, goal, action)

    @server.tool()
    def agy_refine_session(plan: str, conversation_id: str, feedback: str) -> dict:
        """Send a correction to the same registered agy conversation."""
        return _run(gateway.refine_session, plan, conversation_id, feedback)

    @server.tool()
    def agy_session_info(plan: str) -> dict:
        """Return the stored conversation_id, status and last response of a plan.

        Read-only; use it after a client-side timeout instead of re-dispatching.
        """
        return _run(gateway.session_info, plan)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP bridge to agy orchestration")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
        return
    # Accept an external hostname only when explicitly configured for a trusted proxy.
    hostname = os.environ.get("MCP_PUBLIC_HOST")
    security = {}
    if hostname:
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname):
            raise ValueError("MCP_PUBLIC_HOST must be a DNS hostname")
        from mcp.server.transport_security import TransportSecuritySettings
        security["transport_security"] = TransportSecuritySettings(
            allowed_hosts=[hostname, hostname + ":443", "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://" + hostname],
        )
    server.run(transport="streamable-http", host="127.0.0.1", port=args.port, **security)


if __name__ == "__main__":
    main()
