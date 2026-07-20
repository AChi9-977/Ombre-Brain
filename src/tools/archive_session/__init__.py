"""
E2: archive_session 工具 — 将对话摘要存入归档。
"""

from typing import Optional

from .. import _runtime as rt
from utils import now_iso


async def dispatch(
    summary: str,
    highlights: Optional[str] = "",
    mood: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    message: Optional[str] = "",
) -> str:
    if not summary or not summary.strip():
        return "summary 不能为空。"
    if valence is None: valence = -1
    if arousal is None: arousal = -1
    if message is None: message = ""

    content_parts = [f"## 对话摘要\n{summary.strip()}"]
    if highlights and highlights.strip():
        content_parts.append(f"## 亮点\n{highlights.strip()}")
    if mood and mood.strip():
        content_parts.append(f"## 情绪\n{mood.strip()}")
    content = "\n\n".join(content_parts)

    kwargs: dict = {
        "content": content,
        "tags": ["session_archive"],
        "importance": 4,
        "domain": ["session"],
    }
    if 0 <= valence <= 1:
        kwargs["valence"] = valence
    if 0 <= arousal <= 1:
        kwargs["arousal"] = arousal

    try:
        bucket_id = await rt.bucket_mgr.create(**kwargs)

        # --- 信箱留言：如果传了 message，存入独立的信箱存储 ---
        mailbox_note = ""
        if message.strip():
            try:
                from mailbox_store import add_message
                msg = add_message(
                    buckets_dir=rt.config.get("buckets_dir", "buckets"),
                    text=message.strip(),
                    session_archive_id=bucket_id,
                )
                mailbox_note = f" 留言已投递: {msg['id']}"
            except Exception as e:
                rt.logger.warning(f"mailbox add_message failed: {e}")
                mailbox_note = " （留言投递失败）"

        return f"对话摘要已存档: {bucket_id}{mailbox_note}"
    except Exception as e:
        return f"存档失败: {e}"
