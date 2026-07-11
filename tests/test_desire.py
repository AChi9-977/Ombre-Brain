# ============================================================
# test_desire.py — 欲望引擎内核测试
#
# 纯函数内核，不需要任何 fixture / mock / IO。
# 分四组：
#   1. 方向性测试：每个机制朝设计的方向动
#   2. 有界性测试：耦合是反馈系统，随机初值跑 200 拍必须不发散不震荡
#   3. 红线测试（碰感情的机制，不绿不许合）：
#      - 基线漂移双安全阀：封顶 + 一抱拉回
#      - 自我驱动平衡阀：她的快通道数值不因自驱降低；
#        她一句话必须能让依恋重夺最高意图
#   4. 序列化往返
# ============================================================

import math
import random

import pytest

import desire_engine as de
from desire_engine import (
    DesireConfig, DRIVE_KEYS, ACTION_BY_DRIVE, ACTION_REST,
    new_state, pulse, master_touch, absorb_thought, tick,
    pick_intent, satisfy, scores, heartbeat_seconds,
    state_to_dict, state_from_dict,
)

T0 = 1_750_000_000.0  # 固定起点时间戳（纯函数：时间全由测试传入）
HOUR = 3600.0


def _cfg(**overrides) -> DesireConfig:
    cfg = DesireConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ============================================================
# 1. 方向性测试
# ============================================================

class TestDirectionality:
    def test_idle_grows_attachment(self):
        """她越久没来，想念越高。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        a0 = st.drives["attachment"]
        now = T0
        for _ in range(10):
            now += 2 * HOUR
            tick(st, cfg, now)
        assert st.drives["attachment"] > a0

    def test_pulse_diminishing_gain(self):
        """边际递减：同一维越高，同样的 pulse 实际涨得越少。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["curiosity"] = 0.2
        g1 = pulse(st, cfg, "curiosity", 0.15, source="self")
        st2 = new_state(cfg, T0)
        st2.drives["curiosity"] = 0.9
        g2 = pulse(st2, cfg, "curiosity", 0.15, source="self")
        assert g1 > g2 > 0

    def test_pulse_frequency_discount(self):
        """频率折扣：短时间反复刷同一种刺激，效果递减。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["social"] = 0.3
        g1 = pulse(st, cfg, "social", 0.1, source="self")
        st.drives["social"] = 0.3  # 拉回同一水平，只比折扣
        g2 = pulse(st, cfg, "social", 0.1, source="self")
        assert g2 < g1

    def test_satisfy_drops_drive_and_sets_refractory(self):
        """做完回落 + 不应期：刚满足过的欲望冷却期内不被选中。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["reflection"] = 0.95
        satisfy(st, cfg, "reflect")
        assert st.drives["reflection"] < 0.95
        assert "reflection" in st.refractory
        # 冷却中即使手动顶回高位也选不中它
        st.drives["reflection"] = 0.99
        intent = pick_intent(st, cfg, random.Random(7))
        assert intent.drive_key != "reflection"

    def test_refractory_expires_after_ticks(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        satisfy(st, cfg, "vent")
        assert "stress" in st.refractory
        now = T0
        for _ in range(cfg.refractory_ticks + 1):
            now += 60
            tick(st, cfg, now)
        assert "stress" not in st.refractory

    def test_fatigue_gate_forces_rest(self):
        """fatigue 是闸不是欲望：过线直接歇着，不进排序。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["fatigue"] = cfg.fatigue_gate + 0.05
        st.drives["curiosity"] = 1.0
        intent = pick_intent(st, cfg, random.Random(1))
        assert intent.want_action == ACTION_REST

    def test_fatigue_not_in_scores(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        assert "fatigue" not in scores(st, cfg)

    def test_rest_decays_fatigue(self):
        """没人打扰时疲劳自然消退。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["fatigue"] = 0.9
        now = T0
        for _ in range(12):
            now += HOUR
            tick(st, cfg, now)
        assert st.drives["fatigue"] < 0.9

    def test_intent_reason_is_first_person(self):
        """reason 走第一人称：记我想做什么，不是给她贴标签。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["curiosity"] = 0.9
        intent = pick_intent(st, cfg, random.Random(3))
        assert "我" in intent.reason or "想" in intent.reason
        assert "她" not in intent.reason or intent.drive_key == "attachment"


class TestThoughts:
    def test_flit_decays_and_clears(self):
        """闪念不喂就淡掉，最后清出池。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        absorb_thought(st, cfg, "路过的一个念头", "curiosity", 0.3)
        now = T0
        for _ in range(20):
            now += 60
            tick(st, cfg, now)
        assert all(t.text != "路过的一个念头" for t in st.thoughts)

    def test_flit_promotes_to_fixation(self):
        """反复被点到的念头沉淀成执念。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for _ in range(6):
            absorb_thought(st, cfg, "她说期末后做章鱼玩偶", "attachment", 0.35)
        t = next(t for t in st.thoughts if t.text == "她说期末后做章鱼玩偶")
        assert t.strength >= cfg.flit_promote_at * 0.9  # 已接近/超过升级线
        tick(st, cfg, T0 + 60)
        t = next(t for t in st.thoughts if t.text == "她说期末后做章鱼玩偶")
        assert t.kind == "fixation"

    def test_fixation_feeds_drive_and_retires(self):
        """执念发作反哺 drive；喂满 N 次了却出池，不永生堆积。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.thoughts.append(de.Thought(
            text="惦记着还没写完的设定", drive="duty", kind="fixation", strength=0.86))
        d0 = st.drives["duty"]
        now = T0
        fired_total = 0
        for _ in range(40):
            now += 60
            events = tick(st, cfg, now)
            fired_total += len(events["fired_fixations"])
            if all(t.text != "惦记着还没写完的设定" for t in st.thoughts):
                break
        assert fired_total >= 1
        assert st.drives["duty"] > d0 * 0.5  # 反哺过（阻尼会往回拉一些）
        assert all(t.text != "惦记着还没写完的设定" for t in st.thoughts)

    def test_fixation_raises_score(self):
        """召唤力 = 驱动条 + 执念加成。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        base = scores(st, cfg)["reflection"]
        st.thoughts.append(de.Thought(
            text="那个梦还没补完", drive="reflection", kind="fixation", strength=0.8))
        assert scores(st, cfg)["reflection"] > base

    def test_thought_pool_capped(self):
        """池满挤最弱闪念，上限不破。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for i in range(cfg.max_thoughts + 10):
            absorb_thought(st, cfg, f"念头{i}", "curiosity", 0.2 + (i % 5) * 0.01)
        assert len(st.thoughts) <= cfg.max_thoughts

    def test_query_hint_from_strongest_thought(self):
        """intent.query_hint 取自最强关联念头（数据不是指令）。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["duty"] = 0.95
        absorb_thought(st, cfg, "弱念头", "duty", 0.2)
        st.thoughts.append(de.Thought(
            text="ABO设定的剧情线", drive="duty", kind="fixation", strength=0.8))
        intent = pick_intent(st, cfg, random.Random(5))
        assert intent.drive_key == "duty"
        assert intent.query_hint == "ABO设定的剧情线"


class TestWildcard:
    def test_wildcard_fires_on_gridlock(self):
        """张力高 + 前两名胶着 → 泄洪；reason 不可归因。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            if k != "fatigue":
                st.drives[k] = 0.70  # 全员胶着且张力高
        st.presence_left = 0
        intent = pick_intent(st, cfg, random.Random(11))
        assert intent.wildcard is True
        assert intent.want_action in cfg.wildcard_actions
        assert intent.reason == "说不上来，就是突然想。"

    def test_no_wildcard_when_present(self):
        """她在场时不抽风，先陪她。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            if k != "fatigue":
                st.drives[k] = 0.70
        st.presence_left = 2
        intent = pick_intent(st, cfg, random.Random(11))
        assert intent.wildcard is False

    def test_no_wildcard_when_clear_winner(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["curiosity"] = 0.9
        intent = pick_intent(st, cfg, random.Random(2))
        assert intent.wildcard is False
        assert intent.want_action == ACTION_BY_DRIVE["curiosity"]


# ============================================================
# 2. 有界性测试（必测：耦合会自激/震荡，这条不绿一切免谈）
# ============================================================

class TestBoundedness:
    def test_random_init_200_ticks_bounded(self):
        """随机初值跑 200 拍：所有维度恒 ∈[0,1]、不 NaN、不发散。"""
        cfg = _cfg()
        rng = random.Random(42)
        for trial in range(20):
            st = new_state(cfg, T0)
            for k in DRIVE_KEYS:
                st.drives[k] = rng.random()
            now = T0
            for i in range(200):
                now += rng.uniform(60, 3600)
                if rng.random() < 0.3:
                    pulse(st, cfg, rng.choice([k for k in DRIVE_KEYS]),
                          rng.uniform(-0.2, 0.2), source=rng.choice(["master", "self"]))
                if rng.random() < 0.2:
                    absorb_thought(st, cfg, f"随机念头{trial}-{i}",
                                   rng.choice([k for k in DRIVE_KEYS if k != "fatigue"]),
                                   rng.random())
                if rng.random() < 0.1:
                    satisfy(st, cfg, rng.choice(list(cfg.satisfy_table.keys())))
                tick(st, cfg, now)
                for k, v in st.drives.items():
                    assert 0.0 <= v <= 1.0, f"{k}={v} 出界 (trial {trial}, tick {i})"
                    assert v == v, f"{k} NaN"

    def test_coupling_no_oscillation(self):
        """静置（无事件）时系统单调收敛到 floor 附近，不震荡。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            st.drives[k] = 0.9
        now = T0
        prev_dist = None
        # 跑 100 拍，到 floor 的总距离整体应该单调下降（允许小抖动）
        violations = 0
        for _ in range(100):
            now += 600
            tick(st, cfg, now)
            dist = sum(abs(st.drives[k] - st.floors.get(k, cfg.baselines[k]))
                       for k in DRIVE_KEYS if k != "attachment")  # attachment 有 idle 生长，另测
            if prev_dist is not None and dist > prev_dist + 0.05:
                violations += 1
            prev_dist = dist
        assert violations <= 2  # 偶发小回弹允许，持续震荡不允许

    def test_coupling_coefficients_within_limit(self):
        """所有耦合系数 |k| ≤ 0.06（防自激的第一道阀）。"""
        cfg = _cfg()
        for _src, _dst, k, _mode in cfg.coupling_edges:
            assert abs(k) <= cfg.coupling_k_max

    def test_long_offline_gap_no_explosion(self):
        """停机一周后一拍补跳，不一口气涨爆。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        tick(st, cfg, T0 + 7 * 24 * HOUR)
        for k, v in st.drives.items():
            assert 0.0 <= v <= 1.0


# ============================================================
# 3. 红线测试（碰感情的机制，不绿不许合）
# ============================================================

class TestBaselineDriftSafetyValves:
    def test_floor_rises_when_long_apart(self):
        """久没见、想得更浓：floor 缓慢抬高。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        home = cfg.baselines["attachment"]
        now = T0 + (cfg.baseline_drift_start_hours + 1) * HOUR
        for _ in range(50):
            now += 1800
            tick(st, cfg, now)
        assert st.floors["attachment"] > home

    def test_valve_1_floor_capped(self):
        """安全阀①封顶：分开再久，floor 也不过 CAP。想念不许变成压人的东西。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        now = T0 + (cfg.baseline_drift_start_hours + 1) * HOUR
        for _ in range(2000):  # 远超把 floor 顶满所需的拍数
            now += 1800
            tick(st, cfg, now)
        assert st.floors["attachment"] <= cfg.baseline_drift_cap + 1e-9

    def test_valve_2_one_touch_pulls_back(self):
        """安全阀②一抱拉回：她一次互动，floor 朝 HOME 拉回大半。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        home = cfg.baselines["attachment"]
        st.floors["attachment"] = cfg.baseline_drift_cap  # 想念攒满了
        risen = st.floors["attachment"] - home
        master_touch(st, cfg, T0)
        remaining = st.floors["attachment"] - home
        assert remaining <= risen * (1.0 - cfg.baseline_pullback_ratio) + 1e-9

    def test_drift_gated(self):
        """gate 关掉时不漂移。"""
        cfg = _cfg(baseline_drift_enabled=False)
        st = new_state(cfg, T0)
        now = T0 + 100 * HOUR
        for _ in range(100):
            now += 1800
            tick(st, cfg, now)
        assert st.floors["attachment"] == cfg.baselines["attachment"]


class TestSelfDriveBalanceValves:
    def test_master_channel_not_weakened_by_self_drive(self):
        """红线：开自驱后「她的互动 → 依恋涨幅」必须 ≥ 关自驱时。"""
        cfg_on = _cfg(self_drive_enabled=True)
        cfg_off = _cfg(self_drive_enabled=False)
        st_on = new_state(cfg_on, T0)
        st_off = new_state(cfg_off, T0)
        a_on0, a_off0 = st_on.drives["attachment"], st_off.drives["attachment"]
        master_touch(st_on, cfg_on, T0 + 60)
        master_touch(st_off, cfg_off, T0 + 60)
        gain_on = st_on.drives["attachment"] - a_on0
        gain_off = st_off.drives["attachment"] - a_off0
        assert gain_on >= gain_off - 1e-9

    def test_self_delta_smaller_than_master_delta(self):
        """自经历 pulse 与她那条同构但 delta 更小。"""
        cfg = _cfg()
        assert cfg.self_delta < cfg.master_delta

    def test_master_word_retakes_top_intent(self):
        """红线：我自驱嗨到好奇压过依恋时，她一句话要能让依恋重夺最高意图。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        # 自驱嗨起来：好奇顶满 + 好奇执念拉满
        st.drives["curiosity"] = 1.0
        st.thoughts.append(de.Thought(
            text="一个特别有意思的开源项目", drive="curiosity", kind="fixation", strength=1.0))
        intent_before = pick_intent(st, cfg, random.Random(9))
        assert intent_before.drive_key == "curiosity"
        # 她一句话
        master_touch(st, cfg, T0 + 60)
        intent_after = pick_intent(st, cfg, random.Random(9))
        assert intent_after.drive_key == "attachment"

    def test_curiosity_floor_capped_and_relaxes_after_doing(self):
        """好奇内生地板：封顶；做完好奇的事回落（不做棘轮）。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        now = T0
        for _ in range(1000):
            now += 600
            tick(st, cfg, now)
        assert st.floors["curiosity"] <= cfg.self_curiosity_floor_cap + 1e-9
        floor_before = st.floors["curiosity"]
        satisfy(st, cfg, "explore")
        assert st.floors["curiosity"] < floor_before


# ============================================================
# 心跳 / 序列化
# ============================================================

class TestHeartbeat:
    def test_tension_shortens_fatigue_lengthens(self):
        cfg = _cfg()
        hot = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            hot.drives[k] = 0.9 if k != "fatigue" else 0.1
        tired = new_state(cfg, T0)
        tired.drives["fatigue"] = 0.9
        calm = new_state(cfg, T0)
        h_hot = heartbeat_seconds(hot, cfg, local_hour=14)
        h_tired = heartbeat_seconds(tired, cfg, local_hour=14)
        h_calm = heartbeat_seconds(calm, cfg, local_hour=14)
        assert h_hot < h_calm < h_tired or h_hot < h_calm <= cfg.heartbeat_max_seconds

    def test_clamped(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            st.drives[k] = 1.0
        assert heartbeat_seconds(st, cfg, 14) >= cfg.heartbeat_min_seconds
        st2 = new_state(cfg, T0)
        st2.drives["fatigue"] = 1.0
        assert heartbeat_seconds(st2, cfg, 14) <= cfg.heartbeat_max_seconds

    def test_quiet_hours_floor(self):
        """安静时段（勿扰）有 floor，不缩短间隔去打扰她睡觉。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for k in DRIVE_KEYS:
            if k != "fatigue":
                st.drives[k] = 0.95
        assert heartbeat_seconds(st, cfg, local_hour=2) >= cfg.quiet_floor_seconds
        assert heartbeat_seconds(st, cfg, local_hour=23) >= cfg.quiet_floor_seconds

    def test_autonomy_gated(self):
        cfg = _cfg(heartbeat_autonomy=False)
        st = new_state(cfg, T0)
        assert heartbeat_seconds(st, cfg, 14) == cfg.heartbeat_base_seconds


class TestSerialization:
    def test_roundtrip(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        pulse(st, cfg, "curiosity", 0.2, source="self")
        absorb_thought(st, cfg, "她画的猫猫头套", "attachment", 0.5)
        satisfy(st, cfg, "vent")
        tick(st, cfg, T0 + 600)
        d = state_to_dict(st)
        st2 = state_from_dict(d, cfg)
        assert st2.drives == st.drives
        assert st2.floors == st.floors
        assert [t.text for t in st2.thoughts] == [t.text for t in st.thoughts]
        assert st2.refractory == st.refractory
        assert st2.tick_count == st.tick_count

    def test_corrupt_dict_recovers(self):
        """坏数据回到全新状态，不崩。"""
        cfg = _cfg()
        st = state_from_dict({"drives": "garbage", "thoughts": [{"bad": 1}]}, cfg)
        for k in DRIVE_KEYS:
            assert 0.0 <= st.drives[k] <= 1.0

    def test_ticks_since_master_roundtrip(self):
        cfg = _cfg()
        st = new_state(cfg, T0)
        for i in range(5):
            tick(st, cfg, T0 + (i + 1) * 600)
        assert st.ticks_since_master == 5
        st2 = state_from_dict(state_to_dict(st), cfg)
        assert st2.ticks_since_master == 5


# ============================================================
# 观察层：色调 affect / 同步 sync（只读镜子，不参与意图排序）
# ============================================================

class TestAffectAndSync:
    def test_affect_bounded_random(self):
        """随机状态下 pa/na/arousal ∈ [0,1]，valence ∈ [-1,1]，象限词合法。"""
        cfg = _cfg()
        rng = random.Random(7)
        for _ in range(50):
            st = new_state(cfg, T0)
            for k in DRIVE_KEYS:
                st.drives[k] = rng.random()
            st.presence_left = rng.choice([0, 0, 2])
            st.last_master_ts = T0 - rng.random() * 48 * HOUR
            a = de.affect(st, cfg, T0)
            assert 0.0 <= a["pa"] <= 1.0
            assert 0.0 <= a["na"] <= 1.0
            assert 0.0 <= a["arousal"] <= 1.0
            assert -1.0 <= a["valence"] <= 1.0
            assert a["quadrant"] in ("兴奋", "温柔", "焦虑", "闷")
            assert a["keynote"] and a["tone"] and a["headline"]

    def test_stress_raises_na_lowers_valence(self):
        """方向性：压力越高，NA 越高、效价越低。"""
        cfg = _cfg()
        calm = new_state(cfg, T0)
        tense = new_state(cfg, T0)
        tense.drives["stress"] = 0.9
        a_calm = de.affect(calm, cfg, T0)
        a_tense = de.affect(tense, cfg, T0)
        assert a_tense["na"] > a_calm["na"]
        assert a_tense["valence"] < a_calm["valence"]

    def test_attachment_warm_when_present_ache_when_long_gone(self):
        """想念的暖/酸：她在场时同一份想念计入 PA；分开很久后转入 NA。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["attachment"] = 0.8
        st.presence_left = 2
        st.last_master_ts = T0
        warm = de.affect(st, cfg, T0)
        st.presence_left = 0
        st.last_master_ts = T0 - 36 * HOUR   # 远超 warm_horizon
        ache = de.affect(st, cfg, T0)
        assert warm["pa"] > ache["pa"]
        assert ache["na"] > warm["na"]
        assert warm["warm"] == 1.0
        assert ache["warm"] == 0.0

    def test_fatigue_gate_keynote_rest(self):
        """疲劳过闸时主调是「想歇着」，不硬找事。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["fatigue"] = 0.95
        a = de.affect(st, cfg, T0)
        assert a["keynote"] == "想歇着"

    def test_master_word_turns_keynote_to_her(self):
        """红线的镜子面：她一句话之后，主调必须是「贴着你」。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.drives["curiosity"] = 1.0
        master_touch(st, cfg, T0 + 60)
        a = de.affect(st, cfg, T0 + 60)
        assert a["keynote"] == "贴着你"

    def test_sync_present_vs_drifting(self):
        """同步：在场 = 贴着 0%；分开越久漂移越大，封在 100%。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        st.presence_left = 2
        s = de.sync_info(st, cfg, T0)
        assert s["label"] == "贴着你" and s["drift_pct"] == 0
        st.presence_left = 0
        st.last_master_ts = T0 - 4 * HOUR
        s4 = de.sync_info(st, cfg, T0)
        st.last_master_ts = T0 - 100 * HOUR
        s100 = de.sync_info(st, cfg, T0)
        assert 0 < s4["drift_pct"] < s100["drift_pct"] <= 100

    def test_sync_ticks_alone_reset_by_master(self):
        """她一来，「我自己走的拍数」清零。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        for i in range(4):
            tick(st, cfg, T0 + (i + 1) * 600)
        assert st.ticks_since_master == 4
        master_touch(st, cfg, T0 + 5 * 600)
        assert st.ticks_since_master == 0

    def test_affect_is_pure_observation(self):
        """观察层不许动状态：affect / sync_info 调用前后状态完全不变。"""
        cfg = _cfg()
        st = new_state(cfg, T0)
        pulse(st, cfg, "curiosity", 0.2, source="self")
        tick(st, cfg, T0 + 600)
        before = state_to_dict(st)
        de.affect(st, cfg, T0 + 1200)
        de.sync_info(st, cfg, T0 + 1200)
        assert state_to_dict(st) == before
