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
    MAX_HISTORY_DAYS,
    DataManager,
    parse_generate_time,
    resolve_business_now,
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
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(self.context, self.config, self.data_mgr)
        self._start_scheduler()

    async def terminate(self):
        self._stop_scheduler()

    # =========================
    # Scheduler
    # =========================

    def _start_scheduler(self):
        try:
            generate_time = str(self.config.get("generate_time", "07:00"))
            hour, minute = parse_generate_time(generate_time)
            tz_setting = self.context.get_config().get("timezone")
            tz = (
                zoneinfo.ZoneInfo(tz_setting)
                if tz_setting
                else zoneinfo.ZoneInfo("Asia/Shanghai")
            )

            self._scheduler = AsyncIOScheduler(
                timezone=tz,
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
                hour=hour,
                minute=minute,
                id="dynamic_life_state_daily",
            )
            self._scheduler.start()
            logger.info(f"[DynamicLifeState] 调度器已启动，每日 {generate_time} 生成")
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
        business_now = resolve_business_now(self.config.get("generate_time"))
        date_str = business_now.strftime("%Y-%m-%d")
        if self.data_mgr.has(date_str) and self.data_mgr.get(date_str).status == "ok":
            logger.info(f"[DynamicLifeState] {date_str} 已有有效状态，跳过生成")
            return
        await self.generator.generate(business_now)

    # =========================
    # LLM Request Hook
    # =========================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在每次 LLM 请求前注入当前时段的生活状态。"""

        # 会话过滤
        if not self._is_session_enabled(event.unified_msg_origin):
            return

        business_now = resolve_business_now(self.config.get("generate_time"))
        today_str = business_now.strftime("%Y-%m-%d")

        # 懒生成（数据不存在或上次生成失败时触发）
        data = self.data_mgr.get(today_str)
        if not data or data.status == "failed":
            if self.generator.is_generating:
                return  # 已有生成任务在跑，本轮跳过
            data = await self.generator.generate(business_now)

        if not data or data.status == "failed":
            return

        # 选择当前时段
        current_entry = select_current_slot(data.timeline)

        # 解析注入方式
        injection_method = str(
            self.config.get("injection_mode", "extra_user_content_parts")
        )
        injection_method = self._resolve_injection_method(req, injection_method)

        # 清理上次注入残留
        self._cleanup_previous_injection(req)

        # 执行注入
        if injection_method == "extra_user_content_parts":
            inject_text = build_injection_text(data, current_entry)
            req.extra_user_content_parts.append(
                TextPart(text=inject_text).mark_as_temp()
            )
            if self.config.get("debug_mode", False):
                logger.debug(f"[DynamicLifeState] 注入内容:\n{inject_text}")

        elif injection_method == "fake_tool_call":
            fake_messages = build_fake_tool_call(data, current_entry)
            req.contexts.extend(fake_messages)
            if self.config.get("debug_mode", False):
                logger.debug(
                    f"[DynamicLifeState] 注入内容(fake_tool_call):\n"
                    f"{fake_messages[1]['content']}"
                )

        if self.config.get("debug_mode", False):
            logger.info(
                f"[DynamicLifeState] 注入方式={injection_method}, "
                f"日期={today_str}, "
                f"时段={current_entry.time if current_entry else 'N/A'}"
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
        """获取 Bot 今日或指定日期的完整生活状态，包括全天概况、氛围和完整时间线。

        date 参数为可选，格式 YYYY-MM-DD，不传则返回今日状态。
        仅在用户明确询问"今天一整天在做什么""全天状态""完整日程""其他时间段的安排""回顾某天的状态"或类似问题时调用。
        普通日常对话中不要调用此工具，LLM 请求时已有的当前时段状态已足够回答问题。
        """
        if not self._is_session_enabled(event.unified_msg_origin):
            return "当前会话未启用动态生活状态功能。"

        if self.generator.is_generating:
            return "生活状态正在生成中，请稍后再试。"

        # 确定查询日期
        if date:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                return f"日期格式错误，请使用 YYYY-MM-DD 格式，例如 2026-07-01。"
            target_str = date
        else:
            business_now = resolve_business_now(self.config.get("generate_time"))
            target_str = business_now.strftime("%Y-%m-%d")

        data = self.data_mgr.get(target_str)
        if not data:
            return f"{target_str} 的生活状态尚未生成。"
        if data.status == "failed":
            return f"{target_str} 的生活状态生成失败。"

        return self._format_full_state(data)

    # =========================
    # 状态格式化
    # =========================

    def _format_current_state(self, state) -> str:
        entry = select_current_slot(state.timeline)
        return (
            f"📅 {state.date}\n"
            f"🕐 当前时段：{entry.time if entry else '无'}\n"
            f"📋 当前安排：{entry.schedule if entry else '无'}\n"
            f"👗 当前穿搭：{entry.outfit if entry else '无'}\n"
            f"⏰ 生成时间：{state.generated_at[:19] if state.generated_at else '未知'}"
        )

    def _format_full_state(self, state) -> str:
        lines = [
            f"📅 {state.date}",
            f"📝 今日概况：{state.schedule_summary or '无'}",
            f"🎨 今日氛围：{state.style_summary or '无'}",
            f"⏰ 生成时间：{state.generated_at[:19] if state.generated_at else '未知'}",
            "",
            "📋 完整时间线：",
        ]
        for e in state.timeline:
            lines.append(f"  [{e.time}] {e.schedule} | 👗 {e.outfit}")
        return "\n".join(lines)

    # =========================
    # 命令：/dls
    # =========================

    @filter.command_group("dls")
    def dls(self):
        pass

    @dls.command("show")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_show(self, event: AstrMessageEvent):
        """查看当前时段状态。"""
        business_now = resolve_business_now(self.config.get("generate_time"))
        today_str = business_now.strftime("%Y-%m-%d")

        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return

        data = self.data_mgr.get(today_str)
        if not data or data.status == "failed":
            yield event.plain_result("今日状态尚未生成，正在生成...")
            data = await self.generator.generate(business_now)

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试或使用 /dls regen 重试。")
            return

        yield event.plain_result(self._format_current_state(data))

    @dls.command("full")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_full(self, event: AstrMessageEvent):
        """查看今日完整状态。"""
        business_now = resolve_business_now(self.config.get("generate_time"))
        today_str = business_now.strftime("%Y-%m-%d")

        data = self.data_mgr.get(today_str)
        if not data:
            if self.generator.is_generating:
                yield event.plain_result("状态正在生成中，请稍后再试。")
                return
            yield event.plain_result("今日状态尚未生成，正在生成...")
            data = await self.generator.generate(business_now)

        if not data or data.status == "failed":
            yield event.plain_result("今日暂无有效状态。")
            return

        yield event.plain_result(self._format_full_state(data))

    @dls.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_regen(self, event: AstrMessageEvent):
        """强制重新生成今日状态，可附加额外要求。"""
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return

        # 从消息中手动提取额外要求，避免命令解析按空格截断
        extra = _extract_args_after(event.message_str, "dls regen")

        business_now = resolve_business_now(self.config.get("generate_time"))

        if extra:
            yield event.plain_result(f"正在根据附加要求重新生成今日状态：{extra}")
        else:
            yield event.plain_result("正在强制重新生成今日状态...")

        data = await self.generator.generate(business_now, force=True, extra=extra)

        if not data or data.status == "failed":
            yield event.plain_result("状态生成失败，请稍后再试。")
            return

        yield event.plain_result(
            f"重新生成完成。\n\n{self._format_current_state(data)}"
        )
