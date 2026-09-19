"""工具层：让大模型能「动手」，但算时间、查数据、做守卫仍然是确定性代码。

为什么是这个形状（实测之后定的）
    - qwen2.5:7b 与 qwen3.5:4b **都会选工具**，但**算日期很不可靠**
      （「下周三下午三点半」算成 10-04；「每周四」把 repeat 丢了）。
      选工具准不准用 ``scripts/eval_router.py`` 量（支持 ``--model`` 换模型对比）。
    - 所以工具的参数只收**用户原话**（``text``），时间解析交给 ``nlp_time``——
      实测这样 100% 正确（「下周三下午三点半跟导师见面」-> 09-23 15:30）。
    - 既然是原话，就干脆复用现成的 :class:`~voice_loop.skills.Skills`：
      同一套（取消要反问、重复不存两条、过去时间顺延）守卫，不用写第二份。

模型只负责「这句话该用哪个工具」，其余全是确定性的。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .settings import Settings
from .skills import TRIGGER_MEMO_ADD, SkillResult, Skills

# 给模型的额外说明（跟人设 system_prompt 分开放，人设仍然由用户自己改）
# 写法上刻意「机械」：第 1 条给的是关键词白名单，而不是「相关就问」这种要靠理解的
# 说法——实测（qwen3.5:4b）靠理解的做法会漏调（「我的备忘里有什么」它直接凭印象答了）。
TOOL_HINT = (
    "你可以调用工具。规则：\n"
    "1. 句子里只要出现这些词——日程、安排、课程、课、会议、开会、提醒、闹钟、备忘——"
    "就先调对应的工具查一遍，再开口。不要凭记忆或习惯回答：你的记忆里没有他的日程。\n"
    "2. 用户用「那件事」「那个会」「我跟导师见面」这种**指代**或模糊说法，"
    "只要沾得上安排/提醒/备忘，也先调工具查一遍：宁可查到空（然后如实说没有），"
    "也不要凭印象说「没这回事」——你并不知道他记过什么。\n"
    "3. 用户要**记东西或排日程**时，把他的话**原样**放进 text 参数，"
    "不要自己换算日期、不要改写时间说法（「下周三下午三点半」就照抄）。\n"
    "4. 上面这些词都没出现、也不涉及安排/提醒/备忘时，直接回答，不要调工具。\n"
    "5. 工具返回的 reply 就是最终答复，不要改数字和时间的说法。\n"
    "6. 要调工具就**直接用工具接口**，不要用文字写出 {'name': ..., 'arguments': ...} 这种 JSON。"
)

# 模型（尤其是小模型）偶尔不调工具，而是把调用**写成一段 JSON 文字**。
# 这段文字绝对不能念给用户听，得拦下来，能抢回就抢回来当工具调用。
_TOOL_TEXT_HINT = re.compile(r'"(?:arguments|function)"\s*:')
# 回复以 {" 开头：先攒着别念，等看清是不是工具调用
SUSPICIOUS_START = re.compile(r'^\s*[{[]\s*"')


def looks_like_tool_text(text: str) -> bool:
    """这段文字像不像「把工具调用当文字写出来」。"""
    return bool(_TOOL_TEXT_HINT.search(text or ""))


def parse_tool_call_text(text: str, allowed: set[str] | None = None) -> list[dict]:
    """尽力从文字里抠出工具调用（抠不出来返回空列表）。

    ``allowed``：只认这些工具名，免得把用户真正想要的 JSON 回答误当成工具调用。
    """
    t = (text or "").strip()
    if not looks_like_tool_text(t):
        return []
    start = t.find("{")
    while start >= 0:
        for end in range(min(len(t), start + 600), start, -1):
            try:
                obj = json.loads(t[start:end])
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            inner = obj.get("function") if isinstance(obj.get("function"), dict) else obj
            name = inner.get("name")
            if not isinstance(name, str) or not name:
                continue
            if allowed is not None and name not in allowed:
                continue
            args = inner.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            return [{"function": {"name": name, "arguments": args}}]
        start = t.find("{", start + 1)
    return []


@dataclass
class ToolResult:
    """一次工具调用的结果。``reply`` 是可以直接念给用户听的话。"""

    ok: bool
    action: str = ""
    reply: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""

    def as_message(self) -> dict:
        """喂回给模型的形式（只给必要字段，别把整个 data 塞进去浪费上下文）。"""
        out: dict[str, Any] = {"ok": self.ok, "reply": self.reply}
        if self.action:
            out["action"] = self.action
        if self.error:
            out["error"] = self.error
        return out


class ToolRegistry:
    """把 :class:`Skills` 包成模型能调的工具。

    复用同一个 Skills 实例是有意的：守卫、解析、去重全都在那边，
    工具层只做「选哪个 handler」和「参数校验」。
    """

    def __init__(
        self,
        settings: Settings,
        skills: Skills,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.skills = skills
        self.log = logger or logging.getLogger("voice_loop")
        self.calls = 0
        self.errors = 0

    # ------------------------------------------------------------ 给模型看的
    def specs(self) -> list[dict]:
        """OpenAI/Ollama 风格的 function 定义。"""
        text_only = {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "用户原话，照抄，不要自己改时间说法",
                }
            },
            "required": ["text"],
        }
        no_arg: dict = {"type": "object", "properties": {}}
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_schedule",
                    "description": (
                        "查日程/课表/会议。用户问「有什么安排」「下周有什么」「有没有课」时用。"
                        "时间段说法也照抄进 text（例如「这周」「下周三」）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "用户原话，含时间说法",
                            }
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "next_schedule",
                    "description": "下一项日程/会议是什么、还有多久。用户问「下一个是什么」时用。",
                    "parameters": no_arg,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_alarms",
                    "description": "还有哪些提醒/闹钟没响。",
                    "parameters": no_arg,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_memos",
                    "description": "备忘里记了什么（没有时间的待办）。",
                    "parameters": no_arg,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_schedule",
                    "description": (
                        "新增一条日程：课、会议、约会（见面/面试/答辩/体检/聚餐…）。"
                        "带时间的一次性安排和「每周X」的固定课都算。text 放用户原话。"
                    ),
                    "parameters": text_only,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_memo",
                    "description": "记一条备忘（没有具体时间的待办），例如「记一下买牛奶」。",
                    "parameters": text_only,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_alarm",
                    "description": (
                        "定一次性提醒：「十分钟后提醒我喝水」「明天早上七点叫我起床」。"
                        "只管响一声、不进日程列表的那种，用这个（带「叫我」的也用它）。"
                    ),
                    "parameters": text_only,
                },
            },
        ]

    def names(self) -> set[str]:
        return {s["function"]["name"] for s in self.specs()}

    @staticmethod
    def _unwrap(call: dict) -> tuple[str, dict]:
        fn = call.get("function") or call
        name = str(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        return name, dict(args or {})

    # ------------------------------------------------------------ 执行
    def call(self, call: dict) -> ToolResult:
        """执行一次工具调用（``call`` 是 Ollama 给的 tool_call 结构）。"""
        name, args = self._unwrap(call)
        self.calls += 1
        handler = {
            "list_schedule": self._list_schedule,
            "next_schedule": self._next_schedule,
            "list_alarms": self._list_alarms,
            "list_memos": self._list_memos,
            "add_schedule": self._add_schedule,
            "add_memo": self._add_memo,
            "add_alarm": self._add_alarm,
        }.get(name)
        if handler is None:
            self.errors += 1
            return ToolResult(False, error=f"没有这个工具：{name}", reply="这个我做不到。")
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.log.warning(f"[工具:{name}] 出错：{exc}")
            return ToolResult(False, error=str(exc), reply="这件事我没做成，你再说一遍？")
        self.log.info(f"[工具:{name}] {args} -> [{result.action}] {result.reply[:60]}")
        return result

    def call_all(self, calls: list[dict]) -> list[ToolResult]:
        return [self.call(c) for c in calls]

    # ------------------------------------------------------------ 各个工具
    def _text_arg(self, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("缺少 text 参数（应该是用户原话）")
        return text

    def _via(self, text: str, handler) -> ToolResult:
        """把原话丢给对应的技能分支处理（守卫、解析都在那边）。"""
        now = datetime.now()
        result: SkillResult | None = handler(text, now)
        if result is None:
            return ToolResult(
                False,
                error="这句话技能层没认出来",
                reply="我没听懂要怎么安排，你换个说法？比如「明天下午三点跟导师见面」。",
            )
        return ToolResult(bool(result.reply), action=result.action, reply=result.reply, data=result.data)

    def _list_schedule(self, args: dict) -> ToolResult:
        text = self._text_arg(args)
        result = self._via(text, self.skills._handle_schedule)  # noqa: SLF001
        if result.ok:
            return result
        # 模型常常把整句原话递过来（「导师见面那件事是什么时候」），
        # 时间范围解析不了 —— 那就按名字去库里找那一条。
        named = self._find_named(text)
        if named is not None:
            return named
        # 还是不行就退一步：给今天有什么，总比回一句「没听懂」有用
        return self._via("今天有什么安排", self.skills._handle_schedule)  # noqa: SLF001

    def _find_named(self, text: str) -> ToolResult | None:
        """按名字在日程里找（「导师见面那件事是什么时候」-> 跟导师见面 9/23 15:30）。"""
        now = datetime.now()
        items = self.skills.schedule.load()   # noqa: SLF001
        if not items:
            return ToolResult(False, action="schedule_miss", reply="日程里现在是空的。")
        hits = self.skills._match_schedule_items(text, items, now)   # noqa: SLF001
        if not hits or hits[0][0] < 2:
            return None
        best = hits[0][1]
        nxt = self.skills.next_occurrence(best, now)                # noqa: SLF001
        when = f"{nxt.strftime('%m月%d日 %H:%M')}" if nxt else "（已经过期）"
        title = best.get("title") or "安排"
        where = f"，地点{best['location']}" if best.get("location") else ""
        reply = f"{title}：{when}{where}，{self.skills._repeat_text(best)}。"  # noqa: SLF001
        if len(hits) > 1 and hits[1][0] >= hits[0][0]:
            names = "、".join(str(s[1].get("title")) for s in hits[:3])
            reply += f"（还有几条也对得上：{names}）"
        return ToolResult(True, action="schedule_find", reply=reply,
                          data={"title": title, "start": when})

    def _add_schedule(self, args: dict) -> ToolResult:
        text = self._text_arg(args)
        result = self._via(text, self.skills._handle_schedule)  # noqa: SLF001
        if not result.ok and "没听懂" in result.reply:
            # 交白卷时给一句更像"没排成"的话
            result.reply = "这条日程我没排上——说清楚点，比如「下周三下午三点半跟导师见面」。"
        return result

    def _add_memo(self, args: dict) -> ToolResult:
        text = self._text_arg(args)
        # 「买牛奶」这种缺了「记一下」的说法也认：补上前缀再交给备忘技能
        if not TRIGGER_MEMO_ADD.search(text):
            text = f"记一下{text}"
        result = self._via(text, self.skills._handle_memo)  # noqa: SLF001
        if not result.ok and "没听懂" in result.reply:
            result.reply = "要记什么？说「记一下买牛奶」这样就行。"
        return result

    def _add_alarm(self, args: dict) -> ToolResult:
        """一次性提醒（「十分钟后提醒我喝水」「明天早上七点叫我起床」）。"""
        text = self._text_arg(args)
        result = self._via(text, self.skills._handle_alarm)   # noqa: SLF001
        if result.ok:
            return result
        # 「叫我/喊我」这类说法如果闹钟分支不认，再让日程试一次（它能吃日期+时刻）
        again = self._via(text, self.skills._handle_schedule)  # noqa: SLF001
        if again.ok:
            return again
        return ToolResult(
            False,
            action="alarm_miss",
            error="技能层没认出来",
            reply="这个提醒我没定上——说个具体时间，比如「明天早上七点叫我起床」。",
        )

    def _next_schedule(self, _args: dict) -> ToolResult:
        result = self.skills._next_item(datetime.now())  # noqa: SLF001
        return ToolResult(bool(result.reply), action=result.action, reply=result.reply, data=result.data)

    def _list_alarms(self, _args: dict) -> ToolResult:
        result = self.skills._list_alarms(datetime.now())  # noqa: SLF001
        return ToolResult(bool(result.reply), action=result.action, reply=result.reply, data=result.data)

    def _list_memos(self, _args: dict) -> ToolResult:
        result = self.skills._handle_memo("我的备忘有哪些", datetime.now())  # noqa: SLF001
        if result is None:
            return ToolResult(False, action="memo_list", reply="备忘里现在是空的。")
        return ToolResult(True, action=result.action, reply=result.reply, data=result.data)


def describe_calls(calls: list[dict]) -> str:
    """日志/评估用：把 tool_calls 变成一行字。"""
    parts = []
    for c in calls:
        name, args = ToolRegistry._unwrap(c)  # noqa: SLF001
        parts.append(f"{name}({args})")
    return "; ".join(parts)


__all__ = [
    "ToolRegistry",
    "ToolResult",
    "TOOL_HINT",
    "SUSPICIOUS_START",
    "describe_calls",
    "looks_like_tool_text",
    "parse_tool_call_text",
]
