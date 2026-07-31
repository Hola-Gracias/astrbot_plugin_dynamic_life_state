import asyncio
import datetime
import json
import re
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


def _render_template(template: str, **kwargs: str) -> str:
    """Replace known placeholders while preserving other braces.

    Args:
        template: Prompt template.
        **kwargs: Supported placeholder values.

    Returns:
        Rendered prompt.
    """
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    """Load the default prompt from the plugin config schema.

    Returns:
        Default prompt template.

    Raises:
        ValueError: The schema does not contain a usable default prompt.
    """
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    template = schema.get("prompt_template", {}).get("default")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("_conf_schema.json 中缺少有效的 prompt_template.default")
    return template


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

    @property
    def is_generating(self) -> bool:
        """Return whether a generation call currently holds the shared lock."""
        return self._gen_lock.locked()

    def _get_prompt_template(self) -> str:
        """Return a supported custom prompt or the new schema default.

        Returns:
            Prompt template without legacy placeholders.
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
        """Render historical states for the generation prompt.

        Args:
            states: States ordered from newest to oldest.

        Returns:
            Historical schedule text, or ``无`` when no state is available.
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
    ) -> LifeState:
        """Generate and persist a natural-date life state.

        Args:
            target_date: Natural date captured by the generation caller.
            force: Whether to replace an existing successful state.
            extra: Optional user requirements for forced regeneration.

        Returns:
            Generated state, including a failed state when generation fails.
        """
        async with self._gen_lock:
            date = target_date.isoformat()

            # Re-check after waiting for the lock to deduplicate concurrent callers.
            if not force:
                existing = self.data_mgr.get_current(date)
                if existing and existing.status == "ok":
                    return existing

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
                return LifeState(
                    date=date,
                    status="failed",
                    generated_at=datetime.datetime.now(CHINA_TIMEZONE).isoformat(),
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
                failed = LifeState(
                    date=date,
                    status="failed",
                    generated_at=datetime.datetime.now(CHINA_TIMEZONE).isoformat(),
                )
                try:
                    self.data_mgr.set(failed)
                except Exception as save_error:
                    logger.error(
                        f"[DynamicLifeState] Failed to persist generation failure "
                        f"for {date}: {save_error}"
                    )
                return failed

            try:
                self.data_mgr.set(state)
            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] Failed to persist generated state "
                    f"for {date}: {e}"
                )
                return LifeState(
                    date=date,
                    status="failed",
                    generated_at=datetime.datetime.now(CHINA_TIMEZONE).isoformat(),
                )

            if debug:
                logger.info(
                    f"[DynamicLifeState] Parsed state:\n"
                    f"{json.dumps(state.to_dict(), ensure_ascii=False, indent=2)}"
                )

            logger.info(
                f"[DynamicLifeState] Life state generated successfully for {date}"
            )
            return state

    # ---------- persona ----------

    @staticmethod
    def _extract_persona_prompt(persona: object) -> str:
        """Extract prompt text from current and legacy persona objects.

        Args:
            persona: Persona mapping or object.

        Returns:
            Persona system prompt, or an empty string.
        """
        if isinstance(persona, dict):
            return persona.get("system_prompt") or persona.get("prompt", "")
        return getattr(persona, "system_prompt", None) or getattr(persona, "prompt", "")

    async def _get_persona(self) -> str:
        """Load the configured or default persona prompt.

        Returns:
            Persona prompt, or an empty string when unavailable.
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
        """Return the configured or active LLM provider."""
        provider_id = str(self.config.get("llm_provider_id", "")).strip()
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider()

    # ---------- LLM ----------

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        """Extract completion text from supported provider responses.

        Args:
            resp: Provider response object.

        Returns:
            Normalized completion text.
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
        """Extract the first complete JSON object from model text.

        Args:
            text: Raw model completion.

        Returns:
            Parsed mapping, or ``None`` when no valid object is found.
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
        """Validate model JSON and build a natural-date state.

        Args:
            payload: Parsed model JSON.
            target_date: Date requested by the caller.
            generated_at: Actual generation completion timestamp.
            holiday: Holiday name resolved by the plugin.

        Returns:
            Validated life state.

        Raises:
            ValueError: The payload does not match the required schema.
        """
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        # Validate the required top-level schema.
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
