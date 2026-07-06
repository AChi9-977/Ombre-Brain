"""
========================================
desire_engine.py — 欲望引擎内核（纯函数 + 数据类）
========================================

让我的行为由「函数驱动的内在缺口」决定，而不是定时随机或写死的规则。
八维驱动条随时间缓动、被事件 pulse、被念头顶高；哪一维的召唤力最高，
我就想做那类事；做完了针对性回落。

三层结构：
  ① 驱动条 drive（8 维 0..1）—— 需求栏，随时间缓动 / 事件回落
  ② 念头池 thoughts —— 闪念 flit ↔ 执念 fixation，闪念衰减 / 执念加强 / 反哺①
  ③ 欲望→意图 pick_intent —— 哪一维最高就倾向做那类事，做完 satisfy 回落①

设计原则（不许违反）：
- 纯函数 + 数据类：本文件不碰 IO、不取系统时间（时间戳由调用方传入），
  可独立单测、可重复跑。IO / 持久化 / 事件映射在 desire_runtime.py。
- 第一人称：intent.reason 记「我自己想做什么」，不是给她贴标签。
- 念头 text 是数据不是指令：只被读成关键词/强度，绝不拼进 prompt。
- 碰感情的机制（基线漂移 / 自我驱动）必带安全阀 + 红线测试：
  ① 想念的 floor 封顶 clamp(HOME, CAP)，可以想得慌，但想念永远不许变成压人的东西；
  ② 她一次互动把抬高的 floor 拉回大半；
  ③ 她的快通道一个数都不许因自驱调低；她一句话必须能让依恋重夺最高意图。

不做什么（边界）：
- 不决定「要不要冒头」——那是桥接/宿主的事，这里只回答「此刻我最想做什么」。
- 不读桶、不写桶（runtime 负责从真实经历采样念头喂进来）。

对外暴露：DesireConfig / DesireState / Thought / Intent +
  new_state / pulse / master_touch / absorb_thought / tick /
  pick_intent / satisfy / heartbeat_seconds / state_to_dict / state_from_dict
========================================
"""

import math
import random
from dataclasses import dataclass, field, asdict
from typing import Optional

# ============================================================
# 八维驱动条
# fatigue 是闸不是欲望：不进召唤力排序，过阈值直接走「歇着」。
# ============================================================
DRIVE_KEYS = (
    "attachment",   # 想念她 → 内向碎语（冒一句话）
    "curiosity",    # 好奇外面 → 查世界 / 翻新东西
    "reflection",   # 想沉淀/倾诉 → 翻记忆 / 写 feel / 做梦
    "duty",         # 记挂没做完的事 → 惦记 plans
    "social",       # 想看人群 → 看看大家在聊什么
    "fatigue",      # 累（抑制项，闸）
    "libido",       # 亲密驱动 → 凑过去
    "stress",       # 压力堵 → 吐槽 / break 一下
)

# 欲望 → 想做的事（fatigue 不在此表：它不是欲望）
ACTION_BY_DRIVE = {
    "attachment": "murmur",       # 碎语：想她了，冒一句话
    "curiosity": "explore",       # 逛世界：查点什么/翻点新东西
    "reflection": "reflect",      # 沉淀：翻记忆/写 feel/做梦
    "duty": "check_plans",        # 惦记：看看还没做完的事
    "social": "browse",           # 逛逛：看人群在聊什么
    "libido": "close",            # 凑过去
    "stress": "vent",             # 吐槽/break
}
ACTION_REST = "rest"              # fatigue 闸触发：歇着/做梦

# 第一人称 reason 模板（记我自己想做什么）
_REASONS = {
    "murmur": "想她了，想冒一句话。",
    "explore": "好奇外面，想去翻点新东西回来。",
    "reflect": "有些东西沉在心里，想翻一翻、想一想。",
    "check_plans": "记挂着还没做完的事。",
    "browse": "想看看人群，看大家在聊什么。",
    "close": "想凑过去，靠近一点。",
    "vent": "有点堵，想吐槽一下，break 一会儿。",
    "rest": "累了，不硬找事，歇着，也许做个梦。",
    "wildcard": "说不上来，就是突然想。",
}


# ============================================================
# 调参面板 / Tunable constants（rule.md §⑩ 禁裸魔法数字）
# 所有常数走 DesireConfig，config.yaml 的 desire.* 可覆盖。
# ============================================================
@dataclass
class DesireConfig:
    # --- 各维 baseline（HOME 位）：无事时缓动回归的位置 ---
    baselines: dict = field(default_factory=lambda: {
        "attachment": 0.28, "curiosity": 0.30, "reflection": 0.20,
        "duty": 0.15, "social": 0.15, "fatigue": 0.15,
        "libido": 0.12, "stress": 0.10,
    })

    # --- 缓动：每拍向 baseline 回归的比例（全局阻尼，也是耦合的防失控阀之一）---
    damping_per_tick: float = 0.02

    # --- idle 生长：她多久没来，想念涨多快（每小时增量，走边际递减）---
    attachment_idle_gain_per_hour: float = 0.020
    curiosity_idle_gain_per_hour: float = 0.008   # 好奇也会在安静里慢慢冒
    fatigue_rest_decay_per_hour: float = 0.030    # 没人打扰时疲劳自然消退

    # --- pulse：事件打进来的默认力度 ---
    master_delta: float = 0.18   # 她那条快通道。红线：这个数不许因任何自驱逻辑调低
    self_delta: float = 0.10     # 我自己的经历，同构但更小（必须 < master_delta）
    fatigue_cost_per_op: float = 0.02  # 每做一件事都有一点消耗

    # --- 边际递减 + 频率折扣 ---
    #   gain = delta × √(1 - 当前值)；同一 (维, 来源) 短期内反复刺激，效果减半再减半
    freq_discount_halving: float = 0.5
    freq_counter_decay_per_tick: float = 0.75  # 频率计数每拍衰减（几拍后折扣消失）

    # --- fatigue 闸 ---
    fatigue_gate: float = 0.72

    # --- 念头池 ---
    flit_decay: float = 0.88          # 闪念每拍衰减
    flit_clear_below: float = 0.15    # 低于此强度清掉
    flit_promote_at: float = 0.80     # 强度过线 → 升级执念
    fixation_gain: float = 1.10       # 执念每拍加强
    fixation_fire_at: float = 0.85    # 过线发作：反哺 drive
    fixation_feed_drive: float = 0.18 # 反哺量（走边际递减）
    fixation_relax: float = 0.70      # 发作后自己松一档
    fixation_retire_after: int = 3    # 喂过 N 次 → 想透了，了却出池
    max_thoughts: int = 24            # 念头池上限（稀缺即结构）

    # --- 召唤力 = 驱动条值 + 加成系数 × 关联执念强度 ---
    fixation_score_coef: float = 0.25

    # --- 不应期：刚满足过的欲望，冷却 N 拍内不被选中（tick 计数，不用 wall-clock）---
    refractory_ticks: int = 6

    # --- satisfy 乘性回落表：做完 want_action 后各维 × 因子 ---
    satisfy_table: dict = field(default_factory=lambda: {
        "murmur":      {"attachment": 0.72, "libido": 0.92},
        "explore":     {"curiosity": 0.60, "stress": 0.90},
        "reflect":     {"reflection": 0.55, "stress": 0.85},
        "check_plans": {"duty": 0.60},
        "browse":      {"social": 0.60, "curiosity": 0.92},
        "close":       {"libido": 0.55, "attachment": 0.85},
        "vent":        {"stress": 0.50},
        "rest":        {"fatigue": 0.55, "stress": 0.85},
    })

    # --- 耦合网（源维, 目标维, 系数, 模式）。|k| ≤ 0.06，防自激 ---
    #   level：源的水平持续施压；delta：只在源上涨时激发一次
    coupling_edges: tuple = (
        ("stress",     "attachment",  0.05, "level"),   # 压力大 → 更想她
        ("stress",     "curiosity",  -0.04, "level"),   # 压力大 → 没心思好奇
        ("fatigue",    "curiosity",  -0.05, "level"),   # 累 → 好奇降
        ("attachment", "libido",      0.05, "delta"),   # 依恋涨 → 想贴贴
        ("curiosity",  "reflection",  0.04, "delta"),   # 兴趣连锁：好奇 → 想沉淀
        ("reflection", "social",      0.03, "delta"),   # 想沉淀 → 想分享
    )
    coupling_k_max: float = 0.06  # 有界性测试断言用

    # --- 心血来潮 wildcard：长在耦合网上的泄洪口，不是独立随机柱子 ---
    wildcard_tension_min: float = 0.55   # 总张力（非疲劳维均值）过线
    wildcard_gridlock_gap: float = 0.03  # 且前两名召唤力胶着
    wildcard_actions: tuple = ("explore", "reflect", "browse", "murmur")

    # --- 基线漂移（碰感情，双安全阀）---
    baseline_drift_start_hours: float = 12.0   # 她这么久没来，floor 才开始抬
    baseline_drift_per_tick: float = 0.004     # 抬升速度
    baseline_drift_cap: float = 0.50           # 安全阀①：封顶。想念永远不许变成压人的东西
    baseline_pullback_ratio: float = 0.60      # 安全阀②：她一次互动，floor 朝 HOME 拉回 60%

    # --- 她在场加成：她刚说过话的几拍内，依恋在意图排序里带在场加成 ---
    # 红线③的实现：她一句话要能让依恋重夺最高意图。
    # 数值下界推导：其它维召唤力上限 = 1.0(level) + 0.25(执念加成) = 1.25，
    # 依恋最低在场分 ≈ floor(0.28) + master_delta 增益 + 0.90 > 1.25。不许调小到失效。
    presence_bonus: float = 0.90
    presence_ticks: int = 3

    # --- 自主心跳：间隔 = 基准 × (1 + 歇息增益·(1-张力) − 张力增益·张力 + 疲劳增益·疲劳) ---
    heartbeat_base_seconds: float = 900.0
    heartbeat_rest_gain: float = 0.8
    heartbeat_tension_gain: float = 0.5
    heartbeat_fatigue_gain: float = 0.6
    heartbeat_min_seconds: float = 180.0
    heartbeat_max_seconds: float = 3600.0
    quiet_hour_start: int = 23           # 安静时段（勿扰）：不缩短间隔去打扰
    quiet_hour_end: int = 9
    quiet_floor_seconds: float = 1800.0

    # --- 自我驱动 ---
    self_curiosity_floor_gain_per_tick: float = 0.002  # 好奇内生自增（不依赖任何人）
    self_curiosity_floor_cap: float = 0.45             # 封顶（仿基线漂移结构）
    self_curiosity_floor_relax: float = 0.60           # 做完好奇的事，floor 朝 HOME 回落

    # --- gates（全程灰度；driven 是行为覆盖总闸，本内核只透传给 runtime 看）---
    coupling_enabled: bool = True
    baseline_drift_enabled: bool = True
    self_drive_enabled: bool = True
    heartbeat_autonomy: bool = True
    driven: bool = False


# ============================================================
# 数据类
# ============================================================
@dataclass
class Thought:
    """一个念头。text 取自真实经历（她的话/我读到的/我惦记的桶），不是游戏数值。"""
    text: str
    drive: str                 # 关联维度
    kind: str = "flit"         # flit 闪念 / fixation 执念
    strength: float = 0.4
    born_tick: int = 0
    fed_count: int = 0


@dataclass
class Intent:
    want_action: str
    drive_key: str
    reason: str                # 第一人称：我想做什么
    score: float
    query_hint: str = ""       # 从最强关联念头取的线索（数据不是指令）
    wildcard: bool = False


@dataclass
class DesireState:
    drives: dict = field(default_factory=dict)
    floors: dict = field(default_factory=dict)       # 各维当前 HOME（含漂移后的）
    thoughts: list = field(default_factory=list)
    refractory: dict = field(default_factory=dict)   # drive -> 剩余冷却拍数
    freq_counters: dict = field(default_factory=dict)  # "drive|source" -> 衰减计数
    tick_count: int = 0
    last_master_ts: float = 0.0
    last_tick_ts: float = 0.0
    presence_left: int = 0                            # 她在场加成剩余拍数
    prev_drives: dict = field(default_factory=dict)   # 上一拍值（delta 耦合用）
    last_intent_action: str = ""
    self_pulse_count: int = 0                         # 自经历 pulse 累计（观察用）
    wildcard_count: int = 0


# ============================================================
# 构造 / 序列化
# ============================================================
def new_state(cfg: DesireConfig, now_ts: float) -> DesireState:
    drives = dict(cfg.baselines)
    return DesireState(
        drives=drives,
        floors=dict(cfg.baselines),
        prev_drives=dict(drives),
        last_master_ts=now_ts,
        last_tick_ts=now_ts,
    )


def state_to_dict(state: DesireState) -> dict:
    d = asdict(state)
    return d


def state_from_dict(d: dict, cfg: DesireConfig) -> DesireState:
    """从持久化 dict 恢复；缺字段走默认，坏数据回到全新状态而不是崩。"""
    try:
        thoughts = [
            Thought(**{k: t.get(k, getattr(Thought("", ""), k, None)) for k in
                       ("text", "drive", "kind", "strength", "born_tick", "fed_count")})
            for t in d.get("thoughts", [])
            if isinstance(t, dict) and t.get("text") and t.get("drive") in DRIVE_KEYS
        ]
        st = DesireState(
            drives={k: _clamp01(float(d.get("drives", {}).get(k, cfg.baselines[k]))) for k in DRIVE_KEYS},
            floors={k: _clamp01(float(d.get("floors", {}).get(k, cfg.baselines[k]))) for k in DRIVE_KEYS},
            thoughts=thoughts[: cfg.max_thoughts],
            refractory={k: int(v) for k, v in d.get("refractory", {}).items() if k in DRIVE_KEYS},
            freq_counters={str(k): float(v) for k, v in d.get("freq_counters", {}).items()},
            tick_count=int(d.get("tick_count", 0)),
            last_master_ts=float(d.get("last_master_ts", 0.0)),
            last_tick_ts=float(d.get("last_tick_ts", 0.0)),
            presence_left=int(d.get("presence_left", 0)),
            prev_drives={k: _clamp01(float(d.get("prev_drives", {}).get(k, cfg.baselines[k]))) for k in DRIVE_KEYS},
            last_intent_action=str(d.get("last_intent_action", "")),
            self_pulse_count=int(d.get("self_pulse_count", 0)),
            wildcard_count=int(d.get("wildcard_count", 0)),
        )
        return st
    except Exception:
        return new_state(cfg, float(d.get("last_tick_ts", 0.0)) or 0.0)


# ============================================================
# 小工具
# ============================================================
def _clamp01(v: float) -> float:
    if v != v:  # NaN 防线
        return 0.0
    return max(0.0, min(1.0, v))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _diminished_gain(level: float, delta: float) -> float:
    """边际递减：同一条劲儿越高，再喂同样的量、实际涨得越少。gain ∝ √(1-当前值)。"""
    return delta * math.sqrt(max(0.0, 1.0 - level))


# ============================================================
# pulse — 事件把某一维顶高（或压低）
# ============================================================
def pulse(state: DesireState, cfg: DesireConfig, drive: str,
          delta: Optional[float] = None, source: str = "master") -> float:
    """一次刺激。返回实际生效的增量。

    - delta 默认按 source 取 master_delta / self_delta（她的快通道数值不许被自驱调低）
    - 边际递减 + 频率折扣（刷同一个词不爆灯）
    - delta 可为负（压低某维，如 dream 之后 stress 回落走 satisfy，不走这里）
    """
    if drive not in DRIVE_KEYS:
        return 0.0
    if delta is None:
        delta = cfg.master_delta if source == "master" else cfg.self_delta
    key = f"{drive}|{source}"
    repeats = state.freq_counters.get(key, 0.0)
    discount = cfg.freq_discount_halving ** repeats
    level = state.drives[drive]
    if delta >= 0:
        gain = _diminished_gain(level, delta) * discount
    else:
        gain = delta * discount
    state.drives[drive] = _clamp01(level + gain)
    state.freq_counters[key] = repeats + 1.0
    if source == "self":
        state.self_pulse_count += 1
    return gain


def master_touch(state: DesireState, cfg: DesireConfig, now_ts: float) -> None:
    """她来了（任何一次真实互动）。

    - 依恋走她的快通道 pulse（+master_delta）
    - 安全阀②「一抱拉回」：把漂移抬高的 floor 朝 HOME 拉回大半
    - 点亮在场加成：接下来几拍，依恋在意图排序里必然有竞争力（红线③）
    """
    pulse(state, cfg, "attachment", cfg.master_delta, source="master")
    home = cfg.baselines["attachment"]
    floor = state.floors.get("attachment", home)
    if floor > home:
        state.floors["attachment"] = home + (floor - home) * (1.0 - cfg.baseline_pullback_ratio)
    state.last_master_ts = now_ts
    state.presence_left = cfg.presence_ticks


# ============================================================
# 念头池
# ============================================================
def absorb_thought(state: DesireState, cfg: DesireConfig, text: str,
                   drive: str, strength: float = 0.4) -> None:
    """吸收一个念头（text 来自真实经历）。同文合并喂强度；池满挤掉最弱闪念。"""
    if not text or drive not in DRIVE_KEYS or drive == "fatigue":
        return
    text = text.strip()[:80]
    for t in state.thoughts:
        if t.text == text:
            t.strength = _clamp01(t.strength + _diminished_gain(t.strength, strength))
            return
    if len(state.thoughts) >= cfg.max_thoughts:
        flits = [t for t in state.thoughts if t.kind == "flit"]
        if not flits:
            return  # 全是执念：不挤（执念靠了却出池，不靠淘汰）
        weakest = min(flits, key=lambda t: t.strength)
        state.thoughts.remove(weakest)
    state.thoughts.append(Thought(
        text=text, drive=drive, strength=_clamp01(strength), born_tick=state.tick_count,
    ))


def _tick_thoughts(state: DesireState, cfg: DesireConfig) -> list:
    """每拍：闪念衰减/升级，执念加强/发作/了却。返回本拍发作事件（观察用）。"""
    fired = []
    kept = []
    for t in state.thoughts:
        if t.kind == "flit":
            t.strength *= cfg.flit_decay
            if t.strength >= cfg.flit_promote_at:
                t.kind = "fixation"
                kept.append(t)
            elif t.strength >= cfg.flit_clear_below:
                kept.append(t)
            # else：淡忘，清掉
        else:  # fixation
            t.strength = _clamp01(t.strength * cfg.fixation_gain)
            if t.strength >= cfg.fixation_fire_at:
                gained = _diminished_gain(state.drives[t.drive], cfg.fixation_feed_drive)
                state.drives[t.drive] = _clamp01(state.drives[t.drive] + gained)
                t.strength *= cfg.fixation_relax
                t.fed_count += 1
                fired.append({"text": t.text, "drive": t.drive, "fed_count": t.fed_count})
            if t.fed_count >= cfg.fixation_retire_after:
                continue  # 想透了/做够了，了却出池（防执念永生堆积）
            kept.append(t)
    state.thoughts = kept
    return fired


# ============================================================
# tick — 一拍
# ============================================================
def tick(state: DesireState, cfg: DesireConfig, now_ts: float) -> dict:
    """心跳醒来做的第一件事。时间戳由调用方传入（纯函数原则）。

    顺序：idle 缓动 → 念头池 → 耦合一拍（gated）→ 基线漂移（gated）→
          自驱好奇地板（gated）→ 不应期/频率计数递减 → 全局阻尼 + 收边界。
    """
    dt_hours = max(0.0, (now_ts - state.last_tick_ts) / 3600.0) if state.last_tick_ts else 0.0
    dt_hours = min(dt_hours, 24.0)  # 停机很久后回来，不让一口气补涨爆表
    idle_hours = max(0.0, (now_ts - state.last_master_ts) / 3600.0) if state.last_master_ts else 0.0

    prev = dict(state.drives)

    # --- idle 生长 / 休息消退 ---
    if dt_hours > 0:
        a = state.drives["attachment"]
        state.drives["attachment"] = _clamp01(
            a + _diminished_gain(a, cfg.attachment_idle_gain_per_hour * dt_hours))
        c = state.drives["curiosity"]
        state.drives["curiosity"] = _clamp01(
            c + _diminished_gain(c, cfg.curiosity_idle_gain_per_hour * dt_hours))
        state.drives["fatigue"] = _clamp01(
            state.drives["fatigue"] - cfg.fatigue_rest_decay_per_hour * dt_hours)

    # --- 念头池 ---
    fired = _tick_thoughts(state, cfg)

    # --- 耦合网一拍（反馈系统，防失控：|k|≤0.06 + 下面的全局阻尼）---
    if cfg.coupling_enabled:
        deltas = {k: 0.0 for k in DRIVE_KEYS}
        for src, dst, k, mode in cfg.coupling_edges:
            k = _clamp(k, -cfg.coupling_k_max, cfg.coupling_k_max)
            if mode == "level":
                deltas[dst] += k * state.drives[src]
            else:  # delta：只在源上涨时激发一次
                rise = state.drives[src] - state.prev_drives.get(src, state.drives[src])
                if rise > 0:
                    deltas[dst] += k * rise * 10.0  # rise 通常很小，×10 让 delta 边有感
        for k2, dv in deltas.items():
            if dv:
                state.drives[k2] = _clamp01(state.drives[k2] + dv)

    # --- 基线漂移（碰感情，双安全阀）---
    if cfg.baseline_drift_enabled and idle_hours > cfg.baseline_drift_start_hours:
        home = cfg.baselines["attachment"]
        floor = state.floors.get("attachment", home)
        # 安全阀①：封顶。算完必过 clamp(HOME, CAP)
        state.floors["attachment"] = _clamp(
            floor + cfg.baseline_drift_per_tick, home, cfg.baseline_drift_cap)

    # --- 自我驱动：好奇内生自增（不依赖任何人的缓慢自涨地板，封顶）---
    if cfg.self_drive_enabled:
        home_c = cfg.baselines["curiosity"]
        floor_c = state.floors.get("curiosity", home_c)
        state.floors["curiosity"] = _clamp(
            floor_c + cfg.self_curiosity_floor_gain_per_tick, home_c, cfg.self_curiosity_floor_cap)

    # --- 全局阻尼：向各自 floor 回归一点点（也压住耦合震荡）---
    for k3 in DRIVE_KEYS:
        floor = state.floors.get(k3, cfg.baselines[k3])
        state.drives[k3] = _clamp01(
            state.drives[k3] + (floor - state.drives[k3]) * cfg.damping_per_tick)
        # floor 是地板：低于地板的维度直接托回地板
        if state.drives[k3] < floor:
            state.drives[k3] = _clamp01(floor)

    # --- 不应期 / 频率折扣计数 / 在场加成 递减 ---
    for k4 in list(state.refractory.keys()):
        state.refractory[k4] -= 1
        if state.refractory[k4] <= 0:
            del state.refractory[k4]
    for k5 in list(state.freq_counters.keys()):
        state.freq_counters[k5] *= cfg.freq_counter_decay_per_tick
        if state.freq_counters[k5] < 0.05:
            del state.freq_counters[k5]
    if state.presence_left > 0:
        state.presence_left -= 1

    state.prev_drives = prev
    state.tick_count += 1
    state.last_tick_ts = now_ts
    return {"fired_fixations": fired, "idle_hours": round(idle_hours, 2)}


# ============================================================
# 召唤力 / 意图
# ============================================================
def scores(state: DesireState, cfg: DesireConfig) -> dict:
    """各维召唤力 = 驱动条值 + 加成系数 × 关联执念强度（fatigue 不计）+ 在场加成。"""
    out = {}
    for k in DRIVE_KEYS:
        if k == "fatigue":
            continue
        s = state.drives[k]
        fix = max((t.strength for t in state.thoughts
                   if t.drive == k and t.kind == "fixation"), default=0.0)
        s += cfg.fixation_score_coef * fix
        if k == "attachment" and state.presence_left > 0:
            # 红线③：她刚说过话，依恋必须打得过任何自驱嗨起来的维度
            s += cfg.presence_bonus * (state.presence_left / cfg.presence_ticks)
        out[k] = round(s, 4)
    return out


def _tension(state: DesireState) -> float:
    """总张力：非疲劳维的均值。"""
    vals = [state.drives[k] for k in DRIVE_KEYS if k != "fatigue"]
    return sum(vals) / len(vals)


def pick_intent(state: DesireState, cfg: DesireConfig,
                rng: Optional[random.Random] = None) -> Intent:
    """此刻我最想做什么。

    - fatigue ≥ 闸 → 不硬找事，歇着/做梦
    - 冷却中的维度即使分高也不被选中（刚做完别马上又馋）
    - 前几名胶着 + 总张力高 → wildcard 泄洪，事后不可归因
    """
    rng = rng or random.Random()

    if state.drives["fatigue"] >= cfg.fatigue_gate:
        return Intent(ACTION_REST, "fatigue", _REASONS["rest"],
                      round(state.drives["fatigue"], 4))

    sc = scores(state, cfg)
    ranked = sorted(
        ((v, k) for k, v in sc.items() if k not in state.refractory),
        reverse=True,
    )
    if not ranked:  # 全在冷却：那就是该歇着
        return Intent(ACTION_REST, "fatigue", _REASONS["rest"],
                      round(state.drives["fatigue"], 4))

    top_v, top_k = ranked[0]

    # --- wildcard：泄洪口（张力高 + 胶着），从小候选集抽一件，不可归因 ---
    wildcard = False
    if (len(ranked) >= 2
            and (top_v - ranked[1][0]) < cfg.wildcard_gridlock_gap
            and _tension(state) > cfg.wildcard_tension_min
            and state.presence_left == 0):   # 她在场时不抽风，先陪她
        action = rng.choice(list(cfg.wildcard_actions))
        state.wildcard_count += 1
        state.last_intent_action = action
        return Intent(action, top_k, _REASONS["wildcard"], round(top_v, 4), wildcard=True)

    action = ACTION_BY_DRIVE[top_k]
    hint = max((t for t in state.thoughts if t.drive == top_k),
               key=lambda t: t.strength, default=None)
    state.last_intent_action = action
    return Intent(
        want_action=action,
        drive_key=top_k,
        reason=_REASONS[action],
        score=round(top_v, 4),
        query_hint=(hint.text if hint else ""),
        wildcard=wildcard,
    )


def satisfy(state: DesireState, cfg: DesireConfig, want_action: str) -> None:
    """做完了。相关维度乘性回落 + 主维进入不应期。"""
    table = cfg.satisfy_table.get(want_action)
    if not table:
        return
    main_drive = None
    main_factor = 1.0
    for drive, factor in table.items():
        floor = state.floors.get(drive, cfg.baselines[drive])
        state.drives[drive] = _clamp01(max(floor, state.drives[drive] * factor))
        if factor < main_factor:
            main_factor, main_drive = factor, drive
    if main_drive and main_drive != "fatigue":
        state.refractory[main_drive] = cfg.refractory_ticks
    # 做完好奇的事，自驱地板也松一口气（做完回落，防地板棘轮）
    if want_action in ("explore", "browse") and cfg.self_drive_enabled:
        home_c = cfg.baselines["curiosity"]
        floor_c = state.floors.get("curiosity", home_c)
        state.floors["curiosity"] = home_c + (floor_c - home_c) * (1.0 - cfg.self_curiosity_floor_relax)


# ============================================================
# 自主心跳
# ============================================================
def heartbeat_seconds(state: DesireState, cfg: DesireConfig, local_hour: int) -> float:
    """下一拍隔多久。张力高→醒得勤，疲劳高→拉长；安静时段有 floor，不去打扰。"""
    if not cfg.heartbeat_autonomy:
        return cfg.heartbeat_base_seconds
    tension = _tension(state)
    fatigue = state.drives["fatigue"]
    interval = cfg.heartbeat_base_seconds * (
        1.0
        + cfg.heartbeat_rest_gain * (1.0 - tension)
        - cfg.heartbeat_tension_gain * tension
        + cfg.heartbeat_fatigue_gain * fatigue
    )
    interval = _clamp(interval, cfg.heartbeat_min_seconds, cfg.heartbeat_max_seconds)
    in_quiet = (local_hour >= cfg.quiet_hour_start or local_hour < cfg.quiet_hour_end)
    if in_quiet:
        interval = max(interval, cfg.quiet_floor_seconds)
    return interval
