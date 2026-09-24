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

from . import event_text as et
from .events import display_title
from .nlp_time import parse_datetime
from .settings import Settings
from .skills import (
    ACTIVITY_NOUN,
    TRIGGER_ALARM,
    TRIGGER_APPOINT,
    TRIGGER_MEMO_ADD,
    TRIGGER_SCHEDULE,
    SkillResult,
    Skills,
    has_clock_expr,
)

# 给模型的额外说明（跟人设 system_prompt 分开放，人设仍然由用户自己改）
# 写法上刻意「机械」：第 1 条给的是关键词白名单，而不是「相关就问」这种要靠理解的
# 说法——实测（qwen3.5:4b）靠理解的做法会漏调（「我的备忘里有什么」它直接凭印象答了）。
TOOL_HINT = (
    "你可以调用工具。规则：\n"
    "1. 句子里只要出现这些词——日程、安排、课程、课、会议、开会、提醒、闹钟、备忘、几点——"
    "就先调对应的工具，再开口。不要凭记忆或习惯回答：你的记忆里没有他的日程。\n"
    "2. 用户用「那件事」「那个会」「我跟导师见面」这种**指代**或模糊说法，"
    "只要沾得上安排/提醒/备忘，也先调工具查一遍：宁可查到空（然后如实说没有），"
    "也不要凭印象说「没这回事」——你并不知道他记过什么。\n"
    "3. 用户要**记东西或排日程**时，把他的话**原样**放进 text 参数，"
    "不要自己换算日期、不要改写时间说法（「下周三下午三点半」就照抄）。\n"
    "4. 要**改 / 挪 / 取消 / 删掉**什么，也走工具（change_event），"
    "text 同样是原话——你不需要知道库里现在有什么，也不需要算时间。\n"
    "5. 上面这些词都没出现、也不涉及安排/提醒/备忘时，直接回答，不要调工具。"
    "闲聊、问知识、让你写代码、问你的看法（「你觉得我中午吃什么好」「要不要继续写」）"
    "都属于这种——不要拿这些去查日程。\n"
    "6. 工具返回的 reply 就是最终答复，不要改数字和时间的说法。\n"
    "7. 要调工具就**直接用工具接口**，不要用文字写出 {'name': ..., 'arguments': ...} 这种 JSON。"
)

# 模型（尤其是小模型）偶尔不调工具，而是把调用**写成一段 JSON 文字**。
# 这段文字绝对不能念给用户听，得拦下来，能抢回就抢回来当工具调用。
_TOOL_TEXT_HINT = re.compile(r'"(?:arguments|function)"\s*:')
# 回复以 {" 开头：先攒着别念，等看清是不是工具调用
SUSPICIOUS_START = re.compile(r'^\s*[{[]\s*"')


def looks_like_tool_text(text: str) -> bool:
    """这段文字像不像「把工具调用当文字写出来」。"""
    return bool(_TOOL_TEXT_HINT.search(text or ""))


# 拿 text 去**算时间**的工具：这些的参数必须跟原话一致，不能是模型改写过的版本
_TIME_SENSITIVE_TOOLS = {
    "add_event",
    "change_event",
    "fix_last",
}

# 这些工具**一律用用户原话**，不看模型给的 text。
# 为什么：修正句（「我说是今晚十点」）**全部内容就是一个时间**，
# 实测模型会把「十点」改写成「8点45」（凭空换了个时间），而原话本身短且准。
_ALWAYS_ORIGINAL_TOOLS = {"fix_last"}


def reroute_correction(call: dict, user_text: str, skills: Skills | None) -> dict:
    """「刚记下一条 + 这句话只是时间」→ 拉回 fix_last（它在改上一条，不是新建）。

    ★为什么要用代码管这件事★（实测）：说完「提醒我明天早上七点练琴」，
    紧接着说「我说是今晚十点」——模型选了 add_event（还自己编了句
    「今晚十点提醒我练琴」），结果库里两条闹钟，用户以为改了。
    工具描述写了「别新建」也不能保证，所以这里机械地掰回来：

        ＊技能层刚记下一条（3 分钟内）；
        ＊这句话是**纯时间**（「我说是今晚十点」这种，没说别的事）。

    两条都满足时，新建一定不是他想要的。只要带一点别的东西（「九点提醒我写作业」）
    就不会命中，照旧新建。
    """
    if not isinstance(call, dict) or not user_text or skills is None:
        return call
    fn = call.get("function") or call
    name = str(fn.get("name") or "")
    if name not in _CORRECTION_PRONE_TOOLS:
        return call
    now = datetime.now()
    if skills.recent_add() and skills._looks_time_only(user_text, now):  # noqa: SLF001
        return {"function": {"name": "fix_last", "arguments": {"text": user_text.strip()}}}
    return call


def _has_action_word(text: str) -> bool:
    """这句里有没有「提醒 / 叫我 / 记一下 / 安排」这类**动宾词**。

    为什么要看它：模型改写时会把动词吞掉（「提醒我明天早上七点练琴」→
    「明天早上七点练琴」），时间还在，但技能层认不出这是要新建提醒。
    """
    t = text or ""
    return bool(
        TRIGGER_ALARM.search(t)
        or TRIGGER_SCHEDULE.search(t)
        or TRIGGER_MEMO_ADD.search(t)
    )


# 「刚记下一条 + 只说时间」时要改掉新建念头的工具
_CORRECTION_PRONE_TOOLS = {"add_event", "add_memo"}


def repair_args(call: dict, user_text: str) -> dict:
    """把「被模型改写坏了」的参数换回用户原话（机械校验，不靠模型自觉）。

    为什么要它（实测数据）：qwen3.5:4b 在 23 次调用里有 6 次**改写了原话**，两种都见过：
        - 把时间说法丢掉：「下周三下午三点半跟导师见面」→「跟导师见面」；
        - 把动词丢掉：「提醒我明天早上七点练琴」→「明天早上七点练琴」
          （后者技能层认不得，回了一句「这个提醒我没定上」）。

    两条机械规则，任一命中就用原话：
        ＊原话里带着能解析出的时间、模型那段没有 → 用原话；
        ＊原话里有「提醒/叫我/记一下/安排」这类动宾词、模型那段没有 → 用原话。

    查询类（list_*）不修：模型自己缩小范围（「帮我看看下周都有什么」→「下周」）是合理的。
    """
    if not isinstance(call, dict) or not user_text:
        return call
    fn = call.get("function") or call
    name = str(fn.get("name") or "")
    if name not in _TIME_SENSITIVE_TOOLS:
        return call
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    args = dict(args or {})
    original = user_text.strip()
    text = str(args.get("text") or "").strip()
    if not original or text == original:
        return call
    if name in _ALWAYS_ORIGINAL_TOOLS:
        args["text"] = original
        return {"function": {"name": name, "arguments": args}}
    if parse_datetime(original) is not None and parse_datetime(text) is None:
        args["text"] = original
        return {"function": {"name": name, "arguments": args}}
    if _has_action_word(original) and not _has_action_word(text):
        args["text"] = original
        return {"function": {"name": name, "arguments": args}}
    return call


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
                    "name": "list_events",
                    "description": (
                        "查提醒 / 日程 / 课表 / 会议——它们现在是同一种东西。"
                        "用户问「有什么安排」「下周有什么」「还有哪些提醒没响」时用。"
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
                    "name": "next_event",
                    "description": "下一项是什么、还有多久。用户问「下一个是什么」时用。",
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
                    "name": "add_event",
                    "description": (
                        "新增一条提醒或日程：课、会议、约会（见面/面试/答辩/聚餐…）、"
                        "「十分钟后提醒我喝水」「明天早上七点叫我起床」「每个工作日八点半"
                        "叫我起床」都算。**说了什么就记什么**：带时长就存时长，带「每周X」"
                        "就存重复规则，带「提前半小时」就存提前量，没说就按准时提醒。"
                        "带「叫我/提醒我」的一次性提醒也用这个。text 放用户原话。"
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
                    "name": "change_event",
                    "description": (
                        "改 / 挪 / 删 / 取消 / 跳过 已经记下的东西——不管是提醒还是日程："
                        "「把组会挪到周五上午十点」「下周三的课不上了」「删掉体检」"
                        "「取消明天早上的闹钟」「不用提醒我了」「所有课程提前半小时提醒」。"
                        "带重复规则的那条，它会先问一句确认再动手。text 给原话就行。"
                    ),
                    "parameters": text_only,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "now",
                    "description": "现在几点 / 今天几号 / 今天星期几。",
                    "parameters": text_only,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fix_last",
                    "description": (
                        "用户纠正**刚刚**记下的那一条的时间：「我说今天晚上8点45」「不对，"
                        "是明天早上」。刚记完一条、他紧接着只说了个新时间时，**用它**，"
                        "不要再新建一条（那样会响两次）。text 给原话。"
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
            "list_events": self._list_events,
            "next_event": self._next_event,
            "list_memos": self._list_memos,
            "add_event": self._add_event,
            "add_memo": self._add_memo,
            "change_event": self._change_event,
            "now": self._now,
            "fix_last": self._fix_last,
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

    def _list_events(self, args: dict) -> ToolResult:
        # ★允许不带 text★：模型问「还有哪些提醒」时可能什么都不给，
        # 那就退化成「列出全部待办」（合并前 list_alarms 就是不带参数的）。
        text = str(args.get("text") or "").strip() or "有哪些提醒"
        result = self._via(text, self.skills._handle_schedule_query)  # noqa: SLF001
        if result.ok:
            return result
        # 「还有哪些提醒没响」这类走列表
        listed = self._via(text, self.skills._event_list)  # noqa: SLF001
        if listed.ok:
            return listed
        # ★说了时间段的，先按时间答★（问的是「下周三下午有没有空」，不是「哪一条」）
        if self.skills.mentions_time(text):  # noqa: SLF001
            now = datetime.now()
            if self.skills.range_understood(text, now):  # noqa: SLF001
                when = self.skills._day_items(text, now)  # noqa: SLF001
                if when is not None and when.reply.strip():
                    return ToolResult(
                        True, action=when.action, reply=when.reply, data=when.data
                    )
            # ★别悄悄把时间段换成「今天」★：以前只要解析不出来就退到「今天」，
            # 于是「下周三下午」被答成「今天没有课程或会议安排」——模型拿这句当依据，
            # 直接下了结论（实测它就这么回了「下周三下午有空」）。如实说没听懂才对。
            return ToolResult(
                False,
                action="schedule_miss",
                error="这段时间没解析出来",
                reply="这个时间段我没听懂——换个说法？比如「下周三有什么安排」「这周呢」。",
            )
        # 模型常常把整句原话递过来（「导师见面那件事是什么时候」），
        # 时间范围解析不了 —— 那就按名字去库里找那一条。
        named = self._find_named(text)
        if named is not None:
            return named
        # 连时间都没提（「我的日程」）：给今天有什么，总比回一句「没听懂」有用
        return self._via("今天有什么安排", self.skills._handle_schedule_query)  # noqa: SLF001

    def _find_named(self, text: str) -> ToolResult | None:
        """按名字在日程里找（「导师见面那件事是什么时候」-> 跟导师见面 9/23 15:30）。"""
        now = datetime.now()
        items = self.skills.store.load()   # noqa: SLF001
        if not items:
            return ToolResult(False, action="event_miss", reply="现在什么都没记。")
        hits = self.skills._match_schedule_items(text, items, now)   # noqa: SLF001
        if not hits or hits[0][0] < 2:
            return None
        best = hits[0][1]
        nxt = self.skills._next_of(best, now)                       # noqa: SLF001
        when = f"{nxt.strftime('%m月%d日 %H:%M')}" if nxt else "（已经过期）"
        title = display_title(best)
        where = f"，地点{best['location']}" if best.get("location") else ""
        reply = f"{title}：{when}{where}，{self.skills._when_of(best, now)}。"  # noqa: SLF001
        if len(hits) > 1 and hits[1][0] >= hits[0][0]:
            names = "、".join(display_title(s[1]) for s in hits[:3])
            reply += f"（还有几条也对得上：{names}）"
        return ToolResult(True, action="event_find", reply=reply,
                          data={"title": title, "start": when})

    def _add_event(self, args: dict) -> ToolResult:
        """新增提醒或日程——**同一个入口**（不再分「闹钟」和「日程」两个工具）。"""
        text = self._text_arg(args)
        result = self._via(text, self.skills._handle_event)  # noqa: SLF001
        if result.ok:
            return result
        # 交白卷时给一句更像「没排成」的话；能看出「有事情、就差钟点」就直接问几点
        name = et.clean_content(text)
        looks_event = bool(ACTIVITY_NOUN.search(text) or TRIGGER_APPOINT.search(text)
                           or TRIGGER_ALARM.search(text))
        if looks_event and not has_clock_expr(text):
            result.reply = (
                f"「{name}」这件事我还没记上——几点开始？"
                f"把时间带上再说一遍就行，比如「下午三点{name}」。"
            )
        else:
            result.reply = "这条我没记上——说清楚点，比如「下周三下午三点半跟导师见面」。"
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

    def _next_event(self, _args: dict) -> ToolResult:
        result = self.skills._next_item(datetime.now())  # noqa: SLF001
        return ToolResult(bool(result.reply), action=result.action, reply=result.reply, data=result.data)

    def _list_memos(self, _args: dict) -> ToolResult:
        result = self.skills._handle_memo("我的备忘有哪些", datetime.now())  # noqa: SLF001
        if result is None:
            return ToolResult(False, action="memo_list", reply="备忘里现在是空的。")
        return ToolResult(True, action=result.action, reply=result.reply, data=result.data)

    # ------------------------------------------------- 改 / 取消 / 报时 / 纠正
    def _change_event(self, args: dict) -> ToolResult:
        """改 / 挪 / 删 / 取消 / 跳过——都走同一个入口（含两道「不会因为模型说反了就多一条」的闸门）。

        1. 句子里没「取消/删除/改成…」这类词，也不像在说已有的东西 → 直接回「没找到」，
           **不调**任何添加逻辑（实测：说「取消今晚十点的闹钟」时库里没有那条，
           旧代码一路走到新建，多出一条 what=「取消」的闹钟）；
        2. 结果必须是改/删类动作，不是的话就算回复能看也不当成功。
        """
        text = self._text_arg(args)
        now = datetime.now()
        result = self.skills._handle_schedule_change(text, now)  # noqa: SLF001
        if result is None:
            result = self.skills._cancel_by_time(text, now)  # noqa: SLF001
        if result is None:
            return ToolResult(
                False,
                action="event_change_miss",
                error="技能层没认出来",
                reply="我没找到要改的那一条，你说个名字？比如「把组会挪到周五上午十点」。",
            )
        return ToolResult(bool(result.reply), action=result.action, reply=result.reply, data=result.data)

    def _now(self, args: dict) -> ToolResult:
        text = str(args.get("text") or "").strip() or "现在几点"
        result = self.skills._handle_clock(text, datetime.now())  # noqa: SLF001
        if result is None:
            return ToolResult(
                False, action="clock_miss", error="这不是问时间的说法", reply="（没听懂问的是几点）"
            )
        return ToolResult(True, action=result.action, reply=result.reply, data=result.data)

    def _fix_last(self, args: dict) -> ToolResult:
        """「我说是今晚8点45」——改刚记下的那一条（而不是新建一条）。"""
        text = self._text_arg(args)
        result = self.skills._handle_correction(text, datetime.now())  # noqa: SLF001
        if result is None:
            return ToolResult(
                False,
                action="fix_last_miss",
                error="没有可改的那一条（刚记下的那条太久了，或者这句不像在改时间）",
                reply="我不确定你是要改哪一条，再说一遍要改成什么？",
            )
        return ToolResult(True, action=result.action, reply=result.reply, data=result.data)


def call_name(call: dict) -> str:
    """取出工具名（容忍 ``{"function": {...}}`` 与扁平两种形状）。"""
    return str(((call or {}).get("function") or call or {}).get("name") or "")


def call_text(call: dict) -> str:
    """取出这次调用的 ``text`` 参数（没有就返回空串）。"""
    args = ((call or {}).get("function") or call or {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    return str((args or {}).get("text") or "").strip()


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
    "repair_args",
    "reroute_correction",
    "call_name",
    "call_text",
]
