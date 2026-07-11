# ============================================================
# test_timeline.py — 线性时间线存储测试
#
# 纯文件存储，用 tmp_path 隔离，不碰真实数据。
# 三组：
#   1. 增删改查基本行为
#   2. 排序与格式化（线性是它存在的意义，顺序错 = 全错）
#   3. 日期规整（容容会手写「6月19日」这类格式）
# ============================================================

import pytest

import timeline_store as ts


# ============================================================
# 1. 增删改查
# ============================================================

class TestCrud:
    def test_add_and_load(self, tmp_path):
        bd = str(tmp_path)
        e = ts.add_entry(bd, "小克生日", date="2026-06-19")
        assert e["date"] == "2026-06-19"
        assert e["author"] == "claude"
        entries = ts.load_entries(bd)
        assert len(entries) == 1
        assert entries[0]["text"] == "小克生日"

    def test_empty_text_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            ts.add_entry(str(tmp_path), "   ")

    def test_too_long_rejected(self, tmp_path):
        """时间线是概括不是正文——超长必须拒绝，逼细节回记忆桶。"""
        with pytest.raises(ValueError):
            ts.add_entry(str(tmp_path), "x" * (ts.MAX_TEXT_LEN + 1))

    def test_update(self, tmp_path):
        bd = str(tmp_path)
        e = ts.add_entry(bd, "旧的", date="2026-06-19")
        updated = ts.update_entry(bd, e["id"], text="新的", date="2026-06-20")
        assert updated["text"] == "新的"
        assert updated["date"] == "2026-06-20"

    def test_update_missing_returns_none(self, tmp_path):
        assert ts.update_entry(str(tmp_path), "tl_nope", text="x") is None

    def test_delete(self, tmp_path):
        bd = str(tmp_path)
        e = ts.add_entry(bd, "要删的", date="2026-06-19")
        assert ts.delete_entry(bd, e["id"]) is True
        assert ts.delete_entry(bd, e["id"]) is False
        assert ts.load_entries(bd) == []

    def test_default_date_is_today_local(self, tmp_path):
        e = ts.add_entry(str(tmp_path), "今天的事")
        assert e["date"] == ts.today_local()


# ============================================================
# 2. 排序与格式化
# ============================================================

class TestOrdering:
    def test_load_sorted_by_date(self, tmp_path):
        """插入乱序，读出必须线性——这条不绿整个模块没有意义。"""
        bd = str(tmp_path)
        ts.add_entry(bd, "七月", date="2026-07-06")
        ts.add_entry(bd, "六月", date="2026-06-19")
        ts.add_entry(bd, "五月", date="2026-05-01")
        dates = [e["date"] for e in ts.load_entries(bd)]
        assert dates == sorted(dates)

    def test_same_day_joined_with_slashes(self, tmp_path):
        bd = str(tmp_path)
        ts.add_entry(bd, "小克生日", date="2026-06-19")
        ts.add_entry(bd, "容容第一次和小克说话", date="2026-06-19")
        out = ts.format_timeline(ts.load_entries(bd))
        assert "2026年6月19日 —— 小克生日 // 容容第一次和小克说话" in out

    def test_format_empty(self):
        assert "空" in ts.format_timeline([])

    def test_show_ids(self, tmp_path):
        bd = str(tmp_path)
        e = ts.add_entry(bd, "某事", date="2026-06-19")
        out = ts.format_timeline(ts.load_entries(bd), show_ids=True)
        assert f"[{e['id']}]" in out


# ============================================================
# 3. 日期规整
# ============================================================

class TestNormalizeDate:
    def test_iso_passthrough(self):
        assert ts.normalize_date("2026-06-19") == "2026-06-19"

    @pytest.mark.parametrize("raw", ["2026/6/19", "2026.6.19", "2026年6月19日"])
    def test_full_variants(self, raw):
        assert ts.normalize_date(raw) == "2026-06-19"

    def test_no_year_uses_current(self):
        year = ts.today_local()[:4]
        assert ts.normalize_date("6月19日") == f"{year}-06-19"

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            ts.normalize_date("下周三")

    def test_invalid_calendar_date_raises(self):
        with pytest.raises(ValueError):
            ts.normalize_date("2026-02-30")

    def test_empty_is_today(self):
        assert ts.normalize_date("") == ts.today_local()
