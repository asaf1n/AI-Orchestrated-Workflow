"""Unit checks for the local agy handoff protocol (no live agy required)."""

from pathlib import Path
import asyncio
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bridge.agy_bridge import AgyBridge, BridgeError
from bridge.server import build_server


CONVERSATION = "055a398f-db14-4c5f-abbb-1bf03f8120a7"


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("# Project\n", encoding="utf-8")
        (self.workspace / "orchestrate_workflow").mkdir()
        (self.workspace / "orchestrate_workflow" / "method.md").write_text("# Binding\n", encoding="utf-8")
        self.bridge = AgyBridge(self.workspace, root / "state", model="Orchestrator (Argon)")

    def reply(self, status="SUCCESS", stderr="", returncode=0):
        body = {"conversation_id": CONVERSATION, "status": status, "response": "ok"}
        return subprocess.CompletedProcess([], returncode, json.dumps(body), stderr)

    @patch("bridge.agy_bridge.subprocess.run")
    def test_dispatch_and_refine_same_session(self, run):
        run.return_value = self.reply()
        result = self.bridge.dispatch_goal("Сборка", "Сформировать план")
        self.assertEqual(result["conversation_id"], CONVERSATION)
        self.assertEqual(result["status"], "SUCCESS")
        first = run.call_args.args[0]
        self.assertIn("/orchestrate Создай план Сборка", first[2])
        self.assertNotIn("--dangerously-skip-permissions", first)
        self.bridge.refine_session("Сборка", CONVERSATION, "Учти новый критерий")
        second = run.call_args.args[0]
        self.assertEqual(second[second.index("--conversation") + 1], CONVERSATION)
        self.assertNotIn("--model", second)
        self.assertEqual(run.call_args.kwargs["cwd"], self.workspace)
        stored = json.loads((self.bridge.state_dir / "sessions.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["Сборка"]["conversation_id"], CONVERSATION)

    @patch("bridge.agy_bridge.subprocess.run")
    def test_cannot_start_second_owner_or_hijack(self, run):
        run.return_value = self.reply()
        self.bridge.dispatch_goal("plan", "Goal")
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_goal("plan", "New goal")
        with self.assertRaises(BridgeError):
            self.bridge.refine_session("other", CONVERSATION, "Message")
        self.assertEqual(run.call_count, 1)

    @patch("bridge.agy_bridge.subprocess.run")
    def test_permission_soft_denial_is_not_success(self, run):
        run.return_value = self.reply(stderr="permission denied: write_file skipped")
        result = self.bridge.dispatch_goal("plan", "Goal", action="run")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["exit_code"], 0)

    @patch("bridge.agy_bridge.subprocess.run")
    def test_timeout_preserves_uncertain_state(self, run):
        run.side_effect = subprocess.TimeoutExpired("agy", 20)
        with self.assertRaisesRegex(BridgeError, "effects are unknown"):
            self.bridge.dispatch_goal("plan", "Goal")
        state = json.loads((self.bridge.state_dir / "sessions.json").read_text(encoding="utf-8"))
        self.assertEqual(state["plan"]["status"], "UNKNOWN")
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_goal("plan", "Retry")

    @patch("bridge.agy_bridge.subprocess.run")
    def test_invalid_input_does_not_invoke_cli(self, run):
        for name in ("../escape", "a/b", "", ".."):
            with self.assertRaises(BridgeError):
                self.bridge.dispatch_goal(name, "Goal")
        self.assertFalse(run.called)

    @patch("bridge.agy_bridge.subprocess.run")
    def test_missing_conversation_id_is_not_success(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"status": "SUCCESS", "response": "ok"}), "")
        result = self.bridge.dispatch_goal("plan", "Goal")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["conversation_id"])

    @patch("bridge.agy_bridge.subprocess.run")
    def test_feedback_is_a_correction_not_a_slash_command(self, run):
        run.return_value = self.reply()
        self.bridge.dispatch_goal("plan", "Goal")
        self.bridge.refine_session("plan", CONVERSATION, "/model other")
        self.assertTrue(run.call_args.args[0][2].startswith("Корректировка текущей сессии"))

    @patch("bridge.agy_bridge.subprocess.run")
    def test_session_info_recovers_result_without_cli(self, run):
        run.return_value = self.reply()
        self.bridge.dispatch_goal("plan", "Goal")
        info = self.bridge.session_info("plan")
        self.assertEqual(info["conversation_id"], CONVERSATION)
        self.assertEqual(info["response"], "ok")
        self.assertFalse(info["active_turn"])
        self.assertEqual(run.call_count, 1)
        with self.assertRaises(BridgeError):
            self.bridge.session_info("missing")

    def test_busy_lock_prevents_parallel_calls(self):
        (self.bridge.state_dir / "active.lock").write_text("pid=1")
        with self.assertRaisesRegex(BridgeError, "active or needs recovery"):
            self.bridge.dispatch_goal("plan", "Goal")

    @unittest.skipIf(importlib.util.find_spec("mcp") is None, "mcp SDK is optional")
    def test_mcp_tool_surfaces_bridge_error_message(self):
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

        with patch.dict(
            os.environ,
            {
                "ORCHESTRATE_WORKSPACE": str(self.workspace),
                "ORCHESTRATE_STATE_DIR": str(self.bridge.state_dir),
            },
        ):
            server = build_server()
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(server.call_tool("agy_session_info", {"plan": "missing"}))
        self.assertNotIsInstance(ctx.exception, UnexpectedToolError)
        self.assertIn("Plan is not registered", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
