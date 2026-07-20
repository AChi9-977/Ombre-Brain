"""
========================================
mailbox_store.py — 信箱存储（独立 JSON，跨窗口留言）
========================================

解决的问题：archive_session 可以附带一段写给下一个对话窗口的自由文本。
它是留言，不是记忆——不需要向量化、不参与衰减、不进入 breath/dream。

关键行为：
- 存储：<buckets_dir>/mailbox.json（Railway volume 上持久化）
- 原子写：tmp + os.replace，避免半截 JSON
- 条目结构：{id, session_archive_id, text, created}
- 读取：按 created 倒序，默认取最新 N 条

不做什么（边界）：
- 不参与任何检索/浮现/衰减/脱水/embedding——纯留言本
- 不提供修改/删除接口（留言发出即不可改，和真实信箱一样）

对外暴露：add_message / list_messages / latest_message
========================================
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone

_lock = threading.Lock()

MAX_TEXT_LEN = 500  # 留言是短文本，不是正文


def _path(buckets_dir: str) -> str:
    return os.path.join(buckets_dir, "mailbox.json")


def _load(buckets_dir: str) -> list[dict]:
    """读取信箱全部留言，文件不存在或损坏时返回空列表。"""
    path = _path(buckets_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save(buckets_dir: str, messages: list[dict]) -> None:
    """原子写：先写临时文件再 os.replace。"""
    path = _path(buckets_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def add_message(
    buckets_dir: str,
    text: str,
    session_archive_id: str = "",
) -> dict:
    """存入一条新留言。返回完整的 message 对象。"""
    text = text.strip()[:MAX_TEXT_LEN]
    if not text:
        raise ValueError("留言内容不能为空")

    msg = {
        "id": uuid.uuid4().hex[:12],
        "session_archive_id": session_archive_id,
        "text": text,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with _lock:
        messages = _load(buckets_dir)
        messages.append(msg)
        _save(buckets_dir, messages)

    return msg


def list_messages(buckets_dir: str, limit: int = 20) -> list[dict]:
    """按时间倒序返回最近的留言。"""
    messages = _load(buckets_dir)
    messages.sort(key=lambda m: m.get("created", ""), reverse=True)
    return messages[:limit]


def latest_message(buckets_dir: str) -> dict | None:
    """返回最新一条留言，无人留言时返回 None。"""
    messages = _load(buckets_dir)
    if not messages:
        return None
    messages.sort(key=lambda m: m.get("created", ""), reverse=True)
    return messages[0]
