"""角色资料卡：在控制台里**按格式新建**或**修改**一个人格文件，也可以**选个文件导进来**。

为什么要有它：新加一个角色原来得手写 JSON（README 第 14 节那一大段字段），
而人格文件是★白名单式解析★（`Character.from_dict`）—— 字段名写错、或者写到
不认识的位置，**不会报错，只会被静默忽略**，于是「我明明写了风格，她却不这么说话」。
所以这里把「格式」变成唯一真相源：后端校验、前端表单、导出的模板都用同一份 `SPEC`。

写文件只有两处（都带 `.bak` + 写完当场校验 JSON）：
1. ``data/personas/<id>.json`` —— 资料卡本体（新建 :func:`create` / 修改 :func:`update`）；
2. ``data/characters.json`` —— 索引里追加一行（★只有索引里有的角色才会被唤醒★）。

★改（update）和覆盖（create+overwrite）不是一回事★：表单只覆盖它自己那几栏，
`enabled` / `default` / `voice_dir` 这些「表单不暴露」的键必须原样保住 ——
见 :func:`merge_into`。

★默认 dry-run★：`create()` 先报告「会写哪两个文件、加哪一行、有没有问题」，
控制台点「创建」才真写。id 必须是安全文件名（不能带 `/`、`\\`、`..` 这类）——
这条不是洁癖：它是拼路径用的。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..manifest import parse_manifest
from ..persona import Character

# id 直接当文件名用，所以只允许安全字符（小写字母/数字/下划线/短横）
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PERSONA_SUBDIR = "personas"          # 约定：人格文件放在索引同目录的 personas/ 下
IMPORT_EXTS = (".json", ".txt")

# --------------------------------------------------------------------------- #
# ★字段规范：唯一真相源★（后端校验 + 前端表单 + 导出模板都用它）
# kind: text | textarea | list（每行一条） | json（对象/数组） | number | bool | select
SPEC: list[dict] = [
    {
        "title": "① 身份（必填两栏）",
        "note": "id 是文件名，写完不要再改（改名 = 换了一个角色）；name 是她自称/被称呼的名字。",
        "fields": [
            {"key": "id", "label": "id（文件名）", "kind": "text", "required": True,
             "placeholder": "小写英文/数字/下划线，如 shining",
             "hint": "只能是 a-z 0-9 _ -，2~32 位；它同时是文件名和 --character 用的标识"},
            {"key": "name", "label": "名字", "kind": "text", "required": True,
             "placeholder": "对话里显示的名字", "hint": "例：凯尔希"},
            {"key": "title", "label": "身份一句话", "kind": "text",
             "placeholder": "例：罗德岛医疗主管", "hint": "写在名字下面那行，一行说完"},
            {"key": "background", "label": "背景", "kind": "textarea", "rows": 3,
             "placeholder": "世界观、性格由来、和「我」的关系…",
             "hint": "★不要写「她是……，会……」这种第三人称介绍★：提示词里会加「用第一人称」，"
                     "但这里写成第一人称的口吻更稳"},
            {"key": "user_title", "label": "她对「我」的称呼", "kind": "text",
             "placeholder": "你", "hint": "例：博士。留空 = 「你」"},
        ],
    },
    {
        "title": "② 唤醒与应答",
        "note": "喊谁切谁靠这里的 wake_words；aliases 是「喊错也认」的说法（ASR 常把生僻名听岔）。",
        "fields": [
            {"key": "wake_words", "label": "唤醒词", "kind": "list", "required": True,
             "hint": "一行一个；至少写一个，否则只能手动切换，喊不出来"},
            {"key": "aliases", "label": "别名（喊错也认）", "kind": "json",
             "placeholder": "{\"凯尔希\": [\"开尔信\", \"凯尔西\"]}",
             "hint": "键是唤醒词，值是「听错的说法」列表；实测工具：python scripts/test_wake.py"},
            {"key": "ack", "label": "应答语", "kind": "text",
             "placeholder": "在的", "hint": "被唤醒时先说的那一句（留空则不出声）"},
        ],
    },
    {
        "title": "③ 说话风格（直接进提示词）",
        "note": "这几栏就是「人设」本体：写得越具体越像她。示例台词会被她学语气，别写成问答对。",
        "fields": [
            {"key": "style", "label": "风格", "kind": "list", "hint": "一行一条，如「语速快、句子短」"},
            {"key": "rules", "label": "硬规矩", "kind": "list",
             "hint": "一行一条，如「不知道就直说不知道」"},
            {"key": "avoid", "label": "明确不要出现", "kind": "list",
             "hint": "一行一条，如「不要用『作为一个AI』」"},
            {"key": "lines", "label": "示例台词", "kind": "json",
             "placeholder": "[{\"scene\": \"被唤醒\", \"text\": \"我在，博士。\"}]",
             "hint": "★写「场景 + 她的一句话」★；写成「问题 + 完整答案」会让模型当查表原样复读"},
            {"key": "temperature", "label": "温度", "kind": "number", "step": 0.1,
             "placeholder": "0", "hint": "0 = 用全局配置；角色话多/话少可以在这微调（0~2）"},
        ],
    },
    {
        "title": "④ 声线（可全部留空 = 用全局）",
        "note": "不填就用 config.toml 的 [tts]。克隆类字段的细节见 README 第 14 节；"
                "素材不会挑就用 python scripts/pick_voice_ref.py --who <id>。",
        "fields": [
            {"key": "backend", "label": "TTS 后端", "kind": "select",
             "options": ["", "piper", "zipvoice"],
             "hint": "空 = 跟着全局；原创角色建议 piper（不碰任何克隆素材）"},
            {"key": "voice", "label": "piper 声线名", "kind": "text",
             "placeholder": "zh_CN-huayan-medium", "hint": "用的是 models/tts/piper/ 里的 onnx"},
            {"key": "voice_ref", "label": "克隆参考音频", "kind": "text",
             "placeholder": "data/personas/<id>/xxx.wav",
             "hint": "零样本克隆的默认参考；文本可留空（同名 .txt / 清单 / 自动转写）"},
            {"key": "voice_ref_text", "label": "参考音频的逐字文本", "kind": "text",
             "hint": "必须和音频逐字一致；不一致音色会明显退化"},
            {"key": "voice_refs", "label": "分风格的多条参考", "kind": "json",
             "placeholder": "{\"calm\": \"data/personas/<id>/a.wav\"}",
             "hint": "★当前模型下唯一的「情绪控制」手段★：换参考 = 换语气（见 README 第 14 节）"},
            {"key": "voice_model", "label": "专属模型目录", "kind": "text",
             "placeholder": "models/tts/zipvoice/personas/<id>",
             "hint": "训过专属声线才填；不填就用通用模型"},
        ],
    },
    {
        "title": "⑤ 它知道什么（第 15 节，可全留空）",
        "note": "留空 = 只看共享知识库；世界观组让同一个 IP 的角色共享一套设定。",
        "fields": [
            {"key": "worlds", "label": "挂在哪个世界观上", "kind": "list",
             "hint": "一行一个，如「明日方舟」；同名的角色共享 data/knowledge/_worlds/<名字>/"},
            {"key": "knowledge", "label": "专属知识库", "kind": "list",
             "hint": "一行一个文件/目录（相对项目根）"},
            {"key": "knowledge_shared", "label": "也看共享知识库", "kind": "bool",
             "hint": "关掉 = 全隔离（连顶层共享的也不看）"},
            {"key": "memory_all", "label": "能查所有人的记忆", "kind": "bool",
             "hint": "★这是权限不是配置★：开着能看到别的角色的经历，默认关（只有白泽开）"},
            {"key": "notes", "label": "给自己看的备注", "kind": "textarea", "rows": 2,
             "hint": "不进提示词，只写给你自己看"},
        ],
    },
]

# 白名单之外的字段：解析阶段会被吞掉，所以提交时明确报出来（别让人以为写进去了）
KNOWN_KEYS = {f["key"] for group in SPEC for f in group["fields"]}
# 同上，但保留「表单里的顺序」（合并写回时要按它逐栏对账）
KNOWN_KEYS_LIST = [f["key"] for group in SPEC for f in group["fields"]]
# 这几个字段允许写在人格文件里，但表单不暴露（索引/工具在管）
QUIET_KEYS = {"enabled", "default", "voice_dir", "knowledge_all", "knowledge_title", "world"}


# --------------------------------------------------------------------------- #
def groups() -> list[dict]:
    """给前端的表单规范（深拷一份，免得调用方改到 SPEC）。"""
    return json.loads(json.dumps(SPEC, ensure_ascii=False))


def _blank() -> dict:
    """空表：list → 空数组，bool → False，其余空串（JSON 里空值统一成一种写法）。"""
    out: dict[str, Any] = {}
    for group in SPEC:
        for f in group["fields"]:
            out[f["key"]] = False if f["kind"] == "bool" else (
                [] if f["kind"] in ("list", "json") else "")
    return out


def example(char: Any = None) -> dict:
    """给「填表的人」看的样板：有角色就照她抄一份，没有就写个最小可用集。"""
    if char is None:
        return {**_blank(), "id": "shining", "name": "临光", "title": "罗德岛干员",
                "background": "我来自卡西米尔，现在在罗德岛做事。说话直，不喜欢绕弯子。",
                "user_title": "博士", "wake_words": ["临光"],
                "aliases": {"临光": ["邻光", "林光"]}, "ack": "在。",
                "style": ["句子短，先给结论", "自称「我」，称呼对方「博士」"],
                "rules": ["★用第一人称说话，不要自我介绍★", "不知道就说不确定，绝不编造"],
                "avoid": ["不要说「作为一个AI」"], "temperature": 0.0, "backend": "piper",
                "notes": "★资料卡★ 手写的原创角色"}
    data = char.to_dict() if hasattr(char, "to_dict") else dict(char or {})
    out = _blank()
    for key in out:
        if key in data:
            out[key] = data[key]
    return out


def persona_path(settings: Any, char_id: str) -> Path:
    """资料卡写到哪：索引同目录的 `personas/<id>.json`（与现有人格文件同一套约定）。"""
    index = settings.resolve(settings.persona.file)
    return index.parent / PERSONA_SUBDIR / f"{char_id}.json"


def index_path(settings: Any) -> Path:
    return settings.resolve(settings.persona.file)


def file_of(settings: Any, char_id: str) -> Path:
    """这个角色的人格文件在哪：★索引里写的那条为准★（索引不在项目根下也照样找得到）。

    索引里没写、或者写的那份不在 → 退回约定路径 `personas/<id>.json`
    （**不保证存在**，调用方自己 `is_file()`）。克隆那边也用这一份实现。
    """
    index = index_path(settings)
    try:
        raw = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    for item in raw.get("characters") or []:
        entry = item if isinstance(item, dict) else {"file": item}
        cid = str(entry.get("id") or Path(str(entry.get("file") or "")).stem)
        if cid == char_id:
            target = (index.parent / str(entry.get("file") or "")).resolve()
            if target.is_file():
                return target
    return persona_path(settings, char_id)


def fields_of(char: Any) -> dict:
    """把一个**已有的**角色整理成表单字段（编辑时用它把表单填满）。

    ★只有一份实现★：跟「点填模板」用的是同一份 `example()` ——
    区别只是传的是真角色而不是样板，免得两处各写一套映射、加了字段忘一边。
    """
    return example(char)


def existing_ids(settings: Any) -> list[str]:
    """索引里已经有的角色 id（用来挡住「重名覆盖」）。"""
    try:
        raw = json.loads(index_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in raw.get("characters") or []:
        entry = item if isinstance(item, dict) else {"file": item}
        cid = str(entry.get("id") or Path(str(entry.get("file") or "")).stem)
        if cid:
            out.append(cid)
    return out


# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list[str]:
    """list 字段：前端给数组，导入的文件可能给字符串（按行拆）。"""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]


def _as_json(value: Any, want: type) -> tuple[Any, str]:
    """json 字段：前端给对象/数组原样用；给字符串就解析一次。返回 (值, 错误)。"""
    if isinstance(value, want):
        return value, ""
    # ★空数组/空对象都当「没填」★：`_blank()` 给 json 栏的默认值是 []，
    # 那对 dict 栏（aliases/voice_refs）是合法输入，不该报「应该是对象」。
    if value is None or value == "" or value == [] or value == {}:
        return ({} if want is dict else []), ""
    try:
        got = json.loads(str(value))
    except ValueError as exc:
        return ({} if want is dict else []), f"JSON 格式不对：{exc}"
    if not isinstance(got, want):
        return ({} if want is dict else []), f"应该是 {'对象' if want is dict else '数组'}"
    return got, ""


def parse(raw: dict) -> tuple[dict, list[str], list[str]]:
    """把表单/导入的原始数据整理成「能写进人格文件的 fields」。

    返回 ``(fields, problems, warnings)``：problems 非空就不该写文件；
    warnings 是「写得进去但你可能不是这个意思」（比如参考音频文件不在）。
    """
    raw = dict(raw or {})
    fields: dict[str, Any] = {}
    problems: list[str] = []
    warnings: list[str] = []

    for group in SPEC:
        for f in group["fields"]:
            key, kind = f["key"], f["kind"]
            value = raw.get(key, "")
            if kind in ("list", "json"):
                value = _as_list(value) if kind == "list" else value
            if kind == "list":
                fields[key] = value
            elif kind == "json":
                want = dict if key in ("aliases", "voice_refs") else list
                got, err = _as_json(value, want)
                if err:
                    problems.append(f"{f['label']}：{err}")
                fields[key] = got
            elif kind == "bool":
                fields[key] = bool(value)
            elif kind == "number":
                text = str(value or "").strip()
                try:
                    fields[key] = float(text) if text else 0.0
                except ValueError:
                    problems.append(f"{f['label']}：得是数字（现在写的是 {text!r}）")
                    fields[key] = 0.0
            elif kind == "select":
                got = str(value or "").strip()
                if got not in f.get("options", [got]):
                    problems.append(f"{f['label']}：只能是 {'/'.join(f['options'])} 之一")
                fields[key] = got
            else:
                fields[key] = str(value or "").strip()

    # ---- 必填与格式 ----
    if not ID_RE.match(str(fields.get("id") or "")):
        problems.append("id：只能用小写字母/数字/下划线/短横，2~32 位（它是文件名）")
    if not fields.get("name"):
        problems.append("名字：不能为空")
    if not fields.get("wake_words"):
        warnings.append("唤醒词为空 → 喊不出她，只能用 --character 或控制台手动切")
    if not 0.0 <= float(fields.get("temperature") or 0.0) <= 2.0:
        problems.append("温度：得在 0~2 之间（0 = 用全局）")

    # ---- 提醒「写了但会被忽略」的字段（白名单之外的）----
    unknown = [k for k in raw if k not in KNOWN_KEYS and k not in QUIET_KEYS
               and not str(k).startswith("_")]
    if unknown:
        warnings.append("这些字段人格文件不认识，会被忽略：" + "、".join(sorted(unknown)[:6]))

    # ---- 声线字段的现实检查 ----
    ref = str(fields.get("voice_ref") or "")
    if ref and not str(ref).endswith(".wav"):
        warnings.append(f"参考音频一般用 wav（现在写的是 {ref}）")
    return fields, problems, warnings


def preview(settings: Any, fields: dict) -> dict:
    """会写出去的 JSON 长什么样（按读得回的顺序排：id/name 在最前）。"""
    char = Character.from_dict({**fields, "id": fields.get("id") or "x",
                                "name": fields.get("name") or "x"})
    data = char.to_dict()          # ★用真解析器过一遍★：写进去是什么样，读出来就是什么样
    for key in ("id", "name"):
        if key in data:
            data[key] = fields.get(key, data[key])
    ordered = {k: data[k] for k in ("id", "name") if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})
    return ordered


# --------------------------------------------------------------------------- #
def _write_json(path: Path, data: dict, *, backup: bool = True) -> str:
    """写 JSON：先备份、再原子写、写完当场读回来校验。返回备份路径（没备份就是空串）。"""
    bak = ""
    if path.exists() and backup:
        bak = str(path.with_suffix(path.suffix + ".bak"))
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))     # 写坏了至少当场知道
    tmp.replace(path)
    return bak


def _empty(value: Any) -> bool:
    """「这一栏是空的」统一判定（表单里清空的栏 = ''/[]/{}/False）。"""
    return value is None or value == "" or value == [] or value == {} or value is False


def _normalized(raw: dict) -> dict:
    """把人格文件里的原始 JSON 过一遍真解析器（只用于「跟表单比一比」）。

    ★为什么必须归一化再比★：解析器是有默认值的 —— 比如 `user_title` 缺省就是「你」。
    拿原始 JSON 直接跟表单比，会得出「你改了 user_title」这种假改动，
    而且会把那个默认值真的写进文件（看着像没改什么，文件却变了）。
    """
    return preview(None, raw)


def merge_into(raw: dict, form: dict) -> tuple[dict, list[str], list[str]]:
    """把表单结果合进一张**已存在**的资料卡：★只动表单管得着的键★。

    返回 ``(合并后的 JSON, 改动的键, 被清掉的键)``。

    ★为什么不能整份替换★：`Character.from_dict` 是白名单解析 —— 表单里没有的键
    （`enabled` / `default` / `voice_dir` / `world` / 以及以后新加的字段）**不会报错，
    只会被静默丢掉**。直接覆盖的后果就是「我就改了个称呼，怎么喊不醒了」。
    所以：表单里有值的按键写进去；表单里**清空**了的键真的删掉（那是用户的意图）。

    ★只写真的变了的★：跟归一化后的当前值一样就不写（见 :func:`_normalized`），
    否则报告里会刷出一大片假的「改了 user_title」。
    """
    merged = dict(raw)
    base = _normalized(raw)
    changed: list[str] = []
    removed: list[str] = []
    for key in KNOWN_KEYS_LIST:
        if key in form:
            if base.get(key) != form[key]:
                changed.append(key)
                merged[key] = form[key]
        elif key in merged:
            if not _empty(base.get(key)):
                removed.append(key)
            merged.pop(key)
    ordered = {k: merged[k] for k in ("id", "name") if k in merged}
    ordered.update({k: v for k, v in merged.items() if k not in ordered})
    return ordered, changed, removed


def create(settings: Any, fields: dict, *, overwrite: bool = False,
           dry_run: bool = True) -> dict:
    """把资料卡落地：写 `personas/<id>.json` + 往索引里追加一行。

    ★只动这两个文件★：不动别的人格文件、不重排索引里已有的行、
    也不写 `voice_ref` 指向的音频（那是素材的事）。

    ★控制台不再用 `overwrite` 改角色★（改用 :func:`update`）——这里留着是给脚本用；
    真走到覆盖时也**合并**而不是整份替换（见 :func:`merge_into`），
    免得「覆盖」把 `enabled` / `voice_dir` 这些静默删掉。
    """
    fields, problems, warnings = parse(fields)
    if problems:
        raise ValueError("；".join(problems))
    char_id = str(fields["id"])
    target = persona_path(settings, char_id)
    index = index_path(settings)
    ids = existing_ids(settings)
    if char_id in ids and not overwrite:
        raise ValueError(f"索引里已经有 {char_id} 了（想覆盖就勾「覆盖已存在」）")

    # 索引怎么改：没有就追加一行；有了就原样不动（覆盖人格文件时才走到这儿）
    raw_index = json.loads(index.read_text(encoding="utf-8"))
    entries = list(raw_index.get("characters") or [])
    add_row = {"id": char_id, "file": f"{PERSONA_SUBDIR}/{char_id}.json", "enabled": True}
    will_add = char_id not in ids
    if will_add:
        entries.append(add_row)
    new_index = {**raw_index, "characters": entries}

    data = preview(settings, fields)
    changed: list[str] = []
    removed: list[str] = []
    overwriting = char_id in ids and overwrite
    if overwriting and target.is_file():
        try:
            data, changed, removed = merge_into(
                json.loads(target.read_text(encoding="utf-8")), data)
        except (OSError, ValueError):          # 旧文件坏了：那就只能按表单写
            pass
    report = {
        "id": char_id, "name": fields.get("name"),
        "file": str(target), "file_rel": rel(settings, target),
        "index": str(index), "index_rel": rel(settings, index),
        "added_index_row": will_add, "overwrote": overwriting,
        "changed": changed, "removed": removed,
        "fields": data, "warnings": warnings, "dry_run": bool(dry_run),
        "backup": "", "index_backup": "",
    }
    if dry_run:
        return report
    report["backup"] = _write_json(target, data)
    if will_add:
        report["index_backup"] = _write_json(index, new_index)
    return report


def update(settings: Any, fields: dict, *, dry_run: bool = True) -> dict:
    """★改一个已经存在的角色★：以表单为准，但保住表单管不着的键。

    跟 :func:`create` 的区别（也是为什么上一个「覆盖」不够用）：

    - **不会建文件**：索引里没有这个 id 就报错（编辑不会偷偷造一个角色）；
    - **不会丢字段**：`enabled` / `default` / `voice_dir` / `world` 与不认识的键原样保留；
    - **不会动索引**：索引里那一行本来就在，重写它反而多一次风险。
    """
    fields, problems, warnings = parse(fields)
    if problems:
        raise ValueError("；".join(problems))
    char_id = str(fields["id"])
    if char_id not in existing_ids(settings):
        raise ValueError(f"索引里没有 {char_id} —— 要建一个新的就用「创建」（编辑不会悄悄建文件）")
    target = file_of(settings, char_id)
    if not target.is_file():
        raise ValueError(f"找不到 {char_id} 的人格文件（{rel(settings, target)}）")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{rel(settings, target)} 读不动（{exc}）——先修好它再编辑") from exc
    data, changed, removed = merge_into(raw, preview(settings, fields))
    report = {
        "id": char_id, "name": fields.get("name"), "updated": True,
        "file": str(target), "file_rel": rel(settings, target),
        "index_rel": rel(settings, index_path(settings)),
        "changed": changed, "removed": removed,
        "fields": data, "warnings": warnings, "dry_run": bool(dry_run), "backup": "",
    }
    if dry_run:
        return report
    report["backup"] = _write_json(target, data)
    return report


def rel(settings: Any, path: Path) -> str:
    """显示用的相对路径（在项目里就给相对路径，外面就给绝对路径）。"""
    try:
        return path.relative_to(settings.root).as_posix()
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
def read_import(filename: str, text: str) -> tuple[dict, list[str], str]:
    """解析导入的文件。返回 ``(fields, problems, 说明)``。

    认两种（都是本项目自己的格式，不另发明）：
    - ``.json``：一张资料卡；若整份是 ``characters.json`` 那种索引/数组，取**第一个**并说明；
    - ``.txt``：素材清单（名字一行 + 正文一行）→ 只填「示例台词」这一栏，
      和 ``scripts/import_lines.py`` 同一套解析（``voice_loop.manifest``）。
    """
    name = str(filename or "").strip()
    ext = Path(name).suffix.lower()
    if ext == ".txt":
        pairs = [(scene, body) for scene, body in parse_manifest(text) if body.strip()]
        if not pairs:
            return {}, ["这个 txt 里没解析出「名字 + 正文」，看看排版（见 README 的素材清单格式）"], ""
        lines = [{"scene": scene or "随意对话", "text": body} for scene, body in pairs]
        fields = {**_blank(), "lines": lines,
                  "id": Path(name).stem.lower().replace(" ", "_"),
                  "name": Path(name).stem}
        return fields, [], f"从素材清单读到 {len(lines)} 条台词（只填了「示例台词」，其余请自己补）"
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return {}, [f"不是合法 JSON：{exc}"], ""
    note = ""
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
        note = "文件里是一个数组，只取了第一条"
    elif isinstance(raw, dict) and "characters" in raw and not raw.get("id"):
        first = (raw.get("characters") or [{}])[0]
        raw = first if isinstance(first, dict) else {"file": str(first)}
        note = "看起来是索引文件，只取了第一个角色（若是索引里的路径写法，请手填字段）"
    if not isinstance(raw, dict) or not raw:
        return {}, ["文件里没读到资料卡字段"], note
    fields = {**_blank(), **{k: v for k, v in raw.items() if k in KNOWN_KEYS}}
    if not fields.get("id"):
        fields["id"] = Path(name).stem.lower().replace(" ", "_")
        note = (note + "；" if note else "") + "文件里没写 id，按文件名推的，请确认"
    return fields, [], note
