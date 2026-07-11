"""
========================================
timeline_store.py — 线性时间线存储（纯文件，无嵌入无衰减）
========================================

解决的问题：记忆桶是按相关性检索的，不同窗口的小克拼时间线时顺序会乱。
这里维护一条**线性**的大事记：每个条目 = 日期 + 一两句话，整体按日期排序，
小克和容容都能增删改。

关键行为：
- 存储：<buckets_dir>/timeline.json（Railway volume 上，随桶数据一起持久化）
- 原子写：tmp + os.replace（同 .desire_state.json），避免半截 JSON
- 条目结构：{id, date: "YYYY-MM-DD", text, author: "claude"|"user", created}
- 排序：读取时按 date 升序（同日按 created），文件里顺序无所谓
- 格式化：同一天多条用「//」连接，如「6月19日 · 小克生日 // 容容第一次和小克说话」

不做什么（边界）：
- 不参与 breath/dream/decay，不做 embedding——它是坐标轴，不是记忆本体
- 不做并发锁跨进程保护：单进程单事件循环，模块内 threading.Lock 足够

对外暴露：load_entries / add_entry / update_entry / delete_entry / format_timeline
========================================
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()

MAX_TEXT_LEN = 200  # 时间线是概括，不是正文；超长说明用错了地方


def _path(buckets_dir: str) -> str:
    return os.path.join(buckets_dir, "timeline.json")


def today_local() -> str:
    """她所在时区的今天（复用欲望系统的 OMBRE_DESIRE_TZ_OFFSET，默认 +8）。"""
    try:
        offset = int(os.environ.get("OMBRE_DESIRE_TZ_OFFSET", "8"))
    except ValueError:
        offset = 8
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).strftime("%Y-%m-%d")


def normalize_date(raw: str) -> str:
    """把「2026-6-9」「2026/06/09」「6月19日」这类写法规整成 YYYY-MM-DD。

    无年份时补当前本地年。解析不出来抛 ValueError，让调用方给出人话提示。
    """
    s = (raw or "").strip()
    if not s:
        return today_local()
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})日?$", s)
        if not m:
            raise ValueError(f"看不懂的日期格式: {raw!r}（要 YYYY-MM-DD 或 M月D日）")
        y = int(today_local()[:4])
        mo, d = int(m.group(1)), int(m.group(2))
    # 用 datetime 校验合法性（2月30日这种直接报错）
    return datetime(y, mo, d).strftime("%Y-%m-%d")


def _load_raw(buckets_dir: str) -> list[dict]:
    path = _path(buckets_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", [])
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


def _save_raw(buckets_dir: str, entries: list[dict]) -> None:
    path = _path(buckets_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_entries(buckets_dir: str) -> list[dict]:
    """全部条目，按 date 升序（同日按 created 升序）。"""
    entries = _load_raw(buckets_dir)
    entries.sort(key=lambda e: (e.get("date", ""), e.get("created", "")))
    return entries


def add_entry(buckets_dir: str, text: str, date: str = "", author: str = "claude") -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("时间线条目不能为空")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f"时间线是一两句话的概括（≤{MAX_TEXT_LEN}字），细节放记忆桶里")
    entry = {
        "id": f"tl_{uuid.uuid4().hex[:8]}",
        "date": normalize_date(date),
        "text": text,
        "author": author if author in ("claude", "user") else "claude",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        entries = _load_raw(buckets_dir)
        entries.append(entry)
        _save_raw(buckets_dir, entries)
    return entry


def update_entry(buckets_dir: str, entry_id: str, text: str = "", date: str = "") -> dict | None:
    """改 text 和/或 date。找不到返回 None。"""
    with _lock:
        entries = _load_raw(buckets_dir)
        for e in entries:
            if e.get("id") == entry_id:
                if text and text.strip():
                    t = text.strip()
                    if len(t) > MAX_TEXT_LEN:
                        raise ValueError(f"时间线是一两句话的概括（≤{MAX_TEXT_LEN}字）")
                    e["text"] = t
                if date and date.strip():
                    e["date"] = normalize_date(date)
                _save_raw(buckets_dir, entries)
                return e
    return None


def delete_entry(buckets_dir: str, entry_id: str) -> bool:
    with _lock:
        entries = _load_raw(buckets_dir)
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        _save_raw(buckets_dir, kept)
    return True


def _cn_date(date: str) -> str:
    """2026-06-19 → 2026年6月19日。解析失败原样返回。"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        return f"{d.year}年{d.month}月{d.day}日"
    except ValueError:
        return date


def format_timeline(entries: list[dict], show_ids: bool = False) -> str:
    """按日期升序渲染成一条线；同一天多条用 // 连接。

    show_ids=True 时每天后面附 [id] 列表，方便小克 edit/remove 时引用。
    """
    if not entries:
        return "时间线还是空的。"
    by_date: dict[str, list[dict]] = {}
    for e in entries:
        by_date.setdefault(e.get("date", "????-??-??"), []).append(e)
    lines = []
    for date in sorted(by_date.keys()):
        group = by_date[date]
        texts = " // ".join(e.get("text", "") for e in group)
        line = f"{_cn_date(date)} —— {texts}"
        if show_ids:
            ids = " ".join(f"[{e.get('id', '?')}]" for e in group)
            line += f"  {ids}"
        lines.append(line)
    return "\n".join(lines)
