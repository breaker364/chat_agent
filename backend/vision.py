from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import load_vision_config


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_IMAGE_SUFFIX_PATTERN = r"(?:png|jpe?g|webp|gif|bmp)"
_BACKTICK_IMAGE_RE = re.compile(
    rf"`([^`\r\n]+?\.{_IMAGE_SUFFIX_PATTERN})`",
    re.IGNORECASE,
)
_PLAIN_IMAGE_RE = re.compile(
    rf"((?:[A-Za-z]:[\\/]|/|\.{{0,2}}[\\/]|tmp[\\/])"
    rf"[^\r\n`\"']+?\.{_IMAGE_SUFFIX_PATTERN})",
    re.IGNORECASE,
)


class VisionConfigurationError(RuntimeError):
    pass


def _workspace_root(workspace_root: str | Path | None = None) -> Path:
    return Path(workspace_root or Path.cwd()).resolve()


def _resolve_image_path(path: str, workspace_root: str | Path | None = None) -> Path:
    root = _workspace_root(workspace_root)
    candidate = Path(path.strip().strip("`").strip('"')).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Image path is outside the workspace: {target}") from exc
    if not target.is_file():
        raise FileNotFoundError(f"Image file not found: {target}")
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"Unsupported image type: {target.suffix or '(none)'}")
    return target


def extract_image_paths(message: str, workspace_root: str | Path | None = None) -> list[str]:
    """Extract existing workspace image paths from a user message."""
    candidates = _BACKTICK_IMAGE_RE.findall(message or "")
    candidates.extend(_PLAIN_IMAGE_RE.findall(message or ""))
    paths: list[str] = []
    seen: set[str] = set()
    root = _workspace_root(workspace_root)
    for candidate in candidates:
        try:
            target = _resolve_image_path(candidate, root)
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        relative = target.relative_to(root).as_posix()
        if relative not in seen:
            seen.add(relative)
            paths.append(relative)
    return paths


def _image_content(path: Path, max_image_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_image_bytes:
        raise ValueError(
            f"Image '{path.name}' exceeds the configured "
            f"{max_image_bytes // (1024 * 1024)} MB limit."
        )
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
    }


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("Vision model returned no choices.")
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def analyze_image_files(
    paths: list[str],
    prompt: str,
    workspace_root: str | Path | None = None,
) -> str:
    """Analyze one or more images with the configured multimodal model."""
    config = load_vision_config()
    if not config["enabled"]:
        raise VisionConfigurationError(
            "Vision is disabled. Set runtime_config.json vision.enabled=true."
        )
    missing = [
        name
        for name in ("base_url", "model", "api_key")
        if not str(config.get(name) or "").strip()
    ]
    if missing:
        key_hint = f"environment variable {config['api_key_env']}" if "api_key" in missing else ""
        raise VisionConfigurationError(
            "Vision configuration is incomplete: "
            + ", ".join(missing)
            + (f". Configure the API key via {key_hint}." if key_hint else ".")
        )

    unique_paths = list(dict.fromkeys(path for path in paths if path))
    max_images = max(1, int(config["max_images_per_request"]))
    if len(unique_paths) > max_images:
        unique_paths = unique_paths[:max_images]

    root = _workspace_root(workspace_root)
    resolved = [_resolve_image_path(path, root) for path in unique_paths]
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                prompt.strip()
                or "Analyze the supplied image accurately and return useful structured observations."
            ),
        }
    ]
    content.extend(
        _image_content(path, int(config["max_image_bytes"]))
        for path in resolved
    )

    client = OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        timeout=float(config["timeout_seconds"]),
    )
    response = client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": content}],
        max_tokens=int(config["max_output_tokens"]),
        temperature=0,
    )
    text = _response_text(response)
    if not text:
        raise RuntimeError("Vision model returned an empty response.")
    return text


def analyze_image_file(
    path: str,
    prompt: str = "",
    workspace_root: str | Path | None = None,
) -> str:
    return analyze_image_files([path], prompt, workspace_root)
