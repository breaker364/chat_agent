from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from pydantic import BaseModel, Field
from langchain_core.tools import tool


class BashInput(BaseModel):
    command: str = Field(..., min_length=1, max_length=8192)
    working_directory: str = Field(default="", max_length=2048)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    max_output_chars: int | None = Field(default=None, ge=256, le=100_000)
    executable: str = Field(default="", max_length=4096)


class GlobInput(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=4096)
    scope: str = Field(default=".", min_length=1, max_length=2048)
    max_matches: int | None = Field(default=None, ge=1, le=10_000)
    max_output_chars: int | None = Field(default=None, ge=256, le=100_000)


class GrepInput(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=4096)
    scope: str = Field(default=".", min_length=1, max_length=2048)
    max_matches: int | None = Field(default=None, ge=1, le=10_000)
    max_file_bytes: int | None = Field(default=None, ge=1, le=100 * 1024 * 1024)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    max_output_chars: int | None = Field(default=None, ge=256, le=100_000)


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _config() -> dict[str, Any]:
    from .config import load_shell_tools_config

    return load_shell_tools_config()


def get_shell_tools_diagnostics() -> dict[str, Any]:
    """Return deployment-safe shell-tool readiness and limit information."""
    settings = _config()
    if not settings["enabled"] or not settings["bash_enabled"]:
        bash_status = "disabled"
        executable_policy = "disabled"
    else:
        configured = bool(str(settings.get("bash_executable") or "").strip())
        bash_status = "available" if _resolve_bash_executable() else "unavailable"
        executable_policy = "configured" if configured else "path_discovery"
    return {
        "enabled": bool(settings["enabled"]),
        "bash_enabled": bool(settings["bash_enabled"]),
        "bash_status": bash_status,
        "executable_policy": executable_policy,
        "limits": {
            "default_timeout_seconds": settings["default_timeout_seconds"],
            "max_timeout_seconds": settings["max_timeout_seconds"],
            "max_output_chars": settings["max_output_chars"],
            "max_glob_matches": settings["max_glob_matches"],
            "max_grep_matches": settings["max_grep_matches"],
            "max_file_bytes": settings["max_file_bytes"],
        },
    }


def _workspace_helpers() -> tuple[Any, Any, Any, Any]:
    from .tools import _ensure_readable, _is_within, _knowledge_index_root, _workspace_root

    return _ensure_readable, _is_within, _knowledge_index_root, _workspace_root


def _base_result(*, status: str, pattern: str = "", scope: str = ".") -> dict[str, Any]:
    return {
        "status": status,
        "pattern": pattern,
        "scope": scope,
        "matches": [],
        "returned_count": 0,
        "truncated": False,
    }


def _reject_unsafe_relative(value: str, *, field_name: str, max_length: int) -> None:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > max_length or "\x00" in text:
        raise ValueError(f"{field_name} exceeds its configured length limit")
    windows_path = PureWindowsPath(text)
    normalized_parts = text.replace("\\", "/").split("/")
    if windows_path.is_absolute() or windows_path.drive or text.startswith(("/", "\\")):
        raise PermissionError(f"{field_name} must be workspace-relative")
    if any(part == ".." for part in normalized_parts):
        raise PermissionError(f"{field_name} cannot traverse outside the workspace")


def _resolve_workspace_scope(scope: str, *, allow_file: bool = True) -> Path:
    settings = _config()
    _reject_unsafe_relative(
        scope or ".",
        field_name="scope",
        max_length=int(settings["max_scope_chars"]),
    )
    _, is_within, knowledge_index_root, workspace_root = _workspace_helpers()
    workspace = workspace_root().resolve()
    candidate = (workspace / (scope or ".").replace("\\", "/")).resolve()
    if not is_within(candidate, workspace):
        raise PermissionError("scope is outside the approved workspace")
    ensure_readable, _, _, _ = _workspace_helpers()
    ensure_readable(candidate)
    if not allow_file and candidate.exists() and not candidate.is_dir():
        raise ValueError("scope must be a directory")
    if os.environ.get("CHAT_AGENT_RUN_ID") and is_within(candidate, knowledge_index_root()):
        raise PermissionError("scope is protected during an active agent run")
    return candidate


def _normalize_glob_pattern(pattern: str, max_length: int) -> str:
    _reject_unsafe_relative(pattern, field_name="pattern", max_length=max_length)
    normalized = str(pattern).strip().replace("\\", "/")
    if normalized in {"", "."}:
        return "*"
    return normalized


def _safe_relative_path(path: Path, workspace: Path, is_within: Any) -> str | None:
    resolved = path.resolve()
    if not is_within(resolved, workspace):
        return None
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return None


def _resolve_bash_executable(preferred: str = "") -> str | None:
    settings = _config()
    if not settings["bash_enabled"]:
        return None
    configured = str(settings.get("bash_executable") or "").strip()
    requested = str(preferred or "").strip()
    if requested and configured and Path(requested).resolve() != Path(configured).resolve():
        return None
    candidate = requested or configured or shutil.which("bash") or ""
    if not candidate or "\x00" in candidate:
        return None
    resolved = Path(candidate).expanduser().resolve(strict=False)
    return str(resolved) if resolved.is_file() else None


def _resolve_bash_cwd(working_directory: str) -> Path:
    _, is_within, _, workspace_root = _workspace_helpers()
    workspace = workspace_root().resolve()
    raw = str(working_directory or "").strip()
    if "\x00" in raw or len(raw) > int(_config()["max_scope_chars"]):
        raise ValueError("working_directory is invalid or too long")
    requested = Path(raw).expanduser() if raw else Path(".")
    candidate = requested.resolve() if requested.is_absolute() else (workspace / requested).resolve()
    if not is_within(candidate, workspace):
        raise PermissionError("working_directory must remain inside the workspace")
    ensure_readable, _, _, _ = _workspace_helpers()
    ensure_readable(candidate)
    if not candidate.is_dir():
        raise ValueError("working_directory must be an existing directory")
    return candidate


def _command_hash(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        from .tools import _kill_process_tree

        _kill_process_tree(process)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _redact_text(value: str, environment: Mapping[str, str]) -> str:
    redacted = value
    for secret in environment.values():
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _run_bounded_process(
    command: str,
    *,
    executable: str,
    cwd: Path,
    timeout_seconds: int,
    max_output_chars: int,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
        "env": dict(environment),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    started_at = time.monotonic()
    try:
        process = subprocess.Popen([executable, "-c", command], **popen_kwargs)
    except FileNotFoundError:
        return {
            "status": "runtime_unavailable",
            "error_category": "bash_unavailable",
            "command_hash": _command_hash(command),
            "cwd": str(cwd),
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "stdout": "",
            "stderr": "",
            "stdout_chars": 0,
            "stderr_chars": 0,
            "timed_out": False,
            "truncated": False,
        }
    except OSError as exc:
        return {
            "status": "command_failed",
            "error_category": "process_start_failed",
            "error_description": str(exc)[:160],
            "command_hash": _command_hash(command),
            "cwd": str(cwd),
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "stdout": "",
            "stderr": "",
            "stdout_chars": 0,
            "stderr_chars": 0,
            "timed_out": False,
            "truncated": False,
        }

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_sizes = {"stdout": 0, "stderr": 0}
    output_exceeded = threading.Event()

    def drain(stream: Any, name: str, chunks: list[str]) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                stream_sizes[name] += len(chunk)
                if sum(stream_sizes.values()) <= max_output_chars * 2:
                    remaining = max_output_chars - sum(len(item) for item in chunks)
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                if stream_sizes[name] > max_output_chars:
                    output_exceeded.set()
                    return
        except Exception:
            return

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "stdout", stdout_chunks), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr", stderr_chunks), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    terminated_for_output = False
    deadline = started_at + timeout_seconds
    while process.poll() is None:
        if output_exceeded.is_set():
            terminated_for_output = True
            _terminate_process_tree(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process_tree(process)
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=2)
    except Exception:
        _terminate_process_tree(process)
        try:
            process.wait(timeout=2)
        except Exception:
            pass
    for thread in threads:
        thread.join(timeout=2)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except Exception:
            pass

    stdout_text = _redact_text("".join(stdout_chunks)[:max_output_chars], environment)
    stderr_text = _redact_text("".join(stderr_chunks)[:max_output_chars], environment)
    truncated = terminated_for_output or output_exceeded.is_set()
    status = "timed_out" if timed_out else ("output_truncated" if truncated else ("ok" if process.returncode == 0 else "command_failed"))
    payload: dict[str, Any] = {
        "status": status,
        "command_hash": _command_hash(command),
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_chars": len(stdout_text),
        "stderr_chars": len(stderr_text),
        "timed_out": timed_out,
        "truncated": truncated,
        "output_limit": max_output_chars,
    }
    if timed_out:
        payload["timeout_seconds"] = timeout_seconds
        payload["error_category"] = "timeout"
    elif truncated:
        payload["error_category"] = "output_limit"
        payload["truncated_streams"] = [
            name for name, size in stream_sizes.items() if size > max_output_chars
        ]
    elif process.returncode != 0:
        payload["error_category"] = "nonzero_exit"
    return payload
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return None


def _bounded_path_matches(
    candidates: list[str],
    *,
    limit: int,
    max_output_chars: int,
) -> tuple[list[str], bool, list[str]]:
    ordered = sorted(set(candidates), key=lambda item: (item.casefold(), item))
    selected: list[str] = []
    reasons: list[str] = []
    for item in ordered:
        if len(selected) >= limit:
            reasons.append("match_limit")
            break
        proposed = selected + [item]
        preview = _json_result({"matches": proposed})
        if len(preview) > max_output_chars and selected:
            reasons.append("output_limit")
            break
        if len(preview) > max_output_chars:
            selected.append(item[:max(1, max_output_chars)])
            reasons.append("output_limit")
            break
        selected.append(item)
    return selected, bool(reasons), sorted(set(reasons))


@tool(args_schema=BashInput)
def bash(
    command: str,
    working_directory: str = "",
    timeout_seconds: int | None = None,
    max_output_chars: int | None = None,
    executable: str = "",
) -> str:
    """Run one bounded non-interactive Bash command in the approved workspace."""
    settings = _config()
    result: dict[str, Any] = {
        "status": "invalid_input",
        "command_hash": _command_hash(str(command or "")),
        "stdout": "",
        "stderr": "",
        "stdout_chars": 0,
        "stderr_chars": 0,
        "timed_out": False,
        "truncated": False,
    }
    command_text = str(command or "")
    if not command_text.strip():
        result["error_category"] = "empty_command"
        return _json_result(result)
    if len(command_text) > int(settings["max_command_chars"]):
        result["error_category"] = "command_length"
        return _json_result(result)
    timeout_value = int(timeout_seconds or settings["default_timeout_seconds"])
    output_limit = int(max_output_chars or settings["max_output_chars"])
    if timeout_value > int(settings["max_timeout_seconds"]):
        result["error_category"] = "timeout_limit"
        return _json_result(result)
    if output_limit > int(settings["max_output_chars"]):
        result["error_category"] = "output_limit"
        return _json_result(result)
    executable_path = _resolve_bash_executable(executable)
    if not executable_path:
        result.update(status="runtime_unavailable", error_category="bash_unavailable")
        return _json_result(result)
    try:
        cwd = _resolve_bash_cwd(working_directory)
    except PermissionError:
        result.update(status="permission_denied", error_category="working_directory_policy")
        return _json_result(result)
    except ValueError:
        result.update(status="invalid_input", error_category="working_directory")
        return _json_result(result)

    environment: dict[str, str] = {}
    for name in settings.get("environment_allowlist", []):
        if name in os.environ:
            environment[name] = os.environ[name]
    return _json_result(
        _run_bounded_process(
            command_text,
            executable=executable_path,
            cwd=cwd,
            timeout_seconds=timeout_value,
            max_output_chars=output_limit,
            environment=environment,
        )
    )


@tool(args_schema=GlobInput)
def glob(
    pattern: str,
    scope: str = ".",
    max_matches: int | None = None,
    max_output_chars: int | None = None,
) -> str:
    """Find bounded, deterministic workspace-relative paths matching a glob pattern."""
    settings = _config()
    result = _base_result(status="ok", pattern=pattern, scope=scope)
    if not settings["enabled"]:
        result.update(status="runtime_unavailable", error_category="shell_tools_disabled")
        return _json_result(result)
    try:
        normalized_pattern = _normalize_glob_pattern(pattern, int(settings["max_pattern_chars"]))
        scope_path = _resolve_workspace_scope(scope or ".")
        _, is_within, _, workspace_root = _workspace_helpers()
        workspace = workspace_root().resolve()
    except PermissionError as exc:
        result.update(status="permission_denied", error_category="scope_policy")
        return _json_result(result)
    except ValueError:
        result.update(status="invalid_input", error_category="invalid_pattern")
        return _json_result(result)

    candidates: list[str] = []
    try:
        if scope_path.is_file():
            if fnmatch.fnmatchcase(scope_path.name, normalized_pattern):
                relative = _safe_relative_path(scope_path, workspace, is_within)
                if relative is not None:
                    candidates.append(relative)
        else:
            for candidate in scope_path.glob(normalized_pattern):
                relative = _safe_relative_path(candidate, workspace, is_within)
                if relative is not None:
                    candidates.append(relative)
    except (OSError, RuntimeError):
        result.update(status="permission_denied", error_category="scope_unreadable")
        return _json_result(result)

    limit = min(
        int(settings["max_glob_matches"]),
        int(max_matches) if max_matches is not None else int(settings["max_glob_matches"]),
    )
    matches, truncated, reasons = _bounded_path_matches(
        candidates,
        limit=limit,
        max_output_chars=min(
            int(settings["max_output_chars"]),
            int(max_output_chars) if max_output_chars is not None else int(settings["max_output_chars"]),
        ),
    )
    result.update(
        pattern=normalized_pattern,
        matches=matches,
        returned_count=len(matches),
        limit=limit,
        truncated=truncated,
        truncation_reasons=reasons,
    )
    return _json_result(result)


@tool(args_schema=GrepInput)
def grep(
    pattern: str,
    scope: str = ".",
    max_matches: int | None = None,
    max_file_bytes: int | None = None,
    timeout_seconds: float | None = None,
    max_output_chars: int | None = None,
) -> str:
    """Search bounded approved text files with a caller-provided regular expression."""
    settings = _config()
    result = _base_result(status="ok", pattern=pattern, scope=scope)
    result.update(
        {
            "files_scanned": 0,
            "bytes_scanned": 0,
            "skipped": [],
            "truncation_reasons": [],
        }
    )
    if not settings["enabled"]:
        result.update(status="runtime_unavailable", error_category="shell_tools_disabled")
        return _json_result(result)

    try:
        expression = re.compile(pattern)
    except re.error as exc:
        result.update(
            status="invalid_input",
            error_category="invalid_regex",
            error_description=str(exc)[:160],
        )
        return _json_result(result)

    try:
        if not pattern or len(str(pattern)) > int(settings["max_pattern_chars"]) or "\x00" in str(pattern):
            raise ValueError("pattern exceeds its configured length limit")
        _reject_unsafe_relative(
            scope or ".",
            field_name="scope",
            max_length=int(settings["max_scope_chars"]),
        )
        scope_path = _resolve_workspace_scope(scope or ".")
        _, is_within, _, workspace_root = _workspace_helpers()
        workspace = workspace_root().resolve()
        ensure_readable, _, _, _ = _workspace_helpers()
    except PermissionError:
        result.update(status="permission_denied", error_category="scope_policy")
        return _json_result(result)
    except ValueError:
        result.update(status="invalid_input", error_category="invalid_scope")
        return _json_result(result)

    limit = min(
        int(settings["max_grep_matches"]),
        int(max_matches) if max_matches is not None else int(settings["max_grep_matches"]),
    )
    file_limit = min(
        int(settings["max_file_bytes"]),
        int(max_file_bytes) if max_file_bytes is not None else int(settings["max_file_bytes"]),
    )
    deadline = time.monotonic() + min(
        float(settings["max_timeout_seconds"]),
        float(timeout_seconds) if timeout_seconds is not None else float(settings["default_timeout_seconds"]),
    )
    output_limit = min(
        int(settings["max_output_chars"]),
        int(max_output_chars) if max_output_chars is not None else int(settings["max_output_chars"]),
    )

    candidates: list[Path] = []
    if scope_path.is_file():
        candidates = [scope_path]
    elif scope_path.is_dir():
        try:
            candidates = [candidate for candidate in scope_path.rglob("*") if candidate.is_file()]
        except (OSError, RuntimeError):
            result.update(status="permission_denied", error_category="scope_unreadable")
            return _json_result(result)
    candidates.sort(
        key=lambda candidate: (
            str(candidate.relative_to(workspace)).replace("\\", "/").casefold()
            if is_within(candidate, workspace)
            else str(candidate).casefold(),
        )
    )

    truncation_reasons: set[str] = set()
    for candidate in candidates:
        if time.monotonic() >= deadline:
            truncation_reasons.add("time_limit")
            break
        relative = (
            candidate.relative_to(workspace).as_posix()
            if is_within(candidate, workspace)
            else ""
        )
        resolved = candidate.resolve()
        if not is_within(resolved, workspace):
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "symlink_outside_workspace"})
            continue
        try:
            ensure_readable(resolved)
            size = candidate.stat().st_size
        except PermissionError:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "permission_denied"})
            continue
        except OSError:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "unreadable"})
            continue
        if size > file_limit:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "file_size_limit"})
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "unreadable"})
            continue
        if b"\x00" in raw:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "binary"})
            continue
        try:
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("gb18030")
        except UnicodeDecodeError:
            if relative and len(result["skipped"]) < 32:
                result["skipped"].append({"path": relative, "reason": "encoding"})
            continue

        result["files_scanned"] += 1
        result["bytes_scanned"] += len(raw)
        for line_number, line in enumerate(content.splitlines(), 1):
            if time.monotonic() >= deadline:
                truncation_reasons.add("time_limit")
                break
            if not expression.search(line):
                continue
            if len(result["matches"]) >= limit:
                truncation_reasons.add("match_limit")
                break
            match = {
                "path": relative,
                "line_number": line_number,
                "line": line[: min(4000, output_limit)],
            }
            candidate_matches = [*result["matches"], match]
            preview = _json_result({**result, "matches": candidate_matches})
            if len(preview) > output_limit:
                truncation_reasons.add("output_limit")
                break
            result["matches"].append(match)
        if "time_limit" in truncation_reasons or "output_limit" in truncation_reasons:
            break
        if "match_limit" in truncation_reasons:
            break

    result["returned_count"] = len(result["matches"])
    result["limit"] = limit
    result["truncated"] = bool(truncation_reasons)
    result["truncation_reasons"] = sorted(truncation_reasons)
    return _json_result(result)
