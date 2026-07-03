from __future__ import annotations

import base64
import gzip
import io
import json
import random
import string
import time
import uuid
from typing import Any

from .config import DOC_HOST
from .http_utils import http_post_with_cookies


def _rand_id(prefix: str, length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(random.choice(alphabet) for _ in range(length))


def new_table_id() -> str:
    return _rand_id("tbl", 13)


def new_field_id() -> str:
    return _rand_id("fld", 7)


def new_view_id() -> str:
    return _rand_id("vew", 7)


def new_record_id() -> str:
    return _rand_id("rec", 11)


def new_member_id() -> int:
    return random.randint(10**13, 10**14 - 1)


_TRACE_RANKS = ["i00000000", "i0000mh34", "i00018y68", "i0001vf9c", "i0002hwcg"]


def _next_rank_strings(record_ids: list[str]) -> dict[str, str]:
    rank_map: dict[str, str] = {}
    for idx, record_id in enumerate(record_ids):
        rank_map[record_id] = _TRACE_RANKS[idx] if idx < len(_TRACE_RANKS) else f"i{idx:08x}"
    return rank_map


def build_add_table_operation(name: str, index: int, owner_user_id: str, owner_name: str, owner_en_name: str, owner_avatar: str = "") -> tuple[str, str, str, list[dict[str, Any]]]:
    table_id = new_table_id()
    field_id = new_field_id()
    view_id = new_view_id()
    record_ids = [new_record_id() for _ in range(5)]
    timestamp = int(time.time())
    rank_map = _next_rank_strings(record_ids)

    snapshot = {
        "recordMap": {record_id: {} for record_id in record_ids},
        "fieldMap": {
            field_id: {
                "id": field_id,
                "name": "文本",
                "type": 1,
                "property": None,
                "fieldUIType": "Text",
                "allowedEditModes": {
                    "manual": True,
                    "scan": False,
                },
            }
        },
        "primaryKey": field_id,
        "viewMap": {
            view_id: {
                "id": view_id,
                "name": "表格",
                "publicLevel": 0,
                "type": 1,
                "isPrivate": False,
                "owner": owner_user_id,
                "property": {
                    "rowHeightLevel": 1,
                    "records": [],
                    "fields": [field_id],
                    "colInfos": {},
                    "filterInfo": None,
                    "sortInfo": [],
                    "frozenColCount": 1,
                    "group": [],
                    "autoSort": False,
                    "cardViewSetting": None,
                    "colorInfo": None,
                },
                "index": 0,
            }
        },
        "userMap": {
            owner_user_id: {
                "name": owner_name,
                "enName": owner_en_name,
                "avatarUrl": owner_avatar,
            }
        },
        "recordMeta": {
            record_id: {
                "recMeta": {
                    "rev": 0,
                    "historyLevel": 0,
                    "createdTime": timestamp,
                    "modifiedTime": timestamp,
                    "createdUser": owner_user_id,
                    "modifiedUser": owner_user_id,
                }
            }
            for record_id in record_ids
        },
        "commentMap": {},
        "resourceMap": {},
        "milestoneMap": {},
        "recordsNum": len(record_ids),
        "rankInfo": {
            "rankStep": 1_048_576,
            "nextRank": "i00034dfk",
            "rankMap": rank_map,
            "viewRankMap": {
                view_id: {
                    "rankMap": {},
                }
            },
        },
    }

    operation = {
        "command": "AddTableV2",
        "type": 1,
        "actions": [
            {
                "action": "base.addTableV2",
                "type": 1,
                "tableId": table_id,
                "isRecover": False,
                "contentCreation": False,
                "sourceTableId": None,
                "renewRecord": True,
                "data": {
                    "index": index,
                    "blockIndex": index,
                    "name": name,
                    "exInfo": {
                        "contentCreation": False,
                    },
                    "parentId": None,
                    "snapshot": snapshot,
                    "total": index,
                },
            }
        ],
        "authInfo": {
            "src_prod_type": "BITABLE_TABLE_IND",
        },
    }
    return table_id, field_id, view_id, [operation]


def submit_base_operations(
    cookies,
    *,
    base_token: str,
    operations: list[dict[str, Any]],
    user_ticket: str,
    member_id: int,
    local_rev: int,
    req_id: int = 1,
) -> dict[str, Any]:
    ops_json = json.dumps(operations, ensure_ascii=False)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
        gz.write(ops_json.encode("utf-8"))
    ops_gz = base64.b64encode(buffer.getvalue()).decode("ascii")

    body = {
        "type": "BITABLE_BASE",
        "data": {
            "member_id": member_id,
            "user_ticket": user_ticket,
            "type": "USER_CHANGES",
            "token": base_token,
            "lang": "zh",
            "localRev": local_rev,
            "operations": ops_gz,
            "signature": str(uuid.uuid4()),
            "content_type": "gzip/base64",
        },
        "version": 2,
        "req_id": req_id,
        "context": {
            "os": "windows",
            "app_version": "1.0.19.5080",
            "os_version": "10",
            "platform": "web",
            "request_id": str(uuid.uuid4()),
        },
    }

    response = http_post_with_cookies(
        cookies,
        DOC_HOST,
        f"/space/api/rce/messages?member_id={member_id}",
        body,
    )
    payload = response.get("data")
    if not isinstance(payload, dict):
        raise RuntimeError(f"BITABLE_BASE USER_CHANGES returned non-JSON payload: {payload!r}")
    data = payload.get("data") or {}
    if payload.get("code") != 0 or data.get("code") != 0 or data.get("type") != "ACCEPT_COMMIT":
        raise RuntimeError(
            f"BITABLE_BASE USER_CHANGES failed: top_code={payload.get('code')} inner_code={data.get('code')} type={data.get('type')} full={payload}"
        )
    return payload
