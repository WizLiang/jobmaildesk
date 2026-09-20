from __future__ import annotations

import re


TERMINAL_STAGE_LABELS = (
    "已拒绝",
    "未通过",
    "已结束",
    "已撤回",
    "已关闭",
    "已终止",
)

_STRUCTURED_TERMINAL_STATUS = re.compile(
    r"^(?:(?:招聘)?流程|申请|应聘|职位|岗位)?"
    r"(?:已)?(?:拒绝|撤回|关闭|结束|终止)(?:[：:（(].*)?$"
)
_INTERVIEW_ROUND = re.compile(
    r"(?:第(?P<round>[一二三四五六七八九十]|\d+)轮"
    r"|(?P<short>[一二三四五六七八九十]|\d+)面)"
)
_CHINESE_ROUNDS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _normalized_stage(value: str | None) -> str:
    return "".join(str(value or "").split())


def _interview_round(value: str) -> int | None:
    match = _INTERVIEW_ROUND.search(value)
    if not match:
        return None
    token = match.group("round") or match.group("short")
    if token is None:
        return None
    if token.isdigit():
        number = int(token)
    else:
        number = _CHINESE_ROUNDS[token]
    return number if number > 0 else None


def is_terminal_stage(value: str | None) -> bool:
    stage = _normalized_stage(value)
    if not stage:
        return False
    if any(label in stage for label in TERMINAL_STAGE_LABELS):
        return True
    return bool(_STRUCTURED_TERMINAL_STATUS.fullmatch(stage))


def stage_depth(value: str | None) -> int:
    stage = _normalized_stage(value)
    if not stage or is_terminal_stage(stage):
        return 0
    lowered = stage.casefold()
    if "offer" in lowered or "录用" in stage:
        return 11
    if (
        "hrbp" in lowered
        or "hr面" in lowered
        or any(label in stage for label in ("人力面", "人力资源面"))
    ):
        return 10
    if any(label in stage for label in ("终面", "最终面", "决赛面")):
        return 9
    round_number = _interview_round(stage)
    if round_number is not None:
        return 3 + min(round_number, 5)
    if "第n轮" in lowered:
        return 4
    if any(
        label in stage
        for label in ("群面", "AI面试", "ai面试", "技术面", "面试")
    ):
        return 4
    if any(label in stage for label in ("笔试", "编程测试", "机试")):
        return 3
    if any(label in stage for label in ("测评", "性格测试", "能力测试")):
        return 2
    if any(label in stage for label in ("网申", "投递", "简历筛选", "申请")):
        return 1
    return 0


def is_stage_advance(current: str | None, incoming: str | None) -> bool:
    return stage_depth(incoming) > stage_depth(current)


def is_same_stage(current: str | None, incoming: str | None) -> bool:
    current_stage = _normalized_stage(current)
    incoming_stage = _normalized_stage(incoming)
    if not current_stage or not incoming_stage:
        return current_stage == incoming_stage
    if current_stage.casefold() == incoming_stage.casefold():
        return True
    current_round = _interview_round(current_stage)
    incoming_round = _interview_round(incoming_stage)
    return (
        current_round is not None
        and incoming_round is not None
        and current_round == incoming_round
    )
