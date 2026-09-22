"""角色设定：索引 + 独立人格文件。

为什么这么改
    以前人设是 ``[llm] system_prompt`` 里的一大段中文；结构化之后又全塞在
    ``data/characters.json`` 一个文件里——想给「第三个角色」试设定，得先挤进
    那个文件，而且只要写进去就等于**挂上了**（会被唤醒词命中、会切人设）。
    现在拆成两层：

        data/characters.json          ← 索引：只写「要加载哪些人格」+ 默认是谁
        data/personas/kaltsit.json    ← 一个人格一个文件（完整字段）
        data/personas/amiya.json
        data/personas/别人.json        ← 放着不挂：不会被加载、也不会被唤醒

    索引里的一条 = 一个「可唤醒的角色」：``{"id": ..., "file": "personas/kaltsit.json"}``。
    人格文件自己也可以带 ``enabled: false``/``default: true``，但**索引里的写法优先**
    （临时停用某位不用去改她的人格文件）。

人格文件字段（只列常用的，全部字段见 README 第 14 节）
    {
      "id": "kaltsit",                       // 可省略：省略时用文件名
      "name": "凯尔希",                       // 名字
      "title": "罗德岛医疗主管",                // 身份
      "background": "……",                    // 背景（世界观 + 性格由来 + 和「我」的关系）
      "user_title": "博士",                   // 对「我」的称呼
      "wake_words": ["凯尔希"],               // 唤醒词（多角色靠它区分）
      "aliases": {"凯尔希": ["开尔希", "老猫"]}, // 常被听错的写法
      "ack": "我在，博士。",                    // 被唤醒时的应答
      "style": ["冷静、克制"],                 // 说话风格（每条一句）
      "rules": ["不知道就直说不知道"],           // 硬性要求
      "avoid": ["不要用「作为一个AI」"],         // 明确不要出现什么
      "lines": [{"scene": "被唤醒", "text": "我在，博士。"}],
      "voice": "",                            // 可选：这个角色用自己的 piper 声线
      "voice_ref": "",                        // 可选：克隆音色的参考音频（wav，backend = "zipvoice" 时用）
      "voice_ref_text": "",                   // 可选：参考音频的逐字文本；留空则用同名 .txt 或自动转写
      "voice_model": "",                      // 可选：这个角色专用的 ZipVoice 模型目录
                                              //   （用 scripts/persona_voice.py 微调出来的，
                                              //    如 models/tts/zipvoice/personas/kaltsit）
      "voice_dir": "",                        // 可选：语音素材目录（空 = data/personas/<id>/）
      "temperature": 0.7,                     // 可选：覆盖全局温度（0 = 用全局）
      "notes": "给自己看的备注"                 // 不进提示词
    }

热加载
    索引和**所有人格文件**的改动都会被监听到（``maybe_reload``），保存即生效。
    新写一个人格文件、在索引里加一行，她就上线了；删掉那一行就下线（文件留着）。

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
# 人格文件放在索引文件所在目录的这个子目录里（`data/personas/`）
PERSONA_DIR = "personas"
# 索引里除了 id/file/enabled/default 之外的键，都是「说明」，不该写进人格文件
_INDEX_ONLY_KEYS = {"file", "enabled", "default", "_说明", "_用法", "_字段说明", "_note"}


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
    voice_ref: str = ""                   # 克隆音色的参考音频（backend = "zipvoice" 时生效）
    voice_ref_text: str = ""              # 参考音频的逐字文本（空 = 同名 .txt / 自动转写）
    voice_model: str = ""                 # 这个角色专用的 ZipVoice 模型目录
                                          #   （空 = 用 [tts] clone_dir；见 scripts/persona_voice.py）
    voice_dir: str = ""                   # 语音素材目录（空 = data/personas/<id>/）
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
            voice_ref=str(raw.get("voice_ref") or "").strip(),
            voice_ref_text=str(raw.get("voice_ref_text") or "").strip(),
            voice_model=str(raw.get("voice_model") or "").strip(),
            voice_dir=str(raw.get("voice_dir") or "").strip(),
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
    """角色清单：读索引 + 人格文件；热重载、按唤醒词找角色、拼 system prompt。

    ``path`` 指的是**索引文件**（``data/characters.json``）。人格文件里的
    ``enabled`` / ``default`` 可以被索引里的同名键覆盖——这样临时停用某位
    不用去改她的人格文件。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.dir = self.path.parent
        self._watch: dict[Path, float] = {}
        self.characters: list[Character] = []
        self.files: dict[str, Path] = {}      # 角色 id → 人格文件（从文件加载的才有）
        self.orphans: list[Path] = []         # 放在 personas/ 里但索引没挂上
        self.warnings: list[str] = []
        self.load()

    # ------------------------------------------------------------------ 读写
    @staticmethod
    def write_default(path: str | Path) -> None:
        """没有索引/人格文件时写一份默认的（凯尔希 + 阿米娅）。已有的文件不动。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        persona_dir = p.parent / PERSONA_DIR
        persona_dir.mkdir(parents=True, exist_ok=True)
        for cid, data in DEFAULT_PERSONAS.items():
            f = persona_dir / f"{cid}.json"
            if not f.exists():
                f.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
        if not p.exists():
            p.write_text(
                json.dumps(DEFAULT_INDEX, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _read_json(path: Path) -> tuple[dict | None, str]:
        """读一个 json，返回 (内容, 错误说明)。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return None, "找不到文件"
        except OSError as exc:
            return None, f"读不了（{exc}）"
        except json.JSONDecodeError as exc:
            return None, f"解析失败（{exc}）"
        if not isinstance(raw, dict):
            return None, "内容不是一个 JSON 对象"
        return raw, ""

    def _entry(self, item: Any) -> tuple[Path | None, dict]:
        """索引里的一条 → (人格文件路径 | None, 覆盖项/内联内容)。"""
        if isinstance(item, str):                     # 简写：直接给文件路径
            item = {"file": item}
        if not isinstance(item, dict):
            return None, {}
        raw_file = str(item.get("file") or "").strip()
        if raw_file:
            f = Path(raw_file)
            return (f if f.is_absolute() else self.dir / f), item
        return None, item                             # 没写 file = 旧的内联写法

    def load(self, force: bool = False) -> list[Character]:
        mtimes = self._mtimes()
        if not force and mtimes and mtimes == self._watch and self.characters:
            return self.characters

        self.warnings = []
        if not self.path.exists():
            self.write_default(self.path)
        raw, err = self._read_json(self.path)
        if raw is None:
            self.warnings.append(f"{self.path.name} {err}，沿用上一次的设定")
            for w in self.warnings:
                print(f"[persona] {w}")
            return self.characters

        entries = raw.get("characters")
        if not isinstance(entries, list):
            entries = []
        default_key = str(raw.get("default") or "").strip()

        chars: list[Character] = []
        files: dict[str, Path] = {}
        seen: set[str] = set()
        for item in entries:
            src, overrides = self._entry(item)
            if src is not None:
                data, err = self._read_json(src)
                if data is None:
                    self.warnings.append(
                        f"人格文件 {src.name} {err}，已跳过（索引里挂着它）"
                    )
                    continue
                if not data.get("id"):
                    data = {**data, "id": src.stem}   # 省略 id 时用文件名
            else:
                data = dict(overrides)
                name = data.get("id") or data.get("name") or "（没写名字）"
                self.warnings.append(
                    f"索引里直接写了角色「{name}」——建议拆成独立人格文件："
                    f"python main.py persona --split"
                )
            char = Character.from_dict(data)
            # 索引里的 enabled / default 覆盖人格文件里的（临时停用不用改人格文件）
            for key in ("enabled", "default"):
                if isinstance(overrides, dict) and key in overrides:
                    setattr(char, key, bool(overrides[key]))
            if default_key and (char.id == default_key or char.name == default_key):
                char.default = True
            if not char.id:
                self.warnings.append("有角色既没写 id 也没写 name，已跳过")
                continue
            if char.id in seen:
                self.warnings.append(f"角色 id 重复：{char.id}（只保留第一个）")
                continue
            seen.add(char.id)
            chars.append(char)
            if src is not None:
                files[char.id] = src

        if not chars:
            self.warnings.append("索引里一个可用角色都没有（不会有人被唤醒）")
        self.characters = chars
        self.files = files
        self.orphans = self._scan_orphans(keep=set(seen))
        self._watch = self._mtimes()
        for w in self.warnings:
            print(f"[persona] {w}")
        return chars

    def _mtimes(self) -> dict[Path, float]:
        """关注的文件的 mtime：索引 + 已加载的人格文件（任何一个变了都要重载）。"""
        out: dict[Path, float] = {}
        for p in [self.path, *self.files.values()]:
            try:
                out[p] = p.stat().st_mtime
            except OSError:
                continue
        return out

    def _scan_orphans(self, keep: set[str]) -> list[Path]:
        """``personas/`` 里放着、但索引没挂上的人格文件（不会被加载/唤醒）。"""
        folder = self.dir / PERSONA_DIR
        if not folder.is_dir():
            return []
        used = {p.resolve() for p in self.files.values()}
        out: list[Path] = []
        for p in sorted(folder.glob("*.json")):
            if p.resolve() in used:
                continue
            data, _err = self._read_json(p)
            if data is not None and not data.get("id"):
                continue          # 连 id/name 都没有：不是人格文件，别乱报
            out.append(p)
        return out

    def split_inline(self) -> list[Path]:
        """把索引里**直接写着**的角色拆成独立人格文件，索引改成指向它们。

        给旧版（角色全塞在 ``characters.json`` 里）用的迁移：``main.py persona --split``。
        已经拆过的再跑一次也不会重复写（索引里没有内联角色就直接返回空）。
        """
        raw, err = self._read_json(self.path)
        if raw is None:
            raise RuntimeError(f"{self.path} {err}")
        entries = raw.get("characters")
        if not isinstance(entries, list):
            return []
        persona_dir = self.dir / PERSONA_DIR
        created: list[Path] = []
        new_entries: list[dict] = []
        for item in entries:
            src, overrides = self._entry(item)
            if src is not None:                 # 已经是文件引用了，原样保留
                new_entries.append(item)
                continue
            data = {k: v for k, v in dict(overrides).items() if k not in _INDEX_ONLY_KEYS}
            cid = str(data.get("id") or data.get("name") or "").strip()
            if not cid:
                continue
            data["id"] = cid
            persona_dir.mkdir(parents=True, exist_ok=True)
            f = persona_dir / f"{cid}.json"
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            created.append(f)
            entry: dict = {"id": cid, "file": f"{PERSONA_DIR}/{f.name}"}
            for key in ("enabled", "default"):
                if key in overrides:
                    entry[key] = overrides[key]
            new_entries.append(entry)

        if not created:
            return []
        new_index = {k: v for k, v in raw.items() if k != "characters"}
        new_index["characters"] = new_entries
        self.path.write_text(
            json.dumps(new_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.load(force=True)
        return created

    def reload(self) -> None:
        self.load(force=True)

    def maybe_reload(self) -> bool:
        """索引或人格文件被改过就重载；返回「内容是否真的变了」。"""
        mtimes = self._mtimes()
        if not mtimes or mtimes == self._watch:
            return False
        before = self._signature()
        self.load(force=True)
        return before != self._signature()

    def _signature(self) -> list[tuple]:
        return [(c.id, c.name, tuple(c.wake_words), c.ack) for c in self.characters]

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
        names = "、".join(
            f"{c.name}({'/'.join(c.wake_words) or '无唤醒词'}{'' if c.enabled else '，停用'})"
            for c in self.characters
        )
        line = f"{len(on)}/{len(self.characters)} 个角色启用：{names or '（无）'}"
        if self.orphans:
            stems = "、".join(p.stem for p in self.orphans)
            line += f"；库里另有 {len(self.orphans)} 个没挂上（不会被唤醒）：{stems}"
        return line


# --------------------------------------------------------------------------- #
# 默认人格：凯尔希填好（可直接用），阿米娅作为「第二个角色」的现成例子。
# 索引（DEFAULT_INDEX）只负责「挂上谁」——库里有几个、挂哪个都行。
# --------------------------------------------------------------------------- #
PERSONA_KALTSIT: dict = {
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
}

PERSONA_AMIYA: dict = {
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
}

# 库里自带的人格（首次运行会写到 ``data/personas/`` 下）
DEFAULT_PERSONAS: dict[str, dict] = {
    "kaltsit": PERSONA_KALTSIT,
    "amiya": PERSONA_AMIYA,
}

# 索引：只写「要加载哪些人格」+ 默认是谁
DEFAULT_INDEX: dict = {
    "_说明": "角色索引。改完保存即生效（人格文件改动也会被监听到），无需重启。",
    "_用法": "characters 里每一条 = 一个**可唤醒**的角色。写 {\"id\", \"file\"} 或直接写文件路径；"
             "不在这里的人格文件不会被加载，也不会被唤醒（放心把人设放着）。",
    "_字段说明": {
        "characters": "要加载的人格文件列表 = 可唤醒的角色",
        "file": "人格文件的路径，相对本文件所在目录（也可以写绝对路径）；"
                "默认约定放在 personas/ 子目录里",
        "id": "索引里的标识（可省略，默认用人格文件名）；--character 用它",
        "enabled": "false = 这位不参与唤醒（人格文件不动，随时改回来）",
        "default": "没指定角色时用谁。也可以写在人格文件里；两边都写时以索引为准",
        "_人格文件里的字段": "name / title / background / user_title / wake_words / aliases / "
                          "ack / style / rules / avoid / lines / voice / temperature / notes，"
                          "详见 README 第 14 节",
    },
    "default": "kaltsit",
    "characters": [
        {"id": "kaltsit", "file": "personas/kaltsit.json", "enabled": True, "default": True},
        {"id": "amiya", "file": "personas/amiya.json", "enabled": True},
    ],
}

# 兼容旧名字（以前只有一个大文件，现在它只是索引）
DEFAULT_CONFIG = DEFAULT_INDEX

__all__ = [
    "Character",
    "CharacterRegistry",
    "DEFAULT_CONFIG",
    "DEFAULT_FILE",
    "DEFAULT_INDEX",
    "DEFAULT_PERSONAS",
    "PERSONA_DIR",
    "SPEECH_RULES",
    "render_system_prompt",
]
