import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    date: str  # yyyy-mm-dd
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"  # "ok" | "failed"
    generated_at: str = ""  # ISO timestamp

    @classmethod
    def from_dict(cls, d: dict) -> "LifeState":
        timeline_raw = d.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        return cls(
            date=str(d.get("date", "")),
            schedule_summary=str(d.get("schedule_summary", "")),
            style_summary=str(d.get("style_summary", "")),
            timeline=[TimelineEntry.from_dict(e) for e in timeline_raw],
            status=str(d.get("status", "ok")),
            generated_at=str(d.get("generated_at", "")),
        )


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
        self._data[state.date] = state
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
                data[date_str] = LifeState.from_dict(item)
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

_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})")


def _parse_time_slot(time_str: str) -> tuple[int, int] | None:
    """将时间段字符串解析为 (start_minute_of_day, end_minute_of_day)。"""
    time_str = time_str.strip()
    m = _TIME_RANGE_RE.search(time_str)
    if m:
        h1, m1, h2, m2 = map(int, m.groups())
        return (h1 * 60 + m1, h2 * 60 + m2)

    for keyword, (start_h, end_h) in _NATURAL_SLOTS.items():
        if keyword in time_str:
            return (start_h * 60, end_h * 60)

    return None


def select_current_slot(
    timeline: list[TimelineEntry],
    now: datetime.datetime | None = None,
) -> TimelineEntry | None:
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
        匹配的 TimelineEntry，或 None（timeline 为空且无法确定自然时段时）。
    """
    if not timeline:
        return None

    now = now or datetime.datetime.now()
    current_minute = now.hour * 60 + now.minute

    # 确定当前自然时段名称
    current_slot_name: str | None = None
    for name, (start_h, end_h) in _NATURAL_SLOTS.items():
        if start_h * 60 <= current_minute < end_h * 60:
            current_slot_name = name
            break

    # 解析所有条目，区分具体区间和自然时段
    specific_parsed: list[tuple[int, int, TimelineEntry]] = []
    natural_parsed: list[tuple[int, int, TimelineEntry]] = []
    for entry in timeline:
        slot = _parse_time_slot(entry.time)
        if slot is None:
            continue
        if _TIME_RANGE_RE.search(entry.time):
            specific_parsed.append((slot[0], slot[1], entry))
        else:
            natural_parsed.append((slot[0], slot[1], entry))

    all_parsed = specific_parsed + natural_parsed

    # 优先：具体时间区间覆盖当前时间
    for start_min, end_min, entry in specific_parsed:
        if start_min <= current_minute < end_min:
            return entry

    # 其次：自然时段覆盖当前时间
    for start_min, end_min, entry in natural_parsed:
        if start_min <= current_minute < end_min:
            return entry

    # 当前自然时段缺失 → 构造临时条目
    if current_slot_name:
        best: TimelineEntry | None = None
        best_start = -1
        for start_min, _, entry in all_parsed:
            if start_min <= current_minute and start_min > best_start:
                best_start = start_min
                best = entry
        inherited_outfit = best.outfit if best else ""
        return TimelineEntry(
            time=current_slot_name,
            schedule="空闲",
            outfit=inherited_outfit,
        )

    # 极端情况：无法确定自然时段（不应发生）
    return None


# =========================
# 业务日期
# =========================


def parse_generate_time(generate_time: str | None) -> tuple[int, int]:
    """解析 generate_time 为 (hour, minute)，非法值兜底为 (0, 0)。"""
    try:
        hour, minute = map(int, (generate_time or "00:00").split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return 0, 0
        return hour, minute
    except Exception:
        return 0, 0


def resolve_business_now(
    generate_time: str | None,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    """根据 generate_time 计算当前业务日期。"""
    now = now or datetime.datetime.now()
    hour, minute = parse_generate_time(generate_time)
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < boundary:
        return now - datetime.timedelta(days=1)
    return now
