from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


EXECUTOR_CONTRACT_VERSION = 1
EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION = 1
EXECUTOR_RESULT_CONTRACT_VERSION = 1
EXECUTOR_WATCHDOG_CONTRACT_VERSION = 1
CAPABILITY_CLASSIFICATION_SCHEMA_VERSION = 1
DAGGER_ENABLE_ENV = "ENGINEERING_HARNESS_ENABLE_DAGGER"
OPENHANDS_ENABLE_ENV = "ENGINEERING_HARNESS_ENABLE_OPENHANDS"
OPENHANDS_BINARY_ENV = "ENGINEERING_HARNESS_OPENHANDS_BINARY"
TASK_AGENT_DEVELOPER_BINARY_ENV = "ENGINEERING_HARNESS_TASK_AGENT_DEVELOPER_BINARY"
HARNESS_SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
SENSITIVE_ENV_NAME_PATTERN = (
    r"[A-Z0-9_]*(?:API[-_]?KEY|ACCESS[-_]?KEY|TOKEN|SECRET|PASSWORD|PASS|"
    r"PRIVATE[-_]?KEY|MNEMONIC|SEED(?:[-_]?PHRASE)?|CREDENTIALS?)[A-Z0-9_]*"
)
SENSITIVE_QUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*[:=]\s*)(['\"])(.*?)(\3)"
)
SENSITIVE_UNQUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*=\s*)([^\s\"'`]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
OPENAI_STYLE_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{8,})\b")
CAPABILITY_CLASS_BY_NAME = {
    "agent": "agent",
    "artifact_collection": "observability",
    "browser_automation": "network",
    "containerized_execution": "filesystem",
    "deployment": "deploy",
    "deploy": "deploy",
    "exit_code": "observability",
    "filesystem_escape": "filesystem",
    "host_filesystem_write": "filesystem",
    "live": "deploy",
    "live_operations": "deploy",
    "local_dagger_cli": "process",
    "local_openhands_cli": "process",
    "local_process": "process",
    "local_task_agent_developer_cli": "process",
    "network": "network",
    "network_access": "network",
    "requires_explicit_configuration": "configuration",
    "secret_access": "secret",
    "secrets": "secret",
    "stderr": "observability",
    "stdout": "observability",
    "workspace_write": "filesystem",
}
CORE_CAPABILITY_CLASSES = ("filesystem", "network", "secret", "deploy")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(text: str) -> str:
    redacted = str(text)
    redacted = SENSITIVE_QUOTED_VALUE_RE.sub(r"\1\2\3[REDACTED]\5", redacted)
    redacted = SENSITIVE_UNQUOTED_VALUE_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = BEARER_TOKEN_RE.sub(r"\1[REDACTED]", redacted)
    redacted = OPENAI_STYLE_TOKEN_RE.sub("[REDACTED]", redacted)
    return redacted


def classify_capabilities(capabilities: tuple[str, ...] | list[str]) -> dict[str, Any]:
    classes: dict[str, list[str]] = {class_name: [] for class_name in CORE_CAPABILITY_CLASSES}
    for class_name in ("process", "observability", "agent", "configuration"):
        classes.setdefault(class_name, [])
    unknown: list[str] = []
    for capability in capabilities:
        name = str(capability).strip()
        if not name:
            continue
        class_name = CAPABILITY_CLASS_BY_NAME.get(name)
        if class_name is None:
            unknown.append(name)
            continue
        classes.setdefault(class_name, []).append(name)
    classes = {key: sorted(dict.fromkeys(value)) for key, value in classes.items()}
    return {
        "schema_version": CAPABILITY_CLASSIFICATION_SCHEMA_VERSION,
        "classes": classes,
        "core_classes": {
            class_name: {
                "capabilities": classes.get(class_name, []),
                "supported": bool(classes.get(class_name)),
            }
            for class_name in CORE_CAPABILITY_CLASSES
        },
        "unknown": sorted(dict.fromkeys(unknown)),
    }


@dataclass(frozen=True)
class ExecutorMetadata:
    id: str
    name: str
    kind: str
    adapter: str
    input_mode: str
    capabilities: tuple[str, ...]
    requires_agent_approval: bool = False
    uses_command_policy: bool = False

    def as_contract(self) -> dict[str, Any]:
        capability_classifications = classify_capabilities(self.capabilities)
        return {
            "schema_version": EXECUTOR_CONTRACT_VERSION,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "adapter": self.adapter,
            "input_mode": self.input_mode,
            "capabilities": list(self.capabilities),
            "capability_classifications": capability_classifications,
            "requires_agent_approval": self.requires_agent_approval,
            "uses_command_policy": self.uses_command_policy,
        }


@dataclass(frozen=True)
class ExecutorInvocation:
    project_root: Path
    task_id: str
    name: str
    command: str | None
    prompt: str | None
    timeout_seconds: int
    model: str | None = None
    sandbox: str = "workspace-write"
    environment: dict[str, str] = field(default_factory=dict)
    phase: str | None = None
    no_progress_timeout_seconds: int | None = None
    context_pack: dict[str, Any] | None = None
    progress_callback: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False, compare=False)

    def env(self) -> dict[str, str]:
        env = {**os.environ, **self.environment, "ENGINEERING_HARNESS": "1"}
        pythonpath_entries = [
            str(HARNESS_SOURCE_ROOT),
            *(entry for entry in str(env.get("PYTHONPATH", "")).split(os.pathsep) if entry),
        ]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath_entries))
        return env


@dataclass(frozen=True)
class ExecutorTaskCommand:
    name: str
    command: str | None
    prompt: str | None
    executor: str
    spec_refs: tuple[str, ...] = ()

    def summary(self) -> str:
        summary = self.command or self.prompt or self.executor
        if self.spec_refs:
            summary = f"{summary} [spec_refs: {', '.join(self.spec_refs)}]"
        return summary


@dataclass(frozen=True)
class ExecutorTaskContext:
    project_root: Path
    task_id: str
    title: str
    milestone_id: str
    milestone_title: str
    spec_refs: tuple[str, ...]
    file_scope: tuple[str, ...]
    acceptance: tuple[ExecutorTaskCommand, ...]
    e2e: tuple[ExecutorTaskCommand, ...]
    phase: str | None = None
    current_command: ExecutorTaskCommand | None = None
    relevant_spec_refs: tuple[str, ...] = ()
    requirement_excerpts: tuple[dict[str, Any], ...] = ()
    context_pack: dict[str, Any] | None = None


def _format_spec_refs(refs: tuple[str, ...]) -> str:
    return "\n".join(f"- {ref}" for ref in refs) or "- none declared"


def _format_requirement_excerpts(requirements: tuple[dict[str, Any], ...]) -> str:
    if not requirements:
        return "- none available"
    chunks: list[str] = []
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "").strip() or "unknown"
        title = str(requirement.get("title") or "").strip()
        source_path = str(requirement.get("source_path") or "").strip()
        excerpt = str(requirement.get("excerpt") or requirement.get("message") or "").strip()
        heading = requirement_id if not title else f"{requirement_id}: {title}"
        chunks.append(f"### {heading}")
        if source_path:
            chunks.append(f"Source: {source_path}")
        chunks.append(excerpt or "No bounded excerpt was found for this requirement.")
    return "\n\n".join(chunks)


def _format_context_pack(context_pack: dict[str, Any] | None) -> str:
    if not context_pack:
        return "- not persisted"
    path = str(context_pack.get("path") or "").strip()
    if not path:
        return "- not persisted"
    sha256 = str(context_pack.get("sha256") or "").strip()
    suffix = f" (sha256: {sha256})" if sha256 else ""
    return f"- {path}{suffix}"


@dataclass(frozen=True)
class ExecutorResult:
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    stdout: str
    stderr: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _tail_redacted(chunks: list[bytes]) -> str:
    return redact(b"".join(chunks).decode("utf-8", errors="replace")[-8000:])


def _safe_positive_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _emit_progress(invocation: ExecutorInvocation, payload: dict[str, Any]) -> None:
    if invocation.progress_callback is None:
        return
    try:
        invocation.progress_callback(payload)
    except Exception:
        return


def _env_flag_enabled(env: dict[str, str], name: str) -> bool:
    return str(env.get(name, "")).lower() in {"1", "true", "yes", "on"}


def _compact_json_preview(value: Any, *, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return redact(text[:limit])


def _diagnostic_env(environment: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, **(environment or {}), "ENGINEERING_HARNESS": "1"}


def _binary_diagnostic_status(*, enabled: bool, binary_found: bool) -> str:
    if not enabled:
        return "disabled"
    return "ready" if binary_found else "missing_binary"


def _terminate_owned_process_tree(
    process: subprocess.Popen[bytes],
    *,
    owned_process_group_id: int | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "pid": process.pid,
        "owned_process_group": False,
        "terminated_process_group": False,
        "termination_signal": None,
        "killed": False,
    }
    if process.poll() is not None and owned_process_group_id is None:
        details["already_exited"] = True
        return details

    pgid: int | None = owned_process_group_id
    if os.name == "posix":
        if pgid is None:
            try:
                pgid = os.getpgid(process.pid)
            except OSError:
                pgid = None
    details["process_group_id"] = pgid
    owned_process_group = pgid is not None and (owned_process_group_id is not None or pgid == process.pid)
    details["owned_process_group"] = owned_process_group

    try:
        if owned_process_group:
            os.killpg(pgid, signal.SIGTERM)
            details["terminated_process_group"] = True
            details["termination_signal"] = "SIGTERM"
        else:
            process.terminate()
            details["termination_signal"] = "SIGTERM"
    except ProcessLookupError:
        return details
    except OSError as exc:
        details["termination_error"] = str(exc)
        return details

    deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is not None:
        return details

    try:
        if owned_process_group:
            os.killpg(pgid, signal.SIGKILL)
            details["termination_signal"] = "SIGKILL"
        else:
            process.kill()
            details["termination_signal"] = "SIGKILL"
        details["killed"] = True
    except ProcessLookupError:
        pass
    except OSError as exc:
        details["kill_error"] = str(exc)
    return details


def _read_available(fd: int) -> bytes | None:
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def _run_subprocess_with_watchdog(
    invocation: ExecutorInvocation,
    *,
    args: str | list[str],
    executor_id: str,
    shell: bool = False,
    executable: str | None = None,
    metadata: dict[str, Any] | None = None,
    output_event_parser: Callable[[str, bytes], list[dict[str, Any]]] | None = None,
) -> ExecutorResult:
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    last_progress_at = started_at
    last_progress_monotonic = started_monotonic
    no_progress_seconds = _safe_positive_seconds(invocation.no_progress_timeout_seconds)
    timeout_seconds = _safe_positive_seconds(invocation.timeout_seconds)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    process: subprocess.Popen[bytes] | None = None
    owned_process_group_id: int | None = None
    termination: dict[str, Any] = {}
    watchdog_status = "running"
    watchdog_reason: str | None = None
    watchdog_message: str | None = None

    def watchdog_payload(*, event: str, finished_at: str | None = None) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        threshold_seconds = (
            no_progress_seconds
            if watchdog_reason == "no_progress"
            else timeout_seconds
            if watchdog_reason == "runtime_timeout"
            else no_progress_seconds
        )
        return {
            "schema_version": EXECUTOR_WATCHDOG_CONTRACT_VERSION,
            "event": event,
            "status": watchdog_status,
            "reason": watchdog_reason,
            "message": watchdog_message,
            "phase": invocation.phase,
            "executor_id": executor_id,
            "command_name": invocation.name,
            "pid": process.pid if process is not None else None,
            "started_at": started_at,
            "finished_at": finished_at,
            "runtime_seconds": round(max(0.0, now_monotonic - started_monotonic), 3),
            "timeout_seconds": timeout_seconds,
            "no_progress_timeout_seconds": no_progress_seconds,
            "threshold_seconds": threshold_seconds,
            "last_progress_at": last_progress_at,
            "last_output_at": last_progress_at,
            "stdout_bytes": sum(len(chunk) for chunk in stdout_chunks),
            "stderr_bytes": sum(len(chunk) for chunk in stderr_chunks),
            **({"termination": termination} if termination else {}),
        }

    try:
        process = subprocess.Popen(
            args,
            cwd=invocation.project_root,
            shell=shell,
            executable=executable,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=invocation.env(),
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        raise
    if os.name == "posix":
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = None
        if process_group_id == process.pid:
            owned_process_group_id = process_group_id

    _emit_progress(invocation, watchdog_payload(event="started"))
    streams: dict[int, tuple[str, list[bytes]]] = {}
    for stream_name, pipe, chunks in (
        ("stdout", process.stdout, stdout_chunks),
        ("stderr", process.stderr, stderr_chunks),
    ):
        if pipe is None:
            continue
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        streams[fd] = (stream_name, chunks)

    drain_deadline: float | None = None
    while streams or process.poll() is None:
        now = time.monotonic()
        if watchdog_reason is None:
            if timeout_seconds is not None and now - started_monotonic >= timeout_seconds:
                watchdog_status = "timeout"
                watchdog_reason = "runtime_timeout"
                watchdog_message = f"Command timed out after {timeout_seconds} seconds."
                termination = _terminate_owned_process_tree(
                    process,
                    owned_process_group_id=owned_process_group_id,
                )
                drain_deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
                _emit_progress(invocation, watchdog_payload(event="timeout"))
            elif no_progress_seconds is not None and now - last_progress_monotonic >= no_progress_seconds:
                watchdog_status = "no_progress"
                watchdog_reason = "no_progress"
                watchdog_message = f"Command produced no output for {no_progress_seconds} seconds."
                termination = _terminate_owned_process_tree(
                    process,
                    owned_process_group_id=owned_process_group_id,
                )
                drain_deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
                _emit_progress(invocation, watchdog_payload(event="no_progress"))

        if streams:
            timeout = 0.05
            if watchdog_reason is None:
                deadlines = []
                if timeout_seconds is not None:
                    deadlines.append(timeout_seconds - (time.monotonic() - started_monotonic))
                if no_progress_seconds is not None:
                    deadlines.append(no_progress_seconds - (time.monotonic() - last_progress_monotonic))
                positive_deadlines = [item for item in deadlines if item > 0]
                if positive_deadlines:
                    timeout = max(0.01, min(timeout, min(positive_deadlines)))
            readable, _, _ = select.select(list(streams), [], [], timeout)
            for fd in readable:
                data = _read_available(fd)
                if data is None:
                    continue
                if data == b"":
                    streams.pop(fd, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    continue
                stream_name, chunks = streams[fd]
                chunks.append(data)
                last_progress_at = _utc_now()
                last_progress_monotonic = time.monotonic()
                _emit_progress(
                    invocation,
                    {
                        **watchdog_payload(event="output"),
                        "stream": stream_name,
                        "bytes": len(data),
                    },
                )
                if output_event_parser is not None:
                    try:
                        output_events = output_event_parser(stream_name, data)
                    except Exception:
                        output_events = []
                    for output_event in output_events:
                        if not isinstance(output_event, dict):
                            continue
                        _emit_progress(
                            invocation,
                            {
                                **watchdog_payload(event="executor_event"),
                                "stream": stream_name,
                                "executor_event": output_event,
                            },
                        )
        else:
            time.sleep(0.02)

        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            for fd in list(streams):
                streams.pop(fd, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

    try:
        returncode = process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        termination = termination or _terminate_owned_process_tree(
            process,
            owned_process_group_id=owned_process_group_id,
        )
        returncode = process.poll()

    finished_at = _utc_now()
    stdout = _tail_redacted(stdout_chunks)
    stderr = _tail_redacted(stderr_chunks)
    if watchdog_message:
        stderr = f"{stderr}\n{watchdog_message}".strip()

    if watchdog_reason == "runtime_timeout":
        status = "timeout"
        returncode_payload = None
    elif watchdog_reason == "no_progress":
        status = "no_progress"
        returncode_payload = None
    else:
        watchdog_status = "passed" if returncode == 0 else "failed"
        status = watchdog_status
        returncode_payload = returncode

    watchdog = watchdog_payload(event="finished", finished_at=finished_at)
    watchdog["status"] = status
    if returncode is not None:
        watchdog["process_returncode"] = returncode
    result_metadata = dict(metadata or {})
    result_metadata["watchdog"] = watchdog
    if watchdog_reason == "runtime_timeout":
        result_metadata["timed_out"] = True
    if watchdog_reason == "no_progress":
        result_metadata["no_progress"] = True
    _emit_progress(invocation, watchdog)
    return ExecutorResult(
        status=status,
        returncode=returncode_payload,
        started_at=started_at,
        finished_at=finished_at,
        stdout=stdout,
        stderr=stderr,
        metadata=result_metadata,
    )


class Executor(Protocol):
    metadata: ExecutorMetadata

    def display_command(self, invocation: ExecutorInvocation) -> str:
        ...

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        ...


class InvocationPreparingExecutor(Protocol):
    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        ...


class ShellExecutorAdapter:
    metadata = ExecutorMetadata(
        id="shell",
        name="Shell",
        kind="process",
        adapter="builtin.shell",
        input_mode="command",
        capabilities=("local_process", "workspace_write", "exit_code", "stdout", "stderr"),
        uses_command_policy=True,
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        return invocation

    def display_command(self, invocation: ExecutorInvocation) -> str:
        return invocation.command or ""

    def diagnostics(
        self,
        *,
        project_root: Path,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        binary = "/bin/bash"
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "id": self.metadata.id,
            "status": "ready" if Path(binary).exists() else "missing_binary",
            "configured": Path(binary).exists(),
            "enabled": True,
            "binary": binary,
            "binary_found": Path(binary).exists(),
            "binary_path": binary if Path(binary).exists() else None,
            "recommended_action": None if Path(binary).exists() else "Install /bin/bash or select a different shell executor.",
        }

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        return _run_subprocess_with_watchdog(
            invocation,
            args=invocation.command or "",
            executor_id=self.metadata.id,
            shell=True,
            executable="/bin/bash",
        )


class CodexExecutorAdapter:
    metadata = ExecutorMetadata(
        id="codex",
        name="Codex",
        kind="agent",
        adapter="builtin.codex",
        input_mode="prompt",
        capabilities=("agent", "workspace_write", "exit_code", "stdout", "stderr"),
        requires_agent_approval=True,
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        prompt = invocation.prompt or invocation.command or task_context.title
        acceptance = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.acceptance)
        e2e = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.e2e)
        file_scope = "\n".join(f"- {scope}" for scope in task_context.file_scope) or "- repository-scoped, but keep changes minimal"
        spec_refs = _format_spec_refs(task_context.relevant_spec_refs or task_context.spec_refs)
        requirement_excerpts = _format_requirement_excerpts(task_context.requirement_excerpts)
        context_pack = _format_context_pack(task_context.context_pack or invocation.context_pack)
        verification = acceptance if not e2e else f"{acceptance}\n\nE2E/user-experience commands:\n{e2e}"
        expanded_prompt = (
            "You are executing one roadmap task for Engineering Orchestrator.\n\n"
            f"Project root: {task_context.project_root}\n"
            f"Milestone: {task_context.milestone_id} - {task_context.milestone_title}\n"
            f"Task: {task_context.task_id} - {task_context.title}\n\n"
            "Spec refs:\n"
            f"{spec_refs}\n\n"
            "Requirement excerpts:\n"
            f"{requirement_excerpts}\n\n"
            "Agent context pack:\n"
            f"{context_pack}\n\n"
            "Goal:\n"
            f"{prompt}\n\n"
            "Allowed file scope:\n"
            f"{file_scope}\n\n"
            "Verification commands that must pass after your changes:\n"
            f"{verification}\n\n"
            "Constraints:\n"
            "- Edit files directly in the working tree.\n"
            "- Do not commit or push; the orchestrator handles git checkpoints.\n"
            "- Do not use private keys, paid live deployment, or live trading.\n"
            "- Prefer focused, test-driven changes that satisfy the acceptance commands.\n"
            "- If the task cannot be completed locally, write a clear blocker into the relevant project docs.\n"
        )
        return replace(invocation, prompt=expanded_prompt)

    def display_command(self, invocation: ExecutorInvocation) -> str:
        model = f" --model {invocation.model}" if invocation.model else ""
        return (
            f"codex exec --full-auto --skip-git-repo-check --sandbox {invocation.sandbox}{model} "
            f"-C {invocation.project_root} <task:{invocation.task_id}>"
        )

    def diagnostics(
        self,
        *,
        project_root: Path,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = _diagnostic_env(environment)
        binary = str(env.get("ENGINEERING_HARNESS_CODEX_BINARY") or "codex").strip() or "codex"
        binary_path = shutil.which(binary, path=env.get("PATH"))
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "id": self.metadata.id,
            "status": "ready" if binary_path else "missing_binary",
            "configured": bool(binary_path),
            "enabled": True,
            "binary": binary,
            "binary_found": bool(binary_path),
            "binary_path": binary_path,
            "requires_agent_approval": self.metadata.requires_agent_approval,
            "recommended_action": None if binary_path else "Install Codex CLI or remove Codex executor tasks from the local roadmap.",
        }

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        args = [
            "codex",
            "exec",
            "--full-auto",
            "--skip-git-repo-check",
            "--sandbox",
            invocation.sandbox,
            "-C",
            str(invocation.project_root),
        ]
        if invocation.model:
            args.extend(["--model", invocation.model])
        args.append(invocation.prompt or invocation.command or "")
        return _run_subprocess_with_watchdog(invocation, args=args, executor_id=self.metadata.id)


class OpenHandsExecutorAdapter:
    metadata = ExecutorMetadata(
        id="openhands",
        name="OpenHands",
        kind="agent",
        adapter="builtin.openhands",
        input_mode="prompt",
        capabilities=(
            "agent",
            "local_openhands_cli",
            "workspace_write",
            "network_access",
            "browser_automation",
            "exit_code",
            "stdout",
            "stderr",
            "requires_explicit_configuration",
        ),
        requires_agent_approval=True,
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        prompt = invocation.prompt or invocation.command or task_context.title
        acceptance = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.acceptance)
        e2e = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.e2e)
        file_scope = "\n".join(f"- {scope}" for scope in task_context.file_scope) or "- repository-scoped, but keep changes minimal"
        spec_refs = _format_spec_refs(task_context.relevant_spec_refs or task_context.spec_refs)
        requirement_excerpts = _format_requirement_excerpts(task_context.requirement_excerpts)
        context_pack = _format_context_pack(task_context.context_pack or invocation.context_pack)
        verification = acceptance if not e2e else f"{acceptance}\n\nE2E/user-experience commands:\n{e2e}"
        expanded_prompt = (
            "You are executing one roadmap task for Engineering Orchestrator through OpenHands headless mode.\n\n"
            f"Project root: {task_context.project_root}\n"
            f"Milestone: {task_context.milestone_id} - {task_context.milestone_title}\n"
            f"Task: {task_context.task_id} - {task_context.title}\n\n"
            "Spec refs:\n"
            f"{spec_refs}\n\n"
            "Requirement excerpts:\n"
            f"{requirement_excerpts}\n\n"
            "Agent context pack:\n"
            f"{context_pack}\n\n"
            "Goal:\n"
            f"{prompt}\n\n"
            "Allowed file scope:\n"
            f"{file_scope}\n\n"
            "Verification commands that must pass after your changes:\n"
            f"{verification}\n\n"
            "Constraints:\n"
            "- Edit files directly in the working tree.\n"
            "- Do not commit or push; the orchestrator handles git checkpoints.\n"
            "- Do not use private keys, paid live deployment, or live trading.\n"
            "- Prefer focused, test-driven changes that satisfy the acceptance commands.\n"
            "- If the task cannot be completed locally, write a clear blocker into the relevant project docs.\n"
        )
        return replace(invocation, prompt=expanded_prompt)

    def display_command(self, invocation: ExecutorInvocation) -> str:
        binary = self._binary(invocation)
        return f"{shlex.quote(binary)} --headless --json --override-with-envs -t <task:{invocation.task_id}>"

    def diagnostics(
        self,
        *,
        project_root: Path,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = _diagnostic_env(environment)
        binary = str(env.get(OPENHANDS_BINARY_ENV) or "openhands").strip() or "openhands"
        enabled = _env_flag_enabled(env, OPENHANDS_ENABLE_ENV)
        health = self._health(binary, env)
        warnings: list[dict[str, Any]] = []
        if enabled and health["binary_found"] and not (
            health["llm_model_configured"] or health["agent_settings_file_exists"]
        ):
            warnings.append(
                {
                    "id": "openhands_model_config_not_detected",
                    "message": "No LLM_MODEL env var or ~/.openhands/agent_settings.json file was detected.",
                }
            )
        status = _binary_diagnostic_status(enabled=enabled, binary_found=bool(health["binary_found"]))
        recommended_action = None
        if status == "disabled":
            recommended_action = f"Set {OPENHANDS_ENABLE_ENV}=1 to enable local OpenHands execution."
        elif status == "missing_binary":
            recommended_action = "Install OpenHands CLI or set ENGINEERING_HARNESS_OPENHANDS_BINARY to a valid binary."
        elif warnings:
            recommended_action = "Configure OpenHands model settings before running agent tasks."
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "id": self.metadata.id,
            "status": status,
            "configured": bool(enabled and health["binary_found"]),
            "enabled": enabled,
            "required_environment": OPENHANDS_ENABLE_ENV,
            "binary_environment": OPENHANDS_BINARY_ENV,
            "health": health,
            "warnings": warnings,
            "warning_count": len(warnings),
            "recommended_action": recommended_action,
        }

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        environment = dict(invocation.environment)
        if invocation.model:
            environment["LLM_MODEL"] = invocation.model
        prepared = replace(invocation, environment=environment)
        env = prepared.env()
        binary = self._binary(prepared)
        health = self._health(binary, env)
        if not _env_flag_enabled(env, OPENHANDS_ENABLE_ENV):
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=(
                    "OpenHands executor is disabled. Set "
                    f"{OPENHANDS_ENABLE_ENV}=1 to enable local OpenHands CLI execution."
                ),
                metadata={
                    "configured": False,
                    "required_environment": OPENHANDS_ENABLE_ENV,
                    "health": health,
                },
            )
        if not health["binary_found"]:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"OpenHands CLI was not found on PATH: {binary}",
                metadata={
                    "configured": True,
                    "missing_binary": binary,
                    "health": health,
                },
            )
        args = [
            binary,
            "--headless",
            "--json",
            "--override-with-envs",
            "-t",
            prepared.prompt or prepared.command or "",
        ]
        try:
            result = _run_subprocess_with_watchdog(
                prepared,
                args=args,
                executor_id=self.metadata.id,
                metadata={
                    "configured": True,
                    "binary": binary,
                    "json_output": True,
                    "headless": True,
                    "health": health,
                },
                output_event_parser=self._jsonl_progress_parser(),
            )
            metadata = dict(result.metadata)
            metadata["openhands_jsonl"] = self._summarize_jsonl(result.stdout)
            return replace(result, metadata=metadata)
        except FileNotFoundError:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"OpenHands CLI was not found on PATH: {binary}",
                metadata={"configured": True, "missing_binary": binary, "health": health},
            )

    def _binary(self, invocation: ExecutorInvocation) -> str:
        binary = str(
            invocation.environment.get(OPENHANDS_BINARY_ENV)
            or os.environ.get(OPENHANDS_BINARY_ENV)
            or "openhands"
        ).strip()
        return binary or "openhands"

    def _health(self, binary: str, env: dict[str, str]) -> dict[str, Any]:
        binary_path = shutil.which(binary, path=env.get("PATH"))
        home = env.get("HOME")
        settings_path = str(Path(home) / ".openhands" / "agent_settings.json") if home else None
        return {
            "binary": binary,
            "binary_found": bool(binary_path),
            "binary_path": binary_path,
            "llm_model_configured": bool(str(env.get("LLM_MODEL", "")).strip()),
            "llm_api_key_configured": bool(str(env.get("LLM_API_KEY", "")).strip()),
            "llm_base_url_configured": bool(str(env.get("LLM_BASE_URL", "")).strip()),
            "agent_settings_file": settings_path,
            "agent_settings_file_exists": bool(settings_path and Path(settings_path).exists()),
        }

    def _summarize_jsonl(self, output: str) -> dict[str, Any]:
        lines = output.splitlines()
        event_counts: dict[str, int] = {}
        touched_paths: list[str] = []
        recent_events: list[dict[str, Any]] = []
        parse_errors: list[dict[str, Any]] = []
        parsed_event_count = 0
        non_json_line_count = 0
        last_event_type: str | None = None

        for line_number, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError as exc:
                non_json_line_count += 1
                if len(parse_errors) < 5:
                    parse_errors.append(
                        {
                            "line": line_number,
                            "error": exc.msg,
                            "preview": redact(text[:200]),
                        }
                    )
                continue
            if not isinstance(event, dict):
                non_json_line_count += 1
                if len(parse_errors) < 5:
                    parse_errors.append(
                        {
                            "line": line_number,
                            "error": "JSONL event is not an object",
                            "preview": _compact_json_preview(event),
                        }
                    )
                continue
            parsed_event_count += 1
            event_type = str(event.get("type") or event.get("event") or event.get("kind") or "unknown")
            last_event_type = event_type
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            compact = self._compact_event(event, line_number=line_number, event_type=event_type)
            if compact.get("path"):
                touched_paths.append(str(compact["path"]))
            recent_events.append(compact)
            recent_events = recent_events[-10:]

        return {
            "schema_version": 1,
            "line_count": len(lines),
            "parsed_event_count": parsed_event_count,
            "non_json_line_count": non_json_line_count,
            "event_counts": dict(sorted(event_counts.items())),
            "last_event_type": last_event_type,
            "touched_paths": sorted(dict.fromkeys(touched_paths)),
            "recent_events": recent_events,
            "parse_errors": parse_errors,
        }

    def _compact_event(self, event: dict[str, Any], *, line_number: int, event_type: str) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "line": line_number,
            "type": event_type,
        }
        for key in ("action", "observation", "status", "path", "command"):
            if key in event and event[key] is not None:
                compact[key] = _compact_json_preview(event[key])
        if "message" in event and event["message"] is not None:
            compact["message"] = _compact_json_preview(event["message"])
        return compact

    def _jsonl_progress_parser(self) -> Callable[[str, bytes], list[dict[str, Any]]]:
        buffers: dict[str, str] = {}
        line_numbers: dict[str, int] = {}

        def parser(stream_name: str, data: bytes) -> list[dict[str, Any]]:
            if stream_name != "stdout":
                return []
            text = data.decode("utf-8", errors="replace")
            pending = f"{buffers.get(stream_name, '')}{text}"
            raw_lines = pending.splitlines(keepends=True)
            if raw_lines and not raw_lines[-1].endswith(("\n", "\r")):
                buffers[stream_name] = raw_lines.pop()
            else:
                buffers[stream_name] = ""
            events: list[dict[str, Any]] = []
            for raw_line in raw_lines:
                line_numbers[stream_name] = line_numbers.get(stream_name, 0) + 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or event.get("event") or event.get("kind") or "unknown")
                events.append(
                    {
                        "schema_version": 1,
                        "source": "openhands_jsonl",
                        **self._compact_event(
                            event,
                            line_number=line_numbers[stream_name],
                            event_type=event_type,
                        ),
                    }
                )
            return events

        return parser


class TaskAgentDeveloperExecutorAdapter:
    metadata = ExecutorMetadata(
        id="task-agent-developer",
        name="Task Agent Developer",
        kind="agent",
        adapter="builtin.task-agent-developer",
        input_mode="command",
        capabilities=(
            "agent",
            "local_task_agent_developer_cli",
            "workspace_write",
            "artifact_collection",
            "exit_code",
            "stdout",
            "stderr",
        ),
        requires_agent_approval=True,
    )

    _artifact_labels = ("candidate", "audit", "report")
    _artifact_dirs = (
        ".engineering/reports/task-agent-developer",
        "artifacts/task-agent-developer",
    )
    _artifact_line_re = re.compile(
        r"(?i)\b(candidate|audit|report)(?:[-_\s]*(?:artifact|path|file))?\s*[:=]\s*([^\s,;]+)"
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        return invocation

    def display_command(self, invocation: ExecutorInvocation) -> str:
        command = (invocation.command or "").strip()
        if command:
            try:
                args = shlex.split(command)
            except ValueError:
                args = []
            if args and self._is_binary_token(args[0], self._binary(invocation)):
                display = command
            else:
                display = f"task-agent-developer {command}"
        else:
            display = "task-agent-developer"
        return f"{display} <task:{invocation.task_id}>"

    def diagnostics(
        self,
        *,
        project_root: Path,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = _diagnostic_env(environment)
        binary = str(env.get(TASK_AGENT_DEVELOPER_BINARY_ENV) or "task-agent-developer").strip()
        binary = binary or "task-agent-developer"
        binary_path = shutil.which(binary, path=env.get("PATH"))
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "id": self.metadata.id,
            "status": "ready" if binary_path else "missing_binary",
            "configured": bool(binary_path),
            "enabled": True,
            "binary": binary,
            "binary_environment": TASK_AGENT_DEVELOPER_BINARY_ENV,
            "binary_found": bool(binary_path),
            "binary_path": binary_path,
            "artifact_collection": {
                "schema_version": 1,
                "conventional_directories": list(self._artifact_dirs),
                "artifact_labels": list(self._artifact_labels),
            },
            "requires_agent_approval": self.metadata.requires_agent_approval,
            "recommended_action": (
                None
                if binary_path
                else "Install Task Agent Developer CLI or set ENGINEERING_HARNESS_TASK_AGENT_DEVELOPER_BINARY."
            ),
        }

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        env = invocation.env()
        binary = self._binary(invocation)
        binary_path = shutil.which(binary, path=env.get("PATH"))
        if not binary_path:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"Task Agent Developer CLI was not found on PATH: {binary}",
                metadata={
                    "configured": False,
                    "missing_binary": binary,
                    "binary_environment": TASK_AGENT_DEVELOPER_BINARY_ENV,
                },
            )

        try:
            args = self._task_agent_developer_args(invocation.command or "", binary=binary)
        except ValueError as exc:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"Invalid Task Agent Developer command: {exc}",
                metadata={"configured": True, "binary": binary, "binary_path": binary_path},
            )

        if not args:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr="Task Agent Developer command is missing.",
                metadata={"configured": True, "binary": binary, "binary_path": binary_path},
            )

        before = self._artifact_snapshot(invocation.project_root)
        try:
            result = _run_subprocess_with_watchdog(
                invocation,
                args=[binary, *args],
                executor_id=self.metadata.id,
                metadata={
                    "configured": True,
                    "binary": binary,
                    "binary_path": binary_path,
                },
            )
        except FileNotFoundError:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"Task Agent Developer CLI was not found on PATH: {binary}",
                metadata={"configured": True, "missing_binary": binary},
            )

        artifacts = self._collect_artifacts(
            invocation.project_root,
            before=before,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        metadata = dict(result.metadata)
        task_agent_developer = {
            "schema_version": 1,
            "binary": binary,
            "binary_path": binary_path,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "artifact_collection": {
                "conventional_directories": list(self._artifact_dirs),
                "structured_output": True,
            },
        }
        metadata["task_agent_developer"] = task_agent_developer
        metadata["artifacts"] = artifacts
        return replace(result, metadata=metadata)

    def _binary(self, invocation: ExecutorInvocation) -> str:
        binary = str(
            invocation.environment.get(TASK_AGENT_DEVELOPER_BINARY_ENV)
            or os.environ.get(TASK_AGENT_DEVELOPER_BINARY_ENV)
            or "task-agent-developer"
        ).strip()
        return binary or "task-agent-developer"

    def _is_binary_token(self, token: str, binary: str) -> bool:
        if token == binary:
            return True
        token_name = Path(token).name
        binary_name = Path(binary).name
        return token_name == "task-agent-developer" or token_name == binary_name

    def _task_agent_developer_args(self, command: str, *, binary: str) -> list[str]:
        args = shlex.split(command)
        if args[:1] and self._is_binary_token(args[0], binary):
            return args[1:]
        return args

    def _artifact_snapshot(self, project_root: Path) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        for directory in self._artifact_dirs:
            base = project_root / directory
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                label = self._artifact_label(kind="", path=str(path.relative_to(project_root)))
                if label is None:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[str(path.relative_to(project_root).as_posix())] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _collect_artifacts(
        self,
        project_root: Path,
        *,
        before: dict[str, tuple[int, int]],
        stdout: str,
        stderr: str,
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[str, str, str]] = []
        candidates.extend(self._artifact_candidates_from_output(stdout, stream_name="stdout"))
        candidates.extend(self._artifact_candidates_from_output(stderr, stream_name="stderr"))

        after = self._artifact_snapshot(project_root)
        for path, signature in sorted(after.items()):
            if before.get(path) == signature:
                continue
            label = self._artifact_label(kind="", path=path)
            if label is not None:
                candidates.append((label, path, "conventional_path_scan"))

        artifacts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for label, path, source in candidates:
            artifact = self._artifact_entry(project_root, label=label, path=path, source=source)
            if artifact is None:
                continue
            key = (str(artifact["kind"]), str(artifact["path"]))
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
            if len(artifacts) >= 25:
                break
        return artifacts

    def _artifact_candidates_from_output(self, output: str, *, stream_name: str) -> list[tuple[str, str, str]]:
        candidates: list[tuple[str, str, str]] = []
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                candidates.extend(self._artifact_candidates_from_mapping(payload, stream_name=stream_name))
            for match in self._artifact_line_re.finditer(text):
                candidates.append((match.group(1).lower(), match.group(2).strip().strip("'\""), stream_name))
        return candidates

    def _artifact_candidates_from_mapping(
        self,
        payload: dict[str, Any],
        *,
        stream_name: str,
    ) -> list[tuple[str, str, str]]:
        candidates: list[tuple[str, str, str]] = []

        def add_candidate(label: str | None, value: Any) -> None:
            path_value: Any = value
            kind_value = label
            if isinstance(value, dict):
                kind_value = str(value.get("kind") or value.get("type") or value.get("label") or label or "")
                path_value = value.get("path") or value.get("file") or value.get("artifact_path")
            if not isinstance(path_value, str) or not path_value.strip():
                return
            resolved_label = self._artifact_label(kind=kind_value or "", path=path_value)
            if resolved_label is None:
                return
            candidates.append((resolved_label, path_value.strip(), stream_name))

        artifacts = payload.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                add_candidate(None, artifact)
        add_candidate(None, payload.get("artifact"))

        for key, value in payload.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            for label in self._artifact_labels:
                if normalized_key in {
                    label,
                    f"{label}_path",
                    f"{label}_file",
                    f"{label}_artifact",
                    f"{label}_artifact_path",
                }:
                    add_candidate(label, value)
            if normalized_key in {"path", "file", "artifact_path"}:
                add_candidate(str(payload.get("kind") or payload.get("type") or ""), value)
        return candidates

    def _artifact_label(self, *, kind: str, path: str) -> str | None:
        kind_text = kind.lower().replace("-", "_")
        for label in self._artifact_labels:
            if label in kind_text:
                return label
        path_obj = Path(path)
        name = path_obj.name.lower().replace("-", "_")
        for label in self._artifact_labels:
            if label in name:
                return label
        ignored_parts = {".engineering", "artifacts", "task-agent-developer"}
        parts = path_obj.parts
        for index, part in enumerate(parts):
            normalized = part.lower().replace("-", "_")
            if normalized in ignored_parts:
                continue
            if normalized == "reports" and index > 0 and parts[index - 1] == ".engineering":
                continue
            for label in self._artifact_labels:
                if normalized in {label, f"{label}s"}:
                    return label
        return None

    def _artifact_entry(
        self,
        project_root: Path,
        *,
        label: str,
        path: str,
        source: str,
    ) -> dict[str, Any] | None:
        relative = self._project_relative_artifact_path(project_root, path)
        if relative is None:
            return None
        artifact_path = project_root / relative
        exists = artifact_path.is_file()
        artifact: dict[str, Any] = {
            "schema_version": 1,
            "kind": f"task_agent_developer_{label}",
            "path": relative,
            "source": source,
            "exists": exists,
        }
        if exists:
            try:
                stat = artifact_path.stat()
                artifact["bytes"] = stat.st_size
                artifact["sha256"] = self._file_sha256(artifact_path)
            except OSError:
                artifact["exists"] = False
        return artifact

    def _project_relative_artifact_path(self, project_root: Path, path: str) -> str | None:
        text = redact(str(path)).strip().strip("'\"")
        if not text or "\n" in text or "\r" in text:
            return None
        root = project_root.resolve(strict=False)
        candidate = Path(text)
        absolute = candidate if candidate.is_absolute() else project_root / candidate
        try:
            resolved = absolute.resolve(strict=False)
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        relative_text = relative.as_posix()
        if not relative_text or relative_text.startswith("../"):
            return None
        return relative_text

    def _file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class DaggerExecutorAdapter:
    metadata = ExecutorMetadata(
        id="dagger",
        name="Dagger",
        kind="container",
        adapter="builtin.dagger",
        input_mode="command",
        capabilities=(
            "local_dagger_cli",
            "containerized_execution",
            "exit_code",
            "stdout",
            "stderr",
            "requires_explicit_configuration",
        ),
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        return invocation

    def display_command(self, invocation: ExecutorInvocation) -> str:
        command = (invocation.command or "").strip()
        if command.startswith("dagger "):
            return f"{command} <task:{invocation.task_id}>"
        if command:
            return f"dagger {command} <task:{invocation.task_id}>"
        return f"dagger <task:{invocation.task_id}>"

    def diagnostics(
        self,
        *,
        project_root: Path,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = _diagnostic_env(environment)
        binary = "dagger"
        binary_path = shutil.which(binary, path=env.get("PATH"))
        enabled = _env_flag_enabled(env, DAGGER_ENABLE_ENV)
        status = _binary_diagnostic_status(enabled=enabled, binary_found=bool(binary_path))
        recommended_action = None
        if status == "disabled":
            recommended_action = f"Set {DAGGER_ENABLE_ENV}=1 to enable local Dagger CLI execution."
        elif status == "missing_binary":
            recommended_action = "Install Dagger CLI and ensure it is on PATH."
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "id": self.metadata.id,
            "status": status,
            "configured": bool(enabled and binary_path),
            "enabled": enabled,
            "required_environment": DAGGER_ENABLE_ENV,
            "binary": binary,
            "binary_found": bool(binary_path),
            "binary_path": binary_path,
            "recommended_action": recommended_action,
        }

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        env = invocation.env()
        if not _env_flag_enabled(env, DAGGER_ENABLE_ENV):
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=(
                    "Dagger executor is disabled. Set "
                    f"{DAGGER_ENABLE_ENV}=1 to enable local Dagger CLI execution."
                ),
                metadata={
                    "configured": False,
                    "required_environment": DAGGER_ENABLE_ENV,
                },
            )

        try:
            args = self._dagger_args(invocation.command or "")
        except ValueError as exc:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"Invalid Dagger command: {exc}",
                metadata={"configured": True},
            )

        if not args:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr="Dagger command is missing.",
                metadata={"configured": True},
            )

        try:
            return _run_subprocess_with_watchdog(
                invocation,
                args=["dagger", *args],
                executor_id=self.metadata.id,
                metadata={"configured": True},
            )
        except FileNotFoundError:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr="Dagger CLI was not found on PATH.",
                metadata={"configured": True, "missing_binary": "dagger"},
            )

    def _dagger_args(self, command: str) -> list[str]:
        args = shlex.split(command)
        if args[:1] == ["dagger"]:
            return args[1:]
        return args


ShellExecutor = ShellExecutorAdapter
CodexExecutor = CodexExecutorAdapter
OpenHandsExecutor = OpenHandsExecutorAdapter
TaskAgentDeveloperExecutor = TaskAgentDeveloperExecutorAdapter
DaggerExecutor = DaggerExecutorAdapter


class ExecutorRegistry:
    def __init__(self, executors: tuple[Executor, ...]) -> None:
        self._executors = {executor.metadata.id: executor for executor in executors}

    def get(self, executor_id: str) -> Executor | None:
        return self._executors.get(executor_id)

    def metadata_for(self, executor_id: str) -> dict[str, Any]:
        executor = self.get(executor_id)
        if executor is None:
            return {
                "schema_version": EXECUTOR_CONTRACT_VERSION,
                "id": executor_id,
                "name": executor_id,
                "kind": "unknown",
                "adapter": None,
                "input_mode": "unknown",
                "capabilities": [],
                "capability_classifications": classify_capabilities(()),
                "requires_agent_approval": None,
                "uses_command_policy": None,
            }
        return executor.metadata.as_contract()

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))


def default_executor_registry() -> ExecutorRegistry:
    return ExecutorRegistry(
        (
            ShellExecutorAdapter(),
            CodexExecutorAdapter(),
            OpenHandsExecutorAdapter(),
            TaskAgentDeveloperExecutorAdapter(),
            DaggerExecutorAdapter(),
        )
    )
