import datetime
import re
import zoneinfo
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
    NATURAL_SLOT_NAMES,
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
    """Extract the complete trailing text after a command.

    Args:
        message_str: Raw event message.
        command: Command prefix without the leading slash.

    Returns:
        Trailing text, or ``None`` when no extra text is present.
    """
    text = message_str.lstrip("/").strip()
    text = re.sub(r"\s+", " ", text)  # Normalize whitespace for command matching.
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
        """Initialize storage, generation, and midnight scheduling."""
        self.timezone = self._resolve_timezone()
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(self.context, self.config, self.data_mgr)
        self._start_scheduler()

    async def terminate(self) -> None:
        """Stop midnight scheduling."""
        self._stop_scheduler()

    # =========================
    # Scheduler
    # =========================

    def _resolve_timezone(self) -> zoneinfo.ZoneInfo:
        """Resolve the configured timezone with a safe default.

        Returns:
            Configured IANA timezone or Asia/Shanghai.
        """
        tz_setting = str(self.config.get("timezone") or "Asia/Shanghai").strip()
        try:
            return zoneinfo.ZoneInfo(tz_setting)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            logger.error(
                f"[DynamicLifeState] Invalid timezone {tz_setting!r}; "
                "falling back to Asia/Shanghai"
            )
            return zoneinfo.ZoneInfo("Asia/Shanghai")

    def _now(self) -> datetime.datetime:
        """Return the current time in the configured timezone."""
        return datetime.datetime.now(self.timezone)

    def _current_date(
        self,
        now: datetime.datetime | None = None,
    ) -> datetime.date:
        """Return the local date in the configured timezone.

        Args:
            now: Optional time supplied by a caller or test.

        Returns:
            Local date.
        """
        value = now or self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        else:
            value = value.astimezone(self.timezone)
        return value.date()

    def _select_current_state(
        self,
        now: datetime.datetime,
    ) -> tuple[LifeState | None, datetime.date]:
        """Select a successful state for the configured local date.

        Args:
            now: Current date and time.

        Returns:
            Matching state, if any, and the captured target date.
        """
        target_date = self._current_date(now)
        state = self.data_mgr.get_current(
            target_date.isoformat(),
            self.timezone.key,
        )
        if state is None or state.status != "ok":
            return None, target_date
        return state, target_date

    def _start_scheduler(self) -> None:
        """Start the fixed local-midnight scheduler."""
        try:
            self._scheduler = AsyncIOScheduler(
                timezone=self.timezone,
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
                f"at 00:00 in {self.timezone.key}"
            )
        except Exception as e:
            logger.error(f"[DynamicLifeState] Scheduler startup failed: {e}")

    def _stop_scheduler(self) -> None:
        """Stop the scheduler when it is running."""
        try:
            if hasattr(self, "_scheduler") and self._scheduler.running:
                self._scheduler.shutdown()
        except Exception:
            pass

    async def _daily_generate(self) -> None:
        """Generate the configured timezone's current date state."""
        now = self._now()
        data, target_date = self._select_current_state(now)
        if data:
            logger.info(
                f"[DynamicLifeState] State for {target_date.isoformat()} "
                "already exists; skipping generation"
            )
            return
        await self.generator.generate(target_date, self.timezone)

    # =========================
    # LLM request hook
    # =========================

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Inject the current time slot before each enabled LLM request.

        Args:
            event: Incoming AstrBot message event.
            req: Mutable provider request.
        """

        # Session filter
        if not self._is_session_enabled(event.unified_msg_origin):
            return

        now = self._now()
        data, target_date = self._select_current_state(now)
        if data is None:
            if self.generator.is_generating:
                return  # Skip injection while the shared generation task runs.
            data = await self.generator.generate(target_date, self.timezone)

        if not data or data.status == "failed":
            return

        # Select the current slot.
        current_match = select_current_slot(data.timeline, minute_of_day(now))

        # Resolve the configured injection method.
        injection_method = str(
            self.config.get("injection_mode", "extra_user_content_parts")
        )
        injection_method = self._resolve_injection_method(req, injection_method)

        # Remove a previous fake injection.
        self._cleanup_previous_injection(req)

        # Inject the current state.
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
        """Return whether life-state injection is enabled for a session.

        Args:
            unified_msg_origin: AstrBot unified message origin.

        Returns:
            Whether the session passes the configured list mode.
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
        """Fall back when a provider cannot accept fake tool calls.

        Args:
            req: Provider request used to resolve the active provider.
            configured: Configured injection method.

        Returns:
            Compatible injection method.
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
        """Remove an earlier fake tool-call injection.

        Args:
            req: Mutable provider request.
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

        date 参数为可选，格式 YYYY-MM-DD，表示配置时区下的日期。
        不传则返回今天的状态。
        仅在用户明确询问"今天一整天在做什么""全天状态""完整日程""其他时间段的安排""回顾某天的状态"或类似问题时调用。
        普通日常对话中不要调用此工具，LLM 请求时已有的当前时段状态已足够回答问题。

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
        """Format all intervals exposed by a slot match.

        Args:
            match: Matched timeline slot.

        Returns:
            Human-readable interval list.
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
        """Format a current or queried time-slot state.

        Args:
            state: Life state for one date.
            match: Selected time slot.
            current_datetime: Optional clock time to display.
            show_current: Whether labels should describe the current moment.

        Returns:
            Human-readable state text.
        """
        time_label = "当前时段" if show_current else "时段"
        schedule_label = "当前安排" if show_current else "安排"
        outfit_label = "当前穿搭" if show_current else "穿搭"
        lines = [
            f"📅 日期：{state.date}",
            f"🌐 时区：{state.timezone}",
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
        if match.entry.mood:
            lines.append(f"🙂 心情：{match.entry.mood}")
        if match.entry.location:
            lines.append(f"📍 地点：{match.entry.location}")
        if match.entry.note:
            lines.append(f"🗒️ 备注：{match.entry.note}")
        lines.append(f"⏰ 实际生成时间：{state.generated_at or '未知'}")
        return "\n".join(lines)

    def _format_full_state(
        self,
        state: LifeState,
    ) -> str:
        """Format the complete state for one date.

        Args:
            state: Life state for one date.

        Returns:
            Human-readable complete state.
        """
        lines = [
            f"📅 日期：{state.date}",
            f"🌐 时区：{state.timezone}",
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
            if entry.mood:
                details.append(f"🙂 {entry.mood}")
            if entry.location:
                details.append(f"📍 {entry.location}")
            if entry.note:
                details.append(f"🗒️ {entry.note}")
            if details:
                lines.append(f"    {' | '.join(details)}")
        return "\n".join(lines)

    # =========================
    # /dls commands
    # =========================

    @filter.command_group("dls")
    def dls(self) -> None:
        """Register the dynamic life-state command group."""
        pass

    @dls.command("show")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_show(
        self,
        event: AstrMessageEvent,
        time_query: str = "",
    ) -> AsyncGenerator[object, None]:
        """Show the current or a queried time-slot state.

        Args:
            event: Current AstrBot message event.
            time_query: Optional natural slot name or HH:MM clock time.

        Yields:
            AstrBot plain-text command results.
        """
        time_query = time_query.strip()

        # Validate before loading state or invoking the LLM.
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
            data = await self.generator.generate(target_date, self.timezone)

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试或使用 /dls regen 重试。")
            return

        if not data.timeline:
            yield event.plain_result("没有可用的时段状态。")
            return

        # No argument: use the current local time.
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

        # Natural slot: select the named slot directly.
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

        # Explicit time: already validated before state access.
        query_minute = parse_clock_time(time_query)
        query_hour, query_minute_part = divmod(query_minute, 60)
        query_datetime = datetime.datetime.combine(
            target_date,
            datetime.time(query_hour, query_minute_part),
            tzinfo=self.timezone,
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
        """Show today's or an archived date's complete state.

        Args:
            event: Current AstrBot message event.
            date: Optional natural date in YYYY-MM-DD format.

        Yields:
            AstrBot plain-text command results.
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
            data = await self.generator.generate(target_date, self.timezone)

        if not data or data.status == "failed":
            yield event.plain_result("当前暂无有效状态。")
            return

        yield event.plain_result(self._format_full_state(data))

    @dls.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_regen(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[object, None]:
        """Force regeneration of today's state.

        Args:
            event: Current AstrBot message event, including trailing requirements.

        Yields:
            Generation progress and result messages.
        """
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return

        # Preserve the full trailing requirement beyond command tokenization.
        extra = _extract_args_after(event.message_str, "dls regen")

        now = self._now()
        _, target_date = self._select_current_state(now)

        if extra:
            yield event.plain_result(f"正在根据附加要求重新生成今日状态：{extra}")
        else:
            yield event.plain_result("正在强制重新生成今日状态...")

        data = await self.generator.generate(
            target_date,
            self.timezone,
            force=True,
            extra=extra,
        )

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试。")
            return

        yield event.plain_result(
            f"全局生活状态重新生成完成。\n\n{self._format_full_state(data)}"
        )
