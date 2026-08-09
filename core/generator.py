import asyncio
import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path

import holidays

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .state import (
    CHINA_TIMEZONE,
    MAX_HISTORY_DAYS,
    OPTIONAL_TIMELINE_FIELDS,
    DataManager,
    LifeState,
    TimelineEntry,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"
_LEGACY_PROMPT_PLACEHOLDERS = (
    "{business_date}",
    "{cycle_start}",
    "{cycle_end}",
    "{timezone}",
)

# 每个自然日自动生成（非 force）失败后最多重试的次数。达到上限后自动
# 路径不再调用 LLM，只能通过 /dls regen 手动重试。计数保存在内存中，
# 插件重载（例如修改配置触发 AstrBot 重载插件）后自动清零。
MAX_AUTO_RETRIES_PER_DAY = 3


def _render_template(template: str, **kwargs: str) -> str:
    """替换已知占位符，同时保留其他花括号。

    Args:
        template: 提示词模板。
        **kwargs: 支持的占位符值。

    Returns:
        渲染后的提示词。
    """
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    """从插件配置 schema 加载默认提示词。

    Returns:
        默认提示词模板。

    Raises:
        ValueError: schema 中不包含可用的默认提示词。
    """
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    template = schema.get("prompt_template", {}).get("default")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("_conf_schema.json 中缺少有效的 prompt_template.default")
    return template


@dataclass(slots=True)
class GenerationResult:
    """单次生成尝试的结果。

    ``state`` 是目标日期的可用状态：成功时为新生成的状态，强制重新生成
    失败时为保留的原成功状态。``error`` 携带失败原因，``used_previous_state``
    告知调用方返回的状态是保留的旧状态而非新生成的。

    ``auto_retries_exhausted`` 标记一次不经过 LLM 调用的拒绝：每日自动
    生成次数已达上限，只能通过 ``/dls regen`` 手动重试。
    """

    succeeded: bool
    state: LifeState | None
    error: str | None
    used_previous_state: bool = False
    auto_retries_exhausted: bool = False


class Generator:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: DataManager,
    ) -> None:
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._gen_lock = asyncio.Lock()
        # 按日期累计的非 force 生成失败次数；仅在内存中，插件重载后清零。
        self._auto_failures: dict[str, int] = {}

    @property
    def is_generating(self) -> bool:
        """返回是否有生成调用当前持有共享锁。"""
        return self._gen_lock.locked()

    def _get_prompt_template(self) -> str:
        """返回受支持的自定义提示词或 schema 中的新默认值。

        Returns:
            不含旧版占位符的提示词模板。
        """
        configured = self.config.get("prompt_template", "")
        if isinstance(configured, str) and configured.strip():
            if any(
                placeholder in configured for placeholder in _LEGACY_PROMPT_PLACEHOLDERS
            ):
                logger.warning(
                    "[DynamicLifeState] Legacy prompt placeholders detected; "
                    "using the current default prompt until the custom template "
                    "is updated"
                )
                return _load_default_prompt_template()
            return configured
        logger.warning(
            "[DynamicLifeState] prompt_template is empty; using the schema default"
        )
        return _load_default_prompt_template()

    @staticmethod
    def _format_history(states: list[LifeState]) -> str:
        """为生成提示词渲染历史状态。

        Args:
            states: 状态列表，从最新到最旧排列。

        Returns:
            历史日程文本；没有可用状态时返回 ``无``。
        """
        if not states:
            return "无"

        sections: list[str] = []
        for state in states:
            lines = [
                f"[{state.date}]",
                f"整体日程：{state.schedule_summary or '无'}",
                f"穿搭风格：{state.style_summary or '无'}",
                "时间线：",
            ]
            for entry in state.timeline:
                lines.extend(
                    [
                        f"- {entry.time}：{entry.schedule or '无'}",
                        f"  - 穿搭：{entry.outfit or '无'}",
                    ]
                )
                for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
                    value = entry.extra_fields.get(key)
                    if value:
                        lines.append(f"  - {spec['label']}：{value}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    async def generate(
        self,
        target_date: datetime.date,
        force: bool = False,
        extra: str | None = None,
    ) -> GenerationResult:
        """生成并持久化一个自然日期的生活状态。

        Args:
            target_date: 生成调用方捕获的自然日期。
            force: 是否替换已有的成功状态。
            extra: 可选的用户附加需求，用于强制重新生成。

        Returns:
            生成结果。失败时先前成功的状态会保留并随 ``used_previous_state``
            返回；没有可用先前状态的失败则返回 ``state=None``，且不会用
            失败标记覆盖已有存储。
        """
        async with self._gen_lock:
            date = target_date.isoformat()

            # 获取锁后再次检查，对并发的调用者去重。
            if not force:
                existing = self.data_mgr.get_current(date)
                if existing and existing.status == "ok":
                    return GenerationResult(succeeded=True, state=existing, error=None)

                if self._auto_failures.get(date, 0) >= MAX_AUTO_RETRIES_PER_DAY:
                    logger.warning(
                        f"[DynamicLifeState] Automatic generation for {date} "
                        f"reached {MAX_AUTO_RETRIES_PER_DAY} failed attempts; "
                        "use /dls regen for a manual retry"
                    )
                    return GenerationResult(
                        succeeded=False,
                        state=None,
                        error="Automatic generation attempts exhausted",
                        auto_retries_exhausted=True,
                    )

            debug = bool(self.config.get("debug_mode", False))

            try:
                logger.info(
                    f"[DynamicLifeState] Generating life state for {date} "
                    f"in {CHINA_TIMEZONE.key}"
                )

                self.data_mgr.archive_before_generation(date)
                try:
                    history_days = int(self.config.get("history_reference_days", 3))
                except (TypeError, ValueError):
                    history_days = 3
                history_days = max(0, min(history_days, MAX_HISTORY_DAYS))
                history_states = self.data_mgr.get_recent_history(
                    date,
                    history_days,
                )
                history_text = self._format_history(history_states)
                extra_text = (extra or "").strip() or "无"
                holiday = "无"
                try:
                    holiday_calendar = holidays.country_holidays(
                        "CN",
                        years=target_date.year,
                        observed=False,
                        language="zh_CN",
                    )
                    holiday = (
                        str(holiday_calendar.get(target_date) or "无").strip() or "无"
                    )
                except Exception as e:
                    logger.warning(
                        f"[DynamicLifeState] Failed to resolve holiday for "
                        f"country='CN', date={date}: {e}; using no holiday"
                    )

            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] Generation preparation failed for "
                    f"{date}; preserving the current state: {e}"
                )
                return self._failure_result(
                    date,
                    e,
                    count_auto_failure=not force,
                )

            try:
                persona = await self._get_persona()
                prompt = _render_template(
                    self._get_prompt_template(),
                    date=date,
                    holiday=holiday,
                    persona=persona,
                    history_states=history_text,
                    extra_requirements=extra_text,
                    timeline_optional_fields="、".join(
                        f"{name}（{spec['label']}）"
                        for name, spec in OPTIONAL_TIMELINE_FIELDS.items()
                    ),
                )

                if debug:
                    logger.info(f"[DynamicLifeState] Generation prompt:\n{prompt}")

                provider = await self._get_provider()
                if not provider:
                    raise RuntimeError("No LLM provider is available")

                sid = f"dynamic_life_state_gen_{date}"
                resp = await provider.text_chat(prompt, session_id=sid)
                text = self._extract_completion_text(resp)

                if debug:
                    logger.info(f"[DynamicLifeState] Raw model response:\n{text}")

                payload = self._extract_json(text)
                generated_at = datetime.datetime.now(CHINA_TIMEZONE).isoformat()
                state = self._validate_and_build(
                    payload,
                    target_date,
                    generated_at,
                    holiday,
                )

            except Exception as e:
                logger.error(f"[DynamicLifeState] Generation failed for {date}: {e}")
                return self._failure_result(
                    date,
                    e,
                    count_auto_failure=not force,
                )

            try:
                self.data_mgr.set(state)
            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] Failed to persist generated state "
                    f"for {date}: {e}"
                )
                return self._failure_result(
                    date,
                    e,
                    count_auto_failure=not force,
                )

            if debug:
                logger.info(
                    f"[DynamicLifeState] Parsed state:\n"
                    f"{json.dumps(state.to_dict(), ensure_ascii=False, indent=2)}"
                )

            logger.info(
                f"[DynamicLifeState] Life state generated successfully for {date}"
            )
            return GenerationResult(succeeded=True, state=state, error=None)

    def _failure_result(
        self,
        date: str,
        error: Exception,
        *,
        count_auto_failure: bool = False,
    ) -> GenerationResult:
        """构建失败结果，同时保留先前成功的状态。

        Args:
            date: 目标日期，格式 YYYY-MM-DD。
            error: 抛出的失败异常。
            count_auto_failure: 此次失败是否计入每日自动生成次数上限。
                强制生成不计入。

        Returns:
            失败结果；当前成功状态保留时 ``used_previous_state`` 为 True。
        """
        if count_auto_failure:
            self._auto_failures[date] = self._auto_failures.get(date, 0) + 1
        previous = self.data_mgr.get_current(date)
        if previous and previous.status == "ok":
            return GenerationResult(
                succeeded=False,
                state=previous,
                error=str(error),
                used_previous_state=True,
            )
        return GenerationResult(succeeded=False, state=None, error=str(error))

    # ---------- persona ----------

    @staticmethod
    def _extract_persona_prompt(persona: object) -> str:
        """从当前和旧版 persona 对象中提取提示词文本。

        Args:
            persona: Persona 映射或对象。

        Returns:
            Persona 系统提示词，或空字符串。
        """
        if isinstance(persona, dict):
            return persona.get("system_prompt") or persona.get("prompt", "")
        return getattr(persona, "system_prompt", None) or getattr(persona, "prompt", "")

    async def _get_persona(self) -> str:
        """加载配置的或默认的 persona 提示词。

        Returns:
            Persona 提示词；不可用时返回空字符串。
        """
        persona_id = str(self.config.get("persona_id", "")).strip()
        if persona_id:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    return self._extract_persona_prompt(persona)
            except Exception as e:
                logger.warning(
                    f"[DynamicLifeState] Failed to load configured persona: {e}"
                )

        try:
            p = await self.context.persona_manager.get_default_persona_v3()
            return self._extract_persona_prompt(p) if p else ""
        except Exception:
            return ""

    # ---------- provider ----------

    async def _get_provider(self):
        """返回配置的或当前活跃的 LLM provider。"""
        provider_id = str(self.config.get("llm_provider_id", "")).strip()
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider()

    # ---------- LLM ----------

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        """从受支持的 provider 响应中提取补全文本。

        Args:
            resp: Provider 响应对象。

        Returns:
            规范化后的补全文本。
        """
        if resp is None:
            return ""
        for key in ("completion_text", "completion", "text", "content"):
            value = getattr(resp, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    # ---------- JSON parse ----------

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从模型输出文本中提取第一个完整的 JSON 对象。

        Args:
            text: 原始模型补全文本。

        Returns:
            解析后的映射；找不到有效对象时返回 ``None``。
        """
        text = text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

        start = text.find("{")
        if start == -1:
            return None

        brace = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                            return data if isinstance(data, dict) else None
                        except Exception:
                            return None
        return None

    # ---------- validate ----------

    @staticmethod
    def _validate_and_build(
        payload: dict | None,
        target_date: datetime.date,
        generated_at: str,
        holiday: str,
    ) -> LifeState:
        """校验模型 JSON 并构建自然日期状态。

        Args:
            payload: 解析后的模型 JSON。
            target_date: 调用方请求的日期。
            generated_at: 实际生成完成的时间戳。
            holiday: 插件解析出的节日名称。

        Returns:
            校验后的生活状态。

        Raises:
            ValueError: payload 不符合要求的 schema。
        """
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        # 校验必需的一级字段结构。
        date = target_date.isoformat()
        date_value = payload.get("date")
        if date_value != date:
            raise ValueError(
                f"date 字段必须与目标日期一致: expected={date}, actual={date_value}"
            )

        timeline_raw = payload.get("timeline")
        if not isinstance(timeline_raw, list) or len(timeline_raw) == 0:
            raise ValueError("timeline 字段缺失或为空列表")

        entries: list[TimelineEntry] = []
        for item in timeline_raw:
            if not isinstance(item, dict):
                continue
            entry = TimelineEntry.from_dict(item)
            if entry is not None:
                entries.append(entry)

        if not entries:
            raise ValueError(
                "timeline 中没有有效条目（每项需要可解析的 time 以及 schedule/outfit）"
            )

        return LifeState(
            date=date,
            holiday=holiday,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=generated_at,
        )
