from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import RagConfig, load_rag_config

try:
    from huggingface_hub import snapshot_download
except Exception:  # pragma: no cover - exercised only when optional dependency is absent.
    snapshot_download = None  # type: ignore[assignment]


def _model_dir_name(repo_id: str) -> str:
    return repo_id.replace("/", "__")


_MODEL_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".onnx", ".gguf")
_INCOMPLETE_DOWNLOAD_SUFFIXES = (".incomplete", ".lock")
_NON_MODEL_ASSET_DIRS = {"assets", "imgs", "images"}
_MODEL_DOWNLOAD_ALLOW_PATTERNS = [
    "*.json",
    "*.txt",
    "*.model",
    "*.py",
    "*.safetensors",
    "*.bin",
    "*.pt",
    "*.onnx",
    "*.onnx_data",
    "1_Pooling/*",
]


def _has_model_weights(path: str | Path) -> bool:
    model_path = Path(path)
    if not model_path.exists():
        return False
    return any(
        candidate.is_file() and candidate.name.endswith(_MODEL_WEIGHT_SUFFIXES)
        for candidate in model_path.rglob("*")
    )


def _has_incomplete_download_artifacts(path: str | Path) -> bool:
    model_path = Path(path)
    if not model_path.exists():
        return False
    for candidate in model_path.rglob("*"):
        if not candidate.is_file() or not candidate.name.endswith(_INCOMPLETE_DOWNLOAD_SUFFIXES):
            continue
        try:
            relative_parts = candidate.relative_to(model_path).parts
        except ValueError:
            relative_parts = candidate.parts
        if any(part in _NON_MODEL_ASSET_DIRS for part in relative_parts):
            continue
        return True
    return False


def resolve_huggingface_model_plan(config: RagConfig) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    if config.embedding_provider == "huggingface_local" and config.embedding_model:
        plan.append(
            {
                "role": "embedding",
                "repo_id": config.embedding_model,
                "local_dir": str(config.model_cache_path / "embedding" / _model_dir_name(config.embedding_model)),
            }
        )
    if config.reranker.provider == "huggingface_local" and config.reranker.model:
        plan.append(
            {
                "role": "reranker",
                "repo_id": config.reranker.model,
                "local_dir": str(config.model_cache_path / "reranker" / _model_dir_name(config.reranker.model)),
            }
        )
    return plan


def download_configured_huggingface_models(
    config: RagConfig,
    *,
    local_files_only: bool = False,
    resume_download: bool = True,
) -> dict[str, Any]:
    if snapshot_download is None:
        return {
            "status": "blocked",
            "reason": "huggingface_hub is not installed",
            "models": [],
        }
    plan = resolve_huggingface_model_plan(config)
    config.model_cache_path.mkdir(parents=True, exist_ok=True)
    models: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for item in plan:
        try:
            local_path = snapshot_download(
                repo_id=item["repo_id"],
                local_dir=item["local_dir"],
                local_dir_use_symlinks=False,
                resume_download=resume_download,
                local_files_only=local_files_only,
                allow_patterns=_MODEL_DOWNLOAD_ALLOW_PATTERNS,
            )
            if _has_incomplete_download_artifacts(local_path):
                errors.append({**item, "error": "snapshot has incomplete model download artifacts", "status": "failed"})
                continue
            if not _has_model_weights(local_path):
                errors.append({**item, "error": "snapshot has no model weight files", "status": "failed"})
                continue
            models.append({**item, "local_path": str(local_path), "status": "downloaded"})
        except Exception as exc:
            errors.append({**item, "error": str(exc), "status": "failed"})

    status = "skipped"
    if plan:
        status = "downloaded" if len(models) == len(plan) and not errors else "failed"
    manifest = {
        "status": status,
        "embedding_model": config.embedding_model,
        "reranker_model": config.reranker.model,
        "vector_backend": config.vector_backend,
        "sparse_backend": config.sparse_backend,
        "models": models,
        "errors": errors,
    }
    (config.model_cache_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download configured local Hugging Face RAG models.")
    parser.add_argument("--local-files-only", action="store_true", help="Only inspect existing local model files.")
    args = parser.parse_args(argv)
    result = download_configured_huggingface_models(
        load_rag_config(),
        local_files_only=args.local_files_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"downloaded", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
