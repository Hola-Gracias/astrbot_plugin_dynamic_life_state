import datetime
import json
import re
import zoneinfo
from dataclasses import asdict, dataclass, field
from pathlib import Path

from astrbot.api import logger

# =========================
# Data models
# =========================


@dataclass(slots=True)
class TimelineEntry:
    time: str
    schedule: str
    outfit: str
    mood: str = ""
    location: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "TimelineEntry":
        """Create a timeline entry from persisted data.

        Args:
            data: Raw timeline entry mapping.

        Returns:
            Normalized timeline entry.
        """
        return cls(
            time=str(data.get("time", "")),
            schedule=str(data.get("schedule", "")),
            outfit=str(data.get("outfit", "")),
            mood=str(data.get("mood", "")),
            location=str(data.get("location", "")),
            note=str(data.get("note", "")),
        )


@dataclass(slots=True)
class LifeState:
    date: str  # YYYY-MM-DD
    timezone: str  # IANA timezone name
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"  # "ok" | "failed"
    generated_at: str = ""  # ISO timestamp

    @classmethod
    def from_dict(cls, data: dict) -> "LifeState":
        """Create and validate a life state from persisted data.

        Args:
            data: Raw state mapping.

        Returns:
            Validated life state.

        Raises:
            ValueError: The date or timezone field is missing or invalid.
            ZoneInfoNotFoundError: The persisted timezone does not exist.
        """
        date = data.get("date")
        if not isinstance(date, str):
            raise ValueError("date field is missing or is not a string")
        datetime.date.fromisoformat(date)

        timezone = data.get("timezone")
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone field is missing or is not a string")
        zoneinfo.ZoneInfo(timezone)

        timeline_raw = data.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        return cls(
            date=date,
            timezone=timezone,
            schedule_summary=str(data.get("schedule_summary", "")),
            style_summary=str(data.get("style_summary", "")),
            timeline=[
                TimelineEntry.from_dict(entry)
                for entry in timeline_raw
                if isinstance(entry, dict)
            ],
            status=str(data.get("status", "ok")),
            generated_at=str(data.get("generated_at", "")),
        )


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start: int
    end: int

    def contains(self, minute: int) -> bool:
        """Return whether a minute of day belongs to the interval.

        Args:
            minute: Minute of day in the range 0 through 1439.

        Returns:
            Whether the interval contains the minute.
        """
        return self.start <= minute < self.end


@dataclass(frozen=True, slots=True)
class SlotMatch:
    entry: TimelineEntry
    intervals: tuple[TimeInterval, ...]
    active_interval: TimeInterval | None = None


def format_datetime(value: datetime.datetime) -> str:
    """Format a timezone-aware current date and time.

    Args:
        value: Date and time to format.

    Returns:
        Human-readable date, time, and timezone.
    """
    return value.strftime("%Y-%m-%d %H:%M %Z")


def format_interval(interval: TimeInterval) -> str:
    """Format a left-closed, right-open interval within one day.

    Args:
        interval: Minute-based interval.

    Returns:
        Interval formatted as ``[HH:MM, HH:MM)``.
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

    def get_current(self, date: str, timezone: str) -> LifeState | None:
        """Return the state matching the configured current day.

        Args:
            date: Target date in YYYY-MM-DD format.
            timezone: Configured IANA timezone name.

        Returns:
            Matching current state, including a failed state, or ``None``.
        """
        datetime.date.fromisoformat(date)
        if (
            self._data is None
            or self._data.date != date
            or self._data.timezone != timezone
        ):
            return None
        return self._data

    def get_by_date(self, date: str) -> LifeState | None:
        """Return the current or archived state for a natural date.

        Args:
            date: Target date in YYYY-MM-DD format.

        Returns:
            Matching state or ``None``.
        """
        datetime.date.fromisoformat(date)
        if self._data is not None and self._data.date == date:
            return self._data
        return self._load_path(self._history_path(date))

    def archive_before_generation(self, target_date: str) -> None:
        """Archive a current state belonging to a different date.

        A same-date state remains in place until the replacement is safely
        persisted. This also covers a timezone change without creating two
        historical states for the same date.

        Args:
            target_date: Date about to be generated, in YYYY-MM-DD format.

        Raises:
            ValueError: The target date is invalid.
            OSError: The archive or current-state update fails.
        """
        datetime.date.fromisoformat(target_date)
        if self._data is None or self._data.date == target_date:
            self._prune_history()
            return

        history_path = self._history_path(self._data.date)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = history_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(asdict(self._data), ensure_ascii=False, indent=2),
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
        """Return recent successful states before a natural date.

        Args:
            before_date: Exclusive upper date bound in YYYY-MM-DD format.
            limit: Maximum number of successful states to return.

        Returns:
            States ordered from newest to oldest.

        Raises:
            ValueError: The date is invalid.
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
        """Persist a state as the only current natural-date state.

        Args:
            state: State to persist.

        Raises:
            ValueError: The state date or timezone is invalid.
            ZoneInfoNotFoundError: The state timezone does not exist.
            OSError: The current state cannot be written.
        """
        datetime.date.fromisoformat(state.date)
        zoneinfo.ZoneInfo(state.timezone)

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
        """Load the current state from disk."""
        self._data = self._load_path(self._path)

    def save(self) -> None:
        """Atomically persist the current state."""
        if self._data is None:
            if self._path.exists():
                self._path.unlink()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(asdict(self._data), ensure_ascii=False, indent=2),
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
        """Load one new-format state file.

        Args:
            path: State file to read.

        Returns:
            Parsed state, or ``None`` for a missing or invalid file.
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
        """Move legacy or unrecognized live storage into a timestamped backup.

        Raises:
            OSError: Moving the old storage fails and cannot be rolled back.
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
                # Preserve recovery files because they may be the only surviving copy.
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
        """Remove invalid files and retain the newest seven dated states."""
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
    """Parse a timeline label into a minute-of-day range.

    Args:
        time_str: Natural slot name or explicit time range.

    Returns:
        Start and end minutes, preserving a cross-midnight end before clipping,
        or ``None`` when the label is invalid.
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
    """Return whether a timeline time label is supported.

    Args:
        time_str: Timeline time label.

    Returns:
        Whether the label can be parsed.
    """
    return _parse_time_slot(time_str) is not None


def parse_clock_time(value: str) -> int:
    """Parse a strict HH:MM query into a minute of day.

    Args:
        value: Clock time in HH:MM format.

    Returns:
        Minute of day in the range 0 through 1439.

    Raises:
        ValueError: The value is not a valid HH:MM time.
    """
    match = _CLOCK_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("time must use HH:MM format")
    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be a valid 24-hour clock value")
    return hour * 60 + minute


def minute_of_day(value: datetime.datetime) -> int:
    """Return the local minute represented by a date and time.

    Args:
        value: Local date and time.

    Returns:
        Minute of day.
    """
    return value.hour * 60 + value.minute


def resolve_entry_intervals(entry: TimelineEntry) -> tuple[TimeInterval, ...]:
    """Resolve an entry within one natural day.

    Cross-midnight explicit ranges are retained but clipped at 24:00. For
    example, ``23:00-02:00`` resolves to ``23:00-24:00``.

    Args:
        entry: Timeline entry to resolve.

    Returns:
        One in-day interval, or an empty tuple for an invalid label.
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
    """Select the best timeline entry for a minute of the current date.

    Explicit ranges take priority over natural slots. When no entry covers the
    minute, a temporary idle entry inherits the most recent earlier outfit.

    Args:
        timeline: Timeline entries to search.
        current_minute: Minute of day in the range 0 through 1439.

    Returns:
        Matching or synthesized slot, or ``None`` when the timeline is empty.
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
    """Find or synthesize a natural time slot.

    Args:
        timeline: Timeline entries to search.
        name: One of the supported natural slot names.

    Returns:
        Matching or synthesized slot, or ``None`` for an unsupported name.
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
