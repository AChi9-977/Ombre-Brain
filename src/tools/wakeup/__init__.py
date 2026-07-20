"""
========================================
tools/wakeup/__init__.py — 一键开机工具
========================================

wakeup 是每次新对话窗口开始时的入口。单次调用返回六个区块的摘要：
钉选桶、最近对话归档、未完结待办、最新信箱留言、感受回声、今日浮现。

各区内容沿用现有摘要格式，总量控制在合理范围。每区附带验真字段
（_source / _count / _as_of），便于模型交叉验证。

若信箱或感受类记忆尚不存在，对应区块输出占位提示，不报错。

对外暴露：dispatch() → str
========================================
"""

import random
from datetime import datetime, timezone, timedelta
from typing import Optional

from .. import _runtime as rt
from utils import strip_wikilinks, count_tokens_approx


# --- 可调参数 ---
_SESSION_ARCHIVE_LIMIT = 3       # 最近对话归档摘要条数
_FEEL_ECHO_MIN_AGE_DAYS = 7      # 感受回声：至少存在多少天才参与随机抽取
_TRIGGER_DATE_WINDOW_DAYS = 0    # 今日浮现：trigger_date 距今天多少天内（0=仅今天）


def _bucket_summary_line(b: dict) -> str:
    """摘要模式单行格式，与 breath/surface.py 保持一致。"""
    meta = b["metadata"]
    name = meta.get("name") or b["id"]
    domains = ",".join(meta.get("domain", []) or []) or "未分类"
    val = float(meta.get("valence") or 0.5)
    aro = float(meta.get("arousal") or 0.3)
    imp = meta.get("importance", "?")
    updated = str(meta.get("last_active") or meta.get("created", ""))[:10]
    return f"[{b['id']}] 《{name}》 主题:{domains} 情感:V{val:.1f}/A{aro:.1f} 重要:{imp} 更新:{updated}"


def _verification(section_name: str, count: int) -> str:
    """生成验真字段行。"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[验真: _source={section_name} _count={count} _as_of={ts}]"


async def dispatch() -> str:
    """主入口：聚合六个区块，返回开机画面。"""
    await rt.decay_engine.ensure_started()
    sections: list[str] = []
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return f"记忆系统暂时无法访问: {e}"

    # ================================================================
    # §1 📌 钉选桶摘要区
    # ================================================================
    pinned_buckets = [
        b for b in all_buckets
        if b["metadata"].get("pinned") or b["metadata"].get("protected")
    ]
    if pinned_buckets:
        lines = [_bucket_summary_line(b) for b in pinned_buckets]
        sections.append(
            f"=== 📌 核心准则（{len(pinned_buckets)} 条）===\n"
            + "\n".join(f"  {l}" for l in lines)
            + f"\n{_verification('pinned', len(pinned_buckets))}"
        )
    else:
        sections.append(
            f"=== 📌 核心准则 ===\n  （暂无钉选桶）\n"
            f"{_verification('pinned', 0)}"
        )

    # ================================================================
    # §2 📄 最近对话归档摘要
    # ================================================================
    session_buckets = [
        b for b in all_buckets
        if "session_archive" in (b["metadata"].get("tags") or [])
    ]
    session_buckets.sort(
        key=lambda b: b["metadata"].get("created", ""), reverse=True
    )
    recent_sessions = session_buckets[:_SESSION_ARCHIVE_LIMIT]
    if recent_sessions:
        lines = []
        for b in recent_sessions:
            created = (b["metadata"].get("created", ""))[:10]
            # 取正文首行作为摘要（去掉 ## 标记）
            content_preview = strip_wikilinks(b.get("content", ""))
            first_line = content_preview.split("\n")[0].strip().lstrip("#").strip()[:120]
            lines.append(f"  [{created}] {first_line}")
        sections.append(
            f"=== 📄 最近对话归档（{len(recent_sessions)} 条 / 共 {len(session_buckets)} 条）===\n"
            + "\n".join(lines)
            + f"\n{_verification('session_archive', len(recent_sessions))}"
        )
    else:
        sections.append(
            f"=== 📄 最近对话归档 ===\n  （暂无对话归档）\n"
            f"{_verification('session_archive', 0)}"
        )

    # ================================================================
    # §3 📋 全库未完结待办
    # ================================================================
    todo_lines: list[str] = []
    import re as _re
    for b in all_buckets:
        meta = b["metadata"]
        if meta.get("resolved", False):
            continue
        todos = meta.get("todos") or []
        if not todos:
            found = _re.findall(r"- \[ \] (.+)", b.get("content", ""))
            if found:
                todos = found
        if not todos:
            continue
        name = meta.get("name") or b["id"]
        imp = meta.get("importance", "?")
        todo_lines.append(f"  [{b['id']}] 《{name}》 重要:{imp}")
        for t in todos:
            todo_lines.append(f"    ☐ {t}")

    if todo_lines:
        bucket_count = len(set(
            l.split("]")[0].split("[")[1] if "[" in l and "]" in l else ""
            for l in todo_lines if l.startswith("  [")
        ))
        sections.append(
            f"=== 📋 未完结待办 ===\n"
            + "\n".join(todo_lines)
            + f"\n{_verification('todos', len(todo_lines))}"
        )
    else:
        sections.append(
            f"=== 📋 未完结待办 ===\n  （没有未完结的待办项）\n"
            f"{_verification('todos', 0)}"
        )

    # ================================================================
    # §4 📬 最新信箱留言
    # ================================================================
    try:
        from mailbox_store import latest_message
        msg = latest_message(rt.config.get("buckets_dir", "buckets"))
        if msg:
            created = msg.get("created", "")[:16]
            text = msg.get("text", "")
            sections.append(
                f"=== 📬 最新信箱留言 ===\n"
                f"  [{created}] {text}\n"
                f"{_verification('mailbox', 1)}"
            )
        else:
            sections.append(
                f"=== 📬 最新信箱留言 ===\n"
                f"  （信箱为空——尚未有人在此留言。可在 archive_session 时附带 message 参数投递留言。）\n"
                f"{_verification('mailbox', 0)}"
            )
    except ImportError:
        sections.append(
            f"=== 📬 最新信箱留言 ===\n"
            f"  （信箱功能尚未建成，此处为占位提示，建成后将自动接入。）\n"
            f"{_verification('mailbox', -1)}"
        )

    # ================================================================
    # §5 🫧 感受回声 —— 随机一条较早的感受类记忆
    # ================================================================
    feel_buckets = [
        b for b in all_buckets
        if b["metadata"].get("type") == "feel"
    ]
    old_feels = []
    cutoff_date = now - timedelta(days=_FEEL_ECHO_MIN_AGE_DAYS)
    for b in feel_buckets:
        created_str = b["metadata"].get("created", "")
        try:
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if created_dt < cutoff_date:
                old_feels.append(b)
        except (ValueError, TypeError):
            # 无法解析日期的 feel 也纳入（宽容处理）
            old_feels.append(b)

    if old_feels:
        picked = random.choice(old_feels)
        created = (picked["metadata"].get("created", ""))[:10]
        content = strip_wikilinks(picked.get("content", ""))[:200]
        content_one_line = content.replace("\n", " ").strip()
        sections.append(
            f"=== 🫧 感受回声 ===\n"
            f"  [{created}] [bucket_id:{picked['id']}] {content_one_line}\n"
            f"{_verification('feel_echo', 1)}"
        )
    elif feel_buckets:
        # 有 feel 但都太新
        sections.append(
            f"=== 🫧 感受回声 ===\n"
            f"  （有 {len(feel_buckets)} 条感受，但都太新（<{_FEEL_ECHO_MIN_AGE_DAYS}天），暂不回声。）\n"
            f"{_verification('feel_echo', 0)}"
        )
    else:
        sections.append(
            f"=== 🫧 感受回声 ===\n"
            f"  （暂无感受类记忆。可以用 hold(feel=True, content=\"我的感受...\") 写下第一条。）\n"
            f"{_verification('feel_echo', 0)}"
        )

    # ================================================================
    # §6 📅 今日浮现 —— 触发日期为今天（或已过期未处理）的桶
    # ================================================================
    if _TRIGGER_DATE_WINDOW_DAYS > 0:
        window_start = (now - timedelta(days=_TRIGGER_DATE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    else:
        window_start = today_str

    triggered_buckets = []
    for b in all_buckets:
        meta = b["metadata"]
        td = meta.get("trigger_date", "")
        if not td:
            continue
        if meta.get("trigger_processed", False):
            continue
        if td <= today_str:
            # 也检查是否在窗口内（避免非常古老的 trigger_date 冒出来）
            if td >= window_start or _TRIGGER_DATE_WINDOW_DAYS > 0:
                triggered_buckets.append(b)
            elif td < window_start:
                # 已过期且超出窗口，但未处理 → 仍然显示（过期未处理是重要信号）
                triggered_buckets.append(b)

    # 去重（同一个桶可能被多条规则命中）
    seen_ids = set()
    unique_triggered = []
    for b in triggered_buckets:
        if b["id"] not in seen_ids:
            seen_ids.add(b["id"])
            unique_triggered.append(b)
    unique_triggered.sort(key=lambda b: b["metadata"].get("trigger_date", ""))

    if unique_triggered:
        lines = []
        for b in unique_triggered:
            td = b["metadata"].get("trigger_date", "?")
            past_mark = " ⚠️已过期" if td < today_str else ""
            lines.append(
                f"  [{td}]{past_mark} {_bucket_summary_line(b)}"
            )
        sections.append(
            f"=== 📅 今日浮现（{len(unique_triggered)} 条）===\n"
            + "\n".join(lines)
            + f"\n（处理完可用 trace(bucket_id, trigger_processed=1) 标记已处理）\n"
            f"{_verification('trigger_today', len(unique_triggered))}"
        )
    else:
        sections.append(
            f"=== 📅 今日浮现 ===\n"
            f"  （今天没有安排触发的事项。\n"
            f"   用 hold(trigger_date=\"YYYY-MM-DD\") 给桶设置一个未来的唤醒日期。）\n"
            f"{_verification('trigger_today', 0)}"
        )

    # ================================================================
    # 拼接输出
    # ================================================================
    header = "☀️ 开机。以下是此刻的记忆概览：\n"
    return header + "\n\n".join(sections)
