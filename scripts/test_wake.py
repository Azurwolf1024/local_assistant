"""唤醒词实测与调优工具。

用途：拿真实嗓音试唤醒词，看语音识别到底把它听成了什么，并直接写进 aliases。

用法：
    python scripts/test_wake.py                 # 说一句测一次
    python scripts/test_wake.py --rounds 5      # 连测 5 次，最后给统计
    python scripts/test_wake.py --apply         # 未命中时自动追加到 aliases
    python scripts/test_wake.py --char amiya    # 只测阿米娅的词
    python scripts/test_wake.py --seconds 8     # 每次最多等 8 秒

★写到哪里（这个弄错过）★：多角色时唤醒词和别名都在**角色自己的**
``data/personas/<id>.json`` 里，所以 ``--apply`` 写进「被喊的那位」的人格文件，
**不是** ``data/wakewords.json``——那个只在「一个可用角色都没有」时兜底
（见 :meth:`voice_loop.wake.WakeWordMatcher.set_characters`：一旦装上角色，
匹配表就只用角色文件里的 aliases 重建，全局那份会被忽略）。
写进人格文件后前台服务几秒内自动热重载，不用重启。

先停掉常驻服务再跑（两边抢麦克风）：
    python main.py stop
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.audio import MicReader, make_segmenter  # noqa: E402
from voice_loop.persona import CharacterRegistry  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.wake import WakeWordMatcher, normalize  # noqa: E402


def listen_utterance(mic, segmenter, seconds: float) -> np.ndarray | None:
    segmenter.reset()
    mic.flush()
    started = False
    t0 = time.time()
    while True:
        frame = mic.read()
        got = segmenter.accept(frame)
        if got is not None:
            return got
        if getattr(segmenter, "speech_detected", False):
            started = True
            t0 = time.time()
        if not started and time.time() - t0 > seconds:
            return None


@dataclass
class WakeTarget:
    """一条「能测的唤醒词」：词、属于谁、该写到哪个文件。"""

    word: str                # 唤醒词（写在 aliases 里的 key）
    owner: str               # 角色 id；"" = 没有可用角色，写全局兜底文件
    label: str               # 打印用的名字
    file: Path               # --apply 要写的 json
    aliases: list[str] = field(default_factory=list)   # 现有别名


def write_alias(target: WakeTarget, heard: str) -> str:
    """把实测到的说法写进 aliases（角色 → 人格文件；没有角色 → 全局 wakewords.json）。

    ★这里踩过坑★：旧版不管有没有角色都写 data/wakewords.json，但多角色时匹配表是用
    **人格文件**里的 aliases 建的（见 WakeWordMatcher.set_characters），全局那份会被忽略
    ——「写进去了」却不起作用，白测一轮。
    """
    # 先归一化：识别结果常带标点（写成「海尔西。」这种别名又脏又容易误命中），
    # 而匹配本身也是按归一化后的文本比的，所以存干净的形式即可。
    clean = normalize(heard)
    if not clean:
        return "已跳过（空的）"
    if not looks_like(clean, target.word):
        return f"已跳过（{clean!r} 与「{target.word}」差太远，大概是环境杂音）"

    try:
        raw = json.loads(target.file.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        return f"写不进去（读 {target.file.name} 失败：{exc}）"
    if not isinstance(raw, dict):
        return f"已跳过（{target.file.name} 顶层不是对象）"
    aliases = raw.setdefault("aliases", {})
    if not isinstance(aliases, dict):  # 手改坏了就别动它
        return f"已跳过（{target.file.name} 的 aliases 不是对象）"
    bucket = [normalize(x) for x in aliases.get(target.word, []) or []]
    if clean in bucket:
        return "已存在（不用重复写）"
    bucket.append(clean)
    aliases[target.word] = sorted(set(bucket), key=lambda x: (len(x), x))

    backup = target.file.with_suffix(target.file.suffix + ".bak")
    try:
        shutil.copyfile(target.file, backup)
    except OSError:
        pass
    tmp = target.file.with_suffix(target.file.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target.file)
    except OSError as exc:
        return f"写不进去（{exc}）"
    return f"已写入 {target.file.name}（「{target.word}」现有别名 {len(bucket)} 个）"


def load_registry(settings):
    """读角色索引；读不到/没启用就返回 None（退回全局唤醒词）。"""
    if getattr(settings, "persona", None) is None or not settings.persona.enabled:
        return None
    try:
        registry = CharacterRegistry(settings.resolve(settings.persona.file))
        registry.load()
        return registry
    except Exception as exc:  # noqa: BLE001
        print(f"  （角色文件读不了，退回全局唤醒词：{exc}）")
        return None


def collect_targets(settings, matcher, registry, only: str = "") -> list[WakeTarget]:
    """列出所有能测的唤醒词及它们各自该写到哪里。

    有角色时以角色文件为准（``data/personas/<id>.json`` 的 ``wake_words`` /
    ``aliases``）；一个可用角色都没有时才退回全局 ``data/wakewords.json``。
    """
    global_file = settings.resolve(settings.wake.file)
    targets: list[WakeTarget] = []
    seen: set[str] = set()
    if registry is not None:
        chars = registry.all(only_enabled=True)
        if only:
            chars = [
                c for c in chars
                if c.id.lower() == only.lower() or c.name == only or only in (c.aliases or {})
            ]
            if not chars:
                known = "、".join(c.id for c in registry.all(only_enabled=True)) or "（无）"
                print(f"  × 没有这个角色：{only}（可用：{known}）")
        for char in chars:
            path = (registry.files or {}).get(char.id)
            if path is None:
                # 唤醒词直接内联在 characters.json 里的角色：要改那个大文件，这里先跳过
                print(f"  （{char.name} 的唤醒词内联在索引文件里，本工具不写它）")
                continue
            for word in char.wake_words:
                key = normalize(word)
                if not key or key in seen:
                    continue
                seen.add(key)
                targets.append(
                    WakeTarget(
                        word=word,
                        owner=char.id,
                        label=f"{char.name}（{char.id}）",
                        file=Path(path),
                        aliases=list(char.aliases.get(word, []) or []),
                    )
                )
    if only:
        # 用户点名要看某个角色：就不再混进全局兜底那些词了（否则 --char 打错字
        # 会静默变成「测全局唤醒词」，看着像成功其实测错了对象）
        return targets
    for word in matcher.settings.words:
        key = normalize(word)
        if not key or key in seen:
            continue
        seen.add(key)
        targets.append(
            WakeTarget(
                word=word,
                owner="",
                label="全局兜底（wakewords.json）",
                file=global_file,
                aliases=list(matcher.settings.aliases.get(word, []) or []),
            )
        )
    return targets


def looks_like(heard: str, word: str) -> bool:
    """挡一下明显不相干的识别结果，别把环境里听到的怪词当成别名存进去。

    唤醒词一般三字上下，错听总是那么几种：首字不同（凯/开/海/太）、
    末字相近（希/西/戏/信）。所以只要长度接近、且有一半以上的字相同就收。
    """
    target = normalize(word)
    if not heard or not target:
        return False
    if abs(len(heard) - len(target)) > 1:
        return False
    same = sum(1 for ch in heard if ch in target)
    return same >= max(1, len(target) // 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="唤醒词实测与调优")
    ap.add_argument("--rounds", type=int, default=1, help="测试轮数")
    ap.add_argument("--seconds", type=float, default=15.0, help="每轮等待说话的秒数")
    ap.add_argument("--apply", action="store_true", help="未命中时自动写进 aliases")
    ap.add_argument("--char", default="", help="只测这个角色（id 或名字，如 amiya / 阿米娅）")
    ap.add_argument("--engine", choices=["sensevoice", "whisper", "both"], default="both")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR)
    settings = load_settings()
    wake_file = settings.resolve(settings.wake.file)
    matcher = WakeWordMatcher(wake_file)
    ratio = matcher.settings.fuzzy_ratio

    # ★多角色★：先把角色装进匹配表。否则喊「阿米娅」永远判未命中——匹配表里没这个词，
    # 而且「命中后切到谁」也来自匹配表（WakeHit.character）。
    registry = load_registry(settings)
    if registry is not None:
        matcher.set_characters(registry.all(only_enabled=True))
    targets = collect_targets(settings, matcher, registry, only=args.char)
    if not targets:
        print("× 一个可测的唤醒词都没有（角色文件里没配 wake_words？）", file=sys.stderr)
        return 2
    words = [t.word for t in targets]

    print("=" * 68)
    print(" 唤醒词实测")
    print("=" * 68)
    print(f"  全局配置 : {wake_file}")
    print(f"  模糊阈值 : {ratio}   静音判定: {matcher.settings.min_silence}s")
    print(f"  待测的词 : {'、'.join(words)}")
    for t in targets:
        where = t.file.name if t.owner else f"{t.file.name}（兜底）"
        print(f"    {t.word:<8} → {t.label:<20} 现有别名 {len(t.aliases)} 个  [{where}]")
    print("  --apply 写进上面方括号里的那个文件（角色的各自人格文件，不是全局那个）")
    print("  待唤醒用的是 SenseVoice，所以只有它听到什么才决定能不能唤醒")
    print("\n  直接对麦克风说唤醒词即可（比如「阿米娅」）。Ctrl+C 结束。\n")

    from voice_loop.asr import SenseVoiceEngine

    standby = SenseVoiceEngine(
        model_path=settings.resolve(settings.asr.sensevoice_model),
        tokens_path=settings.resolve(settings.asr.sensevoice_tokens),
        num_threads=settings.asr.num_threads,
        use_itn=settings.asr.sensevoice_use_itn,
        language=settings.asr.language,
    )
    extra = None
    if args.engine in ("whisper", "both"):
        try:
            from voice_loop.asr import WhisperOpenVinoEngine

            extra = WhisperOpenVinoEngine(
                model_dir=settings.resolve(settings.asr.whisper_model),
                device=settings.asr.whisper_device,
                language=settings.asr.language,
                num_threads=settings.asr.num_threads,
            )
            print("  （Whisper 仅作参考，待唤醒状态不会加载它）\n")
        except Exception as exc:  # noqa: BLE001
            print(f"  Whisper 不可用（{exc}），只看 SenseVoice\n")

    mic = MicReader(settings)
    segmenter = make_segmenter(settings, min_silence=matcher.settings.min_silence, quiet=True)

    hits = 0
    misses: list[tuple[WakeTarget, str]] = []
    try:
        with mic:
            for i in range(1, max(1, args.rounds) + 1):
                print(f"--- 第 {i}/{args.rounds} 轮：请说唤醒词（{'、'.join(words)}）---")
                audio = listen_utterance(mic, segmenter, args.seconds)
                if audio is None or audio.size < 1600:
                    print("   没听到有效语音（太短或没检测到人声）\n")
                    continue
                sr = int(settings.audio.sample_rate)
                heard = standby.transcribe(audio, sr).text.strip()
                hit = matcher.match(heard)

                norm_heard = normalize(heard)
                best = targets[0]
                best_ratio = 0.0
                for t in targets:
                    r = difflib.SequenceMatcher(None, norm_heard, normalize(t.word)).ratio()
                    if r > best_ratio:
                        best, best_ratio = t, r

                print(f"   时长 {audio.size / sr:.2f}s")
                print(f"   SenseVoice 听到：{heard!r}")
                if extra is not None:
                    print(f"   Whisper  听到：{extra.transcribe(audio, sr).text.strip()!r}（仅供参考）")

                if hit is not None:
                    hits += 1
                    kind = "模糊匹配" if hit.fuzzy else "精确/别名"
                    owner = ""
                    if hit.character:
                        who = registry.get(hit.character) if registry is not None else None
                        owner = f"，会切到 {who.name if who else hit.character}"
                    print(f"   √ 命中「{hit.word}」（{kind}{owner}）")
                else:
                    misses.append((best, heard))
                    print(f"   × 未命中。最像「{best.word}」（{best.label}）相似度 {best_ratio:.2f}"
                          f"，需要 ≥ {ratio} 才算模糊命中")
                    if best_ratio < ratio:
                        if len(norm_heard) < 2:
                            print("     → 只听到了一个字，多半是被截断了，再试一次试试")
                        else:
                            print(f"     → 建议把 {heard!r} 加进「{best.word}」的 aliases"
                                  f"（{best.file.name}）")
                            if args.apply:
                                print(f"     → {write_alias(best, heard)}")
                    if best_ratio < 0.34:
                        print("     → 差得比较远。如果多次都这样，建议换个更长的唤醒词（例：凯尔希医生）")
                print()
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        if extra is not None:
            try:
                extra.transcribe(np.zeros(1600, dtype=np.float32), 16000)
            except Exception:  # noqa: BLE001
                pass

    total = hits + len(misses)
    print("=" * 68)
    if total == 0:
        print(" 这一轮没有采集到有效语音，无法判断。")
        print(" 检查：麦克风是否被其它程序占用（先 python main.py stop）、")
        print("       或先用 python scripts/test_mic_loopback.py 确认麦克风电平。")
    elif misses:
        print(f" 命中 {hits}/{total}")
        print(f" 未命中的说法：{dict(Counter(h for _t, h in misses))}")
        print("\n 下面这段贴进对应文件即可（或加 --apply 自动写）：")
        groups: dict[tuple[Path, str], list[str]] = {}
        for target, heard in misses:
            groups.setdefault((target.file, target.word), []).append(normalize(heard))
        alias_map = {(t.file, t.word): t.aliases for t in targets}
        for (path, word), heard_list in groups.items():
            items = sorted(set(heard_list) | {normalize(x) for x in alias_map.get((path, word), [])})
            print(f'   {path.name}  ->  "{word}": {json.dumps(items, ensure_ascii=False)}')
        if not args.apply:
            print("\n 也可以直接改上面那些文件；前台运行的服务会在几秒内自动热重载，不用重启。")
    else:
        print(f" {total} 次全部命中，唤醒词没问题。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
