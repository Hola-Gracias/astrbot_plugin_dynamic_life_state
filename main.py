import datetime
import re
import zoneinfo

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
    DEFAULT_GENERATE_TIME,
    NATURAL_SLOT_NAMES,
    BusinessCycle,
    DataManager,
    LifeState,
    SlotMatch,
    build_business_cycle,
    find_slot_by_name,
    format_cycle,
    format_datetime,
    format_interval,
    parse_generate_time,
    resolve_business_cycle,
    resolve_entry_intervals,
    resolve_time_in_cycle,
    select_current_slot,
)


def _extract_args_after(message_str: str, command: str) -> str | None:
    """从消息中提取指定命令之后的整段剩余文本。

    避免 AstrBot 命令解析按空格截断参数。
    """
    text = message_str.lstrip("/").strip()
    text = re.sub(r"\s+", " ", text)  # 归一化空白，对齐 AstrBot 命令匹配
    prefix = f"{command} "
    idx = text.find(prefix)
    if idx == -1:
        return None
    remainder = text[idx + len(prefix) :].strip()
    return remainder or None


class DynamicLifeStatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.state_file = self.data_dir / "life_state.json"

    async def initialize(self):
        self.timezone = self._resolve_timezone()
        self.generate_time = self._resolve_generate_time()
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(self.context, self.config, self.data_mgr)
        self._start_scheduler()

    async def terminate(self):
        self._stop_scheduler()

    # =========================
    # 调度器
    # =========================

    def _resolve_timezone(self) -> zoneinfo.ZoneInfo:
        tz_setting = str(self.context.get_config().get("timezone") or "Asia/Shanghai")
        try:
            return zoneinfo.ZoneInfo(tz_setting)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            logger.error(
                f"[DynamicLifeState] 无效时区 {tz_setting!r}，回退为 Asia/Shanghai"
            )
            return zoneinfo.ZoneInfo("Asia/Shanghai")

    def _resolve_generate_time(self) -> str:
        configured = str(
            self.config.get("generate_time", DEFAULT_GENERATE_TIME)
        ).strip()
        try:
            parsed = parse_generate_time(configured)
        except ValueError as exc:
            logger.error(
                f"[DynamicLifeState] 无效 generate_time {configured!r}: {exc}; "
                f"回退为 {DEFAULT_GENERATE_TIME}"
            )
            return DEFAULT_GENERATE_TIME
        return parsed.strftime("%H:%M")

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(self.timezone)

    def _current_cycle(
        self,
        now: datetime.datetime | None = None,
    ) -> BusinessCycle:
        return resolve_business_cycle(
            self.generate_time,
            self.timezone,
            now or self._now(),
        )

    def _cycle_for_date(self, business_date: str) -> BusinessCycle:
        return build_business_cycle(
            business_date,
            self.generate_time,
            self.timezone,
        )

    def _start_scheduler(self):
        try:
            boundary_time = parse_generate_time(self.generate_time)

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
                hour=boundary_time.hour,
                minute=boundary_time.minute,
                id="dynamic_life_state_daily",
            )
            self._scheduler.start()
            logger.info(
                f"[DynamicLifeState] 调度器已启动，每日 {self.generate_time} 生成"
            )
        except Exception as e:
            logger.error(f"[DynamicLifeState] 调度器启动失败: {e}")

    def _stop_scheduler(self):
        try:
            if hasattr(self, "_scheduler") and self._scheduler.running:
                self._scheduler.shutdown()
        except Exception:
            pass

    async def _daily_generate(self):
        """每日定时生成任务。"""
        now = self._now()
        cycle = self._current_cycle(now)
        business_date = cycle.business_date.isoformat()
        data = self.data_mgr.get(business_date)
        if data and data.status == "ok":
            logger.info(
                f"[DynamicLifeState] 业务日期 {business_date} 已有有效状态，跳过生成"
            )
            return
        await self.generator.generate(cycle)

    # =========================
    # LLM 请求钩子
    # =========================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在每次 LLM 请求前注入当前时段的生活状态。"""

        # 会话过滤
        if not self._is_session_enabled(event.unified_msg_origin):
            return

        now = self._now()
        cycle = self._current_cycle(now)
        business_date = cycle.business_date.isoformat()

        # 懒生成（数据不存在或上次生成失败时触发）
        data = self.data_mgr.get(business_date)
        if not data or data.status == "failed":
            if self.generator.is_generating:
                return  # 已有生成任务在跑，本轮跳过
            data = await self.generator.generate(cycle)

        if not data or data.status == "failed":
            return

        # 选择当前时段
        current_match = select_current_slot(data.timeline, cycle, now)

        # 解析注入方式
        injection_method = str(
            self.config.get("injection_mode", "extra_user_content_parts")
        )
        injection_method = self._resolve_injection_method(req, injection_method)

        # 清理上次注入残留
        self._cleanup_previous_injection(req)

        # 执行注入
        if injection_method == "extra_user_content_parts":
            inject_text = build_injection_text(data, cycle, current_match, now)
            req.extra_user_content_parts.append(
                TextPart(text=inject_text).mark_as_temp()
            )
            if self.config.get("debug_mode", False):
                logger.debug(f"[DynamicLifeState] 注入内容:\n{inject_text}")

        elif injection_method == "fake_tool_call":
            fake_messages = build_fake_tool_call(data, cycle, current_match, now)
            req.contexts.extend(fake_messages)
            if self.config.get("debug_mode", False):
                logger.debug(
                    f"[DynamicLifeState] 注入内容(fake_tool_call):\n"
                    f"{fake_messages[1]['content']}"
                )

        if self.config.get("debug_mode", False):
            logger.info(
                f"[DynamicLifeState] 注入方式={injection_method}, "
                f"业务日期={business_date}, "
                f"时段={current_match.entry.time if current_match else 'N/A'}"
            )

    # =========================
    # 会话过滤
    # =========================

    def _is_session_enabled(self, unified_msg_origin: str) -> bool:
        mode = str(self.config.get("session_list_mode", "none")).strip()
        session_list: list[str] = list(self.config.get("session_list", []) or [])

        if mode == "none":
            return True
        if mode == "whitelist":
            return unified_msg_origin in session_list
        if mode == "blacklist":
            return unified_msg_origin not in session_list
        return True

    # =========================
    # 注入方式降级
    # =========================

    def _resolve_injection_method(self, req: ProviderRequest, configured: str) -> str:
        """对不兼容的 Provider 做降级。Gemini 不支持 fake_tool_call。"""
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
                    "[DynamicLifeState] Gemini 不支持 fake_tool_call，降级为 extra_user_content_parts"
                )
                return "extra_user_content_parts"
        except Exception:
            pass
        return configured

    # =========================
    # 注入残留清理
    # =========================

    def _cleanup_previous_injection(self, req: ProviderRequest):
        remove_fake_tool_call_from_context(req.contexts)

    # =========================
    # LLM Tool：按需查询完整状态
    # =========================

    @filter.llm_tool(name="get_full_dynamic_life_state")
    async def get_full_dynamic_life_state(
        self, event: AstrMessageEvent, date: str = ""
    ):
        """获取 Bot 当前或指定业务日期的完整生活状态。

        date 参数为可选，格式 YYYY-MM-DD，含义为业务日期。
        不传则返回当前业务周期状态。
        仅在用户明确询问"今天一整天在做什么""全天状态""完整日程""其他时间段的安排""回顾某天的状态"或类似问题时调用。
        普通日常对话中不要调用此工具，LLM 请求时已有的当前时段状态已足够回答问题。
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
            cycle = self._cycle_for_date(target_str)
        else:
            cycle = self._current_cycle(now)
            target_str = cycle.business_date.isoformat()

        data = self.data_mgr.get(target_str)
        if not data:
            return f"{target_str} 的生活状态尚未生成。"
        if data.status == "failed":
            return f"{target_str} 的生活状态生成失败。"

        return self._format_full_state(data, cycle)

    # =========================
    # 状态格式化
    # =========================

    @staticmethod
    def _format_match_intervals(match: SlotMatch) -> str:
        return "、".join(format_interval(interval) for interval in match.intervals)

    def _format_current_state(
        self,
        state: LifeState,
        cycle: BusinessCycle,
        match: SlotMatch,
        *,
        natural_datetime: datetime.datetime | None = None,
        show_current: bool = True,
    ) -> str:
        time_label = "当前时段" if show_current else "时段"
        schedule_label = "当前安排" if show_current else "安排"
        outfit_label = "当前穿搭" if show_current else "穿搭"
        lines = [
            f"📅 业务日期：{state.business_date}",
            f"🔁 状态周期：{format_cycle(cycle)}",
        ]
        if natural_datetime is not None:
            natural_label = "当前自然日期时间" if show_current else "自然日期时间"
            lines.append(f"🗓️ {natural_label}：{format_datetime(natural_datetime)}")
        lines.extend(
            [
                f"🕐 {time_label}：{match.entry.time}",
                f"⌛ 时段范围：{self._format_match_intervals(match) or '无'}",
                f"📋 {schedule_label}：{match.entry.schedule}",
                f"👗 {outfit_label}：{match.entry.outfit}",
                f"⏰ 实际生成时间：{state.generated_at or '未知'}",
            ]
        )
        return "\n".join(lines)

    def _format_full_state(
        self,
        state: LifeState,
        cycle: BusinessCycle,
    ) -> str:
        lines = [
            f"📅 业务日期：{state.business_date}",
            f"🔁 状态周期：{format_cycle(cycle)}",
            f"📝 周期概况：{state.schedule_summary or '无'}",
            f"🎨 周期氛围：{state.style_summary or '无'}",
            f"⏰ 实际生成时间：{state.generated_at or '未知'}",
            "",
            "📋 完整时间线：",
        ]
        for entry in state.timeline:
            intervals = resolve_entry_intervals(entry, cycle)
            interval_text = "、".join(format_interval(item) for item in intervals)
            lines.append(
                f"  [{entry.time}] {interval_text or '无法解析'} | "
                f"{entry.schedule} | 👗 {entry.outfit}"
            )
        return "\n".join(lines)

    # =========================
    # 命令：/dls
    # =========================

    @filter.command_group("dls")
    def dls(self):
        pass

    @dls.command("show")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_show(self, event: AstrMessageEvent, time_query: str = ""):
        """查看当前或指定时段状态。"""
        time_query = time_query.strip()

        # 在加载/生成数据前校验，避免非法输入触发 LLM 调用
        if time_query:
            if time_query in NATURAL_SLOT_NAMES:
                pass
            elif ":" in time_query:
                if not re.fullmatch(r"\d{2}:\d{2}", time_query):
                    yield event.plain_result(
                        "时间格式错误，请使用 HH:MM 格式 (00:00–23:59)。"
                    )
                    return
                try:
                    hour, minute = map(int, time_query.split(":"))
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError
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
        cycle = self._current_cycle(now)
        business_date = cycle.business_date.isoformat()

        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return

        data = self.data_mgr.get(business_date)
        if not data or data.status == "failed":
            yield event.plain_result("当前业务周期状态尚未生成，正在生成...")
            data = await self.generator.generate(cycle)

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试或使用 /dls regen 重试。")
            return

        if not data.timeline:
            yield event.plain_result("没有可用的时段状态。")
            return

        # 无参数：当前时间
        if not time_query:
            match = select_current_slot(data.timeline, cycle, now)
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            yield event.plain_result(
                self._format_current_state(
                    data,
                    cycle,
                    match,
                    natural_datetime=now,
                )
            )
            return

        # 自然时段：直接选择该时段
        if time_query in NATURAL_SLOT_NAMES:
            match = find_slot_by_name(data.timeline, time_query, cycle)
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            yield event.plain_result(
                self._format_current_state(
                    data,
                    cycle,
                    match,
                    show_current=False,
                )
            )
            return

        # 具体时间：已在数据加载前校验
        query_datetime = resolve_time_in_cycle(cycle, time_query)
        match = select_current_slot(data.timeline, cycle, query_datetime)
        if match is None:
            yield event.plain_result("没有可用的时段状态。")
            return
        yield event.plain_result(
            self._format_current_state(
                data,
                cycle,
                match,
                natural_datetime=query_datetime,
                show_current=False,
            )
        )

    @dls.command("full")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_full(self, event: AstrMessageEvent, date: str = ""):
        """查看今日或指定日期的完整状态。"""
        now = self._now()
        current_cycle = self._current_cycle(now)
        current_business_date = current_cycle.business_date.isoformat()

        if date:
            date = date.strip()
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                yield event.plain_result("日期格式错误，请使用 YYYY-MM-DD 格式。")
                return
            target_str = date
        else:
            target_str = current_business_date

        # 当前业务日期：保留懒生成行为
        if target_str == current_business_date:
            cycle = current_cycle
            data = self.data_mgr.get(target_str)
            if not data:
                if self.generator.is_generating:
                    yield event.plain_result("状态正在生成中，请稍后再试。")
                    return
                yield event.plain_result("当前业务周期状态尚未生成，正在生成...")
                data = await self.generator.generate(cycle)

            if not data or data.status == "failed":
                yield event.plain_result("今日暂无有效状态。")
                return

            yield event.plain_result(self._format_full_state(data, cycle))
        else:
            cycle = self._cycle_for_date(target_str)
            data = self.data_mgr.get(target_str)
            if not data:
                yield event.plain_result(f"{target_str} 的状态不存在。")
                return
            if data.status == "failed":
                yield event.plain_result(f"{target_str} 的状态生成失败，无有效状态。")
                return

            yield event.plain_result(self._format_full_state(data, cycle))

    @dls.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_regen(self, event: AstrMessageEvent):
        """强制重新生成今日状态，可附加额外要求。"""
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return

        # 从消息中手动提取额外要求，避免命令解析按空格截断
        extra = _extract_args_after(event.message_str, "dls regen")

        now = self._now()
        cycle = self._current_cycle(now)

        if extra:
            yield event.plain_result(f"正在根据附加要求重新生成今日状态：{extra}")
        else:
            yield event.plain_result("正在强制重新生成今日状态...")

        data = await self.generator.generate(cycle, force=True, extra=extra)

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试。")
            return

        yield event.plain_result(
            f"全局生活状态重新生成完成。\n\n{self._format_full_state(data, cycle)}"
        )
