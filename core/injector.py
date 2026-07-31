import datetime
import json as _json
import uuid

from .state import (
    OPTIONAL_TIMELINE_FIELDS,
    LifeState,
    SlotMatch,
    format_datetime,
    format_interval,
)

# Fake tool-call constants
FAKE_TOOL_CALL_NAME = "get_current_life_state"
FAKE_TOOL_CALL_ID_PREFIX = "fake_dynamic_life_state_"


# =========================
# Injection text
# =========================


def build_injection_text(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> str:
    """Build the temporary ``<life_state>`` context block.

    Args:
        state: Current natural-date life state.
        current_match: Timeline entry matching the current local time.
        now: Current Beijing date and time.

    Returns:
        Text to inject into the LLM request.
    """
    slot_label = "无"
    schedule_label = "无"
    outfit_label = "无"
    interval_label = "无"
    optional_labels: list[str] = []
    if current_match:
        slot_label = current_match.entry.time
        schedule_label = current_match.entry.schedule
        outfit_label = current_match.entry.outfit
        for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
            value = current_match.entry.extra_fields.get(key)
            if value:
                optional_labels.append(f"当前{spec['label']}: {value}\n")
        if current_match.active_interval:
            interval_label = format_interval(current_match.active_interval)

    return (
        f"<life_state>\n"
        f"这是你当前的生活状态，可供参考\n"
        f"话题无关时无需主动提及\n"
        f"日期: {state.date}\n"
        f"今日节日: {state.holiday or '无'}\n"
        f"当前日期时间: {format_datetime(now)}\n"
        f"今日概况: {state.schedule_summary or '无'}\n"
        f"今日氛围: {state.style_summary or '无'}\n"
        f"当前时段: {slot_label}\n"
        f"当前时段范围: {interval_label}\n"
        f"当前安排: {schedule_label}\n"
        f"当前穿搭: {outfit_label}\n"
        f"{''.join(optional_labels)}"
        f"</life_state>"
    )


# =========================
# Fake tool-call messages
# =========================


def build_state_json(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> dict:
    """Build the fake tool-call result payload.

    Args:
        state: Current natural-date life state.
        current_match: Timeline entry matching the current local time.
        now: Current Beijing date and time.

    Returns:
        JSON-compatible current-state payload.
    """
    entry = current_match.entry if current_match else None
    payload = {
        "date": state.date,
        "holiday": state.holiday or "无",
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
    if entry:
        for key in OPTIONAL_TIMELINE_FIELDS:
            value = entry.extra_fields.get(key)
            if value:
                payload[f"current_{key}"] = value
    return payload


def build_fake_tool_call(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> list[dict]:
    """Format the current state as an OpenAI-style fake tool call.

    Args:
        state: Current natural-date life state.
        current_match: Timeline entry matching the current local time.
        now: Current Beijing date and time.

    Returns:
        Assistant and tool messages.
    """
    state_json = build_state_json(state, current_match, now)
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
# Context cleanup
# =========================


def remove_fake_tool_call_from_context(contexts: list[dict]) -> None:
    """Remove a previous fake life-state tool call from request context.

    Args:
        contexts: Mutable provider context messages.
    """
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
