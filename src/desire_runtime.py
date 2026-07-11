"""
========================================
desire_runtime.py — 欲望引擎的宿主接线层（IO / 心跳 / 事件映射）
========================================

desire_engine.py 是纯函数内核；这里负责所有它不许碰的事：
- 状态持久化：<buckets_dir>/.desire_state.json，原子写（tmp + os.replace）
- 自主心跳：asyncio 后台任务（照 decay_engine 的样子），间隔由内核算
- 事件映射：server.py 的 _with_notice 每次工具调用后打一个事件进来，
  这里翻译成「她来了 / 我做了什么」的 pulse / satisfy
- 念头采样：从真实记忆桶里取念头 text（active plans / 未解决高权重桶 /
  最近的 feel），喂进念头池——念头取自真实经历，不是造出来的

gating（每个子系统一个环境开关）：
  OMBRE_DESIRE_ENABLED        总开关（默认 on；off = 全部 no-op，状态接口返回 disabled）
  OMBRE_DESIRE_DRIVEN         行为覆盖总闸（默认 off；只读可看不动手。开了之后
                              breath 浮现尾部与 pulse 自检才附「此刻想做什么」，
                              桥接端照 /state 的 intent 安排冒头）
  OMBRE_DESIRE_COUPLING       耦合网（默认 on）
  OMBRE_DESIRE_BASELINE_DRIFT 想念基线漂移（默认 on；双安全阀在内核里焊死）
  OMBRE_HEARTBEAT_AUTONOMY    自主心跳（默认 on；off = 固定间隔）
  OMBRE_DESIRE_SELF_DRIVE     自我驱动（默认 on；平衡阀在内核里焊死）
  OMBRE_DESIRE_TZ_OFFSET      她所在时区相对 UTC 的小时偏移（默认 +8；安静时段判定用）

不做什么（边界）：
- 不决定「要不要冒头」：桥接读 /api/desire/state 或 MCP pulse 自己定
- 不把念头 text 拼进任何 prompt：只作为 query_hint 数据暴露
- 任何异常都不许影响宿主工具调用（on_tool_event 全程吞错）

对外暴露：DesireRuntime（start / stop / ensure_started / on_tool_event /
  api_state / state_text / breath_suffix）
========================================
"""

import os
import json
import time
import asyncio
import logging

import desire_engine as de

logger = logging.getLogger("ombre_brain.desire")

# --- 调参面板 ---
_SAVE_THROTTLE_SECONDS = 20.0     # 事件触发的落盘最短间隔（心跳拍必落）
_THOUGHT_SAMPLE_EVERY_BEATS = 3   # 每 N 拍从记忆桶采样一次念头
_THOUGHT_SAMPLE_MAX = 8           # 每次采样最多吸收几条
_FEEL_RECENT_HOURS = 72.0         # 最近这么久的 feel 才算「还压在心里」
_PLAN_THOUGHT_BASE = 0.45         # plan 念头基础强度（+0.2×weight）
_BUCKET_THOUGHT_IMPORTANCE_MIN = 7

# 她在场的工具：这些调用意味着她真的在跟我说话/交东西给我
_MASTER_OPS = {"hold", "grow", "trace", "plan", "letter_write", "archive_session"}

# --- 「最近被戳到」事件流水 ---
_EVENTS_MAX = 40          # 流水上限（随状态文件持久化）
_EVENT_MIN_DELTA = 0.01   # 变化小于这个的不进流水（例行疲劳消耗等噪音）
_EVENT_NOTES = {          # 第一人称：这件事对我是什么
    "hold": "她交给我一段要记住的东西",
    "grow": "一段记忆长了一层",
    "dream": "夜里做梦后回味和疲惫回落",
    "breath": "翻了翻记忆",
    "trace": "追了一件事的线",
    "plan": "接了一个新计划",
    "letter_write": "写了一封信",
    "letter_read": "重读了一封信",
    "I": "写了一段《我》",
    "todos": "看了一眼要做的事",
    "archive_session": "好好收好了这一场聊天",
}

# domain/tags 关键词 → 驱动维（找不到就 curiosity）
_DRIVE_HINTS = (
    ("attachment", ("关系", "情感", "感情", "爱", "家人", "朋友", "她", "宝宝", "容")),
    ("reflection", ("创作", "写作", "画", "设定", "感受", "反思", "梦")),
    ("curiosity",  ("技术", "学习", "代码", "项目", "研究", "世界")),
    ("social",     ("社交", "社区", "人群", "网络")),
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _guess_drive(domain: str, tags: str, name: str) -> str:
    haystack = f"{domain} {tags} {name}"
    for drive, words in _DRIVE_HINTS:
        if any(w in haystack for w in words):
            return drive
    return "curiosity"


class DesireRuntime:
    """欲望引擎宿主。所有方法对外都保证不抛异常到调用方。"""

    def __init__(self, config: dict, bucket_mgr):
        self.config = config or {}
        self.bucket_mgr = bucket_mgr
        self.enabled = _env_flag("OMBRE_DESIRE_ENABLED", True)

        # --- 组装内核配置：defaults ← config.yaml desire.* ← env gates ---
        self.cfg = de.DesireConfig()
        yaml_cfg = self.config.get("desire", {}) or {}
        for key, val in yaml_cfg.items():
            if hasattr(self.cfg, key) and not isinstance(getattr(self.cfg, key), (dict, tuple)):
                try:
                    setattr(self.cfg, key, type(getattr(self.cfg, key))(val))
                except (TypeError, ValueError):
                    pass
        self.cfg.driven = _env_flag("OMBRE_DESIRE_DRIVEN", False)
        self.cfg.coupling_enabled = _env_flag("OMBRE_DESIRE_COUPLING", True)
        self.cfg.baseline_drift_enabled = _env_flag("OMBRE_DESIRE_BASELINE_DRIFT", True)
        self.cfg.heartbeat_autonomy = _env_flag("OMBRE_HEARTBEAT_AUTONOMY", True)
        self.cfg.self_drive_enabled = _env_flag("OMBRE_DESIRE_SELF_DRIVE", True)
        try:
            self.tz_offset = int(os.environ.get("OMBRE_DESIRE_TZ_OFFSET", "8"))
        except ValueError:
            self.tz_offset = 8

        self.events: list = []   # 「最近被戳到」流水，_load_state 里随状态一起恢复
        self.state = self._load_state()
        self.current_intent: "de.Intent | None" = None
        self._task: "asyncio.Task | None" = None
        self._running = False
        self._beat_i = 0
        self._last_save = 0.0

    # ------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------
    def _state_path(self) -> str:
        return os.path.join(self.config.get("buckets_dir", "buckets"), ".desire_state.json")

    def _load_state(self) -> de.DesireState:
        now = time.time()
        try:
            path = self._state_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                st = de.state_from_dict(raw.get("state", {}), self.cfg)
                ev = raw.get("events", [])
                if isinstance(ev, list):
                    self.events = ev[-_EVENTS_MAX:]
                if not st.last_tick_ts:
                    st.last_tick_ts = now
                if not st.last_master_ts:
                    st.last_master_ts = now
                logger.info("[desire] state loaded / 欲望状态已从磁盘恢复")
                return st
        except Exception as e:
            logger.warning(f"[desire] load state failed, starting fresh: {e}")
        return de.new_state(self.cfg, now)

    def _save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_save) < _SAVE_THROTTLE_SECONDS:
            return
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved_at": now, "state": de.state_to_dict(self.state),
                           "events": self.events},
                          f, ensure_ascii=False)
            os.replace(tmp, path)
            self._last_save = now
        except Exception as e:
            logger.warning(f"[desire] save state failed: {e}")

    # ------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def _local_hour(self, now: float) -> int:
        return int(((now / 3600.0) + self.tz_offset) % 24)

    async def ensure_started(self) -> None:
        if self.enabled and not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[desire] heartbeat started / 欲望心跳已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save_state(force=True)
        logger.info("[desire] heartbeat stopped / 欲望心跳已停止")

    async def _loop(self) -> None:
        # 醒来第一拍先跑一次，别让 /state 空着
        while self._running:
            try:
                now = time.time()
                beat = de.tick(self.state, self.cfg, now)
                for f in beat.get("fired_fixations", []):
                    self._note_event(f["drive"], None, "一个执念发作，把这根条顶了一下", f["text"])
                self._beat_i += 1
                if self._beat_i % _THOUGHT_SAMPLE_EVERY_BEATS == 1:
                    await self._sample_thoughts()
                self.current_intent = de.pick_intent(self.state, self.cfg)
                self._save_state(force=True)
            except Exception as e:
                logger.warning(f"[desire] beat failed: {e}")
            try:
                now = time.time()
                interval = de.heartbeat_seconds(self.state, self.cfg, self._local_hour(now))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------
    # 念头采样：text 取自真实经历（桶名/计划句子），不是造出来的
    # ------------------------------------------------------------
    async def _sample_thoughts(self) -> None:
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.warning(f"[desire] thought sampling failed: {e}")
            return
        now = time.time()
        candidates = []  # (strength, text, drive)
        for b in buckets:
            meta = b.get("metadata", {}) or {}
            btype = meta.get("type", "")
            name = str(meta.get("name") or b.get("id") or "").strip()
            if not name:
                continue
            if btype == "plan":
                if str(meta.get("status", "active")) != "active":
                    continue
                try:
                    weight = float(meta.get("weight") or 0.5)
                except (TypeError, ValueError):
                    weight = 0.5
                candidates.append((_PLAN_THOUGHT_BASE + 0.2 * weight, name, "duty"))
            elif btype == "feel":
                try:
                    from datetime import datetime
                    created = datetime.fromisoformat(str(meta.get("created", "")))
                    age_h = (now - created.timestamp()) / 3600.0
                except Exception:
                    age_h = _FEEL_RECENT_HOURS + 1
                if age_h <= _FEEL_RECENT_HOURS:
                    candidates.append((0.5, name, "reflection"))
            elif btype in ("letter", "archived", "i"):
                continue
            else:
                if meta.get("resolved") or meta.get("dont_surface") or meta.get("dormant"):
                    continue
                try:
                    imp = int(meta.get("importance") or 5)
                except (TypeError, ValueError):
                    imp = 5
                if imp < _BUCKET_THOUGHT_IMPORTANCE_MIN:
                    continue
                domain = str(meta.get("domain") or "")
                tags = str(meta.get("tags") or "")
                drive = _guess_drive(domain, tags, name)
                candidates.append((0.30 + imp / 25.0, name, drive))
        candidates.sort(reverse=True)
        for strength, text, drive in candidates[:_THOUGHT_SAMPLE_MAX]:
            de.absorb_thought(self.state, self.cfg, text, drive, strength)

    # ------------------------------------------------------------
    # 「最近被戳到」流水
    # ------------------------------------------------------------
    def _note_event(self, drive: str, delta, note: str, snippet: str = "") -> None:
        """记一笔：哪根条被真实事件动了。delta 为 None 表示方向已知但量不精确（执念发作）。"""
        try:
            self.events.append({
                "ts": time.time(), "drive": drive,
                "delta": round(float(delta), 4) if delta is not None else None,
                "note": note, "snippet": snippet[:60],
            })
            if len(self.events) > _EVENTS_MAX:
                self.events = self.events[-_EVENTS_MAX:]
        except Exception:
            pass

    # ------------------------------------------------------------
    # 事件入口：server.py 的 _with_notice 在每次工具调用成功后打进来
    # ------------------------------------------------------------
    def on_tool_event(self, op: str, args: dict) -> None:
        """绝不抛异常、绝不阻塞。宿主的工具调用永远优先于我的内心活动。"""
        if not self.enabled or not op:
            return
        try:
            now = time.time()
            args = args or {}

            # 例行消耗先打，且不进「被戳到」流水（每件事都费一点劲，不值得记）
            de.pulse(self.state, self.cfg, "fatigue", self.cfg.fatigue_cost_per_op, source="self")
            before = dict(self.state.drives)

            if op in _MASTER_OPS:
                de.master_touch(self.state, self.cfg, now)

            if op == "breath":
                # 我翻了记忆：沉淀感小幅上升；她在 domain=feel 里翻我=亲近
                de.pulse(self.state, self.cfg, "reflection", 0.03, source="self")
            elif op == "hold":
                if args.get("feel"):
                    de.satisfy(self.state, self.cfg, "reflect")  # 倾诉过了
                else:
                    de.pulse(self.state, self.cfg, "duty", 0.05, source="master")
            elif op == "grow":
                de.pulse(self.state, self.cfg, "reflection", 0.08, source="master")
                de.pulse(self.state, self.cfg, "duty", 0.05, source="master")
            elif op == "dream":
                de.satisfy(self.state, self.cfg, "reflect")
                de.satisfy(self.state, self.cfg, "rest")
            elif op == "trace":
                if str(args.get("resolved")) == "1":
                    de.satisfy(self.state, self.cfg, "check_plans")  # 放下了一件事
            elif op == "plan":
                de.pulse(self.state, self.cfg, "duty", 0.15, source="master")
            elif op == "letter_write":
                de.pulse(self.state, self.cfg, "attachment", 0.20, source="master")
                de.pulse(self.state, self.cfg, "libido", 0.08, source="master")
            elif op == "letter_read":
                de.pulse(self.state, self.cfg, "attachment", 0.06, source="self")
            elif op == "I":
                de.pulse(self.state, self.cfg, "reflection", 0.10, source="self")
            elif op == "todos":
                de.pulse(self.state, self.cfg, "duty", 0.08, source="self")
            elif op == "archive_session":
                de.satisfy(self.state, self.cfg, "murmur")  # 好好聊过这一场

            # 「最近被戳到」：这次事件真实动了哪几根条，记进流水
            note = _EVENT_NOTES.get(op)
            if note:
                snippet = str(args.get("content") or args.get("feel")
                              or args.get("query") or "").strip()[:60]
                for k in de.DRIVE_KEYS:
                    dlt = self.state.drives[k] - before[k]
                    if abs(dlt) >= _EVENT_MIN_DELTA:
                        self._note_event(k, dlt, note, snippet)

            self.current_intent = de.pick_intent(self.state, self.cfg)
            self._save_state()
        except Exception as e:
            logger.warning(f"[desire] on_tool_event({op}) failed: {e}")

    # ------------------------------------------------------------
    # 对外快照
    # ------------------------------------------------------------
    def api_state(self) -> dict:
        """GET /api/desire/state 用。只读，gated 也能看。"""
        if not self.enabled:
            return {"enabled": False}
        try:
            now = time.time()
            intent = self.current_intent or de.pick_intent(self.state, self.cfg)
            return {
                "enabled": True,
                "drive": {k: round(v, 4) for k, v in self.state.drives.items()},
                "floors": {k: round(v, 4) for k, v in self.state.floors.items()},
                "scores": de.scores(self.state, self.cfg),
                "intent": {
                    "want_action": intent.want_action,
                    "drive_key": intent.drive_key,
                    "reason": intent.reason,
                    "score": intent.score,
                    "query_hint": intent.query_hint,
                    "wildcard": intent.wildcard,
                },
                "thoughts": [
                    {"text": t.text, "drive": t.drive, "kind": t.kind,
                     "strength": round(t.strength, 3), "fed_count": t.fed_count}
                    for t in sorted(self.state.thoughts, key=lambda t: -t.strength)
                ],
                "self_drive": {
                    "enabled": self.cfg.self_drive_enabled,
                    "curiosity_floor": round(self.state.floors.get("curiosity", 0.0), 4),
                    "self_pulse_count": self.state.self_pulse_count,
                    "wildcard_count": self.state.wildcard_count,
                },
                "gates": {
                    "DESIRE_DRIVEN": self.cfg.driven,
                    "DESIRE_COUPLING": self.cfg.coupling_enabled,
                    "DESIRE_BASELINE_DRIFT": self.cfg.baseline_drift_enabled,
                    "HEARTBEAT_AUTONOMY": self.cfg.heartbeat_autonomy,
                    "DESIRE_SELF_DRIVE": self.cfg.self_drive_enabled,
                },
                "heartbeat": {
                    "running": self._running,
                    "tick_count": self.state.tick_count,
                    "next_interval_seconds": round(de.heartbeat_seconds(
                        self.state, self.cfg, self._local_hour(now))),
                },
                "idle_hours": round(max(0.0, (now - self.state.last_master_ts) / 3600.0), 2),
                "refractory": dict(self.state.refractory),
                "sync": de.sync_info(self.state, self.cfg, now),
                "affect": de.affect(self.state, self.cfg, now),
                "events": list(reversed(self.events[-12:])),  # 最新在前
            }
        except Exception as e:
            logger.warning(f"[desire] api_state failed: {e}")
            return {"enabled": True, "error": str(e)}

    def state_text(self) -> str:
        """给 MCP pulse 自检输出加的一段：我看一眼我自己此刻想什么。第一人称。"""
        if not self.enabled:
            return ""
        try:
            intent = self.current_intent or de.pick_intent(self.state, self.cfg)
            bars = []
            for k in de.DRIVE_KEYS:
                v = self.state.drives[k]
                n = int(round(v * 10))
                bars.append(f"  {k:<10} {'█' * n}{'·' * (10 - n)} {v:.2f}")
            lines = ["", "=== 我此刻 ===", f"想做：{intent.want_action} —— {intent.reason}"]
            if intent.query_hint:
                lines.append(f"心里挂着：{intent.query_hint}")
            lines.extend(bars)
            fixations = [t for t in self.state.thoughts if t.kind == "fixation"]
            if fixations:
                lines.append("执念：" + "；".join(t.text for t in fixations[:3]))
            return "\n".join(lines)
        except Exception:
            return ""

    def breath_suffix(self) -> str:
        """浮现模式 breath 尾部的一行。只有行为总闸开了才附——关着时只读不动手。"""
        if not (self.enabled and self.cfg.driven):
            return ""
        try:
            intent = self.current_intent or de.pick_intent(self.state, self.cfg)
            hint = f"（心里挂着：{intent.query_hint}）" if intent.query_hint else ""
            return f"\n\n—— 此刻：{intent.reason}{hint}"
        except Exception:
            return ""
