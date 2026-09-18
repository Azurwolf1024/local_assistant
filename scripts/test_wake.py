"""唤醒词实测与调优工具。

用途：拿真实嗓音试唤醒词，看语音识别到底把它听成了什么，并直接写进 aliases。

用法：
    python scripts/test_wake.py                 # 说一句测一次
    python scripts/test_wake.py --rounds 5      # 连测 5 次，最后给统计
    python scripts/test_wake.py --apply         # 未命中时自动追加到 aliases
    python scripts/test_wake.py --seconds 8     # 每次最多等 8 秒

先停掉常驻服务再跑（两边抢麦克风）：
    python main.py stop
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.audio import MicReader, make_segmenter  # noqa: E402
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


def append_alias(wake_file: Path, word: str, heard: str) -> str:
    """把实测到的说法写进 aliases，保留文件里其它字段与注释。"""
    # 先归一化：识别结果常带标点（写成「海尔西。」这种别名又脏又容易误命中），
    # 而匹配本身也是按归一化后的文本比的，所以存干净的形式即可。
    clean = normalize(heard)
    if not clean:
        return "已跳过（空的）"
    if not looks_like(clean, word):
        return f"已跳过（{clean!r} 与「{word}」差太远，大概是环境杂音）"
    raw = json.loads(wake_file.read_text(encoding="utf-8") or "{}")
    aliases = raw.setdefault("aliases", {})
    bucket = [normalize(x) for x in aliases.get(word, []) or []]
    if clean in bucket:
        return "已存在"
    bucket.append(clean)
    # 顺手去重并保持可读
    aliases[word] = sorted(set(bucket), key=lambda x: (len(x), x))
    wake_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "已写入"


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
    ap.add_argument("--engine", choices=["sensevoice", "whisper", "both"], default="both")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR)
    settings = load_settings()
    wake_file = settings.resolve(settings.wake.file)
    matcher = WakeWordMatcher(wake_file)
    words = matcher.settings.words
    ratio = matcher.settings.fuzzy_ratio

    print("=" * 68)
    print(" 唤醒词实测")
    print("=" * 68)
    print(f"  配置文件 : {wake_file}")
    print(f"  唤醒词   : {'、'.join(words)}")
    print(f"  别名     : {len(matcher.settings.aliases.get(words[0], [])) if words else 0} 个")
    print(f"  模糊阈值 : {ratio}   静音判定: {matcher.settings.min_silence}s")
    print(f"  待唤醒用的是 SenseVoice，所以只有它听到什么才决定能不能唤醒")
    print("\n  直接对麦克风说唤醒词即可（比如「凯尔希」）。Ctrl+C 结束。\n")

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
    misses: list[str] = []
    try:
        with mic:
            for i in range(1, max(1, args.rounds) + 1):
                print(f"--- 第 {i}/{args.rounds} 轮：请说唤醒词 ---")
                audio = listen_utterance(mic, segmenter, args.seconds)
                if audio is None or audio.size < 1600:
                    print("   没听到有效语音（太短或没检测到人声）\n")
                    continue
                sr = int(settings.audio.sample_rate)
                heard = standby.transcribe(audio, sr).text.strip()
                hit = matcher.match(heard)

                best_word = words[0] if words else ""
                best_ratio = 0.0
                for w in words:
                    r = difflib.SequenceMatcher(None, normalize(heard), normalize(w)).ratio()
                    if r > best_ratio:
                        best_word, best_ratio = w, r

                print(f"   时长 {audio.size / sr:.2f}s")
                print(f"   SenseVoice 听到：{heard!r}")
                if extra is not None:
                    print(f"   Whisper  听到：{extra.transcribe(audio, sr).text.strip()!r}（仅供参考）")

                if hit is not None:
                    hits += 1
                    kind = "模糊匹配" if hit.fuzzy else "精确/别名"
                    print(f"   √ 命中「{hit.word}」（{kind}）")
                else:
                    misses.append(heard)
                    print(f"   × 未命中。与「{best_word}」相似度 {best_ratio:.2f}"
                          f"（需要 ≥ {ratio} 才算模糊命中）")
                    if best_ratio < ratio:
                        if len(normalize(heard)) < 2:
                            print("     → 只听到了一个字，多半是被截断了，再试一次试试")
                        else:
                            print(f"     → 建议把 {heard!r} 加进 aliases")
                            if args.apply:
                                print(f"     → {append_alias(wake_file, best_word, heard)}")
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
        print(f" 未命中的说法：{dict(Counter(misses))}")
        print("\n 把下面这段贴进 aliases 里（或下次加 --apply 自动写）：")
        for w in words[:1]:
            items = sorted(set(misses) | set(matcher.settings.aliases.get(w, [])))
            print(f'   "{w}": {json.dumps(items, ensure_ascii=False)}')
        if not args.apply:
            print(f"\n 也可以直接改文件：{wake_file}")
            print(" 前台运行的服务会在几秒内自动重新加载，不用重启。")
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
