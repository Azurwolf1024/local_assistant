"""记忆服务器：把四级记忆暴露成 MCP 工具，让模型**自己决定**去查、去想。

为什么要有它（而不是只靠提示词注入）
    注入是「每轮自动带几条」——省事，但模型只能被动接受。有了工具，它就能：
        用户：「我们上个月是不是聊过这个？」
        模型：→ recall(query="这个", when="上个月")   ← 自己想要去翻旧账
    对应需求里的「提供信息检索和时间检索」（工程日志第 36 节）。

三个工具（默认只露出前两个）
    recall       按内容 + 时间检索记忆（事件 / 事实 / 知识库）
    remember     记一条（和用户聊天里说「记住…」等价）
    memory_stats 四层各有多少（默认不露出：工具越多小模型越容易选错，实测 8 个是甜点）

★隔离★：工具默认操作**当前角色**的记忆（`who` 可以显式指定，用于「凯尔希，你记一下」这类
明确切角色的场合）。角色来源按顺序找：
    1. 进程内（inproc）：宿主传进来的 `character` 回调 = 助手此刻的角色
    2. 子进程（stdio）：环境变量 `VOICE_LOOP_CHARACTER`
    3. 都拿不到 → 角色索引里的默认角色（跟助手一致）
所以「白泽记的事凯尔希查不到」在两条路上都成立。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ...settings import Settings
from ..server import MCPServer

NAME = "memory"

CHARACTER_ENV = "VOICE_LOOP_CHARACTER"     # stdio 模式下指定角色（inproc 用回调，不看它）
DATA_DIR_ENV = "VOICE_LOOP_DATA_DIR"       # 跟 skills 服务器同一套：把数据目录挪走

INSTRUCTIONS = (
    "这是助手的长期记忆。recall 用来「想起来」——尤其是用户提到过去的事、"
    "或者问「上次/上周/以前」时，先查一下再回答，不要凭印象编。"
    "remember 用来「记住」——用户明确说「记住…」、或者提到值得长期记住的偏好/事实时调用。"
    "查不到就如实说记不起来，不要编造。"
    "★记忆默认按角色隔离★：只能查自己的；有 memory_all 权限的角色（如白泽）可以用 who=\"*\" "
    "查全部，但★回答时必须说清每一条是从谁的记忆里查到的★。"
)

# who="*" 的写法（小模型可能写中文，都认）
ALL_ALIASES = {"*", "all", "全部", "所有", "全部角色", "所有角色", "每个角色", "everyone"}


# --------------------------------------------------------------------------- #
def _wants_all(who: str) -> bool:
    """这个 who 是要「所有人的记忆」吗。"""
    return str(who or "").strip().lower() in ALL_ALIASES


def _format_hit(hit) -> str:
    """把一条命中排成一行（★每条都带种类，跨角色时外面还会套上「谁记的」★）。"""
    item = hit.item
    if hit.kind == "episode":
        line = f"[那件事] {str(getattr(item, 'ts', ''))[:16]} {item.title}"
        return line + (f"：{item.summary}" if getattr(item, "summary", "") else "")
    if hit.kind == "fact":
        return f"[事实] {item.key.split('.')[-1]} = {item.value}"
    return f"[资料] {item.title}：{item.text[:160]}"


# --------------------------------------------------------------------------- #
# 依赖解析：inproc 传对象、stdio 自己造 —— 两条路共用下面这份逻辑
# --------------------------------------------------------------------------- #
def _maybe_call(value: Any) -> Any:
    """宿主可能传的是**回调**（要的是「此刻」的角色/hub），这里统一解一下。"""
    return value() if callable(value) else value


def settings_for_standalone() -> Settings:
    """stdio 模式下的配置：config.toml + `VOICE_LOOP_DATA_DIR` 覆盖。

    ★跟 skills 服务器同一套规矩★：标准输入输出服务器是个新进程，读的是真实 config.toml；
    测试或同时跑两个实例时，一句环境变量就能把数据（含记忆）挪到临时目录，
    否则一次自测就会往用户真实的记忆里写东西 —— 这个坑真踩过（工程日志 §36.4 第 8 条）。
    """
    from ...settings import load_settings

    settings = load_settings()
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        base = Path(override)
        settings.skills.data_dir = str(base)
        settings.skills.event_file = str(base / "events.json")
        settings.skills.memo_file = str(base / "memos.json")
        settings.skills.alarm_file = str(base / "alarms.json")
        settings.skills.schedule_file = str(base / "schedule.json")
    return settings


def default_character(settings: Settings) -> str:
    """角色索引里的默认角色（拿不到就 "default"）。★和命令行 `main.py memory` 同一条规矩★。"""
    env = os.environ.get(CHARACTER_ENV, "").strip()
    if env:
        return env
    try:
        from ...persona import CharacterRegistry

        registry = CharacterRegistry(settings.resolve(settings.persona.file))
        char = registry.get(settings.persona.default) or registry.default()
        if char is not None:
            return char.id
    except Exception:  # noqa: BLE001 - 角色文件坏了就退回 default，不要挡住记忆功能
        pass
    return "default"


def _character_knowledge_for(settings: Settings):
    """给独立进程用的「角色 → 知识库说明书」回调（进程内由 pipeline 注入）。"""

    def lookup(char_id: str) -> dict:
        try:
            from ...persona import CharacterRegistry, knowledge_spec_for

            registry = CharacterRegistry(settings.resolve(settings.persona.file))
            return knowledge_spec_for(registry, char_id)
        except Exception:  # noqa: BLE001
            return {"paths": [], "world": "", "title": "", "shared": True}

    return lookup


# --------------------------------------------------------------------------- #
# 服务器
# --------------------------------------------------------------------------- #
def build_server(
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    hub: Any = None,                 # ★宿主注入的回调★：要的是同一个 MemoryHub
    character: Any = None,           # 同上：要的是「此刻」的角色 id
    character_label: Any = None,     # 同上：角色 id → 给人看的名字（白泽）
    can_read_all: Any = None,        # 同上：当前角色有没有跨角色读的权限
    **_ignored: Any,                 # skills/其它宿主依赖，用不到就忽略
) -> MCPServer:
    log = logger or logging.getLogger("voice_loop")
    settings = settings or settings_for_standalone()
    from ...memory import MemoryHub
    from ...memory.schedule import SharedSchedule

    fallback_hub: MemoryHub | None = None

    def get_hub() -> MemoryHub:
        """优先用宿主注入的那个 hub（同一份缓存）；没有就自己建一个。"""
        got = _maybe_call(hub)
        if got is not None:
            return got
        nonlocal fallback_hub
        if fallback_hub is None:
            fallback_hub = MemoryHub(settings, schedule=SharedSchedule(settings),
                                     logger=log, character_knowledge=_character_knowledge_for(settings))
        return fallback_hub

    def who_of(args: dict) -> str:
        wanted = str((args or {}).get("who") or "").strip()
        if wanted and not _wants_all(wanted):
            return wanted
        now = _maybe_call(character)
        if isinstance(now, str) and now.strip():
            return now.strip()
        return default_character(settings)

    def label_of(char_id: str) -> str:
        """给用户看的称呼（白泽 而不是 baize）：优先用宿主给的取名回调。"""
        if callable(character_label):
            try:
                nice = str(character_label(char_id) or "").strip()
                if nice:
                    return nice
            except Exception:  # noqa: BLE001
                pass
        try:
            from ...persona import CharacterRegistry

            char = CharacterRegistry(settings.resolve(settings.persona.file)).get(char_id)
            if char is not None and char.name:
                return str(char.name)
        except Exception:  # noqa: BLE001
            pass
        return char_id

    def allowed_all(char_id: str) -> bool:
        """当前角色能不能查全部（★宿主回调优先，独立进程自己读人格文件★）。"""
        got = _maybe_call(can_read_all)
        if got is not None:
            return bool(got)
        try:
            from ...persona import CharacterRegistry, can_read_all_memory

            registry = CharacterRegistry(settings.resolve(settings.persona.file))
            return can_read_all_memory(registry, char_id)
        except Exception:  # noqa: BLE001 - 读不到就当没权限（权限宁严不松）
            return False

    def all_character_ids() -> list[str]:
        """全部角色：记忆目录里的 + 角色索引里的（只有知识库、还没记忆目录的也要算）。"""
        names = set(get_hub().characters())
        try:
            from ...persona import CharacterRegistry

            names |= {c.id for c in CharacterRegistry(
                settings.resolve(settings.persona.file)).all(only_enabled=True) if c.id}
        except Exception:  # noqa: BLE001
            pass
        return sorted(names)

    server = MCPServer(name=NAME, version="1.0.0", instructions=INSTRUCTIONS)

    def recall(args: dict) -> dict:
        query = str((args or {}).get("query") or "").strip()
        when = str((args or {}).get("when") or "").strip()
        try:
            limit = max(1, min(10, int((args or {}).get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        if not query and not when:
            return {"text": "要查什么？给我一个内容词或一个时间（例如「上周」）。", "is_error": True}
        who = who_of(args)
        wants_all = _wants_all(str((args or {}).get("who") or ""))

        # ★跨角色查询是权限★：能查，但每一条都要说清是谁的
        if wants_all:
            if not allowed_all(who):
                log.info(f"[mcp:memory.recall] {who} 没跨角色权限，已拒")
                return {"text": f"{label_of(who)}没有查所有人记忆的权限 —— "
                                "只能查自己的。需要的话在人格文件里加 \"memory_all\": true。",
                        "is_error": True}
            try:
                pairs = get_hub().recall_everywhere(query, when=when or None, limit=limit,
                                                   characters=all_character_ids())
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[mcp:memory.recall] 跨角色检索失败：{exc}")
                return {"text": f"记忆检索失败：{exc}", "is_error": True}
            if not pairs:
                return (f"查了 {len(all_character_ids())} 个角色的记忆，"
                        f"没有关于「{query or when}」的记录。可以如实说记不起来，不要编。")
            lines = [f"★跨角色（以 {label_of(who)} 的身份查）★查了 {len(all_character_ids())} 个角色，"
                     f"命中 {len(pairs)} 条。回答时★必须说清楚每一条是谁记的★："]
            for cid, hit in pairs:
                lines.append(f"- [{label_of(cid)}] {_format_hit(hit)}")
            log.info(f"[mcp:memory.recall] 跨角色 {query or when} → {len(pairs)} 条")
            return "\n".join(lines)

        try:
            mem = get_hub().for_character(who)
            hits = mem.recall(query, when=when or None, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 服务器不能因为一次检索崩掉
            log.warning(f"[mcp:memory.recall] 失败：{exc}")
            return {"text": f"记忆检索失败：{exc}", "is_error": True}
        if not hits:
            asked = f"「{query}」" if query else ""
            span = f"（时间范围：{when}）" if when else ""
            return f"没有相关记忆{asked}{span}。可以如实说记不起来，不要编。"
        lines = [f"关于「{query or when}」的记忆（来自 {label_of(who)}，{len(hits)} 条）："]
        lines.extend(f"- {_format_hit(hit)}" for hit in hits)
        log.info(f"[mcp:memory.recall] {who} {query or when} → {len(hits)} 条")
        return "\n".join(lines)

    def remember(args: dict) -> dict:
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"text": "要记住什么？把内容给我。", "is_error": True}
        who = who_of(args)
        try:
            mem = get_hub().for_character(who)
            episode, facts = mem.remember(text)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[mcp:memory.remember] 失败：{exc}")
            return {"text": f"没记下来：{exc}", "is_error": True}
        extra = f"，另外记下 {len(facts)} 条事实" if facts else ""
        log.info(f"[mcp:memory.remember] {who} 记住：{text[:40]}{extra}")
        return f"记下了（{who}）：{episode.title}{extra}"

    def memory_stats(args: dict) -> dict:
        who = who_of(args)
        try:
            stats = get_hub().for_character(who).stats()
        except Exception as exc:  # noqa: BLE001
            return {"text": f"读不到记忆统计：{exc}", "is_error": True}
        return "\n".join([
            f"角色：{stats['character']}",
            f"事件：{stats['episodes']} 条（平均重要度 {stats['avg_salience']}）",
            f"事实：{stats['facts']} 条（其中身份 {stats['pinned']} 条永不衰减）",
            f"原始对话：{stats['sessions_tracked']} 个已归档",
            f"知识库：{stats['knowledge']}",
        ])

    server.add_tool(
        "recall",
        "检索长期记忆：过去发生的事（事件）、记住的事实、知识库资料。"
        "用户提到过去（「上次」「上周三」「以前」）时先用它查，再回答；不要凭印象编。"
        "记忆按角色隔离，默认只查当前角色；★有权限的角色（白泽）可以给 who 传 \"*\" "
        "查全部人的记忆，那种情况下回答必须说明每一条是谁记的★。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查的内容关键词（可以留空，只按时间查）"},
                "when": {"type": "string",
                         "description": "时间范围：昨天/今天/上周/上周三/最近7天/上个月/去年，或 2026-09-20"},
                "limit": {"type": "integer", "description": "最多返回几条（1-10，默认 5）"},
                "who": {"type": "string",
                        "description": "查谁的记忆：留空 = 当前角色；\"*\" = 所有人（需权限，仅白泽）"},
            },
        },
        recall,
    )
    server.add_tool(
        "remember",
        "记一条长期记忆：用户的偏好、约定、重要事实，或用户明确说「记住…」时调用。"
        "整句原样传进来就行（会自动拆出事实）。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记住的事，一句话"},
                "who": {"type": "string", "description": "记到哪个角色名下（默认当前角色）"},
            },
            "required": ["text"],
        },
        remember,
    )
    server.add_tool(
        "memory_stats",
        "看记忆里各层有多少东西（事件/事实/知识库）。用户问「你记得我多少事」时可以用。",
        {
            "type": "object",
            "properties": {"who": {"type": "string", "description": "看哪个角色（默认当前角色）"}},
        },
        memory_stats,
    )
    return server
