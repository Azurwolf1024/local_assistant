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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_loop.audio import list_devices, load_wav, save_wav, segment_audio  # noqa: E402
from voice_loop import service_ctl  # noqa: E402
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
    if getattr(args, "character", None):
        settings.persona.default = args.character
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
# ★实现下沉到 voice_loop/service_ctl.py★：控制台 UI 是另一个进程，也要查状态/启停服务，
# 而它不该为了这点事去 import main（会连带加载 numpy / sounddevice）。
# 这里保留同名别名，免得改散落到各处的调用点（也保证只有一份实现）。
# --------------------------------------------------------------------------- #
_pid_alive = service_ctl.pid_alive
_pid_from_log = service_ctl.pid_from_log


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


def cmd_ui(settings: Settings, args: argparse.Namespace) -> int:
    """可视化控制台（独立进程，只监听回环地址）。

    ★不需要麦克风、不需要加载模型★：它靠读同一批数据文件 + 往服务信箱投命令
    来工作（见 voice_loop/console/__init__.py 的说明）。
    """
    try:
        from voice_loop.console import serve as console_serve
    except ImportError as exc:  # 缺 fastapi/uvicorn
        raise SystemExit(
            "控制台需要两个额外依赖：\n"
            "    pip install fastapi uvicorn\n"
            f"（导入失败：{exc}）"
        ) from exc
    return console_serve(
        settings,
        host=args.host,
        port=int(args.port),
        open_browser=not args.no_browser,
        logger=logging.getLogger("voice_loop.console"),
    )


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


def _tail_hint(log_file: Path) -> str:
    """跟着看日志的命令——PowerShell 和 POSIX shell 不是一套。"""
    if os.name == "nt":
        return f"Get-Content '{log_file}' -Wait -Encoding UTF8"
    return f"tail -f '{log_file}'"


def _spawn_background(settings: Settings, args: argparse.Namespace, log_file: Path) -> int:
    """起一个没有控制台/终端绑定的后台进程（Windows 用 pythonw.exe）。"""
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

    # ★不要「起个进程就完事」★：
    #   Windows：DETACHED_PROCESS 让它没有控制台，也不会被关窗口时的 CTRL 事件杀掉；
    #   POSIX  ：必须 start_new_session（setsid），否则终端一关服务就被 SIGHUP 带走。
    creationflags = 0
    popen_kwargs: dict = {}
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    with open(log_file, "ab") as fh:
        proc = subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=fh,
            stdin=subprocess.DEVNULL,
            cwd=str(settings.root),
            creationflags=creationflags,
            close_fds=True,
            **popen_kwargs,
        )
    write_pid(settings, proc.pid)
    print(
        f"√ 唤醒服务已在后台启动\n"
        f"  PID      : {proc.pid}\n"
        f"  日志     : {log_file}\n"
        f"  唤醒词   : {settings.resolve(settings.wake.file)}\n"
        f"  停止服务 : python main.py stop\n"
        f"  看日志   : {_tail_hint(log_file)}\n"
    )
    return 0


def _pid_from_log(settings: Settings) -> int | None:
    """（已下沉到 service_ctl；这里不再重复实现——见本文件顶部别名）"""
    return service_ctl.pid_from_log(settings)


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
        route = str(stats.extra.get("route") or "—")
        if stats.extra.get("skill"):
            print(f"[本地技能 {stats.extra['skill']} / 路由 {route}"
                  f" / 播报 {stats.total_seconds:.2f}s]")
        else:
            tool = f" / 工具 {stats.extra['tool']}" if stats.extra.get("tool") else ""
            print(f"[路由 {route}{tool} / 首字 {stats.llm_first_token:.2f}s"
                  f" / 首音 {stats.first_audio:.2f}s / 总耗时 {stats.total_seconds:.2f}s]")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        loop.close()
    return 0


def cmd_see(settings: Settings, args: argparse.Namespace) -> int:
    """看图：摄像头 / 屏幕 / 剪贴板 / 文件 —— 走完整链路（技能 → 视觉模型 → 语音）。

    这就是语音那句「看看这是什么」的等价物，用来不开麦先验证一遍：
    拍得到 / 找得到 / 模型答得出来。
    """
    require_models(settings, need_asr=False)
    from voice_loop.vision import Vision, VisionError

    if args.fix:
        # 只做本地部分（拍图/截图/找文件/编码），不调模型
        vision = Vision(settings.vision, settings.root, logging.getLogger("voice_loop"))
        try:
            if args.file:
                hits = vision.find_file(args.file)
                if not hits:
                    print(f"没找到「{args.file}」。（只在 {'、'.join(p.name for p in vision.roots())} 里找）")
                    return 1
                for h in hits:
                    print(f"  {h.score:5.1f}  {vision.describe(h)}   [{h.why}]")
                text, kind = vision.read_file(hits[0].path)
                print(f"\n 类型={kind}  长度={len(text)} 字")
                print(" " + text[:300].replace("\n", "\n "))
                return 0
            shot = vision.screen() if args.screen else vision.camera()
            b64 = vision.encode(shot.path)
            print(f"{shot.what}：{shot.path}")
            print(f"编码后 {len(b64) / 1024:.0f} KB（base64），模型用这张")
            return 0
        except VisionError as exc:
            print(f"[失败] {exc}", file=sys.stderr)
            return 2

    loop = build_loop(settings, preload=False, enable_listening=False)
    loop.tts_enabled = not args.no_tts
    try:
        if args.camera:
            line = "用摄像头看看这是什么"
        elif args.screen:
            line = "看看我的屏幕上是什么"
        elif args.clipboard:
            line = "看看我剪贴板里的东西"
        elif args.file:
            line = f"读一下文件 {args.file}"
        else:
            line = args.text
        if not line:
            print("说点什么：main.py see \"看看这个\"，或者 --camera / --screen / --file", file=sys.stderr)
            return 2
        print(f"你：{line}\n助手：", end="", flush=True)
        stats = loop.respond(line, on_delta=lambda d: print(d, end="", flush=True))
        print()
        # 找文件要先确认：--yes 时自动回答「是」，把两轮串起来
        if stats.extra.get("skill") == "vision_ask" and args.yes:
            print(f"\n你：是\n助手：", end="", flush=True)
            stats = loop.respond("是", on_delta=lambda d: print(d, end="", flush=True))
            print()
        if stats.extra.get("shot"):
            print(f"[看的是 {stats.extra['shot']}]")
        if stats.extra.get("skill"):
            print(f"[本地技能 {stats.extra['skill']} / 播报 {stats.total_seconds:.2f}s]")
        else:
            print(f"[首字 {stats.llm_first_token:.2f}s / 总耗时 {stats.total_seconds:.2f}s]")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        loop.close()
    return 0


def cmd_gpu(settings: Settings, args: argparse.Namespace) -> int:
    """看这台机器有哪些加速器、各阶段现在用的是哪个、以及要不要换。"""
    from voice_loop.accel import (
        IGPU_ENV,
        detect,
        enable_igpu,
        ollama_gpu_state,
        pick_whisper_devices,
        report,
        restart_ollama,
    )

    if args.enable_igpu:
        # Ollama 在 Windows 上默认**丢掉**核显，要 OLLAMA_IGPU_ENABLE=1（官方日志原话）
        print("让 Ollama 用上核显：")
        for line in enable_igpu():
            print(f"  {line}")
        if not args.no_restart:
            for line in restart_ollama():
                print(f"  {line}")
        else:
            print("  （--no-restart：请自己重启 Ollama 让变量生效）")
        print("\n重启后验证：python main.py gpu   然后 ollama ps 里应看到 100% GPU")
        return 0

    print("=" * 66)
    print(" 加速设备")
    print("=" * 66)
    for line in report(settings, logging.getLogger("voice_loop")):
        print("  " + line)

    acc = detect(settings.llm)
    state = ollama_gpu_state()
    print("\n  建议：")
    dev = str(settings.asr.whisper_device)
    plan = pick_whisper_devices(dev)
    if acc.has_intel_gpu and plan and plan[0] != "GPU":
        print("    · Whisper 可以改成 GPU：实测 CPU 3.31s → GPU 0.49s（同一段 5.6s 音频）")
        print('      改 config.toml 里的 [asr] whisper_device = "GPU"（或写 "auto"，它也会选 GPU）')
    elif acc.has_intel_gpu:
        print(f"    · Whisper 已经在用 GPU √（{dev} → {' → '.join(plan)}）")
    if not acc.has_intel_gpu and not acc.has_cuda:
        print("    · 没有可用 GPU：Whisper 只能 CPU（这就是当前最快的选择）")
    if state["dropped_igpu"] and "iGPU" not in state.get("device", ""):
        print(f"    · Ollama 把核显丢掉了（它自己的日志写着要设 {IGPU_ENV}=1）")
        print("      一条命令搞定：python main.py gpu --enable-igpu")
        print("      实测收益：文本首字 2.4s→1.16s，看图 33s→11.3s")
    if not (acc.has_intel_gpu and plan and plan[0] == "GPU"):
        print("    · 纯 CPU 时还能调：[llm] num_thread（线程数）、num_batch（批大小）")

    if not args.bench:
        print("\n  （加 --bench 会真的跑一遍 Whisper CPU/GPU 对比，约 1 分钟）")
        return 0

    print("\n" + "=" * 66)
    print(" Whisper 实测（同一段音频，各跑 3 次取热态）")
    print("=" * 66)
    require_models(settings, need_asr=True, need_tts=False)
    import numpy as np

    from voice_loop.asr.whisper_ov import WhisperOpenVinoEngine
    from voice_loop.audio import load_wav

    wav = settings.resolve("models/asr/sensevoice-small/zh.wav")
    if not wav.exists():
        wav = settings.sessions_dir / "prosody_new_整句合成.wav"
    if not wav.exists():
        print(f"  × 找不到测试音频（{wav}），先跑一次 main.py tts 生成一个")
        return 2
    audio, rate = load_wav(wav, 16000)
    seconds = len(audio) / rate
    print(f"  音频：{wav.name}（{seconds:.1f} 秒）\n")

    from voice_loop.accel import detect as _detect

    for device in (args.devices or pick_whisper_devices(settings.asr.whisper_device)):
        if device not in _detect().openvino and device != "CPU":
            print(f"  [{device}] 不可用，跳过")
            continue
        if str(device).upper() == "NPU":
            # 为什么直接跳过：这份导出是动态形状，NPU 连编译都过不了；
            # 就算改成静态形状，核显也比它快约 8 倍（scripts/probe_npu.py 有实测表）
            print("  [NPU] 已跳过：动态形状编译不过，而且核显比它快 8 倍（实测）")
            continue
        try:
            t0 = time.perf_counter()
            engine = WhisperOpenVinoEngine(
                settings.resolve(settings.asr.whisper_model), device=device, language="zh"
            )
            load_s = time.perf_counter() - t0
            times = []
            for _ in range(3):
                r = engine.transcribe(np.ascontiguousarray(audio))
                times.append(r.latency)
                text = r.text
            warm = min(times[1:]) if len(times) > 1 else times[0]
            print(f"  √ {device:3s} 加载 {load_s:5.1f}s  首次 {times[0]:5.2f}s  热态 {warm:5.2f}s"
                  f"  RTF {warm / seconds:.3f}")
            print(f"        「{text[:56]}」")
            del engine
        except Exception as exc:  # noqa: BLE001
            print(f"  × {device:3s} 失败：{type(exc).__name__}: {exc}")
    return 0


def cmd_skills(settings: Settings, args: argparse.Namespace) -> int:
    """查看/测试生活技能（时间、事件、备忘）。"""
    from voice_loop.event_text import leads_text
    from voice_loop.events import display_title, leads_of, repeat_text
    from voice_loop.skills import Skills

    skills = Skills(settings, logging.getLogger("voice_loop"))

    if args.clear:
        n = {"event": skills.store.clear(), "memo": skills.memos.clear()}
        print(f"已清空：事件 {n['event']} 条 / 备忘 {n['memo']} 条")
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
        ("事件（提醒/闹钟/日程）", skills.store.path),
        ("备忘", skills.memos.path),
        ("唤醒词", settings.resolve(settings.wake.file)),
    ):
        mark = "√" if path.exists() else "×"
        print(f"  {mark} {label}: {path}")

    memos = skills.memos.load()
    if memos:
        print("\n备忘：")
        for i, m in enumerate(memos[:10], 1):
            print(f"  {i}. {m.get('content')}")

    items = skills.store.load()
    if items:
        # ★闹钟和日程现在是同一种东西★，所以只列一张表：下一次发生 + 规则 + 提前提醒
        now = datetime.now()
        rows = sorted(
            ((skills._next_of(it, now), it) for it in items),   # noqa: SLF001
            key=lambda p: (p[0] is None, p[0] or now),
        )
        print(f"\n事件（{len(rows)} 条，下一次 / 规则 / 提前提醒）：")
        for i, (nxt, it) in enumerate(rows, 1):
            where = f"  @{it['location']}" if it.get("location") else ""
            note = f"  备注：{it['note']}" if it.get("note") else ""
            print(
                f"  {i}. {nxt.strftime('%m-%d %H:%M') if nxt else '（已结束）'}"
                f"  {repeat_text(it)}  {display_title(it)}{where}{note}"
                f"\n     提前提醒：{leads_text(leads_of(it))}"
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


def cmd_persona(settings: Settings, args: argparse.Namespace) -> int:
    """看 / 查角色设定（索引 + 独立人格文件）。

    用法：
        python main.py persona                    列出所有**已挂上**的角色（含唤醒词与文件）
        python main.py persona --show 阿米娅        看这一个角色的字段
        python main.py persona --show 阿米娅 --prompt   看她拼出来的 system prompt
        python main.py persona --voices            看每个角色的声线装没装
        python main.py persona --split             把旧版内联的角色拆成独立人格文件
    """
    from voice_loop.persona import PERSONA_DIR, CharacterRegistry, render_system_prompt

    path = settings.resolve(settings.persona.file)
    reg = CharacterRegistry(path)
    extra = str(settings.persona.extra_prompt or "")

    if args.split:
        created = reg.split_inline()
        if not created:
            print(f"  {path} 里没有需要拆的角色（已经是「索引 + 人格文件」了）")
            return 0
        print(f"  已拆出 {len(created)} 个人格文件：")
        for f in created:
            print(f"    · {f}")
        print(f"  索引已改成指向它们：{path}")
        print(f"  想再挂一个：把人设放进 {path.parent / PERSONA_DIR}，"
              "再在索引的 characters 里加一行")
        return 0

    print("=" * 72)
    print(f" 角色索引：{path}")
    print("=" * 72)
    if not reg.characters:
        print("  （索引里没有可用角色；删掉索引文件会用默认模板重建）")
        return 0

    if args.voices:
        tts_dir = settings.resolve("models/tts/piper")
        have = sorted(p.stem for p in tts_dir.glob("*.onnx")) if tts_dir.exists() else []
        print(f"  已装的 piper 声线：{'、'.join(have) or '（一个都没有）'}")
        print(f"  全局默认（[tts] voice）：{settings.tts.voice}")
        for char in reg.all():
            want = char.voice or ""
            mark = "✓" if (want in have or not want) else "×"
            print(f"   {mark} {char.name:8s} voice={want or '（用全局）'}")
        return 0

    if args.show:
        char = reg.get(args.show)
        if char is None:
            print(f"  [错误] 没有这个角色：{args.show}")
            print(f"  有的：{'、'.join(c.name for c in reg.all())}")
            return 2
        if args.prompt:
            print(render_system_prompt(char, extra))
            return 0
        print(f"  id          : {char.id}")
        print(f"  名字        : {char.name}")
        print(f"  人格文件    : {reg.files.get(char.id) or '（写在索引里）'}")
        print(f"  身份        : {char.title or '（空）'}")
        print(f"  对「我」称呼 : {char.user_title}")
        print(f"  唤醒词      : {'、'.join(char.wake_words) or '（没写，只能在会话里用）'}")
        print(f"  应答语      : {char.ack or '（不出声）'}")
        print(f"  声线/温度   : {char.voice or '（用全局）'} / {char.temperature or '（用全局）'}")
        print(f"  默认角色    : {'是' if char.default else '否'}   启用：{'是' if char.enabled else '否'}")
        print(f"  背景        : {char.background or '（空）'}")
        print(f"  别名        : {sum(len(v) for v in char.aliases.values())} 条")
        for label, items in (("说话风格", char.style), ("必须做到", char.rules),
                             ("不要出现", char.avoid)):
            print(f"  {label}    :")
            for it in items or ["（空）"]:
                print(f"      · {it}")
        print("  示例台词    :")
        for line in char.lines or [{"scene": "（空）", "text": ""}]:
            print(f"      · {line['scene']} → {line['text']}")
        if char.notes:
            print(f"  备注        : {char.notes}")
        print("\n  想看拼出来的 system prompt："
              f"python main.py persona --show {char.id} --prompt")
        return 0

    print(f"  {reg.stats()}")
    default = reg.default()
    for char in reg.all():
        flags = []
        if default is not None and char.id == default.id:
            flags.append("默认")
        if not char.enabled:
            flags.append("已停用")
        mark = f"  [{('、'.join(flags))}]" if flags else ""
        words = "、".join(char.wake_words) or "（无唤醒词）"
        print(f"\n  · {char.label}{mark}")
        print(f"      对「我」的称呼：{char.user_title}   应答：{char.ack or '（不出声）'}")
        print(f"      唤醒词：{words}")
        print(f"      别名：{sum(len(v) for v in char.aliases.values())} 条    "
              f"风格 {len(char.style)} 条 / 示例 {len(char.lines)} 条")
        f = reg.files.get(char.id)
        print(f"      人格文件：{f if f else '（写在索引里，建议 --split 拆出去）'}")
    if reg.orphans:
        print(f"\n  库里还有 {len(reg.orphans)} 个人格文件没挂上（不会被唤醒）：")
        for f in reg.orphans:
            print(f"      · {f}")
        print(f"  想让她上线：在 {path.name} 的 characters 里加一行"
              f"（{{\"id\": \"{reg.orphans[0].stem}\", \"file\": \"{PERSONA_DIR}/"
              f"{reg.orphans[0].name}\"}}）")
    print("\n  提示：唤醒词写在各角色的 wake_words 里，喊谁就切谁；"
          "改完保存即生效，不用重启。")
    return 0


def cmd_mcp(settings: Settings, args: argparse.Namespace) -> int:
    """自己的 MCP 工具层：看 / 调 / 挂起来给别人用。

    三种用法：
        python main.py mcp                         看工具清单（名字、来自哪个服务器、耗时）
        python main.py mcp --call list_schedule --args '{"text": "下周"}'
        python main.py mcp --serve skills           挂到 stdio，给 Copilot / Claude Code 用
    """
    if args.serve:
        # 直接复用同一份实现，不另写一套：serve 模块就是 stdio 入口
        from voice_loop.mcp.serve import main as serve_main

        sys.argv = ["mcp-serve", args.serve]
        return serve_main()

    import json
    import logging

    from voice_loop.mcp import MCPHost

    log = logging.getLogger("voice_loop")
    skills = None
    if settings.skills.enabled:
        from voice_loop.skills import Skills

        skills = Skills(settings, log)
    host = MCPHost(
        settings.mcp, log, deps={"settings": settings, "skills": skills, "logger": log}
    )
    try:
        t0 = time.perf_counter()
        specs = host.specs()
        cost = time.perf_counter() - t0
        print("=" * 72)
        print(" 自己的 MCP 工具层")
        print("=" * 72)
        print(f"  状态：{host.status()}")
        print(f"  启动耗时：{cost * 1000:.0f} ms（inproc；stdio 服务器会慢一些）")
        if not specs:
            print("\n  （一个工具都没有：检查 config.toml 的 [[mcp.servers]]）")
            return 0
        print(f"\n  模型能看到的 {len(specs)} 个工具：")
        for info in host.describe():
            desc = str(info["description"] or "").replace("\n", " ")
            where = info["server"] + ("." + info["tool"] if info["namespaced"] else "")
            print(f"    · {info['name']:24s} [{where}] {desc[:58]}")

        if args.call:
            raw = args.args or "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                print(f"\n[错误] --args 不是合法 JSON：{raw}")
                return 2
            print(f"\n  调用 {args.call} {payload}")
            t0 = time.perf_counter()
            ok, text = host.call(args.call, payload)
            print(f"  {'√' if ok else '×'} {time.perf_counter() - t0:.2f}s  {text}")
            return 0 if ok else 1
        return 0
    finally:
        host.close()


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
    backend = (settings.tts.backend or "piper").strip().lower()
    clone_ref = str(getattr(settings.tts, "clone_audio", "") or "").strip()
    print(f"\n[4] 语音合成（{'音色克隆' if backend == 'zipvoice' else 'Piper'}）")
    try:
        require_models(settings, need_asr=False)
        from voice_loop.tts import create_tts

        tts = create_tts(settings)
        info = tts.benchmark("你好，我是本地部署的中文语音助手。")
        if backend == "zipvoice":
            who = f"zipvoice · 参考 {Path(clone_ref).name}" if clone_ref else "zipvoice · 没设参考音频（会退到 Piper）"
        else:
            who = settings.tts.voice
        print(
            f"  √ {who}  采样率 {info['sample_rate']} Hz  "
            f"合成 {info['audio_seconds']:.2f}s 用时 {info['synth_seconds']:.2f}s  RTF {info['rtf']:.2f}"
        )
        if backend == "zipvoice":
            # 对话体感看的是「首段出声」，不是总 RTF
            first = float(info.get("first_chunk_seconds") or 0.0)
            steps = int(getattr(settings.tts, "clone_steps", 4) or 4)
            print(f"    步数 {steps}  首段出声 {first:.2f}s（Piper 是 0.13s）")
            from voice_loop.tts.pacing import PacingFixer

            print(f"    {PacingFixer.from_config(settings.tts).describe()}")
            print('    · 克隆音色是「说多久、等多久」；想更快就把 [tts] backend 改回 "piper"')
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
            print(f"   · 没有示例音频，已用当前 TTS（{settings.tts.backend}）合成 {sample.name}")

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

    # 8. 看图（本地部分：截图 / 找文件 / 编码 / 视觉模型是否装好）
    print("\n[8] 看图（摄像头 / 屏幕 / 文件）")
    try:
        from voice_loop.vision import Vision, VisionError

        if not settings.vision.enabled:
            print("  ! [vision] enabled = false，看图功能已关闭")
        else:
            vision = Vision(settings.vision, settings.root, logging.getLogger("voice_loop"))
            print(f"   看图模型: {settings.vision.model}")
            try:
                have = llm.list_models()
                if any(m.split(":")[0] == settings.vision.model.split(":")[0] for m in have):
                    print(f"  √ 视觉模型已安装（{settings.vision.model}）")
                else:
                    print(f"  ! 视觉模型没装：看图前先跑 ollama pull {settings.vision.model}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 查不了模型列表：{exc}")

            try:
                shot = vision.screen()
                b64 = vision.encode(shot.path)
                print(f"  √ 截屏 + 编码：{shot.path.name}  base64 {len(b64) / 1024:.0f} KB")
            except VisionError as exc:
                print(f"  ! 截屏不可用：{exc}")

            try:
                vision.camera()
                print("  √ 摄像头可用")
            except VisionError as exc:
                print(f"  ! 摄像头不可用：{exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 摄像头异常：{exc}")

            roots = "、".join(f"{p.name}({p})" for p in vision.roots()) or "（未配置）"
            print(f"   找文件的根目录：{roots}")
            for q in ("读一下里面的报告", "看看桌面上的数据文件"):
                hits = vision.find_file(q) if vision.roots() else []
                first = vision.describe(hits[0]) if hits else "（没找到）"
                print(f"    · 「{q}」 -> {first}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  × {exc}")

    print("\n[9] 加速设备")
    try:
        from voice_loop.accel import detect, report

        for line in report(settings, logging.getLogger("voice_loop")):
            print("  " + line)
        if detect(settings.llm).has_intel_gpu:
            print("  · 核显可用：想知道实际快多少，跑 python main.py gpu --bench")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 加速设备探测失败：{exc}")

    # 10. MCP 工具层（自己的协议层：能力域拆成服务器，宿主聚合与路由）
    print("\n[10] MCP 工具层")
    try:
        from voice_loop.mcp import MCPHost
        from voice_loop.skills import Skills

        skills = Skills(settings, logging.getLogger("voice_loop")) if settings.skills.enabled else None
        host = MCPHost(
            settings.mcp,
            logging.getLogger("voice_loop"),
            deps={"settings": settings, "skills": skills,
                  "logger": logging.getLogger("voice_loop")},
        )
        try:
            t0 = time.perf_counter()
            specs = host.specs()
            cost = (time.perf_counter() - t0) * 1000
            print(f"   状态：{host.status()}")
            print(f"   启动：{cost:.0f} ms（inproc 很快；stdio 要拉起子进程）")
            if not specs:
                ok = False
                print("  × 一个工具都没露出来：检查 config.toml 的 [[mcp.servers]]")
            else:
                for info in host.describe():
                    where = info["server"] + ("." + info["tool"] if info["namespaced"] else "")
                    print(f"    · {info['name']:22s} [{where}]")
                # 只读那一个：确认协议层真的能把调用转到底下的技能
                ok_call, text = host.call("list_memos", {})
                print(f"  {'√' if ok_call else '×'} 经 MCP 调 list_memos：{text[:40]}")
                if not ok_call:
                    ok = False
        finally:
            host.close()
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
def cmd_memory(settings: Settings, args: argparse.Namespace) -> int:
    """模型记忆：看 / 检索 / 巩固 / 清理。

    ★记忆按角色隔离★（`--who` 指定）；日程是所有角色共享的，不在这里改 ——
    要改日程用「技能」那套（`main.py skills` 或直接跟助手说）。
    """
    from voice_loop.memory import MemoryHub
    from voice_loop.memory.schedule import SharedSchedule

    if not getattr(settings, "memory", None) or not settings.memory.enabled:
        print("记忆功能关着（config.toml 的 [memory] enabled = false）—— 打开它才能用。")
        return 1

    # ★角色级知识库也要接上★：不接的话命令行看不到「世界观组 / 专属资料」那两层
    # （共享那层能看，因为它跟角色无关）—— 助手那边由 pipeline 注入，这里得自己接。
    def _knowledge_spec(char_id: str) -> dict:
        try:
            from voice_loop.persona import CharacterRegistry, knowledge_spec_for

            registry = CharacterRegistry(settings.resolve(settings.persona.file))
            return knowledge_spec_for(registry, char_id)
        except Exception:  # noqa: BLE001 - 角色文件坏了就当「只有共享库」
            return {"paths": [], "worlds": [], "world": "", "title": "", "shared": True}

    hub = MemoryHub(settings, schedule=SharedSchedule(settings),
                    character_knowledge=_knowledge_spec)
    # ★默认角色必须跟助手一致★：它用的是 characters.json 索引里的 default，
    # 只看 `[persona] default`（通常是空的）会落到 "default" 目录 →
    # 命令行记的东西和助手记的东西分家（实打实踩过）。
    who = args.who or settings.persona.default
    if not who:
        try:
            from voice_loop.persona import CharacterRegistry

            char = CharacterRegistry(settings.resolve(settings.persona.file)).default()
            who = char.id if char else "default"
        except Exception:  # noqa: BLE001 - 角色文件坏了就退回 default，不要挡住记忆功能
            who = "default"
    mem = hub.for_character(who)
    acted = False

    if args.remember:
        episode, facts = mem.remember(args.remember)
        print(f"记下了：{episode.title}")
        for fact in facts:
            print(f"  · 事实 {fact.key} = {fact.value}")
        acted = True
    if args.recall is not None or args.when:
        if args.all:
            # ★跨角色查全部★：命令行是机主本人，不需要人格权限（那套是拦「角色」的）
            names = hub.characters()
            try:
                from voice_loop.persona import CharacterRegistry

                registry = CharacterRegistry(settings.resolve(settings.persona.file))
                names = sorted(set(names) | {c.id for c in registry.all(only_enabled=True) if c.id})
            except Exception:  # noqa: BLE001 - 角色文件坏了就只查有记忆目录的
                pass
            pairs = hub.recall_everywhere(args.recall or "", when=args.when, limit=args.limit,
                                          characters=names)
            print(f"检索「{args.recall or ''}」" + (f"（{args.when}）" if args.when else "")
                  + f"：{len(pairs)} 条（查了 {len(names)} 个角色：{'、'.join(names)}）")
            for cid, hit in pairs:
                print(f"  [{cid}] [{hit.kind}] {hit.score:.2f}  {hit.text[:80]}")
        else:
            hits = mem.recall(args.recall or "", when=args.when, limit=args.limit)
            print(f"检索「{args.recall or ''}」" + (f"（{args.when}）" if args.when else "")
                  + f"：{len(hits)} 条（来自 {who}）")
            for hit in hits:
                print(f"  [{hit.kind}] {hit.score:.2f}  {hit.text[:90]}")
        acted = True
    if args.knowledge:
        chunks = mem.knowledge.search(args.knowledge, limit=args.limit)
        print(f"知识库命中 {len(chunks)} 段：")
        for chunk in chunks:
            # ★带归属★：全知角色（白泽）的库里会有别人的世界观，得看得出“这是谁的”
            tag = f"·{chunk.owner}" if getattr(chunk, "owner", "") else ""
            print(f"  [资料{tag}] {chunk.source} {chunk.title}：{chunk.text[:80]}")
        acted = True
    if args.forget:
        print("忘掉了。" if mem.forget(args.forget) else "没找到这条（用 --recall 里的 id / key）。")
        acted = True
    if args.consolidate:
        print("巩固（自清洁）：", mem.consolidate())
        acted = True
    if args.prune:
        victims = mem.prune_raw_sessions(settings.sessions_dir, dry_run=not args.apply)
        what = "删掉" if args.apply else "可以删（加 --apply 才真删）"
        print(f"滑动窗口：{what} {len(victims)} 个原始对话" + (f"：{victims[:5]}" if victims else ""))
        acted = True

    if not acted:
        print("=" * 62)
        print(f" 记忆：{who}")
        print("=" * 62)
        for key, value in mem.stats().items():
            print(f"  {key:<18}{value}")
        print("\n用法：--recall \"组会几点\" [--when 上周] / --knowledge 世界观 / --remember \"…\"")
        print("      --consolidate（自清洁） / --prune [--apply]（清理原始对话） / --forget <id|key>")
    return 0


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
    ap.add_argument("--character", default=None,
                    help="临时指定角色（id 或名字，见 python main.py persona）")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("chat", help="语音对话")
    p.add_argument("--mode", choices=["vad", "ptt"], default=None)
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--device", default=None, help="Whisper 设备：CPU / GPU / AUTO")
    p.add_argument("--llm", default=None, help="覆盖 Ollama 模型名")
    p.add_argument("--character", default=None, help="用哪个角色（默认用角色文件里的默认角色）")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("listen", help="唤醒词服务（后台跑闹钟/日程提醒）")
    p.add_argument("--wake-file", default=None, help="唤醒词 json 路径（默认取 config.toml）")
    p.add_argument("-B", "--background", action="store_true", help="转到后台运行（无窗口）")
    p.add_argument("--stop-first", action="store_true", help="启动前先停掉已在运行的服务")
    p.add_argument("--mode", default=None)
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--device", default=None)
    p.add_argument("--llm", default=None)
    p.add_argument("--character", default=None, help="默认角色（唤醒词喊了别人仍然会切过去）")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("stop", help="停止后台运行的唤醒服务")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("text", help="打字调试（完整技能 + LLM + 语音播报）")
    p.add_argument("--no-tts", action="store_true", help="只输出文字")
    p.add_argument("--strategy", choices=["sensevoice", "whisper", "hybrid"], default=None)
    p.add_argument("--llm", default=None)
    p.add_argument("--character", default=None, help="用哪个角色")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("skills", help="查看/测试生活技能（时间、闹钟、备忘、日程）")
    p.add_argument("text", nargs="*", help="要测试的句子，可给多条")
    p.add_argument("--clear", action="store_true", help="清空所有提醒/备忘/日程")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("ask", help="纯文本提问 + 语音播报")
    p.add_argument("text", help="要提问的内容")
    p.add_argument("--no-tts", action="store_true", help="只输出文字不合成语音")
    p.add_argument("--llm", default=None)
    p.add_argument("--character", default=None, help="用哪个角色")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("see", help="看图：摄像头 / 屏幕 / 剪贴板 / 文件")
    p.add_argument("text", nargs="?", default=None, help="想让它看什么，例如「看看这是什么」")
    p.add_argument("--camera", action="store_true", help="用摄像头拍一张")
    p.add_argument("--screen", action="store_true", help="截屏")
    p.add_argument("--clipboard", action="store_true", help="看剪贴板里的图")
    p.add_argument("--file", default=None, help="读文件（支持模糊名字，会先确认）")
    p.add_argument("--yes", action="store_true", help="需要确认时自动答「是」")
    p.add_argument("--fix", action="store_true", help="只做本地部分（拍图/找文件/编码），不调模型")
    p.add_argument("--no-tts", action="store_true", help="只输出文字不合成语音")
    p.set_defaults(func=cmd_see)

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

    p = sub.add_parser("gpu", help="看加速器（Intel 核显 / NPU / CUDA）在用哪个，可实测")
    p.add_argument("--bench", action="store_true", help="真的跑一遍 Whisper 设备对比（约 1 分钟）")
    p.add_argument("--devices", nargs="*", default=None, help="只跑这几个设备，例如 --devices GPU CPU")
    p.add_argument("--enable-igpu", action="store_true",
                   help="让 Ollama 用上核显（写用户环境变量 OLLAMA_IGPU_ENABLE=1 并重启 Ollama）")
    p.add_argument("--no-restart", action="store_true", help="配合 --enable-igpu：只写变量，不重启")
    p.set_defaults(func=cmd_gpu)

    p = sub.add_parser("selftest", help="全链路自检")
    p.add_argument("--no-play", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("persona", help="看角色设定（名字/背景/称呼/示例台词）")
    p.add_argument("--show", default=None, help="看某一个角色（id 或名字）")
    p.add_argument("--prompt", action="store_true", help="配合 --show：打印拼出来的 system prompt")
    p.add_argument("--voices", action="store_true", help="看每个角色的声线装没装")
    p.add_argument("--split", action="store_true",
                   help="把索引里内联的角色拆成独立人格文件（旧版文件迁移用）")
    p.set_defaults(func=cmd_persona)

    p = sub.add_parser("mcp", help="自己的 MCP 工具层：看 / 调 / 挂到 stdio 给别人用")
    p.add_argument("--call", default=None, help="调一个工具，例如 --call list_schedule")
    p.add_argument("--args", default=None, help="配合 --call：JSON 参数，例如 '{\"text\": \"下周\"}'")
    p.add_argument("--serve", default=None, metavar="服务器",
                   help="把某个服务器挂到 stdio（skills / vision…），给 Copilot / Claude Code 用")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ui", help="可视化控制台（网页：日程/闹钟、备忘、日志、声线、对话）")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址，默认只给本机；填 0.0.0.0 会让同局域网的人也能进来（能改你的日程，慎用）")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不要自动开浏览器")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("memory", help="模型记忆：看 / 检索 / 巩固 / 清理（按角色隔离）")
    p.add_argument("--who", default=None, help="哪个角色的记忆（默认用默认角色）")
    p.add_argument("--all", action="store_true",
                   help="★跨角色检索★：查所有人的记忆并标出每条是谁的（配 --recall/--when）")
    p.add_argument("--recall", default=None, help="按内容检索，例如 --recall \"组会\"")
    p.add_argument("--when", default=None, help="配合 --recall：中文时间，如 上周 / 最近三天")
    p.add_argument("--knowledge", default=None, help="只搜知识库（世界观/资料）")
    p.add_argument("--limit", type=int, default=8, help="最多返回几条")
    p.add_argument("--remember", default=None, help="手动记一条（走同一套信号/事实抽取）")
    p.add_argument("--forget", default=None, help="忘掉一条（episode id 或 fact key）")
    p.add_argument("--consolidate", action="store_true", help="手动做一次自清洁（合并/压缩/淘汰/L2→L3）")
    p.add_argument("--prune", action="store_true", help="滑动窗口：列出可清理的原始对话")
    p.add_argument("--apply", action="store_true", help="配合 --prune：真的删")
    p.set_defaults(func=cmd_memory)

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
