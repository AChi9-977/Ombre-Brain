"""
========================================
tools/dream/__init__.py — dream 工具入口
========================================

dream 是「我做一次梦——读最近 N 小时内有变动的所有桶，自己沉进去想
一遍」。这里把整个流程拆成三步：
1. candidates.py：筛选窗口内的桶 + 软上限
2. hints.py：连接提示 + 结晶提示
3. output.py：拼最终文本（包含 active plan 段、feel 历史段）

dispatch() 只负责把这三步串起来。

对外暴露：dispatch(window_hours, detail_ids, max_tokens) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .candidates import collect_candidates, collect_core_context
from .hints import build_connection_hint, build_crystal_hint
from .output import format_dream_output


async def dispatch(
    window_hours: Optional[int] = 48,
    detail_ids: Optional[str] = "",
    max_tokens: Optional[int] = 0,
) -> str:
    await rt.decay_engine.ensure_started()

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    window_hours = max(1, min(int(window_hours or 48), 24 * 14))
    recent_all = collect_candidates(all_buckets, window_hours)
    recent = recent_all[:5]
    core_context = collect_core_context(all_buckets)

    requested_ids = [
        value.strip() for value in str(detail_ids or "").split(",") if value.strip()
    ]
    if len(requested_ids) > 20 or any(len(value) > 128 for value in requested_ids):
        return "detail_ids 最多接受 20 个 bucket_id，每个 ID 最长 128 个字符。"
    detail_set = set(requested_ids)
    by_id = {b["id"]: b for b in all_buckets}
    recent_ids = {b["id"] for b in recent}
    # Explicit requests override the time window and bucket type.  output.py
    # skips duplicates from the supplemental core/plan/feel sections.
    for bucket_id in requested_ids:
        bucket = by_id.get(bucket_id)
        if not bucket or bucket_id in recent_ids:
            continue
        recent.append(bucket)
        recent_ids.add(bucket_id)

    if not recent and not core_context:
        missing_ids = [bucket_id for bucket_id in requested_ids if bucket_id not in by_id]
        if missing_ids:
            return (
                f"过去 {window_hours} 小时内没有需要消化的新记忆。\n"
                "未找到 detail_ids: " + ", ".join(missing_ids)
            )
        return f"过去 {window_hours} 小时内没有需要消化的新记忆。"

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    try:
        output_budget = int(max_tokens or surfacing_cfg.get("dream_max_tokens") or 6000)
    except (TypeError, ValueError):
        output_budget = 6000
    output_budget = max(500, min(output_budget, 20000))

    connection_hint = await build_connection_hint(recent_all[:5])
    crystal_hint = await build_crystal_hint(all_buckets)

    final_text = format_dream_output(
        recent=recent,
        all_buckets=all_buckets,
        window_hours=window_hours,
        connection_hint=connection_hint,
        crystal_hint=crystal_hint,
        core_context=core_context,
        detail_ids=detail_set,
        max_tokens=output_budget,
    )

    missing_ids = [bucket_id for bucket_id in requested_ids if bucket_id not in by_id]
    if missing_ids:
        final_text += "\n\n未找到 detail_ids: " + ", ".join(missing_ids)

    if rt.fire_webhook:
        await rt.fire_webhook("dream", {"recent": len(recent_all[:5]), "chars": len(final_text)})
    return final_text
