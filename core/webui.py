from __future__ import annotations

import datetime
from dataclasses import replace
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request

from .state import (
    CHINA_TIMEZONE,
    NATURAL_SLOT_NAMES,
    OPTIONAL_TIMELINE_FIELDS,
    DataManager,
    LifeState,
    TimelineEntry,
    WeatherSlot,
    format_interval,
    is_valid_time_slot,
    resolve_entry_intervals,
)
from .weather import (
    condition_icon,
    generate_day_weather,
    normalize_weather_slots,
    weather_for_entry,
)

MAX_SHORT_TEXT_LENGTH = 200
MAX_LONG_TEXT_LENGTH = 4000


def _error(message: str):
    """返回 bridge 可识别的错误响应。

    AstrBot 的插件页面 bridge 只在响应体包含 status=error 时保留后端
    错误信息；HTTP 4xx/5xx 会被 axios 转换为通用错误。因此这里统一使用
    HTTP 200 的错误信封，让前端能展示具体的中文校验信息。

    Args:
        message: 面向用户的中文错误信息。

    Returns:
        包含错误信封的 JSON 响应。
    """
    return json_response({"status": "error", "message": message})


def _clean_text(value: object, *, limit: int) -> str:
    """将输入值规范化为单段纯文本。

    Args:
        value: 前端提交的原始值。
        limit: 允许的最大字符数。

    Returns:
        去除首尾空白并截断到长度上限的字符串；非字符串输入返回空字符串。
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_multiline(value: object, *, limit: int) -> str:
    """将输入值规范化为多行文本。

    Args:
        value: 前端提交的原始值。
        limit: 允许的最大字符数。

    Returns:
        统一换行符、去除首尾空白并截断到长度上限的字符串。
    """
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


class SchedulePageApi:
    """为 WebUI 每日日程页面提供读写接口。

    页面只渲染在 AstrBot Dashboard 的受限 iframe 中，所有请求都经过
    Dashboard 的插件扩展路由转发到 context.register_web_api 注册的
    处理器。此模块不包含任何 UI 资源，页面文件位于 pages/schedule/。
    """

    def __init__(self, plugin: Any, *, plugin_name: str) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self._registered_routes: list[tuple[str, object, tuple[str, ...]]] = []

    @property
    def data_mgr(self) -> DataManager | None:
        """返回已初始化的生活状态存储。"""
        return getattr(self.plugin, "data_mgr", None)

    @property
    def generator(self) -> Any:
        """返回用于检查生成状态的生成器。"""
        return getattr(self.plugin, "generator", None)

    @property
    def weather_store(self):
        """返回手动天气覆写存储。"""
        return getattr(self.plugin, "weather_store", None)

    def register(self) -> None:
        """注册每日日程页面使用的 Web API 路由。"""
        routes = (
            (
                "schedule/state",
                self.get_state,
                ["GET"],
                "Get the daily life schedule",
            ),
            (
                "schedule/save",
                self.save_state,
                ["POST"],
                "Save manual edits to the daily life schedule",
            ),
            (
                "schedule/weather/save",
                self.save_weather,
                ["POST"],
                "Save manual weather for today or tomorrow",
            ),
            (
                "schedule/weather/reset",
                self.reset_weather,
                ["POST"],
                "Reset manual weather to the generated default",
            ),
        )
        for path, handler, methods, description in routes:
            route = f"/{self.plugin_name}/{path}"
            self.plugin.context.register_web_api(
                route,
                handler,
                methods,
                description,
            )
            self._registered_routes.append((route, handler, tuple(methods)))
        logger.info(
            "[DynamicLifeState] Registered schedule page APIs: "
            f"plugin={self.plugin_name}, routes={len(self._registered_routes)}"
        )

    def unregister(self) -> None:
        """移除本实例注册的路由，避免插件重载后残留旧处理器。

        AstrBot 4.28 没有公开的注销接口，因此只移除路由与处理器都匹配的
        注册项；如果新实例已经用相同路由完成注册，旧实例不会误删。
        """
        if not self._registered_routes:
            return
        registry = getattr(self.plugin.context, "registered_web_apis", None)
        if not isinstance(registry, list):
            self._registered_routes.clear()
            return
        try:
            registry[:] = [
                item for item in registry if not self._is_own_registration(item)
            ]
        except Exception as exc:  # pragma: no cover - 依赖 AstrBot 内部结构
            logger.warning(
                "[DynamicLifeState] Failed to unregister schedule page APIs: "
                f"error={exc}"
            )
        finally:
            self._registered_routes.clear()

    def _is_own_registration(self, item: object) -> bool:
        """判断注册表项是否由当前实例注册。"""
        if not isinstance(item, tuple) or len(item) < 2:
            return False
        for route, handler, _methods in self._registered_routes:
            if item[0] == route and item[1] == handler:
                return True
        return False

    @staticmethod
    def _today() -> str:
        """返回北京时间的当前日期字符串。"""
        return datetime.datetime.now(CHINA_TIMEZONE).date().isoformat()

    def _is_generating(self) -> bool:
        """返回生成器当前是否正在生成。"""
        generator = self.generator
        return bool(
            generator is not None and getattr(generator, "is_generating", False)
        )

    @staticmethod
    def _serialize_weather(slots: list[WeatherSlot]) -> list[dict[str, Any]]:
        """将天气时段转换为页面渲染字典。"""
        return [
            {
                **slot.to_dict(),
                "icon": condition_icon(slot.condition),
            }
            for slot in slots
        ]

    @classmethod
    def _serialize_state(
        cls,
        state: LifeState,
        weather_slots: list[WeatherSlot] | None = None,
    ) -> dict[str, Any]:
        """将状态转换为页面可直接渲染的字典。"""
        data = state.to_dict()
        slots = list(state.weather if weather_slots is None else weather_slots)
        data["weather"] = cls._serialize_weather(slots)
        for item, entry in zip(data["timeline"], state.timeline):
            intervals = resolve_entry_intervals(entry)
            item["interval_text"] = (
                "、".join(format_interval(interval) for interval in intervals)
                or "无法解析"
            )
            entry_weather = weather_for_entry(slots, entry)
            if entry_weather is not None:
                item["weather"] = {
                    **entry_weather.to_dict(),
                    "icon": condition_icon(entry_weather.condition),
                }
        return data

    @staticmethod
    def _page_metadata() -> dict[str, Any]:
        """返回页面渲染时间线所需的共享元数据。"""
        return {
            "natural_slots": list(NATURAL_SLOT_NAMES),
            "optional_fields": [
                {"key": key, "label": spec["label"], "icon": spec["icon"]}
                for key, spec in OPTIONAL_TIMELINE_FIELDS.items()
            ],
        }

    async def get_state(self):
        """返回指定日期（默认今天）的日程和天气。"""
        data_mgr = self.data_mgr
        if data_mgr is None:
            return _error(
                "生活状态尚未初始化，请稍后重试。",
            )

        today_date = datetime.date.fromisoformat(self._today())
        tomorrow_date = today_date + datetime.timedelta(days=1)
        today = today_date.isoformat()
        tomorrow = tomorrow_date.isoformat()
        raw_date = str(request.query.get("date") or "").strip() or today
        try:
            parsed_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return _error("日期格式错误，请使用 YYYY-MM-DD 格式。")
        if parsed_date > tomorrow_date:
            return _error("最多只能查看今天和明天，暂不支持更远的日期。")

        try:
            state = data_mgr.get_by_date(raw_date)
        except Exception as exc:
            logger.error(
                "[DynamicLifeState] Failed to load schedule page state: "
                f"date={raw_date}, error={exc}"
            )
            return _error("读取日程失败，请查看 AstrBot 日志。")

        try:
            available_dates = data_mgr.list_available_dates()
        except Exception:
            available_dates = [today]

        is_today = raw_date == today
        is_tomorrow = raw_date == tomorrow
        is_history = raw_date < today
        generating = self._is_generating()
        store = self.weather_store
        override = store.get(raw_date) if store is not None else None

        weather_slots: list[WeatherSlot] = []
        weather_summary = ""
        season = ""
        weather_source = "none"
        if override is not None and (is_today or is_tomorrow):
            weather_slots = list(override.slots)
            weather_summary = override.summary
            season = override.season.label
            weather_source = "override"
        elif state is not None and state.weather:
            weather_slots = list(state.weather)
            weather_summary = state.weather_summary
            season = state.season
            weather_source = "state"
        elif override is not None:
            weather_slots = list(override.slots)
            weather_summary = override.summary
            season = override.season.label
            weather_source = "override"
        elif not is_history:
            preview = generate_day_weather(parsed_date)
            weather_slots = list(preview.slots)
            weather_summary = preview.summary
            season = preview.season.label
            weather_source = "generated"
        elif state is not None:
            season = state.season

        editable = bool(
            is_today and state is not None and state.status == "ok" and not generating
        )
        weather_editable = bool((is_today or is_tomorrow) and not generating)
        return json_response(
            {
                "date": raw_date,
                "today": today,
                "tomorrow": tomorrow,
                "is_today": is_today,
                "is_tomorrow": is_tomorrow,
                "is_history": is_history,
                "editable": editable,
                "weather_editable": weather_editable,
                "generating": generating,
                "state": (
                    self._serialize_state(state, weather_slots)
                    if state is not None
                    else None
                ),
                "weather": self._serialize_weather(weather_slots),
                "weather_summary": weather_summary,
                "season": season,
                "weather_source": weather_source,
                "weather_override": bool(store is not None and store.has(raw_date)),
                "available_dates": available_dates,
                **self._page_metadata(),
            }
        )

    async def save_state(self):
        """保存用户手动修改后的今日日程。"""
        data_mgr = self.data_mgr
        if data_mgr is None:
            return _error(
                "生活状态尚未初始化，请稍后重试。",
            )

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求内容格式错误。")

        today = self._today()
        raw_date = _clean_text(payload.get("date"), limit=10)
        if raw_date != today:
            return _error(
                "仅支持修改今天正在使用的日程。历史日程仅供查看。",
            )
        if self._is_generating():
            return _error(
                "今天的日程正在生成，请等待生成完成后再保存。",
            )

        current = data_mgr.get_current(today)
        if current is None or current.status != "ok":
            return _error(
                "今天还没有可编辑的日程，请等待自动生成或使用 /dls regen。",
            )

        timeline_raw = payload.get("timeline")
        if not isinstance(timeline_raw, list) or not timeline_raw:
            return _error("时间线至少需要一个时段。")

        entries: list[TimelineEntry] = []
        for index, item in enumerate(timeline_raw, start=1):
            if not isinstance(item, dict):
                return _error(f"第 {index} 个时段的格式不正确。")
            entry_data: dict[str, str] = {
                "time": _clean_text(item.get("time"), limit=MAX_SHORT_TEXT_LENGTH),
                "schedule": _clean_multiline(
                    item.get("schedule"), limit=MAX_LONG_TEXT_LENGTH
                ),
                "outfit": _clean_multiline(
                    item.get("outfit"), limit=MAX_LONG_TEXT_LENGTH
                ),
            }
            for key in OPTIONAL_TIMELINE_FIELDS:
                entry_data[key] = _clean_multiline(
                    item.get(key), limit=MAX_LONG_TEXT_LENGTH
                )
            if not entry_data["time"]:
                return _error(f"第 {index} 个时段缺少时间。")
            if not is_valid_time_slot(entry_data["time"]):
                return _error(
                    f"第 {index} 个时段的时间「{entry_data['time']}」不受支持。"
                )
            if not entry_data["schedule"]:
                return _error(f"第 {index} 个时段缺少日程内容。")
            if not entry_data["outfit"]:
                return _error(f"第 {index} 个时段缺少穿搭内容。")
            entry = TimelineEntry.from_dict(entry_data)
            if entry is None:
                return _error(f"第 {index} 个时段无法解析。")
            entries.append(entry)

        weather_summary = current.weather_summary
        weather = list(current.weather)
        season = current.season
        override = (
            self.weather_store.get(today) if self.weather_store is not None else None
        )
        if override is not None:
            weather_summary = override.summary
            weather = list(override.slots)
            season = override.season.label

        new_state = LifeState(
            date=today,
            holiday=_clean_text(payload.get("holiday"), limit=MAX_SHORT_TEXT_LENGTH)
            or "无",
            schedule_summary=_clean_multiline(
                payload.get("schedule_summary"), limit=MAX_LONG_TEXT_LENGTH
            ),
            style_summary=_clean_multiline(
                payload.get("style_summary"), limit=MAX_LONG_TEXT_LENGTH
            ),
            weather_summary=weather_summary,
            weather=weather,
            season=season,
            timeline=entries,
            status="ok",
            generated_at=current.generated_at,
        )

        try:
            data_mgr.set(new_state)
        except Exception as exc:
            logger.error(
                "[DynamicLifeState] Failed to save manual schedule edit: "
                f"date={today}, error={exc}"
            )
            return _error("保存失败，请查看 AstrBot 日志。")

        logger.info(
            "[DynamicLifeState] Manual schedule edit saved: "
            f"date={today}, entries={len(entries)}"
        )
        return json_response(
            {
                "message": "日程已保存，后续对话将立即使用修改后的状态。",
                "date": today,
                "state": self._serialize_state(new_state),
                **self._page_metadata(),
            }
        )

    def _weather_response(
        self,
        day,
        *,
        source: str,
        override: bool,
        state: LifeState | None = None,
        message: str = "",
    ):
        """构建天气保存/重置接口的响应。"""
        today = self._today()
        tomorrow = (
            datetime.date.fromisoformat(today) + datetime.timedelta(days=1)
        ).isoformat()
        payload: dict[str, Any] = {
            "message": message,
            "date": day.date,
            "is_today": day.date == today,
            "is_tomorrow": day.date == tomorrow,
            "weather": self._serialize_weather(day.slots),
            "weather_summary": day.summary,
            "season": day.season.label,
            "weather_source": source,
            "weather_override": override,
            "weather_editable": True,
            "today": today,
            "tomorrow": tomorrow,
        }
        if state is not None:
            payload["state"] = self._serialize_state(state, day.slots)
        return json_response(payload)

    async def save_weather(self):
        """保存今天或明天的手动天气。"""
        data_mgr = self.data_mgr
        if data_mgr is None:
            return _error("生活状态尚未初始化，请稍后重试。")
        store = self.weather_store
        if store is None:
            return _error("天气覆写存储尚未初始化，请稍后重试。")

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求内容格式错误。")

        today = self._today()
        tomorrow = (
            datetime.date.fromisoformat(today) + datetime.timedelta(days=1)
        ).isoformat()
        raw_date = _clean_text(payload.get("date"), limit=10)
        if raw_date not in (today, tomorrow):
            return _error("只能编辑今天或明天的天气。")
        if self._is_generating():
            return _error("日程正在生成，请等待生成完成后再保存天气。")

        raw_weather = payload.get("weather")
        if not isinstance(raw_weather, list) or not raw_weather:
            return _error("天气至少需要一个时段。")
        slots: list[WeatherSlot] = []
        for index, item in enumerate(raw_weather, start=1):
            if not isinstance(item, dict):
                return _error(f"第 {index} 个天气时段格式不正确。")
            slot = WeatherSlot.from_dict(item)
            if slot is None:
                return _error(f"第 {index} 个天气时段数据无效。")
            slots.append(slot)

        parsed_date = datetime.date.fromisoformat(raw_date)
        try:
            normalized = normalize_weather_slots(parsed_date, slots)
            day = store.set(raw_date, normalized)
        except Exception as exc:
            logger.error(
                "[DynamicLifeState] Failed to save manual weather: "
                f"date={raw_date}, error={exc}"
            )
            return _error("保存天气失败，请查看 AstrBot 日志。")

        state = None
        if raw_date == today:
            current = data_mgr.get_current(today)
            if current is not None and current.status == "ok":
                # 构造新状态再落盘，避免 DataManager.set() 回滚时指向已被修改的旧对象。
                updated = replace(
                    current,
                    weather_summary=day.summary,
                    weather=list(day.slots),
                    season=day.season.label,
                )
                try:
                    data_mgr.set(updated)
                except Exception as exc:
                    logger.error(
                        "[DynamicLifeState] Failed to attach manual weather "
                        f"to today's state: {exc}"
                    )
                    return _error("天气已保存，但写入今天状态失败，请查看日志。")
                state = updated

        logger.info(
            "[DynamicLifeState] Manual weather saved: "
            f"date={raw_date}, entries={len(day.slots)}"
        )
        message = (
            "今天的天气已保存；修改天气不会自动重排日程，"
            "可以稍后使用 /dls regen 或在页面手动调整日程。"
            if raw_date == today
            else "明天的手动天气已保存；明天 00:00 生成日程时会使用这份天气。"
        )
        return self._weather_response(
            day,
            source="override",
            override=True,
            state=state,
            message=message,
        )

    async def reset_weather(self):
        """恢复今天或明天的自动天气。"""
        data_mgr = self.data_mgr
        if data_mgr is None:
            return _error("生活状态尚未初始化，请稍后重试。")
        store = self.weather_store
        if store is None:
            return _error("天气覆写存储尚未初始化，请稍后重试。")

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求内容格式错误。")

        today = self._today()
        tomorrow = (
            datetime.date.fromisoformat(today) + datetime.timedelta(days=1)
        ).isoformat()
        raw_date = _clean_text(payload.get("date"), limit=10)
        if raw_date not in (today, tomorrow):
            return _error("只能重置今天或明天的天气。")
        if self._is_generating():
            return _error("日程正在生成，请等待生成完成后再操作。")

        parsed_date = datetime.date.fromisoformat(raw_date)
        generated = generate_day_weather(parsed_date)
        try:
            store.remove(raw_date)
        except Exception as exc:
            logger.error(
                "[DynamicLifeState] Failed to remove manual weather: "
                f"date={raw_date}, error={exc}"
            )
            return _error("重置天气失败，请查看 AstrBot 日志。")

        state = None
        if raw_date == today:
            current = data_mgr.get_current(today)
            if current is not None and current.status == "ok":
                # 同 save_weather：先构造新对象，失败时旧状态保持不变。
                updated = replace(
                    current,
                    weather_summary=generated.summary,
                    weather=list(generated.slots),
                    season=generated.season.label,
                )
                try:
                    data_mgr.set(updated)
                except Exception as exc:
                    logger.error(
                        f"[DynamicLifeState] Failed to reset today's weather: {exc}"
                    )
                    return _error("重置天气失败，请查看 AstrBot 日志。")
                state = updated

        logger.info(
            "[DynamicLifeState] Manual weather reset: "
            f"date={raw_date}, entries={len(generated.slots)}"
        )
        message = (
            "今天的天气已恢复为自动生成。"
            if raw_date == today
            else "明天的天气已恢复为自动生成。"
        )
        return self._weather_response(
            generated,
            source="state" if state is not None else "generated",
            override=False,
            state=state,
            message=message,
        )
