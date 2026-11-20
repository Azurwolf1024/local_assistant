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
      "backend": "",                         // 可选：这个角色用哪个 TTS 后端（piper / zipvoice；
                                              //   空 = 跟 config.toml 的 [tts] backend）
      "voice": "",                            // 可选：这个角色用自己的 piper 声线
      "voice_ref": "",                        // 可选：克隆音色的参考音频（wav，backend = "zipvoice" 时用）
      "voice_ref_text": "",                   // 可选：参考音频的逐字文本；留空则用同名 .txt 或自动转写
      "voice_model": "",                      // 可选：这个角色专用的 ZipVoice 模型目录
                                              //   （用 scripts/persona_voice.py 微调出来的，
                                              //    如 models/tts/zipvoice/personas/kaltsit）
      "voice_dir": "",                        // 可选：语音素材目录（空 = data/personas/<id>/）
      "temperature": 0.7,                     // 可选：覆盖全局温度（0 = 用全局）
      "knowledge": ["data/knowledge/zzz/设定.md"],  // 可选：这个角色专属的知识库文件/目录
      "worlds": ["明日方舟"],                   // 可选：★挂在哪个世界观上★（可多个）
                                              //   同一个世界观的资料放在 data/knowledge/_worlds/明日方舟/
                                              //   挂上去的角色共享它（凯尔希 + 阿米娅各写一行就完事，
                                              //   不用把同一份设定拷进两个目录）
      "knowledge_title": "",                  // 可选：专属世界观的标题（空 = 「<名字> 的设定」）
      "world": "",                            // 可选：直接把它的世界观写在这里
      "knowledge_shared": true,               // 可选：要不要连共享知识库一起看
                                              //   （false = 全隔离，只看自己的那份）
      "memory_all": false,                    // 可选：★能不能查所有人的记忆★（默认不能）
                                              //   只有默认助手白泽开了这个权限；开着的角色
                                              //   查完必须说明每一条是从谁的记忆里查到的
      "knowledge_all": null,                  // 可选：能不能看**全部**知识库（所有人格的世界观 +
                                              //   别人的专属资料）。默认跟着 memory_all 走；
                                              //   ★看得到不等于代入★：提到别人时用旁观说法
                                              //   （“据我所知”），不说成自己的身份
      "notes": "给自己看的备注"                 // 不进提示词
    }

知识库（L4）的分层（详见 memory/ 的模块说明）
    data/knowledge/*.md              ← 顶层文件：**所有角色共享**
    data/knowledge/<角色id>/*.md     ← 子目录：**只有这个角色**看得到
    data/knowledge/_worlds/<世界观>/ ← 世界观组：**挂了它的角色共享**（人格文件 worlds 字段）
    人格文件的 knowledge / world      ← 同上，但路径写得很明白（可以指到仓库外）
    下划线开头的文件/目录             ← 不当知识（给 README 这类说明文件用）
    knowledge_shared = false         ← 连共享那份也不看

热加载
    索引和**所有人格文件**的改动都会被监听到（``maybe_reload``），保存即生效。
    新写一个人格文件、在索引里加一行，她就上线了；删掉那一行就下线（文件留着）。

多角色
    每个角色带自己的唤醒词，``WakeWordMatcher`` 会在命中时告诉上层**是哪位**，
    服务就这样切人设（必要时连声线一起换）。见 pipeline._switch_character。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                                  # 只为类型标注：运行时按需 import
    from .tts.spec import VoiceSpec

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

DEFAULT_LINES_IN_PROMPT = 6
"""提示词里最多放多少条「过往台词」（默认 6 条，只放**短的**）。

★不是越多越好★：台词每轮都在提示词里，而且 4B 模型对长提示词非常敏感 ——
38 条会把它推成「文档模式」（拒绝扮演、或把提示词复述出来，见 §58 实测）。
``[persona] lines_in_prompt`` 可覆盖；填 0 = 用这个默认值。
"""

COMPACT_BACKGROUND_CHARS = 160
"""compact 提示词里背景最多留多少字（够交代身份与关系，不再往下铺）。

★这条数字是实测调的★：160 字时八个角色的 compact 提示词都在 250~420 字之间；
放到 220 字，克莱门莎就到 469 字，lint 也开始报「太长」（§58）。
"""

# ★跟角色无关的「别编」规则★：放在风格之前（小模型对靠后的内容不敏感）。
# 每一条都对应一种实测出现过的幻觉（见 docs/ENGINEERING_LOG.md §58）。
TRUTH_RULES = (
    "【只说真话】这一节比下面的说话风格更重要，两者冲突时以本节为准：",
    "- 你只知道这轮对话里出现过的事。对方刚做了什么、睡没睡、身体怎样、心情如何，"
    "没提到就是不知道——不要猜，也不要用猜测去表达关心。",
    "- 白天还是晚上、现在几点、今天星期几，只看系统给的【此刻】那一行。"
    "没有【此刻】就别提时间；有就按它说，不要自己编。",
    "- 日程、备忘、提醒、闹钟：【只说查回来的结果】。没查过就说「我看看」，不要凭印象报出"
    "「今晚八点有会」这种具体安排。查回来是空的，就如实说空。",
    "- 对方问起你的事（你在哪、在做什么），可以按人设答，但不要编造细节事实"
    "（具体日期、编号、别人说过的话）。",
    "- 一句话里最多带一个设定名词（地名、组织、档案、术语），且要跟这句话有关，别硬塞。",
)

# 提示词里「目标 token 预算」（粗略估计：中文约 0.65 token/字）
PROMPT_TOKEN_BUDGET = 1800
"""渲染后人设部分的建议上限（tokens）。超了就让 lint 提醒 —— 4096 的上下文很紧张。"""

_WEEKDAY = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def day_part(hour: int) -> str:
    """几点 → 「凌晨/早上/上午/中午/下午/傍晚/夜里」。"""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    return "夜里"


def now_block(when: "datetime | None" = None) -> str:
    """每一轮注入的「现在时间」——**一句白话，不带标签**。

    ★为什么每轮都要给★：模型本身**没有时钟**。不给它，它就只能猜 ——
    于是白天说「你又熬夜了」、把「明天」算成别的日子。
    ★为什么不放在人设里★：人设是启动时算一次并缓存的，时间会过期。

    ★为什么不再写成「【此刻】…」★：实测 4B 会把这个方括号词当成要复述的内容，
    回答里直接冒出「现在【此刻】是…」。给时间就用平常的一句话，不加标记。
    """
    from datetime import datetime as _dt  # noqa: PLC0415

    t = when or _dt.now()
    return (f"现在是{t.year}年{t.month}月{t.day}日，{_WEEKDAY[t.weekday()]}，"
            f"{t.hour:02d}:{t.minute:02d}（{day_part(t.hour)}）。")


TRUTH_HINT = ("对方没说过的事你不知道，也别猜他刚才在做什么、睡没睡；"
              "日程和备忘先用工具查，再按查到的说。")
"""每轮随「此刻」一起发的那句「别编」。

★为什么放在运行时而不是人设里（§58）★：写进人设时，4B 会把它当台词念出来
（实测凯尔希答过「我甚至知道博士的日程和备忘录」）；放在紧贴用户那句的
system 里，它才被当成规矩。人设只负责「她是谁、怎么说话」。
"""

# --------------------------------------------------------------------------- #
# 回答质量的兜底：小模型偶尔会「出戏」
#
# ★这不是提示词能 100% 解决的事★（`qwen3.5:4b` 在 temperature 0.7 下同一份人设
# 时而像人、时而交作业），所以除了把提示词写短写白，还要在**开口之前**看一眼：
# 一旦露出下面这些毛病，就丢掉这段、换一份极简人设重答一次。
#
# 为什么必须在「念出来之前」判断：这几个毛病全都在**第一句**就暴露（拒绝扮演、
# 议论提示词、复述人设），而第一句话一旦被 TTS 念出去就收不回来了。
_BROKEN_REPLY: tuple[tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"作为一个人工智能|作为一个 ?AI|作为一个语言模型|作为 AI 助手"
                r"|我(无法|不能)(进行|参与|扮演|按照您(的)?要求)"), "拒绝扮演"),
    (re.compile(r"[你您](提供|刚才|说的)[^。！？\n]{0,10}(文本|文字|字符|内容|输入|这段话)"
                r"|系统提示|提示词|(指令|要求)拆解|按(用户|您)的(要求|指令)"), "议论提示词"),
    (re.compile(r"^\s*(?:#+\s|\*\*\s*(?:结论|状态|安排|当前)|\|\s*时间)"), "交作业排版"),
)


def reply_problem(text: str, persona: str = "") -> str:
    """这段回答「出戏」了吗？返回原因；没问题返回空串。

    ``persona`` 是这次用的人设原文：模型偶尔会把**人设**当成素材复述出来
    （「我甚至知道博士的日程和备忘录」），所以拿人设的 20 字片段去比对。
    """
    body = str(text or "")
    for pattern, reason in _BROKEN_REPLY:
        if pattern.search(body):
            return reason
    clean_answer = re.sub(r"\s+", "", body)
    clean_persona = re.sub(r"\s+", "", str(persona or ""))
    if len(clean_persona) >= 40 and len(clean_answer) >= 20:
        grams = {clean_persona[i:i + 20] for i in range(0, len(clean_persona) - 19)}
        if any(g in clean_answer for g in grams):
            return "复述人设"
    return ""


def plain_prompt(char: "Character") -> str:
    """兜底用的极简人设（一句话），重答时换上它。

    ★为什么能兜住★：实测 4B 在 30~80 字的极简人设下几乎不会拒绝，也不会议论提示词。
    代价是角色味淡一些 —— 但比「我是人工智能助手」强。
    """
    who = char.user_title or "对方"
    head = f"你是{char.name}"
    if char.title:
        head += f"，{char.title}"
    return f"{head}，正和{who}说话。话不长，一次一两句。"


OMNISCIENT_NOTE = ("你还读过别的角色、别的作品的设定，那些是别人的东西，"
                   "提起来就像在说别人家的事。")
"""全知角色（``memory_all``）在 compact 提示词里的「旁观」提示。

★为什么必须先提「这是别人的」★：资料库里混着别的作品的世界观，不写这句，小模型
会把资料当成自己的身份（「我来自泰拉」）。rich 版写得长，compact 只能一句，
而且要是**陈述句** —— 写「不要当成自己的身份」它就会把这句念出来。
"""


def work_name(char: "Character") -> str:
    """角色属于哪个作品（提示词第一行用）。``work`` 优先，否则 ``worlds[0]``，否则空。

    ★空的时候就不要写作品名★：原创角色被硬塞进某个游戏的世界观里，模型就会往那边扯
    （以前写死「《明日方舟》里的白泽」——一个原创助手被要求按泰拉大陆的设定说话）。
    """
    return str(getattr(char, "work", "") or "").strip() or (
        char.worlds[0] if char.worlds else "")


_COPYABLE = re.compile(
    r"[，,（(]?\s*(?:比如|例如|譬如|像是|好比)\s*[说]?\s*[：:]?\s*[「“\"]([^」”\"]{6,})[」”\"]"
)
"""「比如「博士，你又熬夜了」」这种**可以整句照抄**的示例。

★为什么要剥掉★：4B 模型会把引号里那句当成台词，**在毫不相干的回合原样说出来**
（用户反映的「白天莫名其妙说『你又熬夜了』」就是这条 style 直接导致的）。
短引文（<6 字，比如「不用『哎呀』这种口气」）保留——那是词，不是句子换。
"""


def _strip_copyable(text: str) -> str:
    """去掉风格/背景里「可整句照抄」的示例（渲染时做，改的是提示词而不是用户的数据）。"""
    out = _COPYABLE.sub("", str(text or ""))
    return re.sub(r"[，,；;]\s*$", "。", out.strip()) if out != text else out


_DECOR = re.compile(r"[★☆]|\*\*|__|【|】|「|」|『|』")


def _plain(text: str) -> str:
    """把用户数据里的装饰符号抹平，只留能念出来的字。

    ★为什么渲染时要洗一遍★：人格文件是手写的，里面会混进 `★`、`**`、`【】`
    和书引号。4B 模型看到这些就当成「格式要求」，回答里开始出现星号和方括号，
    或者干脆把这段话当成一份待分析的文档（§58 实测）。
    """
    out = _DECOR.sub("", str(text or ""))
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip(" \n，,；;、")


def pick_lines(lines: list[dict], limit: int = DEFAULT_LINES_IN_PROMPT) -> list[dict]:
    """挑哪些台词进提示词：★优先「短而有代表性」的★，保持原来的先后顺序。

    过长的（>90 字，多半是独白）排后面 —— 它们占 token 最多，对学语气的帮助反而最小。
    """
    if limit <= 0 or len(lines) <= limit:
        return list(lines)
    order = sorted(range(len(lines)),
                   key=lambda i: (len(str(lines[i].get("text") or "")) > 90, i))
    keep = sorted(order[:limit])
    return [lines[i] for i in keep]


def prompt_stats(char: "Character", extra: str = "", *, lines_in_prompt: int = 0) -> dict:
    """人设提示词的「体积」：给 lint / CLI / 控制台看（别等溢出才发现）。"""
    text = render_system_prompt(char, extra, lines_in_prompt=lines_in_prompt)
    lines = pick_lines(char.lines, lines_in_prompt or DEFAULT_LINES_IN_PROMPT)
    tokens = int(len(text) * 0.65)          # 中文经验值；只用来提醒，不当精确值
    return {
        "chars": len(text),
        "tokens": tokens,
        "over_budget": tokens > PROMPT_TOKEN_BUDGET,
        "lines_total": len(char.lines),
        "lines_in_prompt": len(lines),
        "lines_chars": sum(len(str(x.get("text") or "")) for x in lines),
        "work": work_name(char),
    }



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
    # ★她的嗓子：一份与后端无关的「声音描述」★（见 voice_loop/tts/spec.py::VoiceSpec）
    #
    #   "tts": {
    #     "provider": "zipvoice",              // piper / zipvoice / deepseek / edge / http / 自己加的
    #     "voice_id": "",                      // stella / zh-CN-XiaoxiaoNeural / piper 声线名
    #     "model": "models/tts/zipvoice/...",  // 专属模型目录（clone 类后端）
    #     "ref_audio": "data/personas/x/a.wav",// 克隆参考音频
    #     "ref_text": "……",                    // 参考音频的逐字文本
    #     "params": { "clone_steps": 4 },      // 这个后端的私有参数
    #     "fallback": ["piper:zh_CN-huayan-medium"],
    #     "styles": { "calm": "...wav" }       // 分风格的参考（原来的 voice_refs）
    #   }
    #
    # ★以前这里是 8 个平铺字段（backend / voice / voice_ref / voice_ref_text /
    #   voice_refs / voice_model / tts_voice），换一个后端就得加字段，而且只能有一个后端成立。★
    tts: dict = field(default_factory=dict)
    voice_dir: str = ""                   # 语音素材目录（空 = data/personas/<id>/）
    # ★这个角色专属的知识库（L4）★：不同 IP 的角色各看各的世界观，见模块说明。
    knowledge: list[str] = field(default_factory=list)   # 文件/目录（相对路径按项目根算）
    # ★挂在哪个世界观上★（可多个）：同 IP 的角色共享一份设定，不用拷来拷去
    #   （凯尔希 + 阿米娅都写 ["明日方舟"] → 两人共享 _worlds/明日方舟/ 下的资料）
    worlds: list[str] = field(default_factory=list)
    # ★能不能看到**全部**知识库★（所有人格的世界观 + 别人的专属资料）：
    # 默认跟着 `memory_all` 走（None = 跟随）；想「只给跨角色查记忆、但不给看别的世界观」
    # 就显式写 `"knowledge_all": false`。看得到不等于会代入 —— 见 render_system_prompt 里的旁观要求。
    knowledge_all: bool | None = None
    knowledge_title: str = ""             # 专属世界观的标题（空 = 「<名字> 的设定」）
    world: str = ""                       # 直接写在人格文件里的世界观正文
    knowledge_shared: bool = True         # false = 只看自己的那份（全隔离）
    # ★跨角色读记忆的权限★（默认关，只给默认助手开）：
    # 开着就能用 recall 的「全部角色」模式，但**必须把来源标出来**（「凯尔希记的」）——
    # 不标来源等于把「这是谁说的」丢掉，比查不到还糟。
    memory_all: bool = False
    temperature: float = 0.0              # 0 = 用全局 [llm] temperature
    default: bool = False
    enabled: bool = True
    notes: str = ""                       # 给自己看的备注（不会进提示词）
    # ★可选：属于哪个作品★（提示词第一行的「你是《X》里的…」）。
    # 空 = 用 worlds[0]；再没有就**不提作品名**（原创角色不该被塞进某个游戏的世界观里 ——
    # 以前这里是写死的「《明日方舟》」，白泽也被写成「《明日方舟》里的白泽」，
    # 模型于是会往游戏设定上扯）。
    work: str = ""

    # ★可选：这个角色的「脑子」跑在哪★（见 README 第 14 节「每个角色的 AI 引擎」）
    #   ""    = 跟 config.toml 的 [api] engine（默认 local）
    #   local = 本机 Ollama
    #   api   = OpenAI 兼容的云端接口（DeepSeek 等）
    # 放在最后：dataclass 的字段顺序会被位置构造依赖。
    engine: str = ""
    api_model: str = ""                   # 空 = 跟 [api] model
    api_base: str = ""                    # 空 = 跟 [api] base_url
    api_key: str = ""                     # 空 = 跟 [api] 的密钥设置（api_key_file / 环境变量）
    # ★API 角色额外开放的工具★：预设名（见 voice_loop/tool_presets.py）或具体工具名。
    # 只在 engine = api 时生效；留空 = 跟 [api] extra_tools。
    api_tools: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 基础
    # ★嗓子的只读视图★：文件里只有 `tts` 一份真相，这些属性让旧的读取点
    # （控制台、克隆面板、风格挑选、自测）不用关心结构。赋值请改 `self.tts`。
    @property
    def backend(self) -> str:
        """用哪个 TTS 后端（= tts.provider；老名字）。"""
        return str(self.tts.get("provider") or "")

    @property
    def voice(self) -> str:
        """音色 id：piper 声线名 / stella / zh-CN-XiaoxiaoNeural …"""
        return str(self.tts.get("voice_id") or "")

    @property
    def tts_voice(self) -> str:
        return self.voice

    @property
    def voice_ref(self) -> str:
        return str(self.tts.get("ref_audio") or "")

    @property
    def voice_ref_text(self) -> str:
        return str(self.tts.get("ref_text") or "")

    @property
    def voice_refs(self) -> dict[str, Any]:
        styles = self.tts.get("styles")
        return dict(styles) if isinstance(styles, dict) else {}

    @property
    def voice_model(self) -> str:
        return str(self.tts.get("model") or "")

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
            # ★嗓子：一份与后端无关的描述★（结构见 VoiceSpec；这里只做归一化，不猜默认值）
            tts=normalize_tts(raw.get("tts")),
            work=str(raw.get("work") or "").strip(),
            voice_dir=str(raw.get("voice_dir") or "").strip(),
            # ★新增字段必须在这里显式搬一次★（白名单式构造，漏了就是静默忽略；
            #   test_memory_mcp 里有专门的断言守这个）
            knowledge=[str(p).strip() for p in _list("knowledge") if str(p).strip()],
            worlds=[str(w).strip() for w in _list("worlds") if str(w).strip()],
            knowledge_all=(None if raw.get("knowledge_all") is None
                           else bool(raw.get("knowledge_all"))),
            knowledge_title=str(raw.get("knowledge_title") or "").strip(),
            world=str(raw.get("world") or "").strip(),
            knowledge_shared=bool(raw.get("knowledge_shared", True)),
            memory_all=bool(raw.get("memory_all", False)),
            temperature=float(raw.get("temperature") or 0.0),
            default=bool(raw.get("default")),
            enabled=bool(raw.get("enabled", True)),
            notes=str(raw.get("notes") or "").strip(),
            # ★同样必须显式搬一次★（白名单式构造，漏了就是静默忽略）
            engine=str(raw.get("engine") or "").strip().lower(),
            api_model=str(raw.get("api_model") or "").strip(),
            api_base=str(raw.get("api_base") or "").strip(),
            api_key=str(raw.get("api_key") or "").strip(),
            api_tools=[str(t).strip() for t in _list("api_tools") if str(t).strip()],
        )


# --------------------------------------------------------------------------- #
def normalize_tts(raw: Any) -> dict:
    """把人格文件里的 ``tts`` 归一化成 VoiceSpec 的形状（不认识就退回空 = 跟全局）。"""
    from .tts.spec import TtsConfigError, VoiceSpec  # noqa: PLC0415 - 按需，别拖慢启动

    try:
        spec = VoiceSpec.parse(raw)
    except TtsConfigError:
        return {}
    data = spec.as_dict()
    styles = raw.get("styles") if isinstance(raw, dict) else None
    if isinstance(styles, dict):
        clean = {str(k).strip(): v for k, v in styles.items() if str(k).strip() and v}
        if clean:
            data["styles"] = clean
    return data


def voice_spec_for(char: Any, style: str = "") -> VoiceSpec:
    """角色 → 这一句话该用的声音（按风格挑参考音频；没有就退回默认那档）。

    ★只在这一处做「角色 → spec」的翻译★：管线、控制台、自测都调它，
    所以「换嗓子」的规则永远不会有两份实现。
    """
    from .tts import style as stylekit  # noqa: PLC0415 - 只有说话/试听时才需要
    from .tts.spec import VoiceSpec     # noqa: PLC0415

    spec = VoiceSpec.parse(getattr(char, "tts", None))
    ref, text = stylekit.pick_ref(char, style)
    if ref:
        # ★文本按音频走，绝不借用另一条音频的文本★：换了参考音却留着上一段的文本，
        # 就成了「声不对词」，克隆质量会明显退化（清单/自动转写负责补空）。
        spec = spec.replace(ref_audio=ref, ref_text=text)
    return spec


# --------------------------------------------------------------------------- #
def knowledge_spec_for(registry: "CharacterRegistry | None", char_id: str) -> dict:
    """某个角色的知识库「说明书」——给记忆层用（memory 不该认识 Character）。

    返回 ``{"paths": [...], "world": str, "title": str, "shared": bool}``；
    找不到角色就返回空说明书（= 只看共享知识库）。
    """
    char = None
    if registry is not None:
        try:
            char = registry.get(char_id) or registry.default()
        except Exception:  # noqa: BLE001 - 角色文件坏了不该把知识库带崩
            char = None
    if char is None:
        return {"paths": [], "worlds": [], "world": "", "title": "", "shared": True,
                "all": False, "label": "", "others": [], "others_inline": []}
    siblings = [c for c in (registry.all(only_enabled=True) if registry else [])
                if c.id and c.id != char.id]
    return {
        "paths": list(char.knowledge),
        "worlds": list(char.worlds),
        "world": char.world,
        "title": char.knowledge_title or f"{char.name} 的设定",
        "shared": bool(char.knowledge_shared),
        # ★全知权限★：默认跟着 memory_all（白泽默认有），也可以单独关掉
        "all": bool(char.memory_all if char.knowledge_all is None else char.knowledge_all),
        "label": char.name,
        "others": [[c.id, c.name] for c in siblings],
        "others_inline": [[f"{c.name} 的设定", c.world, c.name] for c in siblings if c.world],
    }


def can_read_all_memory(registry: "CharacterRegistry | None", char_id: str) -> bool:
    """某个角色能不能查**所有人的**记忆（人格文件里的 `memory_all`，默认不能）。

    ★这是权限，不是配置★：开着就能看到别的角色的经历和事实，所以默认关。
    ★找不到这个角色时**不给**权限★（不要退回默认角色 —— 默认角色恰好开着这个权限，
    一旦退回就是「不小心把权限发出去了」）。
    """
    if registry is None or not str(char_id or "").strip():
        return False
    try:
        char = registry.get(char_id)
    except Exception:  # noqa: BLE001 - 角色文件坏了就当没权限
        return False
    return bool(char and char.memory_all)


def _clip(text: str, limit: int) -> str:
    """截到 ``limit`` 字以内，尽量在句号/问号处断（不截成半句话）。"""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in ("。", "！", "？", "；"):
        pos = cut.rfind(mark)
        if pos >= limit // 2:
            return cut[:pos + 1]
    return cut.rstrip("，、,;；") + "。"


_LINE_NOISE = re.compile(r"提升至|以查看|以解锁|信赖值|潜能|晋升|精英阶段|^\W*$")
"""台词里的「图鉴噪音」：`(提升至精英阶段1以查看)` 这种，念出来莫名其妙。"""

_LINE_SKIP = re.compile(
    # 战斗 / 剧情 / 阴暗
    r"接敌|阵型|布防|部署|全体单位|战场|迎敌|歼|战死|开火|撤退|作战计划"
    r"|暴力|施暴|生死|杀|尸|血|牺牲|死|绝路|复仇|仇|报应|伤口|痛|发抖"
    # ★作息 / 身体状况★：这是用户最先抱怨的那类「凭空关心」的源头。
    # 「您又熬夜了……我也没睡」这种台词留在提示词里，模型白天也会照搬出来。
    r"|熬夜|没睡|早点睡|睡得|作息|这么晚|深夜|失眠"
)
"""**不能**进提示词的台词。

它们确实很像角色的「台词」，但作用相反：战斗台词把日常对话带成念战报，作息台词让模型
白天也问「你又熬夜了」。台词是给人看剧情的，不是给人学闲聊的。
"""

_CHAT_CUE = re.compile(r"[你您吗呢吧？]")


def _short_lines(lines: list[dict], limit: int,
                 low: int = 8, high: int = 34) -> list[str]:
    """挑几句台词当语气样本：★要「一句话」，不要「两个字」也不要独白★。

    实测按长度从短往长挑，会挑到「可以。」这种没有信息量的；太长的又会把回答带成
    演讲。所以先卡长度区间，再按「像不像聊天」排（有 你/您/吗/呢/吧/？ 的优先），
    最后**按原来的先后顺序**输出 —— 那是人物自己的语序，打乱了反而不像。
    """
    texts = [_plain(x.get("text")) for x in lines]
    texts = [t for t in texts if t and not _LINE_NOISE.search(t)]
    texts = [t for t in texts if not _LINE_SKIP.search(t)]
    band = [t for t in texts if low <= len(t) <= high]
    if len(band) < limit:
        band += sorted((t for t in texts if t not in band and len(t) <= 60), key=len)
    scored = sorted(range(len(band)),
                    key=lambda i: (-len(_CHAT_CUE.findall(band[i])), i))
    keep = sorted(scored[:limit])
    return [band[i].rstrip("。！？…") for i in keep]


_NEG = re.compile(r"不|别|勿|没有|绝|避免|禁止")
"""风格条目里的否定词。★compact 里尽量少用否定句★：4B 会把「不用『哎呀』这种口气」
里的『哎呀』学走 —— 或者干脆照念整句否定。"""

_META_WORDS = re.compile(r"这些词|这个词|提到|称呼|自称|结尾|习惯用|硬塞|句式|语气词|每句")
"""风格条目里的**元词汇**（说的是「怎么用词」而不是「什么脾气」）。

★这类条目在 compact 里最危险★：模型分不清「描述」和「要求」，会把它当台词念出来 ——
凯尔希的实测回答是「你不需要硬塞，只要偶尔提一句罗德岛简报或档案，就能让我冷静地回应你」。
"""


def _tone_entries(char: "Character", count: int = 2) -> list[str]:
    """compact 用哪几条风格：★优先「肯定句、第二人称、短」的那几条★，保持原顺序。

    只取每条**第一个分句** —— 脾气写在前面（「冷静、克制、用词精准」），
    而「怎么用词」的说明通常在后半句，那半句会被 4B 当成台词念出来。
    """
    entries = [(_plain(_strip_copyable(s)), str(s)) for s in char.style if str(s).strip()]
    if not entries:
        return []

    def score(item: tuple[str, str]) -> int:
        plain, raw = item
        return (len(_META_WORDS.findall(raw)) * 3
                + len(_NEG.findall(raw)) * 2
                + len(re.findall(r"[她他]", plain)) * 2
                + len(plain) // 40)

    order = sorted(range(len(entries)), key=lambda i: (score(entries[i]), i))[:count]
    picked: list[str] = []
    for i in sorted(order):
        first = re.split(r"[；;。]", entries[i][0])[0].strip(" ，,、")
        if first:
            picked.append(first)
    return picked


def render_system_prompt(char: Character, extra: str = "", *,
                         lines_in_prompt: int = 0, style: str = "") -> str:
    """把角色字段拼成 system prompt。

    ★改这个函数就是在改「所有角色的提示词工程」★：用户只填字段，
    措辞、结构、以及「输出会被朗读」这类硬规则都在这儿统一维护。

    ``style`` 两种（★默认 compact —— 这是实测出来的，不是审美偏好★）：

    ==========  ============================================  ===========================
    ``compact`` 口语化、没有装饰符号、一两百字                 ★4B 小模型只能用这种★
    ``rich``    带 【】/★/**/编号的分节「文档」                 大模型（20B+）可以，小模型会翻车
    ==========  ============================================  ===========================

    ★compact 的五条硬规则（每一条都是 §58 里撞出来的，别顺手改回去）★

    1. **短**。`qwen3.5:4b` 拿到 500 字以上的人设就会把提示词当成「用户送来的一段
       混乱文本」来审阅：回一句「您提供的输入含有矛盾指令」就完了，或者干脆把提示词
       复述出来（用户说的「强行扯到身份卡上的提示词」）。目标 200~300 字。
    2. **像人设卡，不像指令书**。★「学那个语气」「你要做到以下几点」这类话本身
       就是元指令★，模型读到的不是「你是谁」，而是「有人给我派了个任务」，于是它开始
       交作业：拆解要求、列要点、注解「已按用户要求生成」。所以这里只写**陈述句**：
       「你是阿米娅，罗德岛公开领袖，出自《明日方舟》，正和博士说话。」「话不长，一次一两句。」
    3. **只说肯定句**。写「不要自称 AI」，它就在回答里冒出「我是白泽（AI 助手）」；
       写「不要用符号和列表」，它就点评「你给的文本里有符号」。
    4. **不带引号和标签**。`【此刻】` 会被念成「现在【此刻】是…」，
       「这个我不知道」会被复述成「您刚才说的『这个我不知道』是指…」。
    5. **不写「扮演 / AI / 助手」这类词**。一提，它就开始解释自己是个模型 ——
       用户要的是「它就是她」，不是「她在演」。
    6. **不写行为规则**（「不清楚就说不知道」「日程先查再报」）。这类句子同样会被
       当成台词：实测凯尔希回过「我甚至知道博士的日程和备忘录」。真话与工具的规矩
       属于**每轮的运行时提示**（`pipeline` 里的 hint），不属于人设。
    """
    if style == "rich":
        return _render_rich(char, extra, lines_in_prompt=lines_in_prompt)
    who = char.user_title or "对方"
    work = work_name(char)
    # ★开头必须有「你」★：只写「凯尔希，罗德岛医疗主管……」，模型会以为这是在介绍
    # 一个**别人**，于是用第三人称谈她（「关于您提到的那个凯尔希……她不在场」）。
    head = f"你是{char.name}"
    if char.title:
        head += f"，{char.title}"
    if work:
        head += f"，出自《{work}》"
    head += f"，正和{who}说话。"
    parts: list[str] = [head]

    tones = _tone_entries(char)
    tone = "；".join(t.rstrip("。；，, ") for t in tones if t)
    if tone:
        parts.append(f"{tone}。")

    background = _clip(_plain(_strip_copyable(char.background.strip())),
                       COMPACT_BACKGROUND_CHARS)
    if background:
        parts.append(background)
    # ★全知角色的「旁观」要求（compact 版）★：它的资料里混着**别人的**世界观和设定，
    # 不写这一句，小模型会把资料当自己的身份（「我来自泰拉」）。
    # rich 版写了一大段，compact 只能留一句 —— 而且必须是陈述句（否定句会被照念）。
    if char.memory_all or char.knowledge_all:
        parts.append(OMNISCIENT_NOTE)
    parts.append("话不长，一次一两句。")

    samples = _short_lines(char.lines, lines_in_prompt or DEFAULT_LINES_IN_PROMPT)
    if samples:
        parts.append("；".join(samples) + "。")
    if extra.strip():
        parts.append(extra.strip())
    return "\n".join(p for p in parts if p.strip())


def _render_rich(char: Character, extra: str = "", *,
                 lines_in_prompt: int = 0) -> str:
    """分节的「文档式」人设（只有大模型扛得住；小模型会拒绝或复述，见上）。"""
    who = char.user_title or "你"
    work = work_name(char)
    head = f"你是《{work}》里的{char.name}" if work else f"你是{char.name}"
    head += (f"，{char.title}。" if char.title else "。")
    parts: list[str] = [head]
    if char.background:
        parts.append(_strip_copyable(char.background.strip()))
    parts.append(
        f"现在正通过语音和{who}对话——你就是{who}身边的那个{char.name}，"
        f"不是「扮演助手的 AI」，也不要自称 AI 或语言模型。称呼对方为「{who}」。\n"
        "★用第一人称说话（「我」）★：不要用第三人称描述自己，也不要念自己的头衔或自我介绍。"
    )
    # ★真话规则放在风格**前面**★：小模型对越靠后的内容越不敏感，
    # 而「不编造」比「像不像她」重要 —— 冲突时以这一节为准。
    parts.append("\n".join(TRUTH_RULES))

    if char.style:
        parts.append("【说话风格】\n" + "\n".join(
            f"{i}. {_strip_copyable(s)}" for i, s in enumerate(char.style, 1)))
    if char.rules:
        parts.append("【必须做到】\n" + "\n".join(f"- {s}" for s in char.rules))
    if char.avoid:
        parts.append("【不要这么做】\n" + "\n".join(f"- {s}" for s in char.avoid))
    # ★全知角色的「旁观要求」★：它的资料里会有**别人的**世界观和设定。
    # 不写这一段，小模型会把资料当成自己的身份（“我来自泰拉”“我是罗德岛的医疗主管”），
    # 而这件事**不会报错**，只是听起来越来越不像它自己。
    if char.memory_all or char.knowledge_all:
        parts.append(
            "【关于你手里的资料】你读过很多设定和资料，其中有一部分是**别人的**（别的角色、别的作品）。"
            "提到它们时要用旁观的说法（「据我所知」「在……的设定里」），★不要说成自己的身份、来历或经历★；"
            "除非对方明确问到，不要主动把别人的设定抡出来。你自己的身份只有一个：上面写着的那些。"
        )
    if char.lines:
        budget = lines_in_prompt or DEFAULT_LINES_IN_PROMPT
        picked = pick_lines(char.lines, budget)
        shown = "\n".join(f"- 场景：{ln['scene']} → {ln['text']}" for ln in picked)
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
        # ★引擎相关字段也要算进签名★：只改角色卡上的「本地 / API」时，
        # `maybe_reload` 才会真的重装人设（否则改了引擎要重启服务才生效）。
        return [
            (c.id, c.name, tuple(c.wake_words), c.ack, c.engine, c.api_model,
             c.api_base, c.api_key, tuple(c.api_tools),
             # 嗓子：整个 tts 描述都进签名（换 provider/音色/参考/参数都要热加载）
             json.dumps(c.tts, ensure_ascii=False, sort_keys=True),
             c.voice_dir)
            for c in self.characters
        ]

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

    def prompt_for(self, key: str | None, extra: str = "", *,
                   lines_in_prompt: int = 0, style: str = "") -> tuple[str, Character | None]:
        """按 id/名字取角色并拼提示词；找不到就用默认角色。"""
        char = self.get(key) if key else None
        char = char or self.default()
        return (render_system_prompt(char, extra, lines_in_prompt=lines_in_prompt,
                                     style=style)
                if char else "", char)

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
            "notes": "默认角色。别名是拿 scripts/tests/test_wake.py 一条条实测攒的，别删。",
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
