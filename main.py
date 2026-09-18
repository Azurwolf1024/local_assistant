"""本地语音对话链路命令行入口。

常用：
    python main.py selftest              # 全链路自检（推荐第一次运行）
    python main.py listen                # 唤醒词服务（前台，推荐先这么跑）
    python main.py listen -B             # 唤醒词服务（后台，无窗口）
    python main.py stop                  # 停止后台服务
    python main.py chat                  # 普通对话，听到说话就回答
    python main.py chat --mode ptt       # 语音对话（按回车录音）
    python main.py text                  # 打字调试（含技能与语音播报）
    python main.py skills                # 查看闹钟/备忘/日程数据
    python main.py skills "十分钟后提醒我喝水"   # 测试技能识别
    python main.py ask "介绍一下杭州"      # 纯文本问答 + 语音播报
    python main.py asr test.wav          # 音频转文字
    python main.py tts "你好呀" -o a.wav  # 文本转语音
    python main.py devices               # 查看音频设备

数据文件（可直接用编辑器改）：
    data/wakewords.json   唤醒词
    data/alarms.json      闹钟 / 定时提醒
    data/memos.json       备忘
    data/schedule.json    课程表 / 会议
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_loop.audio import list_devices, load_wav, save_wav, segment_audio  # noqa: E402
from voice_loop.settings import Settings, load_settings  # noqa: E402

LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


def ensure_std_streams() -> None:
    """pythonw.exe 下 stdout/stderr 是 None，不给它兜底的话 print 会直接抛异常。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def setup_logging(level: str, log_file: str | Path | None = None) -> None:
    """配置日志；``log_file`` 一旦给出，print 的输出也一并重定向进去（后台运行必需）。"""
    ensure_std_streams()
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        stream = open(p, "a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
    # Windows 控制台本身按 UTF-8 输出中文（PEP 528），这里只兜底避免编码崩溃：
    # 重定向到文件/管道时使用系统编码（cp936），无法表示的字符替换而非抛异常。
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=LOG_LEVELS.get(level.lower(), logging.INFO),
        format="%(asctime)s %(levelname).1s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    if log_file:
        logging.getLogger().info(f"--- 日志开始写入 {log_file} ---")
    for noisy in ("transformers", "optimum", "urllib3", "huggingface_hub", "openvino"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if getattr(args, "strategy", None):
        settings.asr.strategy = args.strategy
    if getattr(args, "mode", None):
        settings.chat.mode = args.mode
    if getattr(args, "device", None):
        settings.asr.whisper_device = args.device
    if getattr(args, "llm", None):
        settings.llm.model = args.llm
    if getattr(args, "log_level", None):
        settings.app.log_level = args.log_level
    return settings


def require_models(settings: Settings, need_tts: bool = True, need_asr: bool = True) -> None:
    missing: list[str] = []
    if need_asr:
        strategy = settings.asr.strategy
        if strategy in ("sensevoice", "hybrid"):
            if not settings.resolve(settings.asr.sensevoice_model).exists():
                missing.append(f"SenseVoice: {settings.asr.sensevoice_model}")
        if strategy in ("whisper", "hybrid"):
            if not settings.resolve(settings.asr.whisper_model).exists():
                missing.append(f"Whisper: {settings.asr.whisper_model}")
    if need_tts and not settings.resolve(settings.tts.model).exists():
        missing.append(f"Piper 语音: {settings.tts.model}")
    if missing:
        raise SystemExit(
            "缺少模型文件：\n  - "
            + "\n  - ".join(missing)
            + "\n请先执行：python scripts/download_models.py"
        )


def build_loop(
    settings: Settings,
    preload: bool = True,
    enable_listening: bool = True,
    lazy_whisper: bool = False,
):
    from voice_loop.pipeline import VoiceLoop

    return VoiceLoop(
        settings,
        logging.getLogger("voice_loop"),
        preload=preload,
        enable_listening=enable_listening,
        lazy_whisper=lazy_whisper,
    )


# --------------------------------------------------------------------------- #
# 进程工具（后台运行用）
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def write_pid(settings: Settings, pid: int | None = None) -> Path:
    pid_file = settings.resolve(settings.wake.pid_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid() if pid is None else int(pid)), encoding="utf-8")
    return pid_file


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    require_models(settings)
    loop = build_loop(settings)
    try:
        loop.chat(args.mode)
    finally:
        loop.close()
    return 0


def cmd_listen(settings: Settings, args: argparse.Namespace) -> int:
    """唤醒词服务。默认前台运行；``--background`` 则转到无窗口的后台进程。"""
    require_models(settings)
    pid_file = settings.resolve(settings.wake.pid_file)
    log_file = settings.resolve(args.log_file or settings.wake.log_file)

    if args.stop_first and pid_file.exists():
        try:
            cmd_stop(settings, args)
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 停止旧服务失败：{exc}")

    if pid_file.exists():
        try:
            old = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            old = 0
        if old and _pid_alive(old) and old != os.getpid():
            print(
                f"[错误] 唤醒服务已经在运行（PID {old}）。\n"
                f"        先停掉：python main.py stop\n"
                f"        或强制重启：python main.py listen -B --stop-first",
                file=sys.stderr,
            )
            return 2
        # 后台启动时父进程会先写好自己的子进程 pid，子进程看到的就是自己，直接忽略
        pid_file.unlink(missing_ok=True)

    if args.background:
        return _spawn_background(settings, args, log_file)

    # 前台运行
    write_pid(settings)
    loop = build_loop(settings, lazy_whisper=settings.wake.lazy_load)
    if getattr(args, "wake_file", None):
        loop.use_wake_file(args.wake_file)
    try:
        loop.service()
    finally:
        loop.close()
    return 0


def _spawn_background(settings: Settings, args: argparse.Namespace, log_file: Path) -> int:
    """用 pythonw.exe 起一个没有控制台窗口的后台进程。"""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe

    stop_file = settings.resolve(settings.wake.stop_file)
    stop_file.unlink(missing_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # 注意：--config / --log-level / --log-file 是全局参数，必须放在子命令前面，
    # 否则 argparse 会报 unrecognized arguments。
    cmd = [str(pythonw), str(Path(__file__).resolve())]
    if getattr(args, "config", None):
        cmd += ["--config", args.config]
    if args.log_level:
        cmd += ["--log-level", args.log_level]
    cmd += ["--log-file", str(log_file), "listen"]
    if getattr(args, "wake_file", None):
        cmd += ["--wake-file", args.wake_file]
    if getattr(args, "strategy", None):
        cmd += ["--strategy", args.strategy]
    if getattr(args, "device", None):
        cmd += ["--device", args.device]
    if getattr(args, "llm", None):
        cmd += ["--llm", args.llm]

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    with open(log_file, "ab") as fh:
        proc = subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=fh,
            stdin=subprocess.DEVNULL,
            cwd=str(settings.root),
            creationflags=creationflags,
            close_fds=True,
        )
    write_pid(settings, proc.pid)
    print(
        f"√ 唤醒服务已在后台启动\n"
        f"  PID      : {proc.pid}\n"
        f"  日志     : {log_file}\n"
        f"  唤醒词   : {settings.resolve(settings.wake.file)}\n"
        f"  停止服务 : python main.py stop\n"
        f"  看日志   : Get-Content '{log_file}' -Wait -Encoding UTF8\n"
    )
    return 0


def _pid_from_log(settings: Settings) -> int | None:
    """pid 文件丢了时，从日志里最后一条「PID=xxxx」恢复。"""
    log_file = settings.resolve(settings.wake.log_file)
    if not log_file.exists():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"PID=(\d+)", text)
    return int(matches[-1]) if matches else None


def cmd_stop(settings: Settings, args: argparse.Namespace) -> int:
    """先发停止信号让服务优雅退出，超时才强杀。"""
    pid_file = settings.resolve(settings.wake.pid_file)
    stop_file = settings.resolve(settings.wake.stop_file)
    pid: int | None = None

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid_file.unlink(missing_ok=True)
            pid = None
    else:
        # pid 文件可能被误删（比如某个脚本退出时顺手删了），从日志里恢复
        recovered = _pid_from_log(settings)
        if recovered and _pid_alive(recovered):
            pid = recovered
            print(f"pid 文件不见了，但日志里找到还在运行的服务（PID {pid}）")

    if pid is None:
        print("没有找到正在运行的服务（缺少 pid 文件，日志里也没有有效记录）")
        return 1

    if not _pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        print(f"进程 {pid} 已经不在了，已清理 pid 文件")
        return 0

    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop", encoding="utf-8")
    print(f"已发送停止信号（PID {pid}），等待优雅退出…")
    for _ in range(60):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)
    else:
        print("服务没有响应，强制结束进程")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 9)
        time.sleep(0.5)

    pid_file.unlink(missing_ok=True)
    stop_file.unlink(missing_ok=True)
    print("√ 唤醒服务已停止")
    return 0


def cmd_text(settings: Settings, args: argparse.Namespace) -> int:
    """打字调试：完整的技能 + LLM + TTS 链路，不用麦克风。"""
    require_models(settings, need_asr=False)
    loop = build_loop(settings, preload=False, enable_listening=False)
    try:
        loop.text_repl(speak=not args.no_tts)
    finally:
        loop.close()
    return 0


def cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    require_models(settings, need_asr=False)
    loop = build_loop(settings, preload=False, enable_listening=False)
    loop.tts_enabled = not args.no_tts
    try:
        print(f"你：{args.text}\n助手：", end="", flush=True)
        stats = loop.respond(args.text, on_delta=lambda d: print(d, end="", flush=True))
        print()
        if stats.extra.get("skill"):
            print(f"[本地技能 {stats.extra['skill']} / 播报 {stats.total_seconds:.2f}s]")
        else:
            print(f"[首字 {stats.llm_first_token:.2f}s / 首音 {stats.first_audio:.2f}s / 总耗时 {stats.total_seconds:.2f}s]")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        loop.close()
    return 0


def cmd_skills(settings: Settings, args: argparse.Namespace) -> int:
    """查看/测试生活技能（时间、闹钟、备忘、日程）。"""
    from voice_loop.skills import Skills

    skills = Skills(settings, logging.getLogger("voice_loop"))

    if args.clear:
        n = {"alarm": skills.alarms.clear(), "memo": skills.memos.clear(), "schedule": skills.schedule.clear()}
        print(f"已清空：提醒 {n['alarm']} 条 / 备忘 {n['memo']} 条 / 日程 {n['schedule']} 条")
        return 0

    if args.text:
        for line in args.text:
            result = skills.handle(line)
            if result is None:
                print(f"「{line}」 -> 未命中技能（会交给 LLM 回答）")
            else:
                print(f"「{line}」 -> [{result.action}] {result.reply}")
        return 0

    # 默认：打印状态与文件位置
    print("技能数据：")
    print(f"  {skills.stats()}")
    for label, path in (
        ("提醒/闹钟", skills.alarms.path),
        ("备忘", skills.memos.path),
        ("日程", skills.schedule.path),
        ("唤醒词", settings.resolve(settings.wake.file)),
    ):
        mark = "√" if path.exists() else "×"
        print(f"  {mark} {label}: {path}")

    alarms = [a for a in skills.alarms.load() if not a.get("fired")]
    if alarms:
        print("\n待触发提醒：")
        for i, a in enumerate(sorted(alarms, key=lambda x: x.get("when", "")), 1):
            print(f"  {i}. {a.get('when')}  {a.get('what')}")
    memos = skills.memos.load()
    if memos:
        print("\n备忘：")
        for i, m in enumerate(memos[:10], 1):
            print(f"  {i}. {m.get('content')}")
    sched = skills.schedule.load()
    if sched:
        print("\n日程（下一次 / 规则 / 提前提醒）：")
        now = datetime.now()
        for i, it in enumerate(sched, 1):
            nxt = skills.next_occurrence(it, now)
            leads = skills._leads_of(it)                       # noqa: SLF001
            lead_text = "、".join(
                "到点" if v <= 0 else (f"{v // 1440}天" if v % 1440 == 0 else f"{v // 60}小时" if v % 60 == 0 else f"{v}分钟")
                for v in leads
            )
            where = f"  @{it['location']}" if it.get("location") else ""
            note = f"  备注：{it['note']}" if it.get("note") else ""
            link = f"  关联：{it.get('linked')}" if it.get("linked") else ""
            skip = f"  跳过：{it['skip']}" if it.get("skip") else ""
            print(
                f"  {i}. {nxt.strftime('%m-%d %H:%M') if nxt else '（已结束）'}"
                f"  {skills._repeat_text(it)}  {it.get('title')}{where}{note}{link}{skip}"
                f"\n     提前提醒：{lead_text}"
            )
    print("\n提示：直接编辑上面这些 json 文件即可增删；用 python main.py skills \"今天有什么课\" 可以测试识别。")
    return 0


def cmd_asr(settings: Settings, args: argparse.Namespace) -> int:
    require_models(settings, need_tts=False)
    from voice_loop.asr import AsrRouter

    path = Path(args.audio)
    if not path.exists():
        print(f"[错误] 找不到音频文件：{path}", file=sys.stderr)
        return 2

    audio, rate = load_wav(path, settings.audio.sample_rate)
    print(f"音频：{path.name}  时长 {audio.size / rate:.2f}s  采样率 {rate} Hz")
    chunks = segment_audio(audio, settings)
    print(f"VAD 切成 {len(chunks)} 段\n")

    router = AsrRouter(settings, logging.getLogger("voice_loop"))
    try:
        if args.engine == "both":
            for i, chunk in enumerate(chunks, 1):
                results = router.transcribe_both(chunk, rate)
                print(f"[片段 {i}] 时长 {chunk.size / rate:.2f}s")
                for r in results:
                    print(f"  {r.engine:11s} ({r.latency:.2f}s, RTF {r.rtf:.2f}): {r.text}")
                print()
        else:
            lines: list[str] = []
            for i, chunk in enumerate(chunks, 1):
                r = router.transcribe(chunk, rate, prefer=None if args.engine == "auto" else args.engine)
                print(f"  [{i:02d}] ({r.engine}, {r.latency:.2f}s, RTF {r.rtf:.2f}) {r.text}")
                lines.append(r.text)
            print("\n全文：\n" + "".join(lines))
    finally:
        router.close()
    return 0


def cmd_tts(settings: Settings, args: argparse.Namespace) -> int:
    require_models(settings, need_asr=False)
    from voice_loop.tts import create_tts

    tts = create_tts(settings)
    text = args.text
    if not text:
        text = input("请输入要合成的文本：").strip()
    if not text:
        print("[错误] 文本为空", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    rate, pcm = tts.synth_bytes(text)
    elapsed = time.perf_counter() - t0
    duration = pcm.size / rate if rate else 0.0
    print(f"合成完成：{duration:.2f}s 音频 / 耗时 {elapsed:.2f}s / RTF {elapsed / max(duration, 1e-6):.2f} / {rate} Hz")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        save_wav(out, pcm.astype("float32") / 32768.0, rate)
        print(f"已保存：{out}")

    if not args.no_play:
        import sounddevice as sd

        print("播放中…")
        sd.play(pcm, rate)
        sd.wait()
    return 0


def cmd_devices(settings: Settings, args: argparse.Namespace) -> int:
    print(list_devices())
    return 0


def cmd_selftest(settings: Settings, args: argparse.Namespace) -> int:
    """逐项检查依赖、模型、Ollama、TTS、ASR，并给出可执行的修复建议。"""
    ok = True
    print("=" * 66)
    print(" 本地语音链路自检")
    print("=" * 66)

    # 1. 依赖
    print("\n[1] Python 依赖")
    deps = {
        "numpy": "numpy",
        "sounddevice": "sounddevice",
        "sherpa_onnx": "sherpa-onnx",
        "openvino": "openvino",
        "onnxruntime": "onnxruntime",
        "piper": "piper-tts",
        "requests": "requests",
    }
    for mod, pkg in deps.items():
        try:
            __import__(mod)
            print(f"  √ {mod}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  × {mod} ({exc})  ->  pip install {pkg}")
    for mod, pkg in (("torch", "torch"), ("transformers", "transformers"), ("optimum.intel", "optimum-intel[openvino]")):
        try:
            __import__(mod)
            print(f"  √ {mod}")
        except Exception:  # noqa: BLE001
            print(f"  · {mod} 未安装（仅 Whisper 需要）->  pip install \"{pkg}\"")

    # 2. 模型
    print("\n[2] 模型文件")
    checks = [
        ("SenseVoiceSmall", settings.asr.sensevoice_model),
        ("SenseVoice tokens", settings.asr.sensevoice_tokens),
        ("Whisper OV", settings.asr.whisper_model),
        ("Piper 语音", settings.tts.model),
        ("Silero VAD（可选）", settings.vad.model),
    ]
    for label, rel in checks:
        p = settings.resolve(rel)
        if p.exists():
            size = p.stat().st_size if p.is_file() else 0
            print(f"  √ {label}: {rel}" + (f"  ({size / 1024 / 1024:.1f} MB)" if size else ""))
        else:
            optional = "可选" in label
            print(f"  {'·' if optional else '×'} {label} 缺失: {rel}")
            if not optional:
                ok = False

    # 3. Ollama
    print("\n[3] Ollama")
    from voice_loop.llm import OllamaClient, OllamaError

    llm = OllamaClient(settings.llm)
    try:
        models = llm.list_models()
        print(f"  √ 服务在线 {settings.llm.host}")
        print(f"   可用模型: {', '.join(models) if models else '(无)'}")
        llm.ensure_model()
        print(f"  √ 使用模型 {settings.llm.model}")
        print(f"   预热耗时 {llm.warmup():.2f}s")
    except OllamaError as exc:
        ok = False
        print(f"  × {exc}")

    # 4. TTS
    print("\n[4] 语音合成 (Piper)")
    try:
        require_models(settings, need_asr=False)
        from voice_loop.tts import create_tts

        tts = create_tts(settings)
        info = tts.benchmark("你好，我是本地部署的中文语音助手。")
        print(
            f"  √ {settings.tts.voice}  采样率 {info['sample_rate']} Hz  "
            f"合成 {info['audio_seconds']:.2f}s 用时 {info['synth_seconds']:.2f}s  RTF {info['rtf']:.2f}"
        )
        if not args.no_play:
            import sounddevice as sd
            from voice_loop.audio import Speaker

            speaker = Speaker(settings)
            for rate, pcm in tts.synth("语音合成测试，你好呀。"):
                speaker.submit(pcm, rate)
            speaker.join()
            speaker.close()
            print("  √ 已播放测试语音（--no-play 可跳过）")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  × {exc}")

    # 5. ASR
    print("\n[5] 语音识别")
    try:
        require_models(settings, need_tts=False)
        from voice_loop.asr import AsrRouter

        router = AsrRouter(settings, logging.getLogger("voice_loop"))
        print(f"   已加载引擎: {', '.join(router.available) or '(无)'}")

        # 找一个测试音频：示例音频 -> 自行用 Piper 合成一段
        candidates = [
            settings.resolve("models/asr/sensevoice-small/test_wavs/zh.wav"),
            settings.resolve("models/asr/sensevoice-small/zh.wav"),
        ]
        sample = next((p for p in candidates if p.exists()), None)
        if sample is None:
            from voice_loop.tts import create_tts

            sample = settings.sessions_dir / "selftest_asr.wav"
            _rate, _pcm = create_tts(settings).synth_bytes("你好，这是一段用于测试的语音。")
            save_wav(sample, _pcm.astype("float32") / 32768.0, _rate)
            print(f"   · 没有示例音频，已用 Piper 合成 {sample.name}")

        audio, rate = load_wav(sample, 16000)
        for r in router.transcribe_both(audio, rate):
            print(f"  √ {r.engine:11s} {r.latency:.2f}s RTF {r.rtf:.2f}  「{r.text}」")
        router.close()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  × {exc}")

    # 6. 音频设备
    print("\n[6] 音频设备与麦克风")
    try:
        print("  " + list_devices().replace("\n", "\n  "))

        from voice_loop.audio import MicReader, make_segmenter

        mic = MicReader(settings)
        with mic:
            frames = [mic.read() for _ in range(12)]
        import numpy as _np

        block = _np.concatenate(frames)
        rms = float(_np.sqrt(_np.mean(block**2)))
        peak = float(_np.max(_np.abs(block)))
        print(f"\n  √ 麦克风读取正常：{block.size} 采样 / RMS {rms:.4f} / 峰值 {peak:.3f}")
        if peak < 0.01:
            print("    ! 采集电平偏低（正常人说话峰值一般在 0.05 以上）：")
            print("      1) 检查 Windows 设置 > 隐私和安全性 > 麦克风 是否允许桌面应用访问")
            print("      2) 换个输入设备试试，例如 [12] 麦克风阵列 3 原生就是 16000 Hz，")
            print("         把 config.toml 里 [audio] input_device 填 12 或设备名关键字")
            print("      3) 麦克风阵列首通道增益可能很低，也可以把 mic_gain 调到 2.0 试试")
        seg = make_segmenter(settings)
        print(f"  √ VAD 就绪：{type(seg).__name__}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  × {exc}")

    # 7. 生活技能与唤醒词
    print("\n[7] 生活技能 / 唤醒词")
    try:
        import shutil
        import tempfile

        from voice_loop.skills import Skills
        from voice_loop.wake import WakeWordMatcher

        probe_dir = Path(tempfile.mkdtemp(prefix="voiceloop_selftest_"))
        probe = Skills(settings, logging.getLogger("voice_loop"))
        probe.data_dir = probe_dir
        probe.alarms = type(probe.alarms)(probe_dir / "alarms.json")
        probe.memos = type(probe.memos)(probe_dir / "memos.json")
        probe.schedule = type(probe.schedule)(probe_dir / "schedule.json")

        real = Skills(settings, logging.getLogger("voice_loop"))
        print(f"  √ {real.stats()}")
        for label, path in (
            ("提醒/闹钟", real.alarms.path),
            ("备忘", real.memos.path),
            ("日程", real.schedule.path),
        ):
            print(f"    {'√' if path.exists() else '×'} {label}: {path}")
        for q in ("现在几点了", "十分钟后提醒我喝水", "今天有什么课"):
            r = probe.handle(q)
            print(f"    · 「{q}」 -> {('[' + r.action + '] ' + r.reply) if r else '交给 LLM'}")
        shutil.rmtree(probe_dir, ignore_errors=True)

        wake_path = settings.resolve(settings.wake.file)
        matcher = WakeWordMatcher(wake_path)
        words = "、".join(matcher.settings.words)
        if matcher.enabled:
            print(f"  √ 唤醒词：{words}    (配置 {wake_path})")
            for item in matcher.settings.words:
                hit = matcher.match(f"{item}，现在几点了")
                if hit is None:
                    ok = False
                print(f"    · 「{item}，现在几点了」 -> {'命中' if hit else '未命中'}")
            print(f"    · 唤醒后应答语：{matcher.settings.ack!r}")
        else:
            print(f"  ! 唤醒词未启用：{wake_path}（listen 命令会退化为普通对话）")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  × {exc}")

    print("\n" + "=" * 66)
    print(" 结果：" + ("全部通过，可以运行 `python main.py listen` 了" if ok else "存在问题，请按上面的提示修复"))
    print("=" * 66)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="本地语音到语音对话链路（Whisper/SenseVoice + Ollama + Piper）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.toml）")
    ap.add_argument("--log-level", choices=["debug", "info", "warning"], default=None)
    ap.add_argument("--log-file", default=None, help="把日志与输出同时写进文件（后台运行需要）")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("chat", help="语音对话")
    p.add_argument("--mode", choices=["vad", "ptt"], default=None)
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--device", default=None, help="Whisper 设备：CPU / GPU / AUTO")
    p.add_argument("--llm", default=None, help="覆盖 Ollama 模型名")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("listen", help="唤醒词服务（后台跑闹钟/日程提醒）")
    p.add_argument("--wake-file", default=None, help="唤醒词 json 路径（默认取 config.toml）")
    p.add_argument("-B", "--background", action="store_true", help="转到后台运行（无窗口）")
    p.add_argument("--stop-first", action="store_true", help="启动前先停掉已在运行的服务")
    p.add_argument("--mode", default=None)
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--device", default=None)
    p.add_argument("--llm", default=None)
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("stop", help="停止后台运行的唤醒服务")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("text", help="打字调试（完整技能 + LLM + 语音播报）")
    p.add_argument("--no-tts", action="store_true", help="只输出文字")
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--llm", default=None)
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("skills", help="查看/测试生活技能（时间、闹钟、备忘、日程）")
    p.add_argument("text", nargs="*", help="要测试的句子，可给多条")
    p.add_argument("--clear", action="store_true", help="清空所有提醒/备忘/日程")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("ask", help="纯文本提问 + 语音播报")
    p.add_argument("text", help="要提问的内容")
    p.add_argument("--no-tts", action="store_true", help="只输出文字不合成语音")
    p.add_argument("--llm", default=None)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("asr", help="音频文件转写")
    p.add_argument("audio", help="wav 文件路径")
    p.add_argument("--engine", choices=["auto", "sensevoice", "whisper", "both"], default="auto")
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--device", default=None)
    p.set_defaults(func=cmd_asr)

    p = sub.add_parser("tts", help="文本转语音")
    p.add_argument("text", nargs="?", default=None)
    p.add_argument("-o", "--out", default=None, help="保存为 wav 文件")
    p.add_argument("--no-play", action="store_true")
    p.set_defaults(func=cmd_tts)

    p = sub.add_parser("devices", help="列出音频设备")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("selftest", help="全链路自检")
    p.add_argument("--no-play", action="store_true")
    p.set_defaults(func=cmd_selftest)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0

    settings = load_settings(args.config)
    settings = apply_overrides(settings, args)
    setup_logging(args.log_level or settings.app.log_level, args.log_file)
    return args.func(settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
