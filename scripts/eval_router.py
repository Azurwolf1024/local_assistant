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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_loop.settings import load_settings  # noqa: E402

# 内置样本：命令类为主，外加几条闲聊（用来量「会不会乱调工具」）
BUILTIN = [
    # 命令（应该调工具）
    "这周有什么安排",
    "明天有什么课",
    "下一个会议是什么",
    "我的提醒有哪些",
    "我的备忘里有什么",
    "记一下买牛奶",
    "记一下明天带伞",
    "下周三下午三点半跟导师见面",
    "每周四上午九点有 AIA3102 机器学习，地点教学楼 A302",
    "明天下午三点有个面试",
    "我周三下午三点半要去见导师",
    "导师见面那件事是什么时候",
    "帮我看看下周都有什么事",
    "我下周有空吗",
    "把后天下午两点的体检记上",
    # 闲聊（不该调工具）
    "你好，你是谁",
    "讲一下插头DP",
    "你觉得我今天中午吃什么比较好",
    "用一句话介绍杭州",
    "我今天有点累，还要不要继续写代码",
]

# 判断「这句话技能层本来就能处理」用的粗略信号
CMD_HINT = (
    "提醒", "备忘", "日程", "安排", "课", "会议", "开会", "见", "面试", "答辩",
    "体检", "聚餐", "记一下", "记下", "体检", "出门", "点",
)


def load_utterances(args) -> list[str]:
    if args.text:
        return [args.text]
    if args.builtin:
        return list(BUILTIN)
    # 默认：从真实会话里捞用户说过的话
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
    return said[: args.limit] if args.limit else said


def main() -> int:
    ap = argparse.ArgumentParser(description="评估模型选工具的准确率")
    ap.add_argument("--sessions", action="store_true", help="用真实会话里的原话（默认）")
    ap.add_argument("--builtin", action="store_true", help="用内置样本")
    ap.add_argument("--text", default=None, help="只跑这一句")
    ap.add_argument("--limit", type=int, default=30, help="最多跑几条（默认 30）")
    ap.add_argument("--dry", action="store_true", help="只列句子，不调模型")
    args = ap.parse_args()

    texts = load_utterances(args)
    print("=" * 72)
    print(f" 评估：模型选工具（共 {len(texts)} 句，工具在临时目录里执行，不碰真实数据）")
    print("=" * 72)
    if args.dry:
        for t in texts:
            print(f"    {t}")
        return 0

    from voice_loop.llm import OllamaClient, OllamaError
    from voice_loop.skills import Skills
    from voice_loop.tools import TOOL_HINT, ToolRegistry, describe_calls

    base = load_settings()
    try:
        probe = OllamaClient(base.llm)
        probe.ensure_model()
    except OllamaError as exc:
        print(f"[错误] Ollama 不可用：{exc}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="voiceloop_eval_"))
    log = logging.getLogger("voice_loop")
    rows: list[dict] = []
    try:
        for text in texts:
            # 每次换一套干净的技能数据，免得上一条的写入影响下一条
            st = load_settings()
            st.skills.data_dir = str(tmp)
            st.skills.alarm_file = str(tmp / "a.json")
            st.skills.memo_file = str(tmp / "m.json")
            st.skills.schedule_file = str(tmp / "s.json")
            st.vision.save_dir = str(tmp / "vision")
            skills = Skills(st, log)
            skills.schedule.save([])
            skills.alarms.save([])
            skills.memos.save([])
            reg = ToolRegistry(st, skills, log)
            llm = OllamaClient(st.llm)

            t0 = time.perf_counter()
            skill = skills.handle(text)                      # 基准：确定性技能层
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
                raw_text = (args or {}).get("text")
                result = reg.call(calls[0])
                ok = result.ok
                reply = result.reply
                wrote = {
                    "add_schedule": len(skills.schedule.load()) > 0,
                    "add_memo": len(skills.memos.load()) > 0,
                }.get(tool)
            else:
                raw_text = None
                wrote = None

            looks_cmd = any(h in text for h in CMD_HINT)
            rows.append({
                "text": text, "skill": skill_action, "tool": tool, "ok": ok,
                "reply": reply, "seconds": dt, "raw_text": raw_text,
                "looks_cmd": looks_cmd, "wrote": wrote,
            })
            mark = "·" if tool is None else ("√" if ok else "×")
            got = f"{tool}" + (f" text={raw_text!r}" if raw_text else "")
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
    if called:
        good = [r for r in called if r["ok"]]
        print(f"  工具执行成功 {len(good)}/{len(called)}")
        print(f"  工具拿到的是原话 "
              f"{sum(1 for r in called if r['raw_text'] == r['text'])}/{len(called)}")
        bad_before = [r for r in called if r["skill"]]
        print(f"  技能层本来就能处理的 {len(bad_before)} 条"
              f"（这些其实不该走到模型：{', '.join(r['text'] for r in bad_before[:3])}）")
    chat = [r for r in rows if not r["tool"] and not r["looks_cmd"]]
    print(f"  没调工具的 {len(rows) - len(called)} 条（闲聊为主：{len(chat)}）")
    missed = [r for r in rows if not r["tool"] and r["skill"] is None and r["looks_cmd"]]
    print(f"  漏网（技能没接住、模型也没调工具）{len(missed)} 条")
    for r in missed:
        print(f"      「{r['text']}」")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
