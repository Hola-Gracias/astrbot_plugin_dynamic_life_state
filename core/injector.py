import datetime
import json as _json
import uuid

from .state import (
    BusinessCycle,
    LifeState,
    SlotMatch,
    format_cycle,
    format_datetime,
    format_interval,
)

# fake tool call 常量
FAKE_TOOL_CALL_NAME = "get_current_life_state"
FAKE_TOOL_CALL_ID_PREFIX = "fake_dynamic_life_state_"


# =========================
# 注入文本构建
# =========================


def build_injection_text(
    state: LifeState,
    cycle: BusinessCycle,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> str:
    """构建注入到 LLM 上下文的 <life_state> 文本。"""
    slot_label = "无"
    schedule_label = "无"
    outfit_label = "无"
    interval_label = "无"
    if current_match:
        slot_label = current_match.entry.time
        schedule_label = current_match.entry.schedule
        outfit_label = current_match.entry.outfit
        if current_match.active_interval:
            interval_label = format_interval(current_match.active_interval)

    return (
        f"<life_state>\n"
        f"这是你当前的生活状态，可供参考，无需主动提及\n"
        f"业务日期: {state.business_date}\n"
        f"状态周期: {format_cycle(cycle)}\n"
        f"当前自然日期时间: {format_datetime(now)}\n"
        f"周期概况: {state.schedule_summary or '无'}\n"
        f"周期氛围: {state.style_summary or '无'}\n"
        f"当前时段: {slot_label}\n"
        f"当前时段范围: {interval_label}\n"
        f"当前安排: {schedule_label}\n"
        f"当前穿搭: {outfit_label}\n"
        f"</life_state>"
    )


# =========================
# fake tool call 构建
# =========================


def build_state_json(
    state: LifeState,
    cycle: BusinessCycle,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> dict:
    """构建状态 JSON，作为 fake tool call 的返回内容。"""
    entry = current_match.entry if current_match else None
    return {
        "business_date": state.business_date,
        "cycle_start": cycle.start.isoformat(),
        "cycle_end": cycle.end.isoformat(),
        "current_datetime": now.isoformat(),
        "schedule_summary": state.schedule_summary,
        "style_summary": state.style_summary,
        "current_time_slot": entry.time if entry else "",
        "current_time_interval": (
            format_interval(current_match.active_interval)
            if current_match and current_match.active_interval
            else ""
        ),
        "current_schedule": entry.schedule if entry else "",
        "current_outfit": entry.outfit if entry else "",
    }


def build_fake_tool_call(
    state: LifeState,
    cycle: BusinessCycle,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> list[dict]:
    """将生活状态格式化为伪造的工具调用消息对（OpenAI 格式）。

    Returns:
        [assistant_msg, tool_msg]
    """
    state_json = build_state_json(state, cycle, current_match, now)
    call_id = f"{FAKE_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"

    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": FAKE_TOOL_CALL_NAME,
                    "arguments": "{}",
                },
            }
        ],
    }

    tool_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": FAKE_TOOL_CALL_NAME,
        "content": _json.dumps(state_json, ensure_ascii=False),
    }

    return [assistant_msg, tool_msg]


# =========================
# 残留清理
# =========================


def remove_fake_tool_call_from_context(contexts: list[dict]) -> None:
    """从 contexts 中移除上次注入的伪造工具调用消息对。"""
    if not contexts:
        return

    indices_to_remove: set[int] = set()
    fake_call_ids: set[str] = set()

    for i, msg in enumerate(contexts):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if tc_id.startswith(FAKE_TOOL_CALL_ID_PREFIX):
                    fake_call_ids.add(tc_id)
                    indices_to_remove.add(i)
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in fake_call_ids:
                indices_to_remove.add(i)

    for i in sorted(indices_to_remove, reverse=True):
        contexts.pop(i)
