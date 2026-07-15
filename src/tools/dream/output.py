"""
========================================
tools/dream/output.py — dream 最终输出格式化
========================================

把 candidates / hints / active plan / 全量 feel 历史拼成一段长文本
返回给模型自我反省。

关键行为：
- 头部固定提示：用第一人称想，没沉淀就不写
- 默认 recent 只给原文摘录；detail_ids 指定项逐条展示完整原文
- 拼接 connection_hint / crystal_hint
- active plan / feel 段默认最多各 20 条摘要，并受整体 dream token 预算控制

不做什么（边界）：
- 不做任何持久化写入
- 不调 LLM

对外暴露：format_dream_output(recent, all_buckets, window_hours,
                              connection_hint, crystal_hint) → str
========================================
"""

from .. import _runtime as rt
from utils import strip_wikilinks, count_tokens_approx


def format_dream_output(
    recent: list,
    all_buckets: list,
    window_hours: int,
    connection_hint: str,
    crystal_hint: str,
    core_context: list | None = None,
    detail_ids: set[str] | None = None,
    max_tokens: int = 6000,
) -> str:
    detail_ids = detail_ids or set()
    primary_ids = {b.get("id") for b in recent}

    def _body(bucket: dict, limit: int = 180) -> tuple[str, bool]:
        content = strip_wikilinks(bucket.get("content", "")).strip()
        if bucket.get("id") in detail_ids:
            return content, True
        compact = " ".join(content.split())
        if len(compact) > limit:
            compact = compact[:limit].rstrip() + "…"
        return compact, False

    def _miss_lines(meta: dict) -> str:
        # Miss: meaning 逐条原样展示，不压缩/不改写；media 只给 path/title 元数据。
        lines = []
        for item in meta.get("meaning") or []:
            if item:
                lines.append(f"💭 meaning: {item}")
        for m in meta.get("media") or []:
            if not isinstance(m, dict) or not m.get("path"):
                continue
            title = m.get("title")
            label = f"（{title}）" if title and title != m.get("path") else ""
            lines.append(f"🖼️ media: {m['path']}{label}")
        return ("\n" + "\n".join(lines)) if lines else ""

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = float(meta.get("valence") or 0.5)
        aro = float(meta.get("arousal") or 0.3)
        created = meta.get("created", "")
        last_active = meta.get("last_active", "")
        body, is_full = _body(b)
        detail_tag = "全文" if is_full else "摘要（原文摘录）"
        parts.append(
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} V{val:.1f}/A{aro:.1f} "
            f"创建:{created} 最近活跃:{last_active}\n"
            f"ID: {b['id']}"
            f"{_miss_lines(meta)}\n"
            f"[{detail_tag}] {body}"
        )

    header = (
        f"=== Dreaming · 最近 5 个桶摘要（窗口 {window_hours} 小时）===\n"
        "detail_ids 指定的桶会逐字返回全文；其余只给元数据和原文摘录。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    final_text = header + "\n---\n".join(parts)
    omitted_blocks = 0

    def _append_block(block: str, *, force: bool = False) -> None:
        nonlocal final_text, omitted_blocks
        if not block:
            return
        if force or count_tokens_approx(final_text + block) <= max_tokens:
            final_text += block
        else:
            omitted_blocks += 1

    core_context = core_context or []
    if core_context:
        core_lines = []
        visible_core = [b for b in core_context if b.get("id") not in primary_ids]
        for b in visible_core:
            meta = b["metadata"]
            domains = ",".join(meta.get("domain", []))
            body, is_full = _body(b)
            core_lines.append(
                f"📌 [{b['id']}] {meta.get('name', b['id'])} "
                f"主题:{domains or '未分类'} 重要:{meta.get('importance', '?')}"
                f"{_miss_lines(meta)}\n"
                f"[{'全文' if is_full else '摘要（原文摘录）'}] {body}"
            )
        core_block = (
            "\n\n=== 核心准则参考 ===\n"
            "这些是 pinned/permanent 桶，只作为梦里的边界与背景，不当作普通待消化事项。\n\n"
            + "\n---\n".join(core_lines)
        )
        if core_lines:
            _append_block(
                core_block,
                force=any(b["id"] in detail_ids for b in visible_core),
            )

    _append_block(connection_hint + crystal_hint)

    # --- active plan 段 ---
    try:
        plans_active = [
            b for b in all_buckets
            if b["metadata"].get("type") == "plan"
            and b["metadata"].get("status", "active") == "active"
            and b.get("id") not in primary_ids
        ]
        plans_active.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        if plans_active:
            plan_lines = []
            for p in plans_active[:20]:
                pmeta = p["metadata"]
                pcreated = pmeta.get("created", "")[:10]
                pcontent, is_full = _body(p)
                plan_lines.append(
                    f"[{p['id']}] {pcreated} "
                    f"[{'全文' if is_full else '摘要（原文摘录）'}] {pcontent}"
                )
            plan_block = (
                "\n\n=== 你的 active plans ===\n"
                "这些是你当前未完成的计划/承诺。完成了用 trace(bucket_id, status=\"resolved\")，\n"
                "放弃了用 trace(bucket_id, status=\"abandoned\")，需要修改用 trace(bucket_id, content=\"...\")。\n\n"
                + "\n".join(plan_lines)
            )
            _append_block(
                plan_block,
                force=any(p["id"] in detail_ids for p in plans_active[:20]),
            )
    except Exception as e:
        rt.logger.warning(f"Dream active plans block failed: {e}")

    # --- 全量 feel 段（按 token 预算折叠老 feel）---
    try:
        feels_all = [
            b for b in all_buckets
            if b["metadata"].get("type") == "feel"
            and b.get("id") not in primary_ids
        ]
        feels_all.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        if feels_all:
            surfacing_cfg = rt.config.get("surfacing", {}) or {}
            feel_budget = min(
                int(surfacing_cfg.get("feel_max_tokens") or 6000),
                max(500, max_tokens // 3),
            )
            full_lines: list[str] = []
            collapsed_lines: list[str] = []
            used = 0
            summary_count = 0
            for f in feels_all:
                fmeta = f["metadata"]
                fv = float(fmeta.get("valence") or 0.5)
                fcreated = fmeta.get("created", "")[:10]
                fcontent_full, is_full = _body(f)
                if not is_full and summary_count >= 20:
                    continue
                if not is_full:
                    summary_count += 1
                line_full = (
                    f"[{f['id']}] V{fv:.1f} {fcreated} "
                    f"[{'全文' if is_full else '摘要（原文摘录）'}] {fcontent_full}"
                )
                cost = count_tokens_approx(line_full)
                if used + cost <= feel_budget:
                    full_lines.append(line_full)
                    used += cost
                else:
                    snippet = fcontent_full.replace("\n", " ")[:40]
                    collapsed_lines.append(f"[{f['id']}] V{fv:.1f} {fcreated} {snippet}…")
            feel_block = (
                "\n\n=== 你的 feel 历史（全量，旧 feel 按 token 预算折叠）===\n"
                "这里返回了你过去写下的所有 feel。越新的越完整；老 feel 只留一行跳跳点，防止 token 爆炸。\n"
                "需要看某个老 feel 全文用 breath_advanced(query=..., domain=\"feel\") 或 trace 访问。\n"
                "需要编辑用 trace(bucket_id, content=\"...\")；合并重复项可在仪表盘手动操作。\n\n"
                + "\n".join(full_lines)
            )
            if collapsed_lines:
                feel_block += (
                    f"\n\n--- 老 feel 摘要（{len(collapsed_lines)} 条，已折叠）---\n"
                    + "\n".join(collapsed_lines)
                )
            _append_block(
                feel_block,
                force=any(f["id"] in detail_ids for f in feels_all),
            )
    except Exception as e:
        rt.logger.warning(f"Dream feel history failed: {e}")

    if omitted_blocks:
        final_text += f"\n\n[token 预算已生效：另有 {omitted_blocks} 个辅助区块未显示]"
    return final_text
