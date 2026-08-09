import datetime
import re
from collections.abc import AsyncGenerator

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.generator import Generator
from .core.injector import (
    build_fake_tool_call,
    build_injection_text,
    remove_fake_tool_call_from_context,
)
from .core.state import (
    CHINA_TIMEZONE,
    NATURAL_SLOT_NAMES,
    OPTIONAL_TIMELINE_FIELDS,
    DataManager,
    LifeState,
    SlotMatch,
    find_slot_by_name,
    format_datetime,
    format_interval,
    minute_of_day,
    parse_clock_time,
    resolve_entry_intervals,
    select_current_slot,
)


def _extract_args_after(message_str: str, command: str) -> str | None:
    """提取命令之后的完整尾随文本。

    Args:
        message_str: 原始事件消息。
        command: 命令前缀（不含前导斜杠）。

    Returns:
        尾随文本，没有额外文本时返回 ``None``。
    """
    text = message_str.lstrip("/").strip()
    text = re.sub(r"\s+", " ", text)  # 规范化空白以便命令匹配。
    prefix = f"{command} "
    idx = text.find(prefix)
    if idx == -1:
        return None
    remainder = text[idx + len(prefix) :].strip()
    return remainder or None


class DynamicLifeStatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.state_file = self.data_dir / "life_state.json"

    async def initialize(self) -> None:
        """初始化存储、生成器和午夜定时调度。"""
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(self.context, self.config, self.data_mgr)
        self._start_scheduler()

    async def terminate(self) -> None:
        """停止午夜定时调度。"""
        self._stop_scheduler()

    # =========================
    # Scheduler
    # =========================

    def _now(self) -> datetime.datetime:
        """返回中国标准时间的当前日期时间。"""
        return datetime.datetime.now(CHINA_TIMEZONE)

    def _current_date(
        self,
        now: datetime.datetime | None = None,
    ) -> datetime.date:
        """返回中国标准时间的日期。

        Args:
            now: 可选的时间，由调用方或测试提供。

        Returns:
            本地日期。
        """
        value = now or self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=CHINA_TIMEZONE)
        else:
            value = value.astimezone(CHINA_TIMEZONE)
        return value.date()

    def _select_current_state(
        self,
        now: datetime.datetime,
    ) -> tuple[LifeState | None, datetime.date]:
        """为当前北京时间选取一个成功生成的状态。

        Args:
            now: 当前日期时间。

        Returns:
            匹配的状态（如果有）和捕获的目标日期。
        """
        target_date = self._current_date(now)
        state = self.data_mgr.get_current(target_date.isoformat())
        if state is None or state.status != "ok":
            return None, target_date
        return state, target_date

    def _start_scheduler(self) -> None:
        """启动固定的本地午夜定时调度器。"""
        try:
            self._scheduler = AsyncIOScheduler(
                timezone=CHINA_TIMEZONE,
                executors={"default": AsyncIOExecutor()},
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": 120,
                },
            )
            self._scheduler.add_job(
                self._daily_generate,
                "cron",
                hour=0,
                minute=0,
                id="dynamic_life_state_daily",
            )
            self._scheduler.start()
            logger.info(
                f"[DynamicLifeState] Scheduler started for daily generation "
                f"at 00:00 in {CHINA_TIMEZONE.key}"
            )
        except Exception as e:
            logger.error(f"[DynamicLifeState] Scheduler startup failed: {e}")

    def _stop_scheduler(self) -> None:
        """停止正在运行的定时调度器。"""
        try:
            if hasattr(self, "_scheduler") and self._scheduler.running:
                self._scheduler.shutdown()
        except Exception:
            pass

    async def _daily_generate(self) -> None:
        """生成当前中国标准时间日期的状态。"""
        now = self._now()
        data, target_date = self._select_current_state(now)
        if data:
            logger.info(
                f"[DynamicLifeState] State for {target_date.isoformat()} "
                "already exists; skipping generation"
            )
            return
        await self.generator.generate(target_date)

    # =========================
    # LLM request hook
    # =========================

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """在每次启用的 LLM 请求前注入当前时段状态。

        Args:
            event: 传入的 AstrBot 消息事件。
            req: 可变的 provider 请求。
        """

        # 会话过滤
        if not self._is_session_enabled(event.unified_msg_origin):
            return

        now = self._now()
        data, target_date = self._select_current_state(now)
        if data is None:
            if self.generator.is_generating:
                return  # 共享生成任务执行期间跳过注入。
            result = await self.generator.generate(target_date)
            if not result.succeeded or result.state is None:
                return  # 无可用的有效状态时跳过注入。
            data = result.state

        # 选取当前时段。
        current_match = select_current_slot(data.timeline, minute_of_day(now))

        # 解析配置的注入方式。
        injection_method = str(
            self.config.get("injection_mode", "extra_user_content_parts")
        )
        injection_method = self._resolve_injection_method(req, injection_method)

        # 移除上一次的伪注入。
        self._cleanup_previous_injection(req)

        # 注入当前状态。
        if injection_method == "extra_user_content_parts":
            inject_text = build_injection_text(data, current_match, now)
            req.extra_user_content_parts.append(
                TextPart(text=inject_text).mark_as_temp()
            )
            if self.config.get("debug_mode", False):
                logger.debug(f"[DynamicLifeState] Injected content:\n{inject_text}")

        elif injection_method == "fake_tool_call":
            fake_messages = build_fake_tool_call(data, current_match, now)
            req.contexts.extend(fake_messages)
            if self.config.get("debug_mode", False):
                logger.debug(
                    f"[DynamicLifeState] Injected fake tool call:\n"
                    f"{fake_messages[1]['content']}"
                )

        if self.config.get("debug_mode", False):
            logger.info(
                f"[DynamicLifeState] Injection method={injection_method}, "
                f"date={data.date}, "
                f"slot={current_match.entry.time if current_match else 'N/A'}"
            )

    # =========================
    # Session filter
    # =========================

    def _is_session_enabled(self, unified_msg_origin: str) -> bool:
        """返回当前会话是否启用了生活状态注入。

        Args:
            unified_msg_origin: AstrBot 统一消息来源。

        Returns:
            会话是否通过配置的列表模式检查。
        """
        mode = str(self.config.get("session_list_mode", "whitelist")).strip()
        session_list: list[str] = list(self.config.get("session_list", []) or [])

        if mode == "none":
            return True
        if mode == "whitelist":
            return unified_msg_origin in session_list
        if mode == "blacklist":
            return unified_msg_origin not in session_list
        if getattr(self, "_warned_invalid_session_mode", None) != mode:
            logger.warning(
                f"[DynamicLifeState] Invalid session_list_mode {mode!r}; "
                "falling back to whitelist behavior"
            )
            self._warned_invalid_session_mode = mode
        return unified_msg_origin in session_list

    # =========================
    # Injection fallback
    # =========================

    def _resolve_injection_method(self, req: ProviderRequest, configured: str) -> str:
        """当 provider 不支持伪工具调用时回退到备选方式。

        Args:
            req: 用于解析当前 provider 的请求对象。
            configured: 配置的注入方式。

        Returns:
            兼容的注入方式。
        """
        if configured != "fake_tool_call":
            return configured
        try:
            provider = self.context.get_using_provider(req.session_id)
            provider_config = getattr(provider, "provider_config", {})
            provider_type = (
                str(provider_config.get("type", ""))
                if isinstance(provider_config, dict)
                else ""
            )
            model = provider.get_model() if hasattr(provider, "get_model") else ""
            if "googlegenai" in provider_type or "gemini" in str(model).lower():
                logger.info(
                    "[DynamicLifeState] Gemini does not support fake_tool_call; "
                    "falling back to extra_user_content_parts"
                )
                return "extra_user_content_parts"
        except Exception:
            pass
        return configured

    # =========================
    # Injection cleanup
    # =========================

    def _cleanup_previous_injection(self, req: ProviderRequest) -> None:
        """移除上一次的伪工具调用注入。

        Args:
            req: 可变的 provider 请求。
        """
        remove_fake_tool_call_from_context(req.contexts)

    # =========================
    # LLM tool for full-state queries
    # =========================

    @filter.llm_tool(name="get_full_dynamic_life_state")
    async def get_full_dynamic_life_state(
        self, event: AstrMessageEvent, date: str = ""
    ) -> str:
        """获取 Bot 当前或指定日期的完整生活状态。

        date 参数为可选，格式 YYYY-MM-DD。不传则默认返回今天的状态。
        仅在用户明确询问"今天一整天在做什么""全天状态""完整日程""其他时间段的安排""回顾某天的状态"或类似问题时调用。

        Args:
            date (string): 可选的 YYYY-MM-DD 日期。

        Returns:
            完整状态或查询错误信息。
        """
        if not self._is_session_enabled(event.unified_msg_origin):
            return "当前会话未启用动态生活状态功能。"

        if self.generator.is_generating:
            return "生活状态正在生成中，请稍后再试。"

        now = self._now()
        if date:
            date = date.strip()
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                return "日期格式错误，请使用 YYYY-MM-DD 格式，例如 2026-07-01。"
            target_str = date
            data = self.data_mgr.get_by_date(target_str)
        else:
            data, target_date = self._select_current_state(now)
            if data is None:
                return "当前时间没有已生成的生活状态。"
            target_str = target_date.isoformat()

        if not data:
            return f"{target_str} 的生活状态尚未生成。"
        if data.status == "failed":
            return f"{target_str} 的生活状态生成失败。"

        return self._format_full_state(data)

    # =========================
    # State formatting
    # =========================

    @staticmethod
    def _format_match_intervals(match: SlotMatch) -> str:
        """格式化时段匹配中的所有区间。

        Args:
            match: 匹配到的时间线条目。

        Returns:
            人类可读的区间列表。
        """
        return "、".join(format_interval(interval) for interval in match.intervals)

    def _format_current_state(
        self,
        state: LifeState,
        match: SlotMatch,
        *,
        current_datetime: datetime.datetime | None = None,
        show_current: bool = True,
    ) -> str:
        """格式化当前或查询的时段状态。

        Args:
            state: 某一天的生活状态。
            match: 选中的时段。
            current_datetime: 可选的要显示的时钟时间。
            show_current: 标签是否应描述"当前"时刻。

        Returns:
            人类可读的状态文本。
        """
        time_label = "当前时段" if show_current else "时段"
        schedule_label = "当前安排" if show_current else "安排"
        outfit_label = "当前穿搭" if show_current else "穿搭"
        lines = [
            f"📅 日期：{state.date}",
            f"🎉 节日：{state.holiday}",
        ]
        if current_datetime is not None:
            datetime_label = "当前日期时间" if show_current else "日期时间"
            lines.append(f"🗓️ {datetime_label}：{format_datetime(current_datetime)}")
        lines.extend(
            [
                f"🕐 {time_label}：{match.entry.time}",
                f"⌛ 时段范围：{self._format_match_intervals(match) or '无'}",
                f"📋 {schedule_label}：{match.entry.schedule}",
                f"👗 {outfit_label}：{match.entry.outfit}",
            ]
        )
        for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
            value = match.entry.extra_fields.get(key)
            if value:
                lines.append(f"{spec['icon']} {spec['label']}：{value}")
        lines.append(f"⏰ 实际生成时间：{state.generated_at or '未知'}")
        return "\n".join(lines)

    def _format_full_state(
        self,
        state: LifeState,
    ) -> str:
        """格式化某一天的完整状态。

        Args:
            state: 某一天的生活状态。

        Returns:
            人类可读的完整状态。
        """
        lines = [
            f"📅 日期：{state.date}",
            f"🎉 节日：{state.holiday}",
            f"📝 今日概况：{state.schedule_summary or '无'}",
            f"🎨 今日氛围：{state.style_summary or '无'}",
            f"⏰ 实际生成时间：{state.generated_at or '未知'}",
            "",
            "📋 完整时间线：",
        ]
        for entry in state.timeline:
            intervals = resolve_entry_intervals(entry)
            interval_text = "、".join(format_interval(item) for item in intervals)
            lines.append(
                f"  [{entry.time}] {interval_text or '无法解析'} | "
                f"{entry.schedule} | 👗 {entry.outfit}"
            )
            details = []
            for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
                value = entry.extra_fields.get(key)
                if value:
                    details.append(f"{spec['icon']} {value}")
            if details:
                lines.append(f"    {' | '.join(details)}")
        return "\n".join(lines)

    # =========================
    # /dls commands
    # =========================

    @filter.command_group("dls")
    def dls(self) -> None:
        """注册动态生活状态命令组。"""
        pass

    @dls.command("show")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_show(
        self,
        event: AstrMessageEvent,
        time_query: str = "",
    ) -> AsyncGenerator[object, None]:
        """显示当前或指定查询时间的时段状态。

        Args:
            event: 当前 AstrBot 消息事件。
            time_query: 可选的自然时段名称或 HH:MM 格式的时间。

        Yields:
            AstrBot 纯文本命令结果。
        """
        time_query = time_query.strip()

        # 加载状态或调用 LLM 之前先做校验。
        if time_query:
            if time_query in NATURAL_SLOT_NAMES:
                pass
            elif ":" in time_query:
                try:
                    parse_clock_time(time_query)
                except ValueError:
                    yield event.plain_result(
                        "时间格式错误，请使用 HH:MM 格式 (00:00–23:59)。"
                    )
                    return
            else:
                allowed = "、".join(NATURAL_SLOT_NAMES)
                yield event.plain_result(
                    f"不支持的时段「{time_query}」，时段仅支持具体时间 (HH:MM) 或自然时段：{allowed}"
                )
                return

        now = self._now()

        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return

        data, target_date = self._select_current_state(now)
        if data is None:
            yield event.plain_result("今天的状态尚未生成，正在生成...")
            result = await self.generator.generate(target_date)
            if not result.succeeded or result.state is None:
                if result.auto_retries_exhausted:
                    yield event.plain_result(
                        "今日自动生成次数已达上限，请使用 /dls regen 手动重试。"
                    )
                else:
                    yield event.plain_result(
                        "状态生成失败，请稍后再试或使用 /dls regen 重试。"
                    )
                return
            data = result.state

        if not data.timeline:
            yield event.plain_result("没有可用的时段状态。")
            return

        # 无参数：使用当前本地时间。
        if not time_query:
            match = select_current_slot(data.timeline, minute_of_day(now))
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            yield event.plain_result(
                self._format_current_state(
                    data,
                    match,
                    current_datetime=now,
                )
            )
            return

        # 自然时段：按名称直接选取。
        if time_query in NATURAL_SLOT_NAMES:
            match = find_slot_by_name(data.timeline, time_query)
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            yield event.plain_result(
                self._format_current_state(
                    data,
                    match,
                    show_current=False,
                )
            )
            return

        # 精确时间：已在访问状态前校验。
        query_minute = parse_clock_time(time_query)
        query_hour, query_minute_part = divmod(query_minute, 60)
        query_datetime = datetime.datetime.combine(
            target_date,
            datetime.time(query_hour, query_minute_part),
            tzinfo=CHINA_TIMEZONE,
        )
        match = select_current_slot(data.timeline, query_minute)
        if match is None:
            yield event.plain_result("没有可用的时段状态。")
            return
        yield event.plain_result(
            self._format_current_state(
                data,
                match,
                current_datetime=query_datetime,
                show_current=False,
            )
        )

    @dls.command("full")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_full(
        self,
        event: AstrMessageEvent,
        date: str = "",
    ) -> AsyncGenerator[object, None]:
        """显示今天或某个归档日期的完整状态。

        Args:
            event: 当前 AstrBot 消息事件。
            date: 可选的自然日期，格式 YYYY-MM-DD。

        Yields:
            AstrBot 纯文本命令结果。
        """
        now = self._now()

        if date:
            date = date.strip()
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                yield event.plain_result("日期格式错误，请使用 YYYY-MM-DD 格式。")
                return
            data = self.data_mgr.get_by_date(date)
            if not data:
                yield event.plain_result(f"{date} 的状态不存在。")
                return
            if data.status == "failed":
                yield event.plain_result(f"{date} 的状态生成失败，无有效状态。")
                return
            yield event.plain_result(self._format_full_state(data))
            return

        data, target_date = self._select_current_state(now)
        if data is None:
            if self.generator.is_generating:
                yield event.plain_result("状态正在生成中，请稍后再试。")
                return
            yield event.plain_result("今天的状态尚未生成，正在生成...")
            result = await self.generator.generate(target_date)
            if not result.succeeded or result.state is None:
                if result.auto_retries_exhausted:
                    yield event.plain_result(
                        "今日自动生成次数已达上限，请使用 /dls regen 手动重试。"
                    )
                else:
                    yield event.plain_result("当前暂无有效状态。")
                return
            data = result.state

        yield event.plain_result(self._format_full_state(data))

    @dls.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_regen(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[object, None]:
        """强制重新生成今日状态。

        Args:
            event: 当前 AstrBot 消息事件，包含尾随的附加需求。

        Yields:
            生成进度和结果消息。
        """
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return

        # 保留命令分词之外的完整附加需求。
        extra = _extract_args_after(event.message_str, "dls regen")

        now = self._now()
        _, target_date = self._select_current_state(now)

        if extra:
            yield event.plain_result(f"正在根据附加要求重新生成今日状态：{extra}")
        else:
            yield event.plain_result("正在强制重新生成今日状态...")

        result = await self.generator.generate(
            target_date,
            force=True,
            extra=extra,
        )

        if not result.succeeded or result.state is None:
            if result.used_previous_state and result.state is not None:
                yield event.plain_result(
                    "重新生成失败，已保留原状态。\n\n"
                    f"{self._format_full_state(result.state)}"
                )
            else:
                yield event.plain_result("状态生成失败，请稍后再试。")
            return

        yield event.plain_result(
            f"全局生活状态重新生成完成。\n\n{self._format_full_state(result.state)}"
        )
