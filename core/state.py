import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_GENERATE_TIME = "07:00"

# =========================
# 数据模型
# =========================


@dataclass(slots=True)
class TimelineEntry:
    time: str
    schedule: str
    outfit: str

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEntry":
        return cls(
            time=str(d.get("time", "")),
            schedule=str(d.get("schedule", "")),
            outfit=str(d.get("outfit", "")),
        )


@dataclass(slots=True)
class LifeState:
    business_date: str  # yyyy-mm-dd
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"  # "ok" | "failed"
    generated_at: str = ""  # ISO 时间戳

    @classmethod
    def from_dict(cls, d: dict) -> "LifeState":
        business_date = d.get("business_date")
        if not isinstance(business_date, str):
            raise ValueError("business_date 字段缺失或非字符串")
        datetime.date.fromisoformat(business_date)

        timeline_raw = d.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        return cls(
            business_date=business_date,
            schedule_summary=str(d.get("schedule_summary", "")),
            style_summary=str(d.get("style_summary", "")),
            timeline=[TimelineEntry.from_dict(e) for e in timeline_raw],
            status=str(d.get("status", "ok")),
            generated_at=str(d.get("generated_at", "")),
        )


@dataclass(frozen=True, slots=True)
class BusinessCycle:
    business_date: datetime.date
    start: datetime.datetime
    end: datetime.datetime

    def contains(self, value: datetime.datetime) -> bool:
        return self.start <= value < self.end


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start: datetime.datetime
    end: datetime.datetime

    def contains(self, value: datetime.datetime) -> bool:
        return self.start <= value < self.end


@dataclass(frozen=True, slots=True)
class SlotMatch:
    entry: TimelineEntry
    intervals: tuple[TimeInterval, ...]
    active_interval: TimeInterval | None = None


def format_datetime(value: datetime.datetime) -> str:
    """格式化带时区的自然日期时间。"""
    return value.strftime("%Y-%m-%d %H:%M %Z")


def format_interval(interval: TimeInterval) -> str:
    """格式化左闭右开的实际时间区间。"""
    return f"[{format_datetime(interval.start)}, {format_datetime(interval.end)})"


def format_cycle(cycle: BusinessCycle) -> str:
    """格式化业务周期。"""
    return f"[{format_datetime(cycle.start)}, {format_datetime(cycle.end)})"


# =========================
# 持久化管理
# =========================

MAX_HISTORY_DAYS = 7


class DataManager:
    def __init__(self, json_path: Path):
        self._path = json_path
        self._data: dict[str, LifeState] = {}
        self.load()

    def has(self, date_str: str) -> bool:
        return date_str in self._data

    def get(self, date_str: str) -> LifeState | None:
        return self._data.get(date_str)

    def set(self, state: LifeState) -> None:
        self._data[state.business_date] = state
        self.save()

    def _prune_old(self):
        """删除超过 MAX_HISTORY_DAYS 天的历史记录。"""
        if len(self._data) <= MAX_HISTORY_DAYS:
            return
        sorted_dates = sorted(self._data.keys(), reverse=True)
        keep = set(sorted_dates[:MAX_HISTORY_DAYS])
        removed = [d for d in self._data if d not in keep]
        for d in removed:
            del self._data[d]

    def load(self) -> None:
        if not self._path.exists():
            self._data.clear()
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._data.clear()
            return
        if not isinstance(raw, dict):
            self._data.clear()
            return
        data: dict[str, LifeState] = {}
        for date_str, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                state = LifeState.from_dict(item)
                if state.business_date != date_str:
                    continue
                data[date_str] = state
            except Exception:
                continue
        self._data = data

    def save(self) -> None:
        self._prune_old()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        payload = {}
        for date_str, state in self._data.items():
            d = asdict(state)
            d["timeline"] = [asdict(e) for e in state.timeline]
            payload[date_str] = d
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)


# =========================
# 时间段选择
# =========================

_NATURAL_SLOTS: dict[str, tuple[int, int]] = {
    "凌晨": (0, 6),
    "早上": (6, 9),
    "上午": (9, 12),
    "中午": (12, 14),
    "下午": (14, 18),
    "傍晚": (18, 20),
    "晚上": (20, 23),
    "深夜": (23, 24),
}

NATURAL_SLOT_NAMES: tuple[str, ...] = tuple(_NATURAL_SLOTS.keys())

_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})")
_CLOCK_TIME_RE = re.compile(r"(\d{2}):(\d{2})")


def _parse_time_slot(time_str: str) -> tuple[int, int] | None:
    """将时间段字符串解析为 (start_minute_of_day, end_minute_of_day)。"""
    time_str = time_str.strip()
    m = _TIME_RANGE_RE.fullmatch(time_str)
    if m:
        h1, m1, h2, m2 = map(int, m.groups())
        if not (0 <= h1 <= 23 and 0 <= m1 <= 59):
            return None
        if not (0 <= h2 <= 24 and 0 <= m2 <= 59):
            return None
        if h2 == 24 and m2 != 0:
            return None
        start_minute = h1 * 60 + m1
        end_minute = h2 * 60 + m2
        if start_minute == end_minute:
            return None
        return (start_minute, end_minute)

    for keyword, (start_h, end_h) in _NATURAL_SLOTS.items():
        if keyword in time_str:
            return (start_h * 60, end_h * 60)

    return None


def is_valid_time_slot(time_str: str) -> bool:
    """返回时间线时段是否可解析。"""
    return _parse_time_slot(time_str) is not None


def parse_generate_time(generate_time: str | None) -> datetime.time:
    """严格解析 HH:MM 格式的业务周期边界。"""
    value = (generate_time or "").strip()
    match = _CLOCK_TIME_RE.fullmatch(value)
    if not match:
        raise ValueError("generate_time 必须使用 HH:MM 格式")
    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("generate_time 必须是有效的 24 小时时间")
    return datetime.time(hour=hour, minute=minute)


def build_business_cycle(
    business_date: datetime.date | str,
    generate_time: str,
    timezone: datetime.tzinfo,
) -> BusinessCycle:
    """根据业务日期和配置边界构造带时区的业务周期。"""
    if isinstance(business_date, str):
        business_date = datetime.date.fromisoformat(business_date)
    boundary_time = parse_generate_time(generate_time)
    start = datetime.datetime.combine(
        business_date,
        boundary_time,
        tzinfo=timezone,
    )
    end = datetime.datetime.combine(
        business_date + datetime.timedelta(days=1),
        boundary_time,
        tzinfo=timezone,
    )
    return BusinessCycle(business_date=business_date, start=start, end=end)


def resolve_business_cycle(
    generate_time: str,
    timezone: datetime.tzinfo,
    now: datetime.datetime | None = None,
) -> BusinessCycle:
    """根据当前自然时间解析其所属业务周期。"""
    now = now or datetime.datetime.now(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)

    boundary = datetime.datetime.combine(
        now.date(),
        parse_generate_time(generate_time),
        tzinfo=timezone,
    )
    business_date = now.date()
    if now < boundary:
        business_date -= datetime.timedelta(days=1)
    return build_business_cycle(business_date, generate_time, timezone)


def resolve_time_in_cycle(cycle: BusinessCycle, time_str: str) -> datetime.datetime:
    """将 HH:MM 映射到业务周期内唯一的自然日期时间。"""
    query_time = parse_generate_time(time_str)
    candidate = datetime.datetime.combine(
        cycle.business_date,
        query_time,
        tzinfo=cycle.start.tzinfo,
    )
    if candidate < cycle.start:
        candidate = datetime.datetime.combine(
            cycle.business_date + datetime.timedelta(days=1),
            query_time,
            tzinfo=cycle.start.tzinfo,
        )
    return candidate


def _datetime_at_minute(
    value_date: datetime.date,
    minute_of_day: int,
    timezone: datetime.tzinfo,
) -> datetime.datetime:
    hour, minute = divmod(minute_of_day, 60)
    return datetime.datetime.combine(
        value_date,
        datetime.time(hour=hour, minute=minute),
        tzinfo=timezone,
    )


def _resolve_range_intervals(
    start_minute: int,
    end_minute: int,
    cycle: BusinessCycle,
) -> tuple[TimeInterval, ...]:
    timezone = cycle.start.tzinfo
    if timezone is None:
        raise ValueError("业务周期必须包含时区")

    intervals: list[TimeInterval] = []
    first_date = cycle.business_date - datetime.timedelta(days=1)
    for offset in range(3):
        value_date = first_date + datetime.timedelta(days=offset)
        start = _datetime_at_minute(value_date, start_minute, timezone)
        if end_minute == 24 * 60:
            end = _datetime_at_minute(
                value_date + datetime.timedelta(days=1),
                0,
                timezone,
            )
        elif end_minute <= start_minute:
            end = _datetime_at_minute(
                value_date + datetime.timedelta(days=1),
                end_minute,
                timezone,
            )
        else:
            end = _datetime_at_minute(value_date, end_minute, timezone)

        clipped_start = max(start, cycle.start)
        clipped_end = min(end, cycle.end)
        if clipped_start < clipped_end:
            intervals.append(TimeInterval(clipped_start, clipped_end))

    return tuple(sorted(set(intervals), key=lambda interval: interval.start))


def resolve_entry_intervals(
    entry: TimelineEntry,
    cycle: BusinessCycle,
) -> tuple[TimeInterval, ...]:
    """将时间线条目解析为其在业务周期内的实际区间。"""
    parsed = _parse_time_slot(entry.time)
    if parsed is None:
        return ()
    return _resolve_range_intervals(parsed[0], parsed[1], cycle)


def _natural_slot_name(now: datetime.datetime) -> str | None:
    current_minute = now.hour * 60 + now.minute
    for name, (start_h, end_h) in _NATURAL_SLOTS.items():
        if start_h * 60 <= current_minute < end_h * 60:
            return name
    return None


def _latest_entry_before(
    timeline: list[TimelineEntry],
    cycle: BusinessCycle,
    before: datetime.datetime,
) -> TimelineEntry | None:
    best: TimelineEntry | None = None
    best_start: datetime.datetime | None = None
    for entry in timeline:
        for interval in resolve_entry_intervals(entry, cycle):
            if interval.start < before and (
                best_start is None or interval.start > best_start
            ):
                best = entry
                best_start = interval.start
    return best


def select_current_slot(
    timeline: list[TimelineEntry],
    cycle: BusinessCycle,
    now: datetime.datetime,
) -> SlotMatch | None:
    """根据当前时间从 timeline 中选择最匹配的时段。

    优先级：
    1. 具体时间区间（如 "12:30-13:30"）覆盖当前时间 → 直接使用。
    2. 自然时段（如 "下午"）覆盖当前时间 → 直接使用。
    3. 当前自然时段缺失 → 构造临时 TimelineEntry：
       - time: 当前自然时段名称
       - schedule: "空闲"
       - outfit: 继承最近一个更早时段的穿搭；若无更早条目则为空字符串。

    不会修改或持久化原始 timeline。

    Args:
        timeline: 时间线条目列表。
        now: 当前时间，用于测试注入。

    Returns:
        匹配的 SlotMatch，或 None（timeline 为空或 now 不在周期内）。
    """
    if not timeline:
        return None

    if now.tzinfo is None:
        now = now.replace(tzinfo=cycle.start.tzinfo)
    else:
        now = now.astimezone(cycle.start.tzinfo)
    if not cycle.contains(now):
        return None

    specific_entries: list[TimelineEntry] = []
    natural_entries: list[TimelineEntry] = []
    for entry in timeline:
        if _parse_time_slot(entry.time) is None:
            continue
        if _TIME_RANGE_RE.fullmatch(entry.time.strip()):
            specific_entries.append(entry)
        else:
            natural_entries.append(entry)

    # 优先：具体时间区间覆盖当前时间
    for entry in specific_entries:
        intervals = resolve_entry_intervals(entry, cycle)
        for interval in intervals:
            if interval.contains(now):
                return SlotMatch(entry, intervals, interval)

    # 其次：自然时段覆盖当前时间
    for entry in natural_entries:
        intervals = resolve_entry_intervals(entry, cycle)
        for interval in intervals:
            if interval.contains(now):
                return SlotMatch(entry, intervals, interval)

    # 当前自然时段缺失 → 构造临时条目
    current_slot_name = _natural_slot_name(now)
    if current_slot_name:
        synthetic = TimelineEntry(current_slot_name, "空闲", "")
        intervals = resolve_entry_intervals(synthetic, cycle)
        active_interval = next(
            (interval for interval in intervals if interval.contains(now)),
            None,
        )
        best = _latest_entry_before(timeline, cycle, now)
        inherited_outfit = best.outfit if best else ""
        return SlotMatch(
            TimelineEntry(
                time=current_slot_name,
                schedule="空闲",
                outfit=inherited_outfit,
            ),
            intervals,
            active_interval,
        )

    # 极端情况：无法确定自然时段（不应发生）
    return None


def find_slot_by_name(
    timeline: list[TimelineEntry],
    name: str,
    cycle: BusinessCycle,
) -> SlotMatch | None:
    """按自然时段名称查找条目。

    缺失时合成临时条目：schedule 为"空闲"，穿搭继承严格早于目标时段的最近条目；
    若没有更早条目则为空字符串。

    Args:
        timeline: 时间线条目列表。
        name: 自然时段名称，如 ``下午`` 或 ``中午``。

    Returns:
        匹配或合成的 SlotMatch；name 不在八个自然时段名称中时返回 None。
    """
    if name not in _NATURAL_SLOTS:
        return None

    for entry in timeline:
        if entry.time == name:
            return SlotMatch(entry, resolve_entry_intervals(entry, cycle))

    synthetic = TimelineEntry(name, "空闲", "")
    intervals = resolve_entry_intervals(synthetic, cycle)
    if not intervals:
        return None
    best = _latest_entry_before(timeline, cycle, intervals[0].start)
    synthetic.outfit = best.outfit if best else ""
    return SlotMatch(synthetic, intervals)
