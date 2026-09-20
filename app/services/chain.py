"""事件哈希链：保证事件链不可断裂、不可篡改。

每个事件的 hash = sha256(prev_hash + canonical_json(事件字段))，
任何对历史事件的修改、插入或删除都会使后续校验失败。
"""
from __future__ import annotations

import hashlib
import json

GENESIS = "0" * 64


def canonical(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)


def compute_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(fields)).encode("utf-8")).hexdigest()


def event_hash_fields(event) -> dict:
    """从 ORM Event 提取参与哈希的字段（与写入时保持一致）。"""
    return {
        "event_id": event.event_id,
        "container_id": event.container_id,
        "event_type": event.event_type,
        "actor_id": event.actor_id,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload,
        "corrects_event_seq": event.corrects_event_seq,
    }
