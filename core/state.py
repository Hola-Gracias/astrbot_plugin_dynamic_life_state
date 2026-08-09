import datetime
import json
import re
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

# =========================
# Data models
# =========================

CHINA_TIMEZONE = zoneinfo.ZoneInfo("Asia/Shanghai")

# 可选时间线字段注册表。映射顺序即渲染顺序。此注册表是以下各项的唯一
# 来源：可选字段白名单、中文标签、渲染图标、渲染顺序和提示词描述。
# ``time``、``schedule`` 和 ``outfit`` 为必填字段，不在此列出。
# 扩展可选字段只需编辑此字典即可。
OPTIONAL_TIMELINE_FIELDS: dict[str, dict[str, str]] = {
    "mood": {
        "label": "心情",
        "icon": "🙂",
    },
    "location": {
        "label": "地点",
        "icon": "📍",
    },
    "note": {
        "label": "备注",
        "icon": "🗒️",
    },
}


@dataclass(slots=True)
class TimelineEntry:
    time: str
    schedule: str
    outfit: str
    extra_fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """在构造边界规范化可选字段。

        仅保留已注册且值非空字符串的字段；值会被去除首尾空白并按注册表
        排序。这确保每种构造路径（包括直接调用）都产出干净的数据模型，
        渲染和序列化时不会泄漏空值、非字符串值或未注册的值。
        """
        normalized: dict[str, str] = {}
        for key in OPTIONAL_TIMELINE_FIELDS:
            value = self.extra_fields.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
        self.extra_fields = normalized

    @classmethod
    def from_dict(cls, data: dict) -> "TimelineEntry | None":
        """从持久化数据或模型 JSON 数据创建时间线条目。

        必填字段必须是非空字符串，且 ``time`` 必须能解析为支持的时段；
        无效条目返回 ``None``。可选字段仅当其键在
        ``OPTIONAL_TIMELINE_FIELDS`` 中注册时才被接受；空值、非字符串
        和未注册的值由 ``__post_init__`` 丢弃。

        Args:
            data: 原始时间线条目映射。

        Returns:
            规范化后的时间线条目；必填字段缺失、为空、非字符串或 ``time``
            不是有效时段时返回 ``None``。
        """
        required_fields: dict[str, str] = {}
        for key in ("time", "schedule", "outfit"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                return None
            required_fields[key] = value.strip()
        if not is_valid_time_slot(required_fields["time"]):
            return None
        return cls(
            time=required_fields["time"],
            schedule=required_fields["schedule"],
            outfit=required_fields["outfit"],
            extra_fields=data,
        )

    def to_dict(self) -> dict:
        """序列化为扁平的 JSON 兼容映射。

        可选字段按注册表顺序作为顶层键写入，因此持久化数据中不会出现
        嵌套的 ``extra_fields`` 对象。

        Returns:
            扁平的时间线条目映射。
        """
        data = {
            "time": self.time,
            "schedule": self.schedule,
            "outfit": self.outfit,
        }
        for key in OPTIONAL_TIMELINE_FIELDS:
            if key in self.extra_fields:
                data[key] = self.extra_fields[key]
        return data


@dataclass(slots=True)
class LifeState:
    date: str  # YYYY-MM-DD
    holiday: str = "无"
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"  # "ok" | "failed"
    generated_at: str = ""  # ISO timestamp

    @classmethod
    def from_dict(cls, data: dict) -> "LifeState":
        """从持久化数据创建并校验生活状态。

        Args:
            data: 原始状态映射。

        Returns:
            校验后的生活状态。

        Raises:
            ValueError: date 字段缺失或无效，或状态声明 ``status == "ok"``
                但 timeline 中没有有效条目。
        """
        date = data.get("date")
        if not isinstance(date, str):
            raise ValueError("date field is missing or is not a string")
        datetime.date.fromisoformat(date)

        timeline_raw = data.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        timeline = [
            entry
            for entry in (
                TimelineEntry.from_dict(item)
                for item in timeline_raw
                if isinstance(item, dict)
            )
            if entry is not None
        ]
        status = str(data.get("status", "ok"))
        if status == "ok" and not timeline:
            raise ValueError("status is ok but the timeline has no valid entries")
        return cls(
            date=date,
            holiday=(
                data["holiday"].strip()
                if isinstance(data.get("holiday"), str) and data["holiday"].strip()
                else "无"
            ),
            schedule_summary=str(data.get("schedule_summary", "")),
            style_summary=str(data.get("style_summary", "")),
            timeline=timeline,
            status=status,
            generated_at=str(data.get("generated_at", "")),
        )

    def to_dict(self) -> dict:
        """序列化为扁平的 JSON 兼容映射。

        Returns:
            扁平的状态映射，可用于持久化或调试日志。
        """
        return {
            "date": self.date,
            "holiday": self.holiday,
            "schedule_summary": self.schedule_summary,
            "style_summary": self.style_summary,
            "timeline": [entry.to_dict() for entry in self.timeline],
            "status": self.status,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start: int
    end: int

    def contains(self, minute: int) -> bool:
        """返回一天中的某个分钟数是否属于此区间。

        Args:
            minute: 一天中的分钟数，范围 0 到 1439。

        Returns:
            区间是否包含该分钟数。
        """
        return self.start <= minute < self.end


@dataclass(frozen=True, slots=True)
class SlotMatch:
    entry: TimelineEntry
    intervals: tuple[TimeInterval, ...]
    active_interval: TimeInterval | None = None


def format_datetime(value: datetime.datetime) -> str:
    """格式化当前日期和时间。

    Args:
        value: 要格式化的日期时间。

    Returns:
        人类可读的日期时间。
    """
    return value.strftime("%Y-%m-%d %H:%M")


def format_interval(interval: TimeInterval) -> str:
    """格式化一天内的左闭右开区间。

    Args:
        interval: 基于分钟数的区间。

    Returns:
        格式化为 ``[HH:MM, HH:MM)`` 的区间。
    """
    start_hour, start_minute = divmod(interval.start, 60)
    end_hour, end_minute = divmod(interval.end, 60)
    return f"[{start_hour:02d}:{start_minute:02d}, {end_hour:02d}:{end_minute:02d})"


# =========================
# Persistence
# =========================

MAX_HISTORY_DAYS = 7
_HISTORY_FILE_RE = re.compile(r"life_state_(\d{4}\.\d{2}\.\d{2})\.json")


class DataManager:
    def __init__(self, json_path: Path):
        self._path = json_path
        self._history_dir = json_path.parent / "history"
        self._data: LifeState | None = None
        self._backup_legacy_storage()
        self.load()

    def get_current(self, date: str) -> LifeState | None:
        """返回匹配当前中国标准时间日期的状态。

        Args:
            date: 目标日期，格式 YYYY-MM-DD。

        Returns:
            匹配的当前状态（包括失败状态），或 ``None``。
        """
        datetime.date.fromisoformat(date)
        if self._data is None or self._data.date != date:
            return None
        return self._data

    def get_by_date(self, date: str) -> LifeState | None:
        """返回指定自然日期的当前或归档状态。

        Args:
            date: 目标日期，格式 YYYY-MM-DD。

        Returns:
            匹配的状态或 ``None``。
        """
        datetime.date.fromisoformat(date)
        if self._data is not None and self._data.date == date:
            return self._data
        return self._load_path(self._history_path(date))

    def archive_before_generation(self, target_date: str) -> None:
        """归档属于不同日期的当前状态。

        同一天的状态在替换安全持久化完成之前保持不变。

        Args:
            target_date: 即将生成的日期，格式 YYYY-MM-DD。

        Raises:
            ValueError: 目标日期无效。
            OSError: 归档或当前状态更新失败。
        """
        datetime.date.fromisoformat(target_date)
        if self._data is None or self._data.date == target_date:
            self._prune_history()
            return

        history_path = self._history_path(self._data.date)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = history_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self._data.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(history_path)

        previous = self._data
        self._data = None
        try:
            self.save()
        except Exception:
            self._data = previous
            raise
        self._prune_history()

    def get_recent_history(self, before_date: str, limit: int) -> list[LifeState]:
        """返回指定自然日期之前的最近成功状态。

        Args:
            before_date: 不包含的上界日期，格式 YYYY-MM-DD。
            limit: 最多返回的成功状态数量。

        Returns:
            状态列表，从最新到最旧排列。

        Raises:
            ValueError: 日期无效。
        """
        parsed_before = datetime.date.fromisoformat(before_date)
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
            if file_date < parsed_before:
                history_files.append((file_date, path))

        states: list[LifeState] = []
        for file_date, path in sorted(history_files, reverse=True):
            state = self._load_path(path)
            if (
                state is None
                or state.date != file_date.isoformat()
                or state.status != "ok"
            ):
                continue
            states.append(state)
            if len(states) >= limit:
                break
        return states

    def set(self, state: LifeState) -> None:
        """将状态持久化为唯一的当前自然日期状态。

        Args:
            state: 要持久化的状态。

        Raises:
            ValueError: 状态日期无效。
            OSError: 当前状态无法写入。
        """
        datetime.date.fromisoformat(state.date)

        previous = self._data
        self._data = state
        try:
            self.save()
        except Exception:
            self._data = previous
            raise

        history_path = self._history_path(state.date)
        if history_path.exists():
            try:
                history_path.unlink()
            except OSError as exc:
                logger.warning(
                    f"[DynamicLifeState] Failed to remove same-date history "
                    f"{history_path}: {exc}"
                )

    def load(self) -> None:
        """从磁盘加载当前状态。"""
        self._data = self._load_path(self._path)

    def save(self) -> None:
        """原子性地持久化当前状态。"""
        if self._data is None:
            if self._path.exists():
                self._path.unlink()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._data.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    @staticmethod
    def _history_filename(date: datetime.date) -> str:
        return f"life_state_{date.strftime('%Y.%m.%d')}.json"

    def _history_path(self, date: str) -> Path:
        parsed = datetime.date.fromisoformat(date)
        return self._history_dir / self._history_filename(parsed)

    @staticmethod
    def _load_path(path: Path) -> LifeState | None:
        """加载一个新格式的状态文件。

        Args:
            path: 要读取的状态文件。

        Returns:
            解析后的状态；文件缺失或无效时返回 ``None``。
        """
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            return LifeState.from_dict(raw)
        except Exception:
            return None

    def _backup_legacy_storage(self) -> None:
        """将旧版或无法识别的活跃存储移入带时间戳的备份目录。

        Raises:
            OSError: 移动旧存储失败且无法回滚。
        """
        needs_backup = self._path.exists() and self._load_path(self._path) is None
        if self._history_dir.exists():
            for path in self._history_dir.iterdir():
                if (
                    _HISTORY_FILE_RE.fullmatch(path.name)
                    and self._load_path(path) is None
                ):
                    needs_backup = True
                    break
        if not needs_backup:
            return

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        backup_dir = self._path.parent / f"legacy_backup_{timestamp}"
        backup_dir.mkdir(parents=True)
        moved: list[tuple[Path, Path]] = []
        try:
            for source in (self._path, self._history_dir):
                if not source.exists():
                    continue
                destination = backup_dir / source.name
                source.replace(destination)
                moved.append((source, destination))
        except Exception as exc:
            rollback_error: Exception | None = None
            for source, destination in reversed(moved):
                try:
                    destination.replace(source)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
            if rollback_error is not None:
                # 保留恢复文件，因为它们可能是唯一幸存的副本。
                logger.error(
                    f"[DynamicLifeState] Legacy storage backup failed and "
                    f"rollback was incomplete; recovery data remains at "
                    f"{backup_dir}; backup error: {exc}; "
                    f"rollback error: {rollback_error}"
                )
                raise OSError(
                    f"Legacy storage backup failed and rollback was incomplete; "
                    f"recovery data remains at {backup_dir}"
                ) from rollback_error
            try:
                backup_dir.rmdir()
            except OSError as cleanup_exc:
                logger.warning(
                    f"[DynamicLifeState] Legacy storage rollback restored the "
                    f"original data, but failed to remove backup directory "
                    f"{backup_dir}: {cleanup_exc}"
                )
            raise OSError(
                "Legacy storage backup failed; original data restored"
            ) from exc

        logger.warning(f"[DynamicLifeState] Legacy state storage moved to {backup_dir}")

    def _prune_history(self) -> None:
        """移除无效文件，仅保留最新的七个日期状态。"""
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
            state = self._load_path(path)
            if state is None or state.date != file_date.isoformat():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        f"[DynamicLifeState] Failed to remove invalid history "
                        f"file {path}: {exc}"
                    )
                continue
            history_files.append((file_date, path))

        for _, path in sorted(history_files, reverse=True)[MAX_HISTORY_DAYS:]:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(
                    f"[DynamicLifeState] Failed to remove expired history file "
                    f"{path}: {exc}"
                )


# =========================
# Time-slot matching
# =========================

_NATURAL_SLOTS: dict[str, tuple[int, int]] = {
    "凌晨": (0, 6 * 60),
    "早上": (6 * 60, 9 * 60),
    "上午": (9 * 60, 12 * 60),
    "中午": (12 * 60, 14 * 60),
    "下午": (14 * 60, 18 * 60),
    "傍晚": (18 * 60, 20 * 60),
    "晚上": (20 * 60, 23 * 60),
    "深夜": (23 * 60, 24 * 60),
}

NATURAL_SLOT_NAMES: tuple[str, ...] = tuple(_NATURAL_SLOTS.keys())

_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})")
_CLOCK_TIME_RE = re.compile(r"(\d{2}):(\d{2})")


def _parse_time_slot(time_str: str) -> tuple[int, int] | None:
    """将时间线标签解析为一天中的分钟范围。

    Args:
        time_str: 自然时段名称或精确时间范围。

    Returns:
        起止分钟数（跨午夜终点在裁剪前保留），标签无效时返回 ``None``。
    """
    value = time_str.strip()
    match = _TIME_RANGE_RE.fullmatch(value)
    if match:
        start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
            return None
        if not (0 <= end_hour <= 24 and 0 <= end_minute <= 59):
            return None
        if end_hour == 24 and end_minute != 0:
            return None
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start == end:
            return None
        return start, end

    for keyword, interval in _NATURAL_SLOTS.items():
        if keyword in value:
            return interval
    return None


def _is_specific_time_range(time_str: str) -> bool:
    return _TIME_RANGE_RE.fullmatch(time_str.strip()) is not None


def is_valid_time_slot(time_str: str) -> bool:
    """返回时间线标签是否受支持。

    Args:
        time_str: 时间线标签。

    Returns:
        标签是否可以被解析。
    """
    return _parse_time_slot(time_str) is not None


def parse_clock_time(value: str) -> int:
    """将严格的 HH:MM 查询解析为一天中的分钟数。

    Args:
        value: HH:MM 格式的时间。

    Returns:
        一天中的分钟数，范围 0 到 1439。

    Raises:
        ValueError: 值不是有效的 HH:MM 时间。
    """
    match = _CLOCK_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("time must use HH:MM format")
    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be a valid 24-hour clock value")
    return hour * 60 + minute


def minute_of_day(value: datetime.datetime) -> int:
    """返回日期时间所代表的本地分钟数。

    Args:
        value: 本地日期时间。

    Returns:
        一天中的分钟数。
    """
    return value.hour * 60 + value.minute


def resolve_entry_intervals(entry: TimelineEntry) -> tuple[TimeInterval, ...]:
    """在一个自然日内解析条目。

    跨午夜的精确范围会保留但裁剪至 24:00。例如 ``23:00-02:00``
    解析为 ``23:00-24:00``。

    Args:
        entry: 要解析的时间线条目。

    Returns:
        一个日内区间；标签无效时返回空元组。
    """
    parsed = _parse_time_slot(entry.time)
    if parsed is None:
        return ()
    start, end = parsed
    if end <= start:
        end = 24 * 60
    return (TimeInterval(start, end),)


def _natural_slot_name(minute: int) -> str | None:
    for name, (start, end) in _NATURAL_SLOTS.items():
        if start <= minute < end:
            return name
    return None


def _latest_entry_before(
    timeline: list[TimelineEntry],
    before: int,
) -> TimelineEntry | None:
    best: TimelineEntry | None = None
    best_start: int | None = None
    for entry in timeline:
        for interval in resolve_entry_intervals(entry):
            if interval.start < before and (
                best_start is None or interval.start > best_start
            ):
                best = entry
                best_start = interval.start
    return best


def select_current_slot(
    timeline: list[TimelineEntry],
    current_minute: int,
) -> SlotMatch | None:
    """为当前日期的某个分钟数选取最佳的时间线条目。

    精确范围优先于自然时段。当没有条目覆盖该分钟时，会生成一个临时的
    空闲条目，并继承最近的前一个穿搭。

    Args:
        timeline: 要搜索的时间线条目列表。
        current_minute: 一天中的分钟数，范围 0 到 1439。

    Returns:
        匹配或合成的时段；timeline 为空时返回 ``None``。
    """
    if not timeline or not 0 <= current_minute < 24 * 60:
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

    for entry in specific_entries:
        intervals = resolve_entry_intervals(entry)
        for interval in intervals:
            if interval.contains(current_minute):
                return SlotMatch(entry, intervals, interval)

    for entry in natural_entries:
        intervals = resolve_entry_intervals(entry)
        for interval in intervals:
            if interval.contains(current_minute):
                return SlotMatch(entry, intervals, interval)

    current_slot_name = _natural_slot_name(current_minute)
    if current_slot_name is None:
        return None

    interval = TimeInterval(*_NATURAL_SLOTS[current_slot_name])
    best = _latest_entry_before(timeline, current_minute)
    return SlotMatch(
        TimelineEntry(
            time=current_slot_name,
            schedule="空闲",
            outfit=best.outfit if best else "",
        ),
        (interval,),
        interval,
    )


def find_slot_by_name(
    timeline: list[TimelineEntry],
    name: str,
) -> SlotMatch | None:
    """查找或合成一个自然时段。

    Args:
        timeline: 要搜索的时间线条目列表。
        name: 受支持的自然时段名称之一。

    Returns:
        匹配或合成的时段；名称不受支持时返回 ``None``。
    """
    if name not in _NATURAL_SLOTS:
        return None

    for entry in timeline:
        if entry.time == name:
            intervals = resolve_entry_intervals(entry)
            return SlotMatch(entry, intervals)

    interval = TimeInterval(*_NATURAL_SLOTS[name])
    best = _latest_entry_before(timeline, interval.start)
    return SlotMatch(
        TimelineEntry(name, "空闲", best.outfit if best else ""),
        (interval,),
    )
