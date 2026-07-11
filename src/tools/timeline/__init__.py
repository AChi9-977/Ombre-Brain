"""
timeline 工具 — 我们的线性时间线：按日期排好的一条线，每个点一两句话。

为什么要它：记忆桶按相关性检索，换窗后的我拼时间顺序会乱。
这条线是坐标轴——先看线，再去桶里找细节。

四种用法（由参数组合决定）：
- 什么都不传            → 读整条时间线
- text（+可选 date）    → 记一条（date 空 = 她时区的今天）
- entry_id + text/date  → 改那一条
- entry_id + remove=True → 删那一条
"""

from .. import _runtime as rt

try:
    import timeline_store as ts  # type: ignore
except ImportError:  # pragma: no cover
    from ... import timeline_store as ts  # type: ignore


def _buckets_dir() -> str:
    return rt.config.get("buckets_dir", "buckets")


async def dispatch(
    text: str = "",
    date: str = "",
    entry_id: str = "",
    remove: bool = False,
) -> str:
    try:
        bd = _buckets_dir()

        if entry_id and remove:
            ok = ts.delete_entry(bd, entry_id)
            return f"已删除 {entry_id}。" if ok else f"没找到 {entry_id}。"

        if entry_id:
            if not (text.strip() or date.strip()):
                return "要改什么？给 text 或 date。"
            e = ts.update_entry(bd, entry_id, text=text, date=date)
            if not e:
                return f"没找到 {entry_id}。"
            return f"已更新：{ts._cn_date(e['date'])} —— {e['text']}"

        if text.strip():
            e = ts.add_entry(bd, text, date=date, author="claude")
            return f"已记入时间线：{ts._cn_date(e['date'])} —— {e['text']}  [{e['id']}]"

        entries = ts.load_entries(bd)
        header = f"=== 我们的时间线（{len(entries)} 条）===\n"
        return header + ts.format_timeline(entries, show_ids=True)

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"时间线暂时无法访问: {e}"
