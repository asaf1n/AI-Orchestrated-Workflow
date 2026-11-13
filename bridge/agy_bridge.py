"""Serialized, persistent handoff from an authenticated external client to agy."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Iterator


class BridgeError(RuntimeError):
    """An actionable dispatch failure; never interpreted as execution success."""


class AgyBridge:
    ACTIONS = {"plan", "run", "resume", "status"}
    PLAN_RE = re.compile(r"^[\w.-]{1,80}$", re.UNICODE)
    CONVERSATION_RE = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")
    DENIAL_RE = re.compile(
        r"permission.{0,90}(denied|skipped)|soft.?deny|approval required|not approved",
        re.IGNORECASE,
    )

    def __init__(
        self,
        workspace: Path | str,
        state_dir: Path | str,
        agy_binary: str = "agy",
        model: str | None = None,
        timeout_seconds: int = 1200,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir() or not (self.workspace / "AGENTS.md").is_file():
            raise BridgeError("Workspace must exist and contain AGENTS.md")
        if not (self.workspace / "orchestrate_workflow" / "method.md").is_file():
            raise BridgeError("Workspace requires orchestrate_workflow/method.md")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.agy_binary = agy_binary
        self.model = model
        if not 1 <= timeout_seconds <= 7200:
            raise ValueError("timeout_seconds must be between 1 and 7200")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def _validate_text(cls, value: str, field: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 12000:
            raise BridgeError(f"{field} must contain 1..12000 nonblank characters")
        return value.strip()

    @classmethod
    def _validate_plan(cls, plan: str) -> str:
        if not isinstance(plan, str) or not cls.PLAN_RE.fullmatch(plan) or plan in {".", ".."}:
            raise BridgeError("Invalid plan name")
        return plan

    def _load(self) -> dict[str, Any]:
        path = self.state_dir / "sessions.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise BridgeError("Invalid session registry")
        return data

    def _store(self, data: dict[str, Any]) -> None:
        target = self.state_dir / "sessions.json"
        fd, name = tempfile.mkstemp(prefix=".sessions-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(name, 0o600)
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # Held for the whole CLI turn, not just while updating sessions.json.
        # On process crash the lock remains; recovery must be explicit.
        path = self.state_dir / "active.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BridgeError("An orchestration turn is active or needs recovery") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(f"pid={os.getpid()}\nworkspace={self.workspace}\n")
            yield
        finally:
            path.unlink(missing_ok=True)

    def _invoke(self, prompt: str, conversation_id: str | None = None) -> dict[str, Any]:
        argv = [self.agy_binary, "-p", prompt, "--output-format", "json",
                "--print-timeout", f"{self.timeout_seconds}s"]
        if self.model and not conversation_id:
            argv.extend(["--model", self.model])
        if conversation_id:
            argv.extend(["--conversation", conversation_id])
        try:
            completed = subprocess.run(
                argv, cwd=self.workspace, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds + 15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("agy timed out; effects are unknown, inspect workspace before retry") from exc
        except OSError as exc:
            raise BridgeError(f"agy could not start: {exc}") from exc
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"agy returned invalid JSON (exit={completed.returncode})") from exc
        if not isinstance(payload, dict):
            raise BridgeError("agy JSON result must be an object")
        status = str(payload.get("status", "UNKNOWN")).upper()
        warning = completed.stderr.strip()[:2000]
        if self.DENIAL_RE.search(warning):
            status = "BLOCKED"
        elif completed.returncode != 0 and status == "SUCCESS":
            status = "ERROR"
        raw_id = payload.get("conversation_id") or conversation_id
        if raw_id is not None and (
            not isinstance(raw_id, str) or not self.CONVERSATION_RE.fullmatch(raw_id)
        ):
            raise BridgeError("agy returned invalid conversation_id")
        response = payload.get("response", "")
        response = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return {
            "conversation_id": raw_id,
            "status": status,
            "response": response[:20000],
            "truncated": len(response) > 20000,
            "exit_code": completed.returncode,
            "diagnostic": warning,
        }

    def dispatch_goal(self, plan: str, goal: str, action: str = "plan") -> dict[str, Any]:
        plan = self._validate_plan(plan)
        goal = self._validate_text(goal, "goal")
        if action not in self.ACTIONS:
            raise BridgeError("action must be plan, run, resume or status")
        prompts = {
            "plan": f"Создай план {plan}. Цель: {goal}. Исполнение не запускай.",
            "run": f"Запусти утверждённый план {plan}. {goal}",
            "resume": f"Продолжи запущенный план {plan}. Сверь состояние и процессы. {goal}",
            "status": f"Доложи подтверждённое состояние плана {plan}. Исполнение не запускай. {goal}",
        }
        with self._lock():
            registry = self._load()
            if plan in registry:
                raise BridgeError("Plan already registered; use agy_refine_session")
            try:
                result = self._invoke("/orchestrate " + prompts[action])
            except BridgeError:
                registry[plan] = {"conversation_id": None, "status": "UNKNOWN"}
                self._store(registry)
                raise
            registry[plan] = {
                "conversation_id": result["conversation_id"], "status": result["status"]
            }
            self._store(registry)
            return {"plan": plan, **result}

    def refine_session(self, plan: str, conversation_id: str, feedback: str) -> dict[str, Any]:
        plan = self._validate_plan(plan)
        feedback = self._validate_text(feedback, "feedback")
        if not isinstance(conversation_id, str) or not self.CONVERSATION_RE.fullmatch(conversation_id):
            raise BridgeError("Invalid conversation_id")
        with self._lock():
            registry = self._load()
            entry = registry.get(plan)
            if not entry or entry.get("conversation_id") != conversation_id:
                raise BridgeError("Conversation is not registered for this plan")
            try:
                result = self._invoke(feedback, conversation_id=conversation_id)
            except BridgeError:
                entry["status"] = "UNKNOWN"
                self._store(registry)
                raise
            entry.update(conversation_id=result["conversation_id"], status=result["status"])
            self._store(registry)
            return {"plan": plan, **result}
