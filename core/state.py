import datetime
import json
import re
import zoneinfo
from collections.abc import Callable
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
    cycle_start: str  # 带时区的 ISO 时间戳
    cycle_end: str  # 带时区的 ISO 时间戳
    timezone: str  # IANA 时区名称
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"  # "ok" | "failed"
    generated_at: str = ""  # ISO 时间戳

    @classmethod
    def from_cycle(cls, cycle: "BusinessCycle", **kwargs) -> "LifeState":
        """使用生成时的业务周期创建状态。"""
        timezone = getattr(cycle.start.tzinfo, "key", None)
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("业务周期必须使用 IANA 时区")
        return cls(
            business_date=cycle.business_date.isoformat(),
            cycle_start=cycle.start.isoformat(),
            cycle_end=cycle.end.isoformat(),
            timezone=timezone,
            **kwargs,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "LifeState":
        business_date = d.get("business_date")
        if not isinstance(business_date, str):
            raise ValueError("business_date 字段缺失或非字符串")
        datetime.date.fromisoformat(business_date)

        cycle_start = d.get("cycle_start")
        cycle_end = d.get("cycle_end")
        timezone = d.get("timezone")
        if not all(isinstance(value, str) for value in (cycle_start, cycle_end)):
            raise ValueError("cycle_start/cycle_end 字段缺失或非字符串")
        if not isinstance(timezone, str):
            raise ValueError("timezone 字段缺失或非字符串")

        timeline_raw = d.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        return cls(
            business_date=business_date,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            timezone=timezone,
            schedule_summary=str(d.get("schedule_summary", "")),
            style_summary=str(d.get("style_summary", "")),
            timeline=[TimelineEntry.from_dict(e) for e in timeline_raw],
            status=str(d.get("status", "ok")),
            generated_at=str(d.get("generated_at", "")),
        )

    def to_cycle(self) -> "BusinessCycle":
        """从持久化字段恢复生成时冻结的业务周期。"""
        timezone = zoneinfo.ZoneInfo(self.timezone)
        start = datetime.datetime.fromisoformat(self.cycle_start)
        end = datetime.datetime.fromisoformat(self.cycle_end)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("cycle_start/cycle_end 必须包含时区")
        start = start.astimezone(timezone)
        end = end.astimezone(timezone)
        business_date = datetime.date.fromisoformat(self.business_date)
        if start.date() != business_date:
            raise ValueError("cycle_start 与 business_date 不一致")
        if start >= end:
            raise ValueError("cycle_start 必须早于 cycle_end")
        return BusinessCycle(business_date=business_date, start=start, end=end)


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
_HISTORY_FILE_RE = re.compile(r"life_state_(\d{4}\.\d{2}\.\d{2})\.json")


class DataManager:
    def __init__(
        self,
        json_path: Path,
        legacy_cycle_resolver: Callable[[str], BusinessCycle] | None = None,
    ):
        self._path = json_path
        self._history_dir = json_path.parent / "history"
        self._legacy_cycle_resolver = legacy_cycle_resolver
        self._data: dict[str, LifeState] = {}
        self.load()

    @staticmethod
    def _cycle_key(cycle: BusinessCycle) -> str:
        return cycle.start.isoformat()

    def get_by_cycle(self, cycle: BusinessCycle) -> LifeState | None:
        return self._data.get(self._cycle_key(cycle))

    def find_active(self, now: datetime.datetime) -> LifeState | None:
        """返回覆盖 now 的最新有效状态。"""
        candidates: list[tuple[datetime.datetime, LifeState]] = []
        for state in self._data.values():
            if state.status != "ok":
                continue
            try:
                cycle = state.to_cycle()
            except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                continue
            if cycle.contains(now):
                candidates.append((cycle.start, state))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def get_by_business_date(self, date_str: str) -> list[LifeState]:
        """按周期起点顺序返回指定业务日期的所有状态。"""
        candidates: list[tuple[datetime.datetime, LifeState]] = []
        for state in self._data.values():
            if state.business_date != date_str:
                continue
            try:
                cycle = state.to_cycle()
            except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                continue
            candidates.append((cycle.start, state))

        if not candidates:
            try:
                history_path = self._history_path(date_str)
            except ValueError:
                return []
            history_data, _ = self._load_path(history_path)
            for state in history_data.values():
                if state.business_date != date_str:
                    continue
                try:
                    cycle = state.to_cycle()
                except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                    continue
                candidates.append((cycle.start, state))

        candidates.sort(key=lambda item: item[0])
        return [state for _, state in candidates]

    def get_latest_for_business_date(self, date_str: str) -> LifeState | None:
        states = self.get_by_business_date(date_str)
        return states[-1] if states else None

    def archive_before_generation(self, business_date: str) -> None:
        """归档目标业务日期之外的当前状态。

        Args:
            business_date: 即将生成状态的业务日期，格式为 YYYY-MM-DD。

        Raises:
            ValueError: 业务日期格式无效时抛出。
            OSError: 历史状态或当前状态写入失败时抛出。
        """
        datetime.date.fromisoformat(business_date)
        archived_keys: list[str] = []

        for cycle_key, state in self._data.items():
            if state.business_date == business_date:
                continue

            history_path = self._history_path(state.business_date)
            history_path.parent.mkdir(parents=True, exist_ok=True)
            state_dict = asdict(state)
            state_dict["timeline"] = [asdict(entry) for entry in state.timeline]
            payload = {cycle_key: state_dict}
            tmp_path = history_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(history_path)
            archived_keys.append(cycle_key)

        if archived_keys:
            for cycle_key in archived_keys:
                del self._data[cycle_key]
            self.save()

        self._prune_history()

    def get_recent_history(
        self,
        before_business_date: str,
        limit: int,
    ) -> list[LifeState]:
        """返回指定业务日期之前最近的成功历史状态。

        Args:
            before_business_date: 历史状态必须早于该业务日期。
            limit: 最多返回的历史业务周期数。

        Returns:
            按业务日期从新到旧排列的成功历史状态。

        Raises:
            ValueError: 业务日期格式无效时抛出。
        """
        before_date = datetime.date.fromisoformat(before_business_date)
        if limit <= 0 or not self._history_dir.exists():
            return []

        history_files: list[tuple[datetime.date, Path]] = []
        for path in self._history_dir.iterdir():
            match = _HISTORY_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                file_date = datetime.datetime.strptime(
                    match.group(1), "%Y.%m.%d"
                ).date()
            except ValueError:
                continue
            if file_date < before_date:
                history_files.append((file_date, path))

        states: list[LifeState] = []
        for file_date, path in sorted(history_files, reverse=True):
            history_data, _ = self._load_path(path)
            candidates: list[tuple[datetime.datetime, LifeState]] = []
            expected_date = file_date.isoformat()
            for state in history_data.values():
                if state.business_date != expected_date or state.status != "ok":
                    continue
                try:
                    cycle = state.to_cycle()
                except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                    continue
                candidates.append((cycle.start, state))
            if candidates:
                states.append(max(candidates, key=lambda item: item[0])[1])
            if len(states) >= limit:
                break

        return states

    def set(self, state: LifeState) -> None:
        cycle = state.to_cycle()
        cycle_key = self._cycle_key(cycle)
        self._data = {cycle_key: state}
        self.save()

        history_path = self._history_path(state.business_date)
        if history_path.exists():
            history_path.unlink()

    @staticmethod
    def _history_filename(business_date: datetime.date) -> str:
        return f"life_state_{business_date.strftime('%Y.%m.%d')}.json"

    def _history_path(self, business_date: str) -> Path:
        parsed = datetime.date.fromisoformat(business_date)
        return self._history_dir / self._history_filename(parsed)

    def _prune_history(self) -> None:
        if not self._history_dir.exists():
            return

        history_files: list[tuple[datetime.date, Path]] = []
        for path in self._history_dir.iterdir():
            match = _HISTORY_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                file_date = datetime.datetime.strptime(
                    match.group(1), "%Y.%m.%d"
                ).date()
            except ValueError:
                continue
            history_files.append((file_date, path))

        for _, path in sorted(history_files, reverse=True)[MAX_HISTORY_DAYS:]:
            path.unlink()

    def _load_path(
        self,
        path: Path,
        *,
        allow_legacy: bool = False,
    ) -> tuple[dict[str, LifeState], bool]:
        """读取并规范化单个状态文件。

        Args:
            path: 要读取的状态文件。
            allow_legacy: 是否允许补齐旧版缺少周期字段的数据。

        Returns:
            规范化后的状态映射，以及是否需要重写源文件。
        """
        if not path.exists():
            return {}, False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, False
        if not isinstance(raw, dict):
            return {}, False

        data: dict[str, LifeState] = {}
        migrated = False
        for cycle_key, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                was_migrated = False
                if "cycle_start" in item:
                    state = LifeState.from_dict(item)
                else:
                    business_date = item.get("business_date")
                    if (
                        not allow_legacy
                        or not isinstance(business_date, str)
                        or self._legacy_cycle_resolver is None
                    ):
                        continue
                    cycle = self._legacy_cycle_resolver(business_date)
                    timeline_raw = item.get("timeline", [])
                    if not isinstance(timeline_raw, list):
                        timeline_raw = []
                    state = LifeState.from_cycle(
                        cycle,
                        schedule_summary=str(item.get("schedule_summary", "")),
                        style_summary=str(item.get("style_summary", "")),
                        timeline=[
                            TimelineEntry.from_dict(entry)
                            for entry in timeline_raw
                            if isinstance(entry, dict)
                        ],
                        status=str(item.get("status", "ok")),
                        generated_at=str(item.get("generated_at", "")),
                    )
                    migrated = True
                    was_migrated = True
                cycle = state.to_cycle()
                resolved_key = self._cycle_key(cycle)
                if not was_migrated and resolved_key != cycle_key:
                    continue
                data[resolved_key] = state
            except Exception:
                continue

        deduplicated: dict[str, LifeState] = {}
        for cycle_key, state in sorted(
            data.items(),
            key=lambda item: item[1].to_cycle().start,
        ):
            previous = next(
                (
                    key
                    for key, existing in deduplicated.items()
                    if existing.business_date == state.business_date
                ),
                None,
            )
            if previous is not None:
                del deduplicated[previous]
            deduplicated[cycle_key] = state

        rewritten = migrated or len(deduplicated) != len(data)
        return deduplicated, rewritten

    def load(self) -> None:
        self._data, rewritten = self._load_path(self._path, allow_legacy=True)
        if rewritten:
            self.save()

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        payload = {}
        for cycle_key, state in self._data.items():
            d = asdict(state)
            d["timeline"] = [asdict(e) for e in state.timeline]
            payload[cycle_key] = d
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


def _is_specific_time_range(time_str: str) -> bool:
    return _TIME_RANGE_RE.fullmatch(time_str.strip()) is not None


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


def resolve_physical_intervals(
    time_str: str,
    cycle: BusinessCycle,
) -> tuple[TimeInterval, ...]:
    """返回时段与业务周期相交的全部物理片段。"""
    parsed = _parse_time_slot(time_str)
    if parsed is None:
        return ()
    return _resolve_range_intervals(parsed[0], parsed[1], cycle)


def resolve_entry_intervals(
    entry: TimelineEntry,
    cycle: BusinessCycle,
) -> tuple[TimeInterval, ...]:
    """返回条目生效区间；自然时段仅保留首个物理片段。"""
    intervals = resolve_physical_intervals(entry.time, cycle)
    if _is_specific_time_range(entry.time):
        return intervals
    return intervals[:1]


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
    2. 自然时段（如 "下午"）的首个有效片段覆盖当前时间 → 直接使用。
    3. 当前自然时段缺失或处于周期尾部残片 → 构造临时 TimelineEntry：
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
        if _is_specific_time_range(entry.time):
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
        physical_intervals = resolve_physical_intervals(current_slot_name, cycle)
        active_interval = next(
            (interval for interval in physical_intervals if interval.contains(now)),
            None,
        )
        intervals = (active_interval,) if active_interval else ()
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
