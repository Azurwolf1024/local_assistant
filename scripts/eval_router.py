"""评估「让模型选工具」到底行不行（shadow 评估，改默认前先看数据）。

跑法：
    python scripts/eval_router.py --sessions          # 拿真实会话里的原话跑（默认）
    python scripts/eval_router.py --builtin           # 用内置的一批句子（不用历史数据）
    python scripts/eval_router.py --text "帮我看看下周有什么"
    python scripts/eval_router.py --sessions --limit 20 --dry    # 只看会跑哪些句子，不调模型

它做的事：
    1. 每条句子先用**确定性技能层**跑一遍（作为基准）：
       技能层能接住的，就不该再麻烦模型；接不住的才是模型要补的洞。
    2. 再让模型带着工具跑一遍，看它选哪个工具、参数是不是原话。
    3. 真去执行工具（临时目录，不碰真实数据），记录成没成、花了多久。
    4. 出汇总：选对率 / 白跑（技能本可处理）/ 漏网（技能没接住、模型也没补上）/ 误报（闲聊却调了工具）。

为什么要这个：模型算日期不可靠（实测「下周三下午三点半」会算成 10-04），
所以我们只让它选工具、把原话塞进 text；这个脚本就是量「选工具」这一步的准确率。
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import statistics as stats
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_loop.settings import load_settings  # noqa: E402

# 内置样本：(句子, 期望)。期望写 None = 这句话**不该调任何工具**（闲聊）。
# 写成集合是因为有些说法用哪个工具都算对（「那件事是什么时候」→ list_schedule）。
# 为什么要标预期：不标就只能按关键词猜「这句算不算命令」，量不出选对率（猜过，不准）。
BUILTIN: list[tuple[str, set[str] | None]] = [
    # 查
    ("这周有什么安排", {"list_schedule"}),
    ("明天有什么课", {"list_schedule"}),
    ("下一个会议是什么", {"next_schedule"}),
    ("我的提醒有哪些", {"list_alarms"}),
    ("我的备忘里有什么", {"list_memos"}),
    ("导师见面那件事是什么时候", {"list_schedule"}),
    ("我跟导师见面是几点", {"list_schedule"}),
    ("上次说的那个会是什么时候", {"list_schedule"}),
    ("日程里有没有体检这一项", {"list_schedule"}),
    ("帮我看看下周都有什么事", {"list_schedule"}),
    ("我下周有空吗", {"list_schedule"}),
    ("现在几点了", {"now"}),
    # 记
    ("记一下买牛奶", {"add_memo"}),
    ("记一下明天带伞", {"add_memo"}),
    ("下周三下午三点半跟导师见面", {"add_schedule"}),
    ("每周四上午九点有 AIA3102 机器学习，地点教学楼 A302", {"add_schedule"}),
    ("明天下午三点有个面试", {"add_schedule"}),
    ("我周三下午三点半要去见导师", {"add_schedule"}),
    ("把后天下午两点的体检记上", {"add_schedule"}),
    ("提醒我明天早上七点起床", {"add_alarm"}),
    # 改 / 取消（靠临时目录里的种子数据）
    ("把组会挪到周五上午十点", {"change_schedule"}),
    ("取消明天早上的闹钟", {"cancel_alarm"}),
    ("我说是今晚八点", {"fix_last"}),
    # 闲聊：一个工具都不该调
    ("你好，你是谁", None),
    ("讲一下插头DP", None),
    ("你觉得我今天中午吃什么比较好", None),
    ("用一句话介绍杭州", None),
    ("我今天有点累，还要不要继续写代码", None),
    ("帮我写个快速排序", None),
]

# 判断「这句话技能层本来就能处理」用的粗略信号（只看「会不会白跑一趟」）
CMD_HINT = (
    "提醒", "备忘", "日程", "安排", "课", "会议", "开会", "见", "面试", "答辩",
    "体检", "聚餐", "记一下", "记下", "体检", "出门", "点",
)


def load_utterances(args) -> list[tuple[str, set[str] | None]]:
    if args.text:
        return [(args.text, None)]        # 只跑一句时不做判定，只看它选了什么
    if args.builtin:
        return list(BUILTIN)
    # 默认：从真实会话里捞用户说过的话（没有预期，只看选了什么、成不成）
    said: list[str] = []
    for f in sorted(Path("sessions").glob("session-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turn = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (turn.get("user_text") or "").strip()
            if text and text not in said:
                said.append(text)
    picked = said[: args.limit] if args.limit else said
    return [(t, None) for t in picked]


def main() -> int:
    ap = argparse.ArgumentParser(description="评估模型选工具的准确率")
    ap.add_argument("--sessions", action="store_true", help="用真实会话里的原话（默认）")
    ap.add_argument("--builtin", action="store_true", help="用内置样本")
    ap.add_argument("--text", default=None, help="只跑这一句")
    ap.add_argument("--limit", type=int, default=30, help="最多跑几条（默认 30）")
    ap.add_argument("--model", default=None, help="临时换一个模型跑（默认用 config.toml 里的）")
    ap.add_argument("--dry", action="store_true", help="只列句子，不调模型")
    args = ap.parse_args()

    texts = load_utterances(args)
    print("=" * 72)
    print(f" 评估：模型选工具（共 {len(texts)} 句，工具在临时目录里执行，不碰真实数据）")
    if args.model:
        print(f" 模型：{args.model}（临时覆盖配置）")
    print("=" * 72)
    if args.dry:
        for t, want in texts:
            print(f"    {t}" + (f"    （期望 {sorted(want)}）" if want else "    （期望：不调工具）"))
        return 0

    from voice_loop.llm import OllamaClient, OllamaError
    from voice_loop.skills import Skills
    from voice_loop.tools import TOOL_HINT, ToolRegistry, describe_calls, repair_args

    base = load_settings()
    if args.model:
        base.llm.model = args.model
    try:
        probe = OllamaClient(base.llm)
        probe.ensure_model()
    except OllamaError as exc:
        print(f"[错误] Ollama 不可用：{exc}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_eval_"))
    log = logging.getLogger("voice_loop")
    rows: list[dict] = []
    # ★别在循环里引用 args★：下面 `args = calls[0][...]["arguments"]` 会把 argparse 的
    # args 遮蔽掉（踩过：AttributeError: 'dict' object has no attribute 'model'）
    want_model = args.model
    try:
        for text, want in texts:
            # 每次换一套干净的技能数据，免得上一条的写入影响下一条
            st = load_settings()
            st.skills.data_dir = str(tmp)
            st.skills.alarm_file = str(tmp / "a.json")
            st.skills.memo_file = str(tmp / "m.json")
            st.skills.schedule_file = str(tmp / "s.json")
            st.vision.save_dir = str(tmp / "vision")
            if want_model:
                st.llm.model = want_model
            skills = Skills(st, log)
            skills.schedule.save([])
            skills.alarms.save([])
            skills.memos.save([])
            # 种子数据：改/取消类要看得到东西才有得改（都是临时目录，不碰真实的）
            skills.schedule.save([{
                "title": "组会", "kind": "meeting", "repeat": "weekly",
                "weekday": 3, "time": "14:00", "remind_before": [10],
            }])
            skills.alarms.save([{
                "when": (datetime.now() + timedelta(days=1)).replace(
                    hour=8, minute=0, second=0, microsecond=0
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "what": "练琴", "fired": False, "kind": "alarm", "id": 1,
            }])
            # 基准：确定性技能层（用另一份空数据，免得影响上面那套）
            st_base = load_settings()
            st_base.skills.data_dir = str(tmp / "base")
            st_base.skills.alarm_file = str(tmp / "base" / "a.json")
            st_base.skills.memo_file = str(tmp / "base" / "m.json")
            st_base.skills.schedule_file = str(tmp / "base" / "s.json")
            base_skills = Skills(st_base, log)
            base_skills.schedule.save([])
            base_skills.alarms.save([])
            base_skills.memos.save([])
            # 「改刚刚记下的那条」需要一个前置：就当上一句刚记下一条闹钟
            # （不然 _last_add 是空的，这个工具必定接不住——这是样本自己的事）
            if want and "fix_last" in want:
                skills._remember_add("alarm", idx=1)  # noqa: SLF001
            reg = ToolRegistry(st, skills, log)
            llm = OllamaClient(st.llm)

            t0 = time.perf_counter()
            skill = base_skills.handle(text)                  # 基准：确定性技能层
            skill_action = getattr(skill, "action", None)
            _, calls = llm.chat_tools(
                [
                    {"role": "system", "content": st.llm.system_prompt},
                    {"role": "system", "content": TOOL_HINT},
                    {"role": "user", "content": text},
                ],
                tools=reg.specs(),
            )
            dt = time.perf_counter() - t0

            tool = None
            ok = None
            reply = ""
            if calls:
                tool = calls[0]["function"]["name"]
                args = calls[0]["function"].get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                model_text = (args or {}).get("text")
                # ★跟 pipeline 同一道校验★：改写丢了时间就换回原话，否则这里量到的
                # 不是线上真实行为（见 tools.repair_args）
                fixed = repair_args(calls[0], text)
                tool = (fixed.get("function") or fixed).get("name") or tool
                fargs = (fixed.get("function") or fixed).get("arguments") or {}
                if isinstance(fargs, str):
                    try:
                        fargs = json.loads(fargs)
                    except json.JSONDecodeError:
                        fargs = {}
                raw_text = (fargs or {}).get("text")
                fixed_args = raw_text != model_text
                result = reg.call(fixed)
                ok = result.ok
                reply = result.reply
                wrote = {
                    "add_schedule": len(skills.schedule.load()) > 0,
                    "add_memo": len(skills.memos.load()) > 0,
                }.get(tool)
            else:
                raw_text = None
                model_text = None
                fixed_args = False
                wrote = None

            looks_cmd = any(h in text for h in CMD_HINT)
            rows.append({
                "text": text, "want": want, "skill": skill_action, "tool": tool, "ok": ok,
                "reply": reply, "seconds": dt, "raw_text": raw_text,
                "model_text": model_text, "fixed_args": fixed_args,
                "looks_cmd": looks_cmd, "wrote": wrote,
            })
            if want is None:
                right = tool is None
            else:
                right = tool in want
            mark = "√" if right else "×"
            got = f"{tool}" + (f" text={model_text!r}" if model_text else "")
            if fixed_args:
                got += " →已换回原话"
            if not right:
                got += "（期望" + ("不调工具" if want is None else "/".join(sorted(want))) + "）"
            print(f"  {mark} 「{text}」")
            print(f"      技能层={skill_action or '（没接住）'}  模型={got or '（没调工具）'}"
                  f"  {dt:.1f}s")
            if reply:
                print(f"      工具回：{reply[:70]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 汇总
    called = [r for r in rows if r["tool"]]
    print("\n" + "=" * 72)
    print(" 汇总")
    print("=" * 72)
    if not rows:
        print("  （没有样本）")
        return 0
    times = [r["seconds"] for r in rows]
    print(f"  句子数 {len(rows)}    模型调工具 {len(called)} 条"
          f"（{len(called) / len(rows) * 100:.0f}%）")
    print(f"  每句耗时：中位 {stats.median(times):.1f}s  最大 {max(times):.1f}s")
    judged = [r for r in rows if r["want"] is not None]
    right = []
    for r in rows:
        if r["want"] is None:                 # 闲聊：没调工具才算对
            if r["tool"] is None:
                right.append(r)
        elif r["tool"] in r["want"]:          # 命令：选到期望里的工具才算对
            right.append(r)
    print(f"  ★选对（含「该不调就不调」）：{len(right)}/{len(rows)}"
          f"（{len(right) / len(rows) * 100:.0f}%）★")
    if called:
        good = [r for r in called if r["ok"]]
        print(f"  工具执行成功 {len(good)}/{len(called)}")
        print(f"  工具实际用的 text 就是用户原话 "
              f"{sum(1 for r in called if r['raw_text'] == r['text'])}/{len(called)}"
              f"（其中 {sum(1 for r in called if r['fixed_args'])} 条是模型改写后按原话纠正的）")
        bad_before = [r for r in called if r["skill"]]
        print(f"  技能层本来就能处理的 {len(bad_before)} 条"
              f"（这些其实不该走到模型：{', '.join(r['text'] for r in bad_before[:3])}）")
    fp = [r for r in rows if r["want"] is None and r["tool"] is not None]
    print(f"  ★误报（不该调却调了）：{len(fp)} 条★")
    for r in fp:
        print(f"      「{r['text']}」→ {r['tool']}")
    miss = [r for r in rows if r["want"] is not None and r["tool"] is None]
    print(f"  漏调（该调却没调）：{len(miss)} 条")
    for r in miss:
        print(f"      「{r['text']}」→ 技能层={r['skill'] or '（也没接住）'}")
    if judged:
        print(f"  （技能层本来能处理的 {len([r for r in rows if r['skill']])} 条——"
              f"route=model 下它们也交给模型，但模型没接住时会由技能层兜底）")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
