import calendar
import datetime
import json
import random
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from .state import (
    CHINA_TIMEZONE,
    NATURAL_SLOT_NAMES,
    NATURAL_SLOT_RANGES,
    TimelineEntry,
    WeatherSlot,
    resolve_entry_intervals,
)

CONDITION_ICONS: dict[str, str] = {
    "雷阵雨": "⛈️",
    "阵雨": "🌦️",
    "小雨": "🌦️",
    "中雨": "🌧️",
    "大雨": "🌧️",
    "小雪": "🌨️",
    "中雪": "❄️",
    "大雪": "❄️",
    "雾": "🌫️",
    "霾": "😷",
    "晴": "☀️",
    "多云": "⛅",
    "阴": "☁️",
}

# 每月温度插值：月初最低/最高，月末最低/最高。用于模拟温带季风气候
# 在一个自然月内的渐进变化，例如 11 月从偏凉逐渐转为寒冷。
_MONTH_TEMPS: dict[int, tuple[int, int, int, int]] = {
    1: (-8, -6, 2, 4),
    2: (-5, -1, 5, 9),
    3: (1, 7, 11, 17),
    4: (8, 13, 19, 24),
    5: (14, 18, 25, 29),
    6: (19, 22, 30, 32),
    7: (23, 24, 33, 34),
    8: (22, 21, 33, 30),
    9: (17, 13, 27, 23),
    10: (10, 6, 20, 15),
    11: (6, 1, 15, 9),
    12: (-2, -7, 7, 3),
}

_MONTH_RAIN_CHANCE: dict[int, float] = {
    1: 0.10,
    2: 0.12,
    3: 0.20,
    4: 0.26,
    5: 0.30,
    6: 0.38,
    7: 0.44,
    8: 0.38,
    9: 0.26,
    10: 0.18,
    11: 0.14,
    12: 0.10,
}

_MONTH_CLOUD_CHANCE: dict[int, float] = {
    1: 0.48,
    2: 0.46,
    3: 0.42,
    4: 0.38,
    5: 0.34,
    6: 0.30,
    7: 0.26,
    8: 0.28,
    9: 0.32,
    10: 0.38,
    11: 0.44,
    12: 0.48,
}

_SLOT_TEMP_FACTORS: dict[str, float] = {
    "凌晨": 0.0,
    "早上": 0.25,
    "上午": 0.65,
    "中午": 0.95,
    "下午": 1.0,
    "傍晚": 0.55,
    "晚上": 0.3,
    "深夜": 0.1,
}

_WIND_DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


@dataclass(frozen=True, slots=True)
class SeasonProfile:
    """一个自然月的季节与穿搭约束。"""

    month: int
    key: str
    label: str
    description: str
    outfit_guidance: str
    forbidden_keywords: tuple[str, ...]
    activity_guidance: str


_MONTH_GUIDES: dict[int, SeasonProfile] = {
    1: SeasonProfile(
        1,
        "winter",
        "隆冬",
        "一年中最冷的月份，严寒干燥，北方多风雪",
        "羽绒服、厚毛衣、保暖内衣、长裤、围巾、手套、帽子",
        ("短袖", "短裤", "凉鞋", "背心", "吊带", "露脐"),
        "户外活动尽量安排在中午前后，注意防寒防滑",
    ),
    2: SeasonProfile(
        2,
        "winter",
        "冬末",
        "冬季末尾，寒意仍在，气温缓慢回升",
        "羽绒服、厚外套、毛衣、长裤、围巾、手套",
        ("短袖", "短裤", "凉鞋", "背心", "吊带", "露脐"),
        "户外活动注意保暖，路面结冰时减少外出",
    ),
    3: SeasonProfile(
        3,
        "spring",
        "初春",
        "气温回升但乍暖还寒，多风，早晚仍冷",
        "厚外套、毛衣、长裤，早晚注意保暖",
        ("短袖", "短裤", "凉鞋", "背心", "吊带", "露脐"),
        "多风，注意防风；适合踏青但要备外套",
    ),
    4: SeasonProfile(
        4,
        "spring",
        "仲春",
        "春暖花开，昼夜温差较大，偶有春雨",
        "长袖衬衫、薄毛衣、风衣或夹克，早晚加外套",
        ("短裤", "凉鞋"),
        "适合户外活动，注意早晚温差",
    ),
    5: SeasonProfile(
        5,
        "spring",
        "暮春",
        "温暖舒适，日照增强，雨水增多",
        "短袖或薄长袖、薄长裤、裙子，早晚备薄外套",
        (),
        "适合户外活动，注意防晒和补水",
    ),
    6: SeasonProfile(
        6,
        "summer",
        "初夏",
        "天气转热，雷阵雨开始增多",
        "短袖、短裤、裙子、凉鞋，注意防晒",
        ("羽绒服", "厚毛衣", "毛呢大衣", "围巾", "手套", "雪地靴"),
        "避免正午暴晒，雷雨时转室内",
    ),
    7: SeasonProfile(
        7,
        "summer",
        "盛夏",
        "全年最热，闷热潮湿，强对流和雷阵雨频繁",
        "短袖、短裤、裙子、凉鞋，注意防晒",
        ("羽绒服", "厚毛衣", "毛呢大衣", "围巾", "手套", "雪地靴"),
        "避免高温时段长时间户外活动，注意防暑补水；雷雨时转室内",
    ),
    8: SeasonProfile(
        8,
        "summer",
        "夏末",
        "暑热未退，早晚开始转凉",
        "短袖、短裤、裙子，早晚备薄外套",
        ("羽绒服", "厚毛衣", "毛呢大衣", "围巾", "手套", "雪地靴"),
        "白天防暑，早晚可安排户外活动",
    ),
    9: SeasonProfile(
        9,
        "autumn",
        "初秋",
        "白天仍热，早晚转凉，空气逐渐干燥",
        "短袖搭配薄外套或长裤，早晚加衣",
        ("羽绒服", "厚毛衣", "毛呢大衣", "围巾", "手套", "雪地靴"),
        "适合户外活动，早晚添衣",
    ),
    10: SeasonProfile(
        10,
        "autumn",
        "仲秋",
        "秋高气爽，昼夜温差明显，天气转凉",
        "长袖、卫衣、薄毛衣、风衣",
        ("短袖", "短裤", "凉鞋"),
        "适合户外活动，注意补水防燥",
    ),
    11: SeasonProfile(
        11,
        "autumn",
        "深秋",
        "气温明显下降，早晚温差大，秋燥明显",
        "长袖、毛衣、卫衣、风衣或外套，可搭配围巾",
        ("短袖", "短裤", "凉鞋", "背心", "吊带", "露脐"),
        "适合户外活动但注意保暖；雨雪天带伞或转室内",
    ),
    12: SeasonProfile(
        12,
        "winter",
        "初冬",
        "天气转冷，北方可能出现初雪，注意保暖",
        "羽绒服、厚外套、毛衣、长裤、围巾、手套",
        ("短袖", "短裤", "凉鞋", "背心", "吊带", "露脐"),
        "户外活动缩短，注意防寒保暖和路面结冰",
    ),
}


@dataclass(slots=True)
class DayWeather:
    """一天逐时段的天气及季节上下文。"""

    date: str
    season: SeasonProfile
    summary: str
    slots: list[WeatherSlot]
    low: int
    high: int


def season_profile(month: int) -> SeasonProfile:
    """返回指定自然月的季节与穿搭约束。

    Args:
        month: 自然月，1 到 12。

    Returns:
        对应月份的季节点。

    Raises:
        ValueError: 月份不在 1 到 12 之间。
    """
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    return _MONTH_GUIDES[month]


def condition_icon(condition: str) -> str:
    """返回天气现象对应的图标。

    Args:
        condition: 天气现象文本，例如“小雨”。

    Returns:
        匹配的 emoji 图标；无法匹配时返回通用天气图标。
    """
    value = str(condition or "").strip()
    for key, icon in CONDITION_ICONS.items():
        if key in value:
            return icon
    return "🌡️"


def _slot_range_text(name: str) -> str:
    start, end = NATURAL_SLOT_RANGES[name]
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _lerp(start: int, end: int, ratio: float) -> float:
    return start + (end - start) * ratio


def _choose_rain_condition(rng: random.Random, month: int) -> str:
    if month in (6, 7, 8) and rng.random() < 0.18:
        return "雷阵雨"
    roll = rng.random()
    if roll < 0.55:
        return "小雨"
    if roll < 0.85:
        return "中雨"
    return "大雨"


def _choose_snow_condition(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.6:
        return "小雪"
    if roll < 0.9:
        return "中雪"
    return "大雪"


def _daily_wind(rng: random.Random, *, wet: bool, month: int) -> str:
    direction = rng.choice(_WIND_DIRECTIONS)
    level = rng.randint(1, 3)
    if wet:
        level += 1
    if month in (11, 12, 1, 2) and rng.random() < 0.35:
        level += 1
    return f"{direction}风 {max(1, min(5, level))}级"


def _default_weather_slots(target_date: datetime.date) -> list[WeatherSlot]:
    """按日期和自然月生成确定性的默认天气时段。

    Args:
        target_date: 目标自然日期。

    Returns:
        8 个自然时段的默认天气。
    """
    month = target_date.month
    days_in_month = calendar.monthrange(target_date.year, month)[1]
    ratio = (target_date.day - 1) / max(1, days_in_month - 1)
    low_start, low_end, high_start, high_end = _MONTH_TEMPS[month]
    base_low = _lerp(low_start, low_end, ratio)
    base_high = _lerp(high_start, high_end, ratio)

    rng = random.Random(target_date.toordinal() * 2654435761 + 1013904223)
    anomaly = rng.uniform(-2.5, 2.5)
    low = round(base_low + anomaly)
    high = round(base_high + anomaly)
    if high - low < 4:
        high = low + 4

    wet = rng.random() < _MONTH_RAIN_CHANCE[month]
    snow = wet and (high <= 3 or (low <= 0 and month in (11, 12, 1, 2, 3)))
    wet_condition = ""
    if wet:
        wet_condition = (
            _choose_snow_condition(rng) if snow else _choose_rain_condition(rng, month)
        )

    wet_slots: set[str] = set()
    if wet:
        if wet_condition == "雷阵雨":
            start = rng.randint(3, 5)
            length = rng.randint(1, 3)
        else:
            start = rng.randint(0, 4)
            length = rng.randint(2, 5)
            if wet_condition in ("大雨", "大雪"):
                length = min(7, length + 2)
        for index in range(start, min(len(NATURAL_SLOT_NAMES), start + length)):
            wet_slots.add(NATURAL_SLOT_NAMES[index])

    cloud_roll = rng.random()
    if cloud_roll < _MONTH_CLOUD_CHANCE[month]:
        dry_condition = "阴" if rng.random() < 0.35 else "多云"
    else:
        dry_condition = "晴"

    fog_slots: set[str] = set()
    if (
        not wet
        and month in (9, 10, 11, 12, 1, 2)
        and rng.random() < 0.12
    ):
        fog_slots = {"凌晨", "早上"}

    wind = _daily_wind(rng, wet=wet, month=month)
    slots: list[WeatherSlot] = []
    for name in NATURAL_SLOT_NAMES:
        if name in wet_slots:
            condition = wet_condition
        elif name in fog_slots:
            condition = "雾"
        else:
            condition = dry_condition

        temperature = round(low + (high - low) * _SLOT_TEMP_FACTORS[name])
        if name in wet_slots:
            temperature -= 1
        if name in fog_slots:
            temperature -= 1

        if name in wet_slots:
            precipitation = wet_condition
            probability = rng.randint(65, 95)
        elif name in fog_slots:
            precipitation = "无"
            probability = 10
        elif condition in ("阴", "多云"):
            precipitation = "无"
            probability = rng.randint(10, 35)
        else:
            precipitation = "无"
            probability = rng.randint(0, 10)

        slots.append(
            WeatherSlot(
                time=name,
                condition=condition,
                temperature=temperature,
                precipitation=precipitation,
                precipitation_probability=probability,
                wind=wind,
            )
        )

    return slots


def _assemble_day_weather(
    target_date: datetime.date,
    slots: list[WeatherSlot],
) -> DayWeather:
    """把时段列表组装成完整的一天天气。

    Args:
        target_date: 目标自然日期。
        slots: 至少包含一个天气时段；缺失的自然时段会用默认天气补齐。

    Returns:
        带季节、摘要和温度范围的完整天气。
    """
    # 按自然时段顺序补齐缺失项，并去掉重复项。
    defaults = {slot.time: slot for slot in _default_weather_slots(target_date)}
    for item in slots:
        if item is None:
            continue
        defaults[item.time] = item
    ordered = [defaults[name] for name in NATURAL_SLOT_NAMES]
    low_actual = min(slot.temperature for slot in ordered)
    high_actual = max(slot.temperature for slot in ordered)
    morning = next(slot.condition for slot in ordered if slot.time == "早上")
    afternoon = next(slot.condition for slot in ordered if slot.time == "下午")
    transition = morning if morning == afternoon else f"{morning}转{afternoon}"
    wind = next((slot.wind for slot in ordered if slot.wind), "")
    summary = f"{transition}，{low_actual}~{high_actual}℃"
    if wind:
        summary += f"，{wind}"
    return DayWeather(
        date=target_date.isoformat(),
        season=season_profile(target_date.month),
        summary=summary,
        slots=ordered,
        low=low_actual,
        high=high_actual,
    )


def normalize_weather_slots(
    target_date: datetime.date,
    slots: list[WeatherSlot],
) -> list[WeatherSlot]:
    """按自然时段补齐、排序并规范化天气列表。

    Args:
        target_date: 目标自然日期。
        slots: 用户提交或持久化的天气时段。

    Returns:
        固定 8 个、按自然时段排序的天气列表。
    """
    return _assemble_day_weather(target_date, slots).slots


def generate_day_weather(
    target_date: datetime.date,
    override_slots: list[WeatherSlot] | None = None,
) -> DayWeather:
    """生成或组装某一天的逐时段天气。

    没有手动覆写时使用内置季节模型；有覆写时优先使用覆写值，缺失的
    时段用默认天气补齐。同一天相同输入会得到相同结果。

    Args:
        target_date: 目标自然日期。
        override_slots: 可选的逐时段天气覆写。

    Returns:
        一整天的逐时段天气。
    """
    if override_slots:
        return _assemble_day_weather(target_date, override_slots)
    return _assemble_day_weather(target_date, _default_weather_slots(target_date))


def format_weather_detail(slot: WeatherSlot) -> str:
    """格式化单个天气时段的详情。

    Args:
        slot: 天气时段。

    Returns:
        人类可读的天气详情。
    """
    parts = [slot.condition, f"{slot.temperature}℃"]
    if slot.precipitation and slot.precipitation != "无":
        parts.append(f"降水 {slot.precipitation} {slot.precipitation_probability}%")
    elif slot.precipitation_probability > 0:
        parts.append(f"降水概率 {slot.precipitation_probability}%")
    if slot.wind:
        parts.append(slot.wind)
    return "，".join(parts)


def format_weather_short(slot: WeatherSlot) -> str:
    """格式化用于标题和列表的简短天气。"""
    return f"{condition_icon(slot.condition)} {slot.condition} {slot.temperature}℃"


def format_weather_prompt(weather: DayWeather) -> str:
    """格式化注入到日程生成提示词中的逐时段天气。

    Args:
        weather: 一天天气。

    Returns:
        可直接拼接到提示词中的天气文本。
    """
    lines = [
        f"今日天气概览：{weather.summary}",
        "逐时段天气（日程必须与对应时段天气一致）：",
    ]
    for slot in weather.slots:
        lines.append(
            f"- {slot.time}（{_slot_range_text(slot.time)}）："
            f"{format_weather_detail(slot)}"
        )
    return "\n".join(lines)


def format_season_prompt(weather: DayWeather) -> str:
    """格式化注入到提示词中的季节、气温和穿搭约束。

    Args:
        weather: 一天天气。

    Returns:
        可直接拼接到提示词中的季节约束文本。
    """
    season = weather.season
    lines = [
        f"当前季节：{season.label}（{season.month}月，温带季风气候）",
        f"季节特征：{season.description}",
        f"今日实际气温：{weather.low}~{weather.high}℃",
        f"穿搭要求：{season.outfit_guidance}",
    ]
    if season.forbidden_keywords:
        lines.append(
            "禁止出现以下与季节不符的穿搭："
            + "、".join(season.forbidden_keywords)
            + "。"
        )
    lines.append(f"天气与日程联动要求：{season.activity_guidance}")
    return "\n".join(lines)


def format_weather_summary(weather: DayWeather) -> str:
    """返回包含季节标签和天气概览的单行摘要。"""
    return f"{weather.season.label}，{weather.summary}"


def weather_for_entry(
    weather: list[WeatherSlot],
    entry: TimelineEntry,
) -> WeatherSlot | None:
    """返回与时间线条目最匹配的天气时段。

    自然时段优先精确匹配；具体时间区间按重叠分钟数选择覆盖最多的天气
    时段。

    Args:
        weather: 全天天气时段。
        entry: 时间线条目。

    Returns:
        匹配的天气时段；没有天气数据时返回 None。
    """
    if not weather:
        return None
    exact = next((slot for slot in weather if slot.time == entry.time), None)
    if exact is not None:
        return exact

    intervals = resolve_entry_intervals(entry)
    if not intervals:
        return None
    best: WeatherSlot | None = None
    best_overlap = 0
    for slot in weather:
        slot_range = NATURAL_SLOT_RANGES.get(slot.time)
        if slot_range is None:
            continue
        overlap = sum(
            max(0, min(interval.end, slot_range[1]) - max(interval.start, slot_range[0]))
            for interval in intervals
        )
        if overlap > best_overlap:
            best = slot
            best_overlap = overlap
    return best


def weather_at_minute(
    weather: list[WeatherSlot],
    minute: int,
) -> WeatherSlot | None:
    """返回覆盖指定分钟数的天气时段。

    Args:
        weather: 全天天气时段。
        minute: 一天中的分钟数。

    Returns:
        匹配的天气时段；没有天气数据时返回 None。
    """
    if not weather:
        return None
    for slot in weather:
        slot_range = NATURAL_SLOT_RANGES.get(slot.time)
        if slot_range and slot_range[0] <= minute < slot_range[1]:
            return slot
    return min(
        weather,
        key=lambda slot: abs(NATURAL_SLOT_RANGES.get(slot.time, (minute,))[0] - minute),
    )


def find_outfit_conflicts(
    entries: list[TimelineEntry],
    season: SeasonProfile,
) -> list[tuple[TimelineEntry, str]]:
    """查找与季节明显冲突的穿搭关键词。

    Args:
        entries: 生成的时间线条目。
        season: 当前季节点。

    Returns:
        冲突条目和命中的关键词列表。
    """
    conflicts: list[tuple[TimelineEntry, str]] = []
    for entry in entries:
        for keyword in season.forbidden_keywords:
            if keyword in entry.outfit:
                conflicts.append((entry, keyword))
                break
    return conflicts


def _today() -> datetime.date:
    """返回北京时间的当前日期。"""
    return datetime.datetime.now(CHINA_TIMEZONE).date()


class WeatherOverrideStore:
    """手动天气覆写存储。

    以日期为键保存 8 个自然时段的天气。生成某天日程时会优先使用覆写
    值，因此可以在今天提前修改明天的天气。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: dict[str, list[dict]] = {}
        self.load()
        if self._prune(_today()):
            self.save()

    @property
    def path(self) -> Path:
        """返回覆写文件路径。"""
        return self._path

    def get(self, date: str) -> DayWeather | None:
        """读取指定日期的手动天气。

        Args:
            date: 目标日期，格式 YYYY-MM-DD。

        Returns:
            手动天气；没有覆写或数据无效时返回 None。
        """
        raw = self._data.get(date)
        if not isinstance(raw, list):
            return None
        try:
            parsed = datetime.date.fromisoformat(date)
        except ValueError:
            return None
        slots = [
            slot
            for slot in (
                WeatherSlot.from_dict(item)
                for item in raw
                if isinstance(item, dict)
            )
            if slot is not None
        ]
        if not slots:
            return None
        return generate_day_weather(parsed, slots)

    def has(self, date: str) -> bool:
        """返回指定日期是否已有手动天气覆写。"""
        return bool(self._data.get(date))

    def set(self, date: str, slots: list[WeatherSlot]) -> DayWeather:
        """保存指定日期的手动天气。

        Args:
            date: 目标日期，格式 YYYY-MM-DD。
            slots: 用户编辑后的天气时段。

        Returns:
            规范化后的完整天气。

        Raises:
            ValueError: 日期格式错误。
            OSError: 文件写入失败。
        """
        parsed = datetime.date.fromisoformat(date)
        normalized = normalize_weather_slots(parsed, slots)
        self._data[date] = [slot.to_dict() for slot in normalized]
        self._prune(_today())
        self.save()
        return generate_day_weather(parsed, normalized)

    def remove(self, date: str) -> bool:
        """删除指定日期的手动天气覆写。"""
        if date not in self._data:
            return False
        del self._data[date]
        self.save()
        return True

    def _prune(self, today: datetime.date) -> bool:
        """删除今天之前的旧覆写，返回是否有变更。"""
        cutoff = today.isoformat()
        stale = [date for date in self._data if date < cutoff]
        for date in stale:
            del self._data[date]
        return bool(stale)

    def load(self) -> None:
        """从磁盘加载手动天气覆写。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[DynamicLifeState] Failed to load weather overrides "
                f"from {self._path}: {exc}"
            )
            return
        if not isinstance(raw, dict):
            logger.warning(
                "[DynamicLifeState] Weather overrides file is not a JSON object: "
                f"{self._path}"
            )
            return
        self._data = {
            str(date): list(slots)
            for date, slots in raw.items()
            if isinstance(date, str) and isinstance(slots, list)
        }

    def save(self) -> None:
        """原子写入手动天气覆写。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
