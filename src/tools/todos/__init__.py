"""
E1: todos 工具 — 拉取所有未 resolved 桶的 todos 字段，按桶分组返回。
"""

from .. import _runtime as rt


async def dispatch() -> str:
    try:
        buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return f"记忆系统暂时无法访问: {e}"

    lines = []
    for b in buckets:
        meta = b["metadata"]
        if meta.get("resolved", False):
            continue
        todos = meta.get("todos") or []
        if not todos:
            # 也从 content 里尝试解析简单的 - [ ] 格式
            import re
            found = re.findall(r"- \[ \] (.+)", b.get("content", ""))
            if found:
                todos = found
        if not todos:
            continue
        name = meta.get("name") or b["id"]
        imp = meta.get("importance", "?")
        lines.append(f"[{b['id']}] 《{name}》 重要:{imp}")
        for t in todos:
            lines.append(f"  ☐ {t}")

    if not lines:
        return "没有找到任何待办项（todos 字段为空，或 content 中无 - [ ] 格式）。"
    return "=== 待办项 ===\n" + "\n".join(lines)
