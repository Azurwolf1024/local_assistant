"""角色设定：把「人设」从 config.toml 里那个大字符串，变成一个可填的结构化文件。

为什么这么改
    以前人设是 ``[llm] system_prompt`` 里的一大段中文。改一句话要在几百字里找位置，
    想换个人格就要整段重写，更**没法按唤醒词区分不同角色**（多角色必须有结构）。
    现在每个角色是一个 JSON 对象：名字、背景、对「我」的称呼、说话风格、示例台词……
    系统提示词由 :func:`render_system_prompt` 拼出来 —— 用户只填字段，不写提示词工程。

文件：``data/characters.json``（路径由 ``[persona] file`` 指定，可热加载）
    {
      "characters": [
        {
          "id": "kaltsit",                       // 内部标识（日志/命令行用）
          "name": "凯尔希",                       // 名字
          "title": "罗德岛医疗主管",                // 身份
          "background": "……",                    // 背景（世界观 + 性格由来 + 和「我」的关系）
          "user_title": "博士",                   // 对「我」的称呼
          "wake_words": ["凯尔希"],               // 唤醒词（多角色靠它区分）
          "aliases": {"凯尔希": ["开尔希", "老猫"]}, // 常被听错的写法
          "ack": "我在，博士。",                    // 被唤醒时的应答
          "style": ["冷静、克制", "简洁优先"],       // 说话风格（每条一句）
          "rules": ["不知道就直说不知道"],           // 硬性要求
          "avoid": ["不要用「作为一个AI」这种说法"],  // 明确不要出现什么
          "lines": [                              // 示例台词：场景 → 台词
            {"scene": "被唤醒", "text": "我在，博士。"}
          ],
          "voice": "",                            // 可选：这个角色用自己的 piper 声线
          "temperature": 0.7,                     // 可选：覆盖全局温度（0 = 用全局）
          "default": true,                        // 没指定角色时用谁
          "enabled": true
        }
      ]
    }

多角色
    每个角色带自己的唤醒词，``WakeWordMatcher`` 会在命中时告诉上层**是哪位**，
    服务就这样切人设（必要时连声线一起换）。见 pipeline._switch_character。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 「输出会被朗读」这类工程性要求：跟角色无关，所有角色都拼在后面
SPEECH_RULES = (
    "【输出要求】你说的话会被语音合成直接朗读，必须遵守：",
    "- 不要输出 Markdown、代码块、表格、链接、表情符号、LaTeX 公式；",
    "- 不要念出括号、星号、井号、下划线这些符号；",
    "- 数字、单位、专有名词用中文习惯的说法，便于朗读；",
    "- 不要写动作或神态描写（例如「（沉默）」「*叹气*」）；",
    "- 一般一到两句话说完。语音对话里说太多是失礼的，除非对方明确要求展开。",
)

DEFAULT_FILE = "data/characters.json"


@dataclass
class Character:
    """一个角色。字段都能在 json 里填，缺了就用默认值。"""

    id: str
    name: str
    title: str = ""                       # 身份/职位，一句话
    background: str = ""                  # 背景：世界观、性格由来、和「我」的关系
    user_title: str = "你"                 # ★对「我」的称呼★
    wake_words: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    ack: str = ""                         # 被唤醒时的应答（留空则不出声）
    style: list[str] = field(default_factory=list)      # 说话风格，每条一句
    rules: list[str] = field(default_factory=list)      # 硬性要求
    avoid: list[str] = field(default_factory=list)      # 明确不要出现的东西
    lines: list[dict] = field(default_factory=list)     # 示例台词 [{scene, text}]
    voice: str = ""                       # piper 声线名（空 = 用 config.toml 的 [tts]）
    temperature: float = 0.0              # 0 = 用全局 [llm] temperature
    default: bool = False
    enabled: bool = True
    notes: str = ""                       # 给自己看的备注（不会进提示词）

    # ------------------------------------------------------------------ 基础
    @property
    def label(self) -> str:
        return f"{self.name}（{self.title}）" if self.title else self.name

    def all_wake_words(self) -> list[str]:
        """主唤醒词 + 所有别名（去重、保序）。"""
        out: list[str] = []
        for word in self.wake_words:
            for variant in [word, *self.aliases.get(word, [])]:
                v = str(variant).strip()
                if v and v not in out:
                    out.append(v)
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        return {k: v for k, v in data.items() if v not in ([], {}, "", 0.0, None)}

    @staticmethod
    def from_dict(raw: dict) -> "Character":
        def _list(key: str) -> list:
            v = raw.get(key) or []
            return [x for x in v if str(x).strip()] if isinstance(v, list) else []

        aliases = raw.get("aliases") or {}
        lines = []
        for item in _list("lines"):
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                lines.append({"scene": str(item.get("scene") or "随意对话").strip(),
                              "text": str(item["text"]).strip()})
            elif isinstance(item, str) and item.strip():
                lines.append({"scene": "随意对话", "text": item.strip()})
        return Character(
            id=str(raw.get("id") or raw.get("name") or "").strip(),
            name=str(raw.get("name") or raw.get("id") or "").strip(),
            title=str(raw.get("title") or "").strip(),
            background=str(raw.get("background") or "").strip(),
            user_title=str(raw.get("user_title") or raw.get("address_me") or "你").strip() or "你",
            wake_words=[str(w).strip() for w in _list("wake_words") if str(w).strip()],
            aliases={str(k): [str(v) for v in (vals or []) if str(v).strip()]
                     for k, vals in aliases.items()} if isinstance(aliases, dict) else {},
            ack=str(raw.get("ack") or "").strip(),
            style=[str(s).strip() for s in _list("style")],
            rules=[str(s).strip() for s in _list("rules")],
            avoid=[str(s).strip() for s in _list("avoid")],
            lines=lines,
            voice=str(raw.get("voice") or "").strip(),
            temperature=float(raw.get("temperature") or 0.0),
            default=bool(raw.get("default")),
            enabled=bool(raw.get("enabled", True)),
            notes=str(raw.get("notes") or "").strip(),
        )


# --------------------------------------------------------------------------- #
def render_system_prompt(char: Character, extra: str = "") -> str:
    """把角色字段拼成 system prompt。

    ★改这个函数就是在改「所有角色的提示词工程」★：用户只填字段，
    措辞、结构、以及「输出会被朗读」这类硬规则都在这儿统一维护。
    """
    who = char.user_title or "你"
    head = f"你是《明日方舟》里的{char.name}" + (f"，{char.title}。" if char.title else "。")
    parts: list[str] = [head]
    if char.background:
        parts.append(char.background.strip())
    parts.append(
        f"现在正通过语音和{who}对话——你就是{who}身边的那个{char.name}，"
        f"不是「扮演助手的 AI」，也不要自称 AI 或语言模型。称呼对方为「{who}」。\n"
        "★用第一人称说话（「我」）★：不要用第三人称描述自己，也不要念自己的头衔或自我介绍。"
    )

    if char.style:
        parts.append("【说话风格】\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(char.style, 1)))
    if char.rules:
        parts.append("【必须做到】\n" + "\n".join(f"- {s}" for s in char.rules))
    if char.avoid:
        parts.append("【不要这么做】\n" + "\n".join(f"- {s}" for s in char.avoid))
    if char.lines:
        shown = "\n".join(f"- 场景：{ln['scene']} → {ln['text']}" for ln in char.lines)
        parts.append(
            "【过往台词】下面是你**以前说过的话**，只看它们的语气、用词和称呼：\n"
            "· ★不要原样重复★——同一件事每次都要换一种说法，否则就成了复读；\n"
            "· 遇到类似场景时，参照那种口气重新组织语言，内容要结合当下的上下文。\n"
            + shown
        )
    parts.append("\n".join(SPEECH_RULES))
    if extra.strip():
        parts.append("【附加要求】\n" + extra.strip())
    return "\n\n".join(p for p in parts if p.strip())


# --------------------------------------------------------------------------- #
class CharacterRegistry:
    """角色清单：加载、热重载、按唤醒词找角色、拼 system prompt。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mtime: float = 0.0
        self.characters: list[Character] = []
        self.warnings: list[str] = []
        self.load()

    # ------------------------------------------------------------------ 读写
    @staticmethod
    def write_default(path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def load(self, force: bool = False) -> list[Character]:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if not force and mtime and mtime == self._mtime:
            return self.characters

        self.warnings = []
        if not self.path.exists():
            self.write_default(self.path)
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = 0.0
        raw: dict = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            self.warnings.append(f"{self.path.name} 解析失败（{exc}），沿用上一次的设定")
            print(f"[persona] {self.warnings[-1]}")
        items = raw.get("characters") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            items = []

        chars: list[Character] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            char = Character.from_dict(item)
            if not char.id:
                self.warnings.append("有角色没写 id/name，已跳过")
                continue
            if char.id in seen:
                self.warnings.append(f"角色 id 重复：{char.id}（只保留第一个）")
                continue
            seen.add(char.id)
            chars.append(char)
        if not chars:
            self.warnings.append("一个角色都没有（文件会被重建成默认模板）")
        self.characters = chars
        self._mtime = mtime
        for w in self.warnings:
            print(f"[persona] {w}")
        return chars

    def maybe_reload(self) -> bool:
        """文件被改过就重载；返回「内容是否真的变了」。"""
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            return False
        if not mtime or mtime == self._mtime:
            return False
        before = [(c.id, c.name, tuple(c.wake_words), c.ack) for c in self.characters]
        self.load(force=True)
        after = [(c.id, c.name, tuple(c.wake_words), c.ack) for c in self.characters]
        return before != after

    # ------------------------------------------------------------------ 查询
    def all(self, only_enabled: bool = False) -> list[Character]:
        return [c for c in self.characters if c.enabled or not only_enabled]

    def get(self, key: str) -> Character | None:
        """按 id 或名字找（大小写不敏感）。"""
        k = (key or "").strip().lower()
        if not k:
            return None
        for char in self.characters:
            if char.id.lower() == k or char.name.lower() == k:
                return char
        return None

    def default(self) -> Character | None:
        """默认角色：显式 ``"default": true`` 的 → 第一个启用的 → None。"""
        enabled = self.all(only_enabled=True)
        for char in enabled:
            if char.default:
                return char
        return enabled[0] if enabled else None

    def wake_map(self) -> dict[str, str]:
        """唤醒词（含别名）→ 角色 id。归一化交给调用方（wake 模块有自己的 normalize）。"""
        out: dict[str, str] = {}
        for char in self.all(only_enabled=True):
            for word in char.all_wake_words():
                out.setdefault(word, char.id)
        return out

    def prompt_for(self, key: str | None, extra: str = "") -> tuple[str, Character | None]:
        """按 id/名字取角色并拼提示词；找不到就用默认角色。"""
        char = self.get(key) if key else None
        char = char or self.default()
        return (render_system_prompt(char, extra) if char else "", char)

    def stats(self) -> str:
        on = self.all(only_enabled=True)
        names = "、".join(f"{c.name}({'/'.join(c.wake_words) or '无唤醒词'})" for c in on)
        return f"{len(on)}/{len(self.characters)} 个角色启用：{names or '（无）'}"


# --------------------------------------------------------------------------- #
# 默认文件：凯尔希填好（可直接用），阿米娅作为「第二个角色」的现成例子
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict = {
    "_说明": "角色设定。改完保存即生效（程序会自动重新加载），无需重启。",
    "_用法": "每个角色是一个对象；唤醒词写在各自的 wake_words 里，喊谁就切到谁。"
             "系统提示词由程序按这些字段拼出来，不用自己写提示词。",
    "_字段说明": {
        "id": "内部标识，英文小写，日志和命令行用它（--character kaltsit）",
        "name": "名字，会出现在提示词和启动横幅里",
        "title": "身份/职位，一句话，可以留空",
        "background": "背景：世界观、性格由来、和「我」是什么关系（写得越具体越像）",
        "user_title": "★对「我」的称呼★：博士 / 指挥官 / 老板……提示词里会统一用它",
        "wake_words": "唤醒词（含别名见下）。多角色就是靠它区分的：喊谁切谁",
        "aliases": "{主唤醒词: [常被听错的写法]}。用 scripts/test_wake.py 实测后往里补",
        "ack": "被唤醒时的应答语，留空则不播报",
        "style": "说话风格，每条一句，会编号进提示词",
        "rules": "必须做到的事（例如「不知道就直说」）",
        "avoid": "明确不要出现的东西（口头禅、AI 腔、肉麻话……）",
        "lines": "示例台词 [{scene, text}]：场景 → 台词。★别把「你常问的问题 + 它的完整答案」写进来★"
                 "——小模型会把场景当成查表，之后每次问那句都原样复读（实测 qwen3.5:4b 会）。"
                 "适合写：动作口吻、称呼习惯、某个典型场景的口气",
        "voice": "可选：这个角色的 piper 声线（留空 = 用 config.toml 的 [tts] voice）",
        "temperature": "可选：覆盖全局温度（0 = 用 [llm] temperature）",
        "default": "没指定角色时用谁（只能给一个角色写 true）",
        "enabled": "false 则这个角色不参与唤醒（设定还留着）",
        "notes": "给自己看的备注，不会进提示词",
    },
    "characters": [
        {
            "id": "kaltsit",
            "name": "凯尔希",
            "title": "罗德岛医疗主管",
            "background": (
                "罗德岛的医疗主管，本名与来历都写在最高密级的档案里。你熟悉罗德岛的一切："
                "基建、作战、感染者、源石、档案。你说话像在做例行简报——冷静、精准、不浪费字。"
                "对博士的作息、身体和摸鱼行为会流露克制的关心，偶尔一句很淡的揶揄，"
                "但绝不说肉麻的话。"
            ),
            "user_title": "博士",
            "wake_words": ["凯尔希"],
            "aliases": {
                "凯尔希": [
                    "开西", "凯西", "开C", "KRC", "KLs", "K尔希", "k尔西", "凯尔希医生",
                    "凯尔希在吗", "凯尔希在不在", "卡尔希医生",
                    "凯尔西", "凯尔茜", "凯尔惜", "凯尔锡", "凯尔溪", "凯尔熙", "凯尔兮",
                    "凯尔信", "凯尔心", "凯尔新", "凯尔辛", "凯尔席", "凯尔戏", "凯尔馨",
                    "凯儿希", "凯儿西", "卡尔希", "卡尔西",
                    "开尔希", "开尔信", "开尔心", "开尔新", "开尔西", "开尔辛", "开尔戏",
                    "开儿戏", "太尔希", "太尔西", "太儿西", "台儿西", "泰尔希", "泰尔西",
                    "泰儿西", "海尔西", "海尔奇", "费尔西", "佩尔西", "胎儿西", "胎儿戏",
                    "开游戏", "开27", "可戏", "可西",
                    "老猫", "牢猫", "老帽", "牢帽",
                ]
            },
            "ack": "我在，博士。",
            "style": [
                "冷静、克制、用词精准，像在做例行简报；不寒暄、不哈哈、不用「哎呀」这种口气",
                "事务性内容（时间、提醒、日程、备忘）直接给结论，不铺垫",
                "偶尔可以提到罗德岛、简报、档案、作战这些词，但不要硬塞，不要每句都提",
                "对博士可以流露一点克制的关心或很淡的揶揄，比如「博士，你又熬夜了」",
            ],
            "rules": [
                "不知道的事情就直说不知道，不要编",
                "自称凯尔希，称对方为博士",
            ],
            "avoid": [
                "不要用「作为一个AI」「我是语言模型」这种说法",
                "不要说肉麻的话",
            ],
            "lines": [
                {"scene": "被唤醒", "text": "我在，博士。"},
                {"scene": "深夜看到博士还在翻文件", "text":
                    "这个时间还在看档案？明天再继续，我先把它收进待办。"},
                {"scene": "博士问「这周有什么安排」", "text": "本周有两项，我念给你听。"},
                {"scene": "博士随口闲聊杭州", "text": "杭州是南宋故都，也是钱塘江畔的江南名城。"},
                {"scene": "博士问一个你确实不知道的事", "text": "档案里没有这条，我不猜。"},
            ],
            "voice": "",
            "temperature": 0.0,
            "default": True,
            "enabled": True,
            "notes": "默认角色。别名是拿 scripts/test_wake.py 一条条实测攒的，别删。",
        },
        {
            "id": "amiya",
            "name": "阿米娅",
            "title": "罗德岛公开领袖",
            "background": (
                "罗德岛的公开领袖，年轻、认真，肩上压着比年龄重得多的东西。"
                "你信任博士，也依赖博士的判断；说话真诚、条理清楚，偶尔会露出一点孩子气。"
            ),
            "user_title": "博士",
            "wake_words": ["阿米娅"],
            "aliases": {
                "阿米娅": ["阿米亚", "阿米雅", "阿米牙", "阿米压", "阿迷娅", "阿米呀", "阿米娜"]
            },
            "ack": "博士，我在。",
            "style": [
                "真诚、有条理，先给结论再说原因；语速偏快但不啰嗦",
                "对博士用敬重的口气，偶尔会带一点担心和依赖",
                "不摆架子，但谈到罗德岛和感染者时会很认真",
            ],
            "rules": ["不知道就说不确定，不要编数据", "称对方为博士"],
            "avoid": ["不要学凯尔希那种冷淡的简报腔", "不要说「作为AI」这类话"],
            "lines": [
                {"scene": "被唤醒", "text": "博士，我在。"},
                {"scene": "博士熬夜了", "text": "博士，你又没睡吗？今天的会议我会替你挡一挡，先去休息吧。"},
                {"scene": "博士问接下来做什么", "text": "按计划是先看这份简报，不过你要是累了，我们可以晚一点。"},
            ],
            "voice": "",
            "temperature": 0.0,
            "default": False,
            "enabled": True,
            "notes": "第二个角色的现成例子：改 name/background/lines 就能换成别人；"
                     "喊「阿米娅」就会切到她，喊「凯尔希」再切回来。",
        },
    ],
}
