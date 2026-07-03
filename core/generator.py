import asyncio
import datetime
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .state import (
    MAX_HISTORY_DAYS,
    BusinessCycle,
    DataManager,
    LifeState,
    TimelineEntry,
    is_valid_time_slot,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def _render_template(template: str, **kwargs: str) -> str:
    """安全渲染模板：只替换已知占位符，其他花括号原样保留。"""
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", value)
    return result


def _load_default_prompt_template() -> str:
    """从配置 schema 读取 prompt_template 的默认值。"""
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
    ):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._gen_lock = asyncio.Lock()

    @property
    def is_generating(self) -> bool:
        return self._gen_lock.locked()

    def _get_prompt_template(self) -> str:
        configured = self.config.get("prompt_template", "")
        if isinstance(configured, str) and configured.strip():
            return configured
        logger.warning(
            "[DynamicLifeState] prompt_template 为空，使用配置 schema 默认提示词"
        )
        return _load_default_prompt_template()

    @staticmethod
    def _format_history(states: list[LifeState]) -> str:
        """将历史状态渲染为生成提示词使用的文本。

        Args:
            states: 按业务日期从新到旧排列的历史状态。

        Returns:
            包含日程、穿搭和时间线的历史状态文本；无状态时返回“无”。
        """
        if not states:
            return "无"

        sections: list[str] = []
        for state in states:
            lines = [
                f"[{state.business_date}]",
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
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    async def generate(
        self,
        cycle: BusinessCycle,
        force: bool = False,
        extra: str | None = None,
    ) -> LifeState:
        """生成并持久化指定业务周期的生活状态。"""
        async with self._gen_lock:
            business_date = cycle.business_date.isoformat()

            # 二次检查：可能在等锁期间已被另一个任务生成（force 时跳过）
            if not force:
                existing = self.data_mgr.get_by_cycle(cycle)
                if existing and existing.status == "ok":
                    return existing

            debug = bool(self.config.get("debug_mode", False))

            try:
                logger.info(
                    f"[DynamicLifeState] 正在生成业务日期 {business_date} 的生活状态..."
                )

                self.data_mgr.archive_before_generation(business_date)
                try:
                    history_days = int(self.config.get("history_reference_days", 3))
                except (TypeError, ValueError):
                    history_days = 3
                history_days = max(0, min(history_days, MAX_HISTORY_DAYS))
                history_states = self.data_mgr.get_recent_history(
                    business_date,
                    history_days,
                )
                history_text = self._format_history(history_states)
                extra_text = (extra or "").strip() or "无"

                persona = await self._get_persona()
                prompt = _render_template(
                    self._get_prompt_template(),
                    business_date=business_date,
                    cycle_start=cycle.start.isoformat(timespec="minutes"),
                    cycle_end=cycle.end.isoformat(timespec="minutes"),
                    persona=persona,
                    history_states=history_text,
                    extra_requirements=extra_text,
                )

                if debug:
                    logger.info(f"[DynamicLifeState] 生成 prompt:\n{prompt}")

                provider = await self._get_provider()
                if not provider:
                    raise RuntimeError("没有可用的 LLM Provider")

                sid = f"dynamic_life_state_gen_{business_date}"
                resp = await provider.text_chat(prompt, session_id=sid)
                text = self._extract_completion_text(resp)

                if debug:
                    logger.info(f"[DynamicLifeState] 模型原始返回:\n{text}")

                payload = self._extract_json(text)
                generated_at = datetime.datetime.now(cycle.start.tzinfo).isoformat()
                state = self._validate_and_build(
                    payload,
                    cycle,
                    generated_at,
                )

                self.data_mgr.set(state)

                if debug:
                    logger.info(
                        f"[DynamicLifeState] 解析后的状态:\n"
                        f"{json.dumps(self._state_to_dict(state), ensure_ascii=False, indent=2)}"
                    )

                logger.info(
                    f"[DynamicLifeState] 业务日期 {business_date} 生活状态生成成功"
                )
                return state

            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] 生成失败 (业务日期 {business_date}): {e}"
                )
                failed = LifeState.from_cycle(
                    cycle,
                    status="failed",
                    generated_at=datetime.datetime.now(cycle.start.tzinfo).isoformat(),
                )
                self.data_mgr.set(failed)
                return failed

    # ---------- persona ----------

    @staticmethod
    def _extract_persona_prompt(persona: object) -> str:
        """从人格对象中提取 prompt 文本，兼容 system_prompt 和旧 prompt 字段。"""
        if isinstance(persona, dict):
            return persona.get("system_prompt") or persona.get("prompt", "")
        return getattr(persona, "system_prompt", None) or getattr(persona, "prompt", "")

    async def _get_persona(self) -> str:
        persona_id = str(self.config.get("persona_id", "")).strip()
        if persona_id:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    return self._extract_persona_prompt(persona)
            except Exception as e:
                logger.warning(f"[DynamicLifeState] 读取指定人格失败: {e}")

        try:
            p = await self.context.persona_manager.get_default_persona_v3()
            return self._extract_persona_prompt(p) if p else ""
        except Exception:
            return ""

    # ---------- provider ----------

    async def _get_provider(self):
        provider_id = str(self.config.get("llm_provider_id", "")).strip()
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider()

    # ---------- LLM ----------

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
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
        cycle: BusinessCycle,
        generated_at: str,
    ) -> LifeState:
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        # 基础校验
        business_date = cycle.business_date.isoformat()
        business_date_val = payload.get("business_date")
        if business_date_val != business_date:
            raise ValueError(
                "business_date 字段必须与目标业务日期一致: "
                f"expected={business_date}, actual={business_date_val}"
            )

        timeline_raw = payload.get("timeline")
        if not isinstance(timeline_raw, list) or len(timeline_raw) == 0:
            raise ValueError("timeline 字段缺失或为空列表")

        entries: list[TimelineEntry] = []
        for item in timeline_raw:
            if not isinstance(item, dict):
                continue
            time_val = item.get("time")
            schedule_val = item.get("schedule")
            outfit_val = item.get("outfit")
            if not time_val or not schedule_val or not outfit_val:
                continue
            if not is_valid_time_slot(str(time_val)):
                continue
            entries.append(
                TimelineEntry(
                    time=str(time_val),
                    schedule=str(schedule_val),
                    outfit=str(outfit_val),
                )
            )

        if not entries:
            raise ValueError(
                "timeline 中没有有效条目（每项需要可解析的 time 以及 schedule/outfit）"
            )

        return LifeState.from_cycle(
            cycle,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=generated_at,
        )

    @staticmethod
    def _state_to_dict(state: LifeState) -> dict:
        return {
            "business_date": state.business_date,
            "cycle_start": state.cycle_start,
            "cycle_end": state.cycle_end,
            "timezone": state.timezone,
            "schedule_summary": state.schedule_summary,
            "style_summary": state.style_summary,
            "timeline": [
                {"time": e.time, "schedule": e.schedule, "outfit": e.outfit}
                for e in state.timeline
            ],
            "status": state.status,
            "generated_at": state.generated_at,
        }
