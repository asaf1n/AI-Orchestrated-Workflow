# Remote LLM controller → Antigravity

The MCP bridge accepts tasks and corrections from an external chat or agent and forwards them into the *same* Antigravity CLI conversation. It does not act as a second Orchestrator and does not execute arbitrary shell commands received from the client.

## Prerequisites

- Python 3.11+, authenticated `agy` with working `agy -p ... --output-format json`.
- Four installed workflow skills and a project with `AGENTS.md` and `orchestrate_workflow/method.md`.
- A trusted caller. Remote clients need a TLS reverse proxy enforcing authentication and authorization before requests reach the local MCP endpoint.

Install the MCP SDK:

```bash
python -m pip install "mcp>=2,<3"
```

Set `ORCHESTRATE_WORKSPACE` to an absolute path inside the trusted machine. Optional: `ORCHESTRATE_STATE_DIR` (default `~/.local/state/ai-orchestrated-workflow`), `AGY_BIN`, `AGY_MODEL` (only when pinning is required), `AGY_TIMEOUT_SECONDS` (1–7200).

Local MCP client (`stdio`):

```json
{
  "mcpServers": {
    "orchestrate": {
      "command": "python",
      "args": ["-m", "bridge.server"],
      "env": {
        "ORCHESTRATE_WORKSPACE": "/absolute/path/to/project"
      }
    }
  }
}
```

Start a network-accessible MCP endpoint behind an authenticated TLS gateway:

```bash
ORCHESTRATE_WORKSPACE=/absolute/path/to/project \
MCP_PUBLIC_HOST=mcp.example.org \
python -m bridge.server --transport streamable-http --port 8765
```

The server **always** binds `127.0.0.1:8765`. Configure the gateway to expose `https://mcp.example.org/mcp`, enforce caller authentication (for example, OAuth/OIDC at the gateway), rate limits and TLS, and forward the permitted Host/Origin. Do not forward the port directly or allow anonymous internet traffic. Provide the HTTPS MCP URL to a compatible remote LLM chat; the chat must support remote MCP servers. For clients without MCP, use a trusted MCP-capable relay rather than publishing an unauthenticated HTTP API.

## Tools

`agy_dispatch_goal(plan, goal, action="plan")` creates a registered `agy` conversation and returns `conversation_id`, `status`, `response`, exit code and diagnostic. Actions are `plan`, `run` (only for approved plans), `resume`, `status`. The first prompt explicitly invokes `/orchestrate`.

`agy_refine_session(plan, conversation_id, feedback)` passes feedback to **that same** conversation, retaining the host context. The bridge checks that the ID belongs to the named plan. Multiple active owners of a plan are rejected; a filesystem lock prevents overlapping calls in a workspace.

If `agy` times out or returns malformed output, the plan is marked `UNKNOWN`. The bridge does **not** automatically replay the request: inspect the workspace, CLI conversation and active processes first. A stale `active.lock` must be investigated before manual removal. The session registry is stored outside the product checkout with owner-only permissions where supported; do not share it or place it in Git.

Headless permission prompts may be denied without a nonzero code. The bridge returns `BLOCKED` when stderr contains a recognized refusal and always includes the exit code and a diagnostic. Inspect the resulting files and command evidence before accepting an implementation. Configure narrow `agy` permissions in advance; do not disable approval checks as a default.

Calls are sequential: feedback is accepted after the preceding CLI turn returns, not injected into an already-running turn. The bridge preserves conversation identity across MCP client reconnects and process restarts, provided the state directory is retained.

## Checks

```bash
python -m unittest discover -s tests -v
python -m compileall -q bridge
```

Unit tests use a stubbed subprocess and do not require agy or network access. Before use, verify the remote MCP handshake, auth boundary, CLI model, allowed tools and read-only plan/status cycle in the actual environment.

References: [Antigravity headless](https://antigravity.google/docs/cli/headless/), [conversation continuation](https://antigravity.google/docs/cli/conversations/), [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/run/).
