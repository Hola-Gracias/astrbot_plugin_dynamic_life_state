import datetime

import pytest

from core.state import TimelineEntry, select_current_slot


def _dt(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 1, hour, minute)


# =========================
# 自然时段边界（通过 select_current_slot 公开行为验证）
# =========================

_ALL_SLOTS_TIMELINE = [
    TimelineEntry(time="凌晨", schedule="sleep", outfit="A"),
    TimelineEntry(time="早上", schedule="wake", outfit="B"),
    TimelineEntry(time="上午", schedule="work", outfit="C"),
    TimelineEntry(time="中午", schedule="lunch", outfit="D"),
    TimelineEntry(time="下午", schedule="meeting", outfit="E"),
    TimelineEntry(time="傍晚", schedule="walk", outfit="F"),
    TimelineEntry(time="晚上", schedule="rest", outfit="G"),
    TimelineEntry(time="深夜", schedule="sleep2", outfit="H"),
]


@pytest.mark.parametrize(
    "hour,minute,expected_slot",
    [
        (0, 0, "凌晨"),
        (5, 59, "凌晨"),
        (6, 0, "早上"),
        (8, 59, "早上"),
        (9, 0, "上午"),
        (11, 59, "上午"),
        (12, 0, "中午"),
        (13, 59, "中午"),
        (14, 0, "下午"),
        (17, 59, "下午"),
        (18, 0, "傍晚"),
        (19, 59, "傍晚"),
        (20, 0, "晚上"),
        (22, 59, "晚上"),
        (23, 0, "深夜"),
        (23, 59, "深夜"),
    ],
)
def test_natural_slot_boundaries_via_select(hour, minute, expected_slot):
    """通过 select_current_slot 验证自然时段边界（左闭右开）。"""
    result = select_current_slot(_ALL_SLOTS_TIMELINE, now=_dt(hour, minute))
    assert result is not None
    assert result.time == expected_slot


# =========================
# select_current_slot — 正常匹配
# =========================


def test_select_exact_natural_slot():
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="下午", schedule="开会", outfit="休闲装"),
        TimelineEntry(time="晚上", schedule="休息", outfit="睡衣"),
    ]
    result = select_current_slot(timeline, now=_dt(15, 0))
    assert result is not None
    assert result.time == "下午"
    assert result.schedule == "开会"
    assert result.outfit == "休闲装"


def test_select_night_slot():
    """22:30 属于晚上 (20-23)，应匹配晚上条目。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="晚上", schedule="休息", outfit="睡衣"),
    ]
    result = select_current_slot(timeline, now=_dt(22, 30))
    assert result is not None
    assert result.time == "晚上"
    assert result.schedule == "休息"


def test_select_deep_night_slot():
    """23:30 属于深夜 (23-24)，若存在深夜条目则匹配。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="深夜", schedule="睡觉", outfit="睡衣"),
    ]
    result = select_current_slot(timeline, now=_dt(23, 30))
    assert result is not None
    assert result.time == "深夜"
    assert result.schedule == "睡觉"


# =========================
# select_current_slot — 缺失回退（有更早条目）
# =========================


def test_missing_slot_with_earlier():
    """深夜缺失，23:30 应构造临时'深夜'，穿搭继承晚上的。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="下午", schedule="开会", outfit="休闲装"),
        TimelineEntry(time="晚上", schedule="休息", outfit="睡衣"),
    ]
    result = select_current_slot(timeline, now=_dt(23, 30))
    assert result is not None
    assert result.time == "深夜"
    assert result.schedule == "空闲"
    assert result.outfit == "睡衣"


def test_missing_noon_inherits_morning():
    """中午缺失，12:30 应构造临时'中午'，穿搭继承上午。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="下午", schedule="开会", outfit="休闲装"),
    ]
    result = select_current_slot(timeline, now=_dt(12, 30))
    assert result is not None
    assert result.time == "中午"
    assert result.schedule == "空闲"
    assert result.outfit == "职业装"


# =========================
# select_current_slot — 缺失回退（无更早条目）
# =========================


def test_missing_slot_no_earlier():
    """凌晨缺失且没有更早条目，5:00 构造临时'凌晨'，穿搭为空。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
        TimelineEntry(time="下午", schedule="开会", outfit="休闲装"),
    ]
    result = select_current_slot(timeline, now=_dt(5, 0))
    assert result is not None
    assert result.time == "凌晨"
    assert result.schedule == "空闲"
    assert result.outfit == ""


# =========================
# select_current_slot — 具体时间区间
# =========================


def test_specific_range_hit():
    """具体区间 12:30-13:30 覆盖 12:45，应命中。"""
    timeline = [
        TimelineEntry(time="12:30-13:30", schedule="午饭", outfit="便装"),
    ]
    result = select_current_slot(timeline, now=_dt(12, 45))
    assert result is not None
    assert result.time == "12:30-13:30"
    assert result.schedule == "午饭"


def test_specific_range_priority_over_natural():
    """具体区间和自然时段同时覆盖时，具体区间优先。"""
    timeline = [
        TimelineEntry(time="中午", schedule="休息", outfit="居家服"),
        TimelineEntry(time="12:30-13:30", schedule="午饭", outfit="便装"),
    ]
    result = select_current_slot(timeline, now=_dt(12, 45))
    assert result is not None
    assert result.time == "12:30-13:30"
    assert result.schedule == "午饭"


def test_specific_range_not_covering():
    """具体区间不覆盖当前时间时不应命中，应回退到自然时段。"""
    timeline = [
        TimelineEntry(time="08:00-09:00", schedule="通勤", outfit="外套"),
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
    ]
    result = select_current_slot(timeline, now=_dt(10, 0))
    assert result is not None
    assert result.time == "上午"
    assert result.schedule == "工作"


# =========================
# select_current_slot — 边界情况
# =========================


def test_empty_timeline():
    assert select_current_slot([], now=_dt(12, 0)) is None


def test_all_unparseable():
    """所有条目的 time 都不可解析时，构造基于当前自然时段的临时条目。"""
    timeline = [
        TimelineEntry(time="???", schedule="未知", outfit="未知"),
        TimelineEntry(time="!!!", schedule="未知", outfit="未知"),
    ]
    result = select_current_slot(timeline, now=_dt(14, 30))
    assert result is not None
    assert result.time == "下午"
    assert result.schedule == "空闲"
    assert result.outfit == ""


def test_synthetic_entry_not_mutate_original():
    """构造的临时条目不应影响原始 timeline。"""
    timeline = [
        TimelineEntry(time="上午", schedule="工作", outfit="职业装"),
    ]
    original_len = len(timeline)
    select_current_slot(timeline, now=_dt(23, 0))
    assert len(timeline) == original_len
    assert timeline[0].schedule == "工作"


def test_noon_boundary_14_00_is_afternoon():
    """14:00 属于下午（左闭右开），不在中午。"""
    timeline = [
        TimelineEntry(time="中午", schedule="午饭", outfit="便装"),
        TimelineEntry(time="下午", schedule="工作", outfit="职业装"),
    ]
    result = select_current_slot(timeline, now=_dt(14, 0))
    assert result is not None
    assert result.time == "下午"


def test_evening_boundary_20_00():
    """20:00 属于晚上。"""
    timeline = [
        TimelineEntry(time="傍晚", schedule="散步", outfit="休闲装"),
        TimelineEntry(time="晚上", schedule="休息", outfit="睡衣"),
    ]
    result = select_current_slot(timeline, now=_dt(20, 0))
    assert result is not None
    assert result.time == "晚上"
