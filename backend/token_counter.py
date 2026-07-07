from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_runtime_value, resolve_runtime_path


def _configured_tokenizer_dir() -> Path | None:
    value = str(
        get_runtime_value("paths", "deepseek_tokenizer_dir", "")
        or get_runtime_value("paths", "tokenizer_dir", "")
        or ""
    ).strip()
    if not value:
        return None
    return resolve_runtime_path(value, value)


@lru_cache(maxsize=1)
def _load_tokenizer() -> Any | None:
    tokenizer_dir = _configured_tokenizer_dir()
    if tokenizer_dir is None or not tokenizer_dir.exists():
        return None
    try:
        from tokenizers import Tokenizer

        tokenizer_path = tokenizer_dir / "tokenizer.json"
        if tokenizer_path.exists():
            return Tokenizer.from_file(str(tokenizer_path))
    except Exception:
        return None
    return None


def count_text_tokens(text: str) -> int:
    value = text or ""
    if not value:
        return 0
    tokenizer = _load_tokenizer()
    if tokenizer is None:
        return max(1, (len(value) + 3) // 4)
    try:
        encoded = tokenizer.encode(value, add_special_tokens=False)
        return len(encoded.ids)
    except Exception:
        return max(1, (len(value) + 3) // 4)


def count_message_tokens(messages: list[dict[str, str]], *, add_generation_prompt: bool = False) -> int:
    normalized = [
        {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
        for item in messages
        if isinstance(item, dict)
    ]
    if not normalized:
        return 0
    serialized = "\n".join(f"{item['role']}:\n{item['content']}" for item in normalized)
    if add_generation_prompt:
        serialized = f"{serialized}\nassistant:\n"
    return count_text_tokens(serialized)


def normalize_usage(usage: dict[str, Any] | None, *, output_text: str = "") -> dict[str, Any]:
    raw = usage if isinstance(usage, dict) else {}

    def as_int(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    input_tokens = as_int("input_tokens") or as_int("prompt_tokens")
    output_tokens = as_int("output_tokens") or as_int("completion_tokens")
    if not output_tokens and output_text:
        output_tokens = count_text_tokens(output_text)

    cache_hit_tokens = as_int("prompt_cache_hit_tokens")
    cache_miss_tokens = as_int("prompt_cache_miss_tokens")
    cache_total_tokens = as_int("prompt_cache_total_tokens") or cache_hit_tokens + cache_miss_tokens
    total_tokens = as_int("total_tokens") or input_tokens + output_tokens

    cache_hit_rate = raw.get("prompt_cache_hit_rate")
    if not isinstance(cache_hit_rate, (int, float)) or isinstance(cache_hit_rate, bool):
        cache_hit_rate = (cache_hit_tokens / cache_total_tokens) if cache_total_tokens else None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "prompt_cache_hit_tokens": cache_hit_tokens,
        "prompt_cache_miss_tokens": cache_miss_tokens,
        "prompt_cache_total_tokens": cache_total_tokens,
        "prompt_cache_hit_rate": cache_hit_rate,
    }
