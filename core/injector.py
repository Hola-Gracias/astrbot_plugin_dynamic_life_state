import datetime
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
    """构建临时的 ``<life_state>`` 上下文块。

    Args:
        state: 当前自然日期的生活状态。
        current_match: 匹配当前本地时间的时间线条目。
        now: 当前北京时间。

    Returns:
        要注入到 LLM 请求中的文本。
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


def build_fake_tool_call(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> list[dict]:
    """将当前状态格式化为 OpenAI 风格的伪工具调用。

    工具响应携带的 ``<life_state>`` 文本与
    ``extra_user_content_parts`` 注入方式相同，仅传递方式不同。

    Args:
        state: 当前自然日期的生活状态。
        current_match: 匹配当前本地时间的时间线条目。
        now: 当前北京时间。

    Returns:
        assistant 和 tool 消息列表。
    """
    inject_text = build_injection_text(state, current_match, now)
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
        "content": inject_text,
    }

    return [assistant_msg, tool_msg]


# =========================
# Context cleanup
# =========================


def remove_fake_tool_call_from_context(contexts: list[dict]) -> None:
    """从请求上下文中移除上一次的伪生活状态工具调用。

    Args:
        contexts: 可变的 provider 上下文消息列表。
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
