import asyncio
import datetime
import json
import re

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .state import DataManager, LifeState, TimelineEntry


def _render_template(template: str, **kwargs: str) -> str:
    """安全渲染模板：只替换已知占位符，其他花括号原样保留。"""
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", value)
    return result


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

    async def generate(
        self,
        date: datetime.datetime | None = None,
        force: bool = False,
        extra: str | None = None,
    ) -> LifeState:
        """生成并持久化今日生活状态。加锁保护，避免并发打到 LLM。"""
        async with self._gen_lock:
            date = date or datetime.datetime.now()
            date_str = date.strftime("%Y-%m-%d")

            # 二次检查：可能在等锁期间已被另一个任务生成（force 时跳过）
            if not force:
                existing = self.data_mgr.get(date_str)
                if existing and existing.status == "ok":
                    return existing

            debug = bool(self.config.get("debug_mode", False))

            try:
                logger.info(f"[DynamicLifeState] 正在生成 {date_str} 的生活状态...")

                persona = await self._get_persona()
                prompt = _render_template(
                    self.config["prompt_template"], date=date_str, persona=persona
                )

                extra_text = (extra or "").strip()
                if extra_text:
                    prompt += f"\n\n额外生成要求：\n{extra_text}"

                if debug:
                    logger.info(f"[DynamicLifeState] 生成 prompt:\n{prompt}")

                provider = await self._get_provider()
                if not provider:
                    raise RuntimeError("没有可用的 LLM Provider")

                sid = f"dynamic_life_state_gen_{date_str}"
                resp = await provider.text_chat(prompt, session_id=sid)
                text = self._extract_completion_text(resp)

                if debug:
                    logger.info(f"[DynamicLifeState] 模型原始返回:\n{text}")

                payload = self._extract_json(text)
                state = self._validate_and_build(payload, date_str)

                self.data_mgr.set(state)

                if debug:
                    logger.info(
                        f"[DynamicLifeState] 解析后的状态:\n"
                        f"{json.dumps(self._state_to_dict(state), ensure_ascii=False, indent=2)}"
                    )

                logger.info(f"[DynamicLifeState] {date_str} 生活状态生成成功")
                return state

            except Exception as e:
                logger.error(f"[DynamicLifeState] 生成失败 ({date_str}): {e}")
                failed = LifeState(
                    date=date_str,
                    status="failed",
                    generated_at=datetime.datetime.now().isoformat(),
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
    def _validate_and_build(payload: dict | None, date_str: str) -> LifeState:
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        # 基础校验
        date_val = payload.get("date")
        if not isinstance(date_val, str) or not date_val:
            raise ValueError("date 字段缺失或非字符串")

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
            entries.append(
                TimelineEntry(
                    time=str(time_val),
                    schedule=str(schedule_val),
                    outfit=str(outfit_val),
                )
            )

        if not entries:
            raise ValueError(
                "timeline 中没有有效条目（每项至少需要 time/schedule/outfit）"
            )

        return LifeState(
            date=date_str,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=datetime.datetime.now().isoformat(),
        )

    @staticmethod
    def _state_to_dict(state: LifeState) -> dict:
        return {
            "date": state.date,
            "schedule_summary": state.schedule_summary,
            "style_summary": state.style_summary,
            "timeline": [
                {"time": e.time, "schedule": e.schedule, "outfit": e.outfit}
                for e in state.timeline
            ],
            "status": state.status,
            "generated_at": state.generated_at,
        }
