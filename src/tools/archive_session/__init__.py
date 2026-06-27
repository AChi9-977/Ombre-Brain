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
) -> str:
    if not summary or not summary.strip():
        return "summary 不能为空。"
    if valence is None: valence = -1
    if arousal is None: arousal = -1

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
        return f"对话摘要已存档: {bucket_id}"
    except Exception as e:
        return f"存档失败: {e}"
