from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_proto_module():
    path = Path(__file__).resolve().parent / "feishu-personal" / "lark_tools" / "proto.py"
    spec = importlib.util.spec_from_file_location("_feishu_personal_proto", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load protobuf helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PROTO = _load_proto_module()


def generic_decode_bytes(raw: bytes) -> Any:
    return _PROTO.generic_decode(raw)
