"""给一个角色训出专属声线：盘点 → 数据 → 训练 → 导出 → 安装 → 校验。

这是「配置了音频数据和对应文本的角色」的**标准流程**，一条命令跑完：

    python scripts/persona_voice.py --list                  # 先看谁够条件
    python scripts/persona_voice.py --persona kaltsit       # 一条龙
    python scripts/persona_voice.py --persona kaltsit --stage 3 --stop-stage 5
    python scripts/persona_voice.py --persona kaltsit --verify      # 只校验装好的模型

它背后用的是官方 ZipVoice 微调配方（见 scripts/finetune_zipvoice.py 里的对照表），
角色相关的东西都按约定推出来，不用记一堆路径：

    data/personas/<id>.json          人格文件（读 voice_dir / voice_ref）
    data/personas/<id>/*.wav         素材音频（约定目录，或用 persona 的 voice_dir 覆盖）
    data/personas/<id>/<id>.txt      素材文本清单（名字一行 + 正文一行）
        ↓
    data/finetune/<id>/              24kHz 数据集 + TSV（可 gitignore）
        ↓
    models/tts/zipvoice/personas/<id>/   训好的角色模型（可以直接给 persona 用）

跑完会打印一行 ``voice_model``，写进人格文件就生效（或在索引里临时覆盖）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.persona import CharacterRegistry  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.voice_data import (  # noqa: E402
    MODEL_SUBDIR,
    VoiceData,
    inspect_all,
    render_report,
)

TRAINER = ROOT / "scripts" / "finetune_zipvoice.py"


def persona_paths(item: VoiceData) -> dict[str, str]:
    """按约定给出一条龙要用的所有路径。"""
    return {
        "audio_dir": str(item.audio_dir),
        "data_dir": str(ROOT / "data" / "finetune" / item.id),
        "prefix": item.id,
        "exp_dir": f"exp/{item.id}",
        "onnx_dir": f"exp/{item.id}_onnx",
        "install_dir": str(item.model_dir or (ROOT / MODEL_SUBDIR / item.id)),
    }


def find_persona(registry: CharacterRegistry, key: str) -> tuple[VoiceData | None, str]:
    char = registry.get(key)
    if char is None:
        return None, f"索引里没有这个角色：{key}"
    path = (registry.files or {}).get(char.id)
    if not path:
        return None, f"角色 {char.name} 是内联写在索引里的，没有独立人格文件（先 python main.py persona --split）"
    from voice_loop.voice_data import inspect

    item = inspect(
        Path(path),
        char.id,
        char.name,
        reference=str(getattr(char, "voice_ref", "") or ""),
        root=ROOT,
        voice_dir=str(getattr(char, "voice_dir", "") or ""),
    )
    if not item.has_audio:
        return item, f"没找到素材音频目录：{item.audio_dir}"
    if not item.has_text:
        return item, (
            f"{len(item.without_text)}/{len(item.clips)} 条音频没有对应文本"
            f"（文本清单放 {item.audio_dir}\\{char.id}.txt：名字一行 + 正文一行）"
        )
    if not item.enough:
        return item, f"素材偏少（{item.seconds:.0f} 秒 / {len(item.clips)} 条），微调容易过拟合"
    return item, ""


def run_trainer(args: list[str]) -> int:
    cmd = [sys.executable, str(TRAINER), *args]
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def do_list() -> int:
    settings = load_settings()
    registry = CharacterRegistry(settings.resolve(settings.persona.file))
    registry.load()
    items = inspect_all(registry, ROOT)
    print(render_report(items, ROOT))
    return 0


def do_train(item: VoiceData, args) -> int:
    paths = persona_paths(item)
    trainer_args: list[str] = [
        "--audio-dir", paths["audio_dir"],
        "--data-dir", paths["data_dir"],
        "--prefix", paths["prefix"],
        "--exp-dir", paths["exp_dir"],
        "--onnx-dir", paths["onnx_dir"],
        "--install-dir", paths["install_dir"],
        "--stage", str(args.stage),
        "--stop-stage", str(args.stop_stage),
        "--threads", str(args.threads),
    ]
    if args.epochs:
        # 按「目标 epoch 数」自动推迭代数（比拍一个 iter 数靠谱）
        per_epoch = max(1.0, item.seconds / float(args.max_duration))
        iters = max(20, int(round(args.epochs * per_epoch)))
        print(
            f"素材 {item.seconds:.0f} 秒，每批最多 {args.max_duration} 秒 → "
            f"1 epoch ≈ {per_epoch:.1f} 批；目标 {args.epochs} epoch → {iters} iter"
        )
        trainer_args += ["--iters", str(iters)]
    elif args.iters:
        trainer_args += ["--iters", str(args.iters)]
    if args.max_duration:
        trainer_args += ["--max-duration", str(args.max_duration)]
    if args.force:
        trainer_args.append("--force")
    if args.dry_run:
        print("（--dry-run：不执行，只显示会跑什么）")
        print(f"$ python scripts/finetune_zipvoice.py {' '.join(trainer_args)}")
        return 0
    code = run_trainer(trainer_args)
    if code != 0:
        return code
    if args.stop_stage >= 8 and not args.no_wire:
        print_usage_snippet(item)
        return wire_persona(item, dry_run=False)
    if args.stop_stage >= 8:
        print_usage_snippet(item)
    return 0


def wire_persona(item: VoiceData, dry_run: bool = False) -> int:
    """把 ``voice_model`` 写进人格文件——不写这一行，训好的模型永远不会被用上。

    ★为什么用文本手术而不是 json 重写★：人格文件是手写的，json.dump 会把格式、
    键序、中文全打乱（还会把 ``_说明`` 之类的注释字段挤到一起）。这里只动
    ``"voice_model"`` 那一行，改完还会 `json.loads` 验一遍，坏了就回滚。
    幂等：已经是目标值就不动；改前存 ``.bak``。
    """
    rel = f"{MODEL_SUBDIR}/{item.id}"
    path = item.persona_json
    if path is None or not path.exists():
        print(f"\n× 找不到人格文件（{path}）——手动加一行：\"voice_model\": \"{rel}\"")
        return 1

    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'("voice_model"\s*:\s*)"[^"]*"')
    match = pattern.search(text)
    if match and match.group(0).endswith(f'"{rel}"'):
        print(f"\n人脸文件里 voice_model 已经是 {rel}，不用改")
        return 0

    if match:
        new_text = pattern.sub(lambda m: f'{m.group(1)}"{rel}"', text, count=1)
        action = "更新"
    else:
        anchor = re.search(r'^(\s*)"voice_ref"\s*:[^\n]*\n', text, re.M)
        if anchor:
            indent = anchor.group(1)
            insert_at = anchor.end()
            new_text = text[:insert_at] + f'{indent}"voice_model": "{rel}",\n' + text[insert_at:]
        else:
            brace = text.index("{") + 1
            new_text = text[:brace] + f'\n  "voice_model": "{rel}",' + text[brace:]
        action = "新增"

    try:
        json.loads(new_text)
    except json.JSONDecodeError as exc:
        print(f"\n× 改完不是合法 JSON（{exc}），已放弃修改")
        return 1
    if dry_run:
        print(f"\n（--dry-run：会给 {path.name} {action} voice_model = {rel}）")
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
    print(f"\n√ 已{action} {path.name} 的 voice_model = {rel}（旧文件备份 {backup.name}）")
    print("  重载即生效：助手改人格文件会被监听到，不用重启。")
    return 0


def print_usage_snippet(item: VoiceData) -> None:
    rel = f"{MODEL_SUBDIR}/{item.id}"
    path = item.persona_json or (item.audio_dir.parent / f"{item.id}.json")
    print("\n" + "=" * 78)
    print("让这个角色用上她自己的声线")
    print("=" * 78)
    print(f"要写进 {path} 的那一行：")
    print(f'    "voice_model": "{rel}",')
    print("（跑完整流程时会自动写；不想自动写就加 --no-wire）")
    print(f"\n提醒：这个模型只适合 {item.name}（单说话人微调会把音色收敛到她一个人），")
    print("其他角色别配它，否则也会变成她的声音。这也正是「按角色配模型」的意义：")
    print("每个角色各用各的 voice_model，没配的继续用出厂零样本模型。")
    print("另外非蒸馏模型比出厂蒸馏版慢 2~3 倍（实测 RTF 2.2 vs 0.7），换音色是要付时间代价的。")


def do_verify(item: VoiceData, args) -> int:
    """用助手自己的解释器加载装好的模型试一句——换配置前先确认它是好的。"""
    model_dir = item.model_dir
    if not model_dir or not item.trained:
        print(f"模型没装好：{model_dir}")
        return 1
    ref = str(getattr(args, "reference", "") or item.reference or "")
    if not ref:
        print("这个角色没配 voice_ref，无法试听（参考音频是克隆模型的必需品）")
        return 1
    script = f'''
import sys
sys.path.insert(0, r"{ROOT}")
from voice_loop.settings import load_settings
from voice_loop.tts.zipvoice_tts import ZipVoiceTts

settings = load_settings()
settings.tts.backend = "zipvoice"
settings.tts.clone_dir = r"{model_dir}"
settings.tts.clone_audio = r"{ref}"
engine = ZipVoiceTts(settings)
print("模型目录 :", engine._clone_dir)
print("参考音频 :", engine.reference)
info = engine.benchmark("我在，博士。今天的巡检报告我放在桌上了。")
for key in ("sample_rate", "audio_seconds", "synth_seconds", "rtf", "reference"):
    print(f"{{key:15s}}: {{info.get(key)}}")
'''
    cmd = [args.assistant_python, "-c", script]
    print(f"$ {args.assistant_python} -c <试听脚本>")
    return subprocess.run(cmd, cwd=str(ROOT), env={**dict(__import__("os").environ), "PYTHONUTF8": "1"}).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="给角色训练专属声线（标准化流程）")
    ap.add_argument("--list", action="store_true", help="盘点所有角色的素材与模型状态")
    ap.add_argument("--persona", default="", help="角色 id 或名字（如 kaltsit / 凯尔希）")
    ap.add_argument("--stage", type=int, default=1, help="从第几步开始（1 数据 … 8 安装）")
    ap.add_argument("--stop-stage", type=int, default=8, help="跑到第几步")
    ap.add_argument("--epochs", type=float, default=20.0, help="目标 epoch 数（自动换算 iter）")
    ap.add_argument("--iters", type=int, default=0, help="直接指定 iter 数（覆盖 --epochs）")
    ap.add_argument("--max-duration", type=int, default=60, help="一个 batch 的总时长（秒）")
    ap.add_argument("--threads", type=int, default=0, help="训练线程数（0 = 全部逻辑核）")
    ap.add_argument("--force", action="store_true", help="安装时覆盖已有文件")
    ap.add_argument("--no-wire", action="store_true", help="跑完不自动写 voice_model（只打印提示）")
    ap.add_argument("--dry-run", action="store_true", help="只打印会执行什么")
    ap.add_argument("--verify", action="store_true", help="只校验装好的模型（试听一句）")
    ap.add_argument("--reference", default="", help="校验时用的参考音频（默认用角色的 voice_ref）")
    ap.add_argument(
        "--assistant-python",
        default=sys.executable,
        help="跑校验用的解释器（要装了 sherpa-onnx 的那个，通常是助手环境的 python）",
    )
    args = ap.parse_args()

    if args.list or not args.persona:
        return do_list()

    settings = load_settings()
    registry = CharacterRegistry(settings.resolve(settings.persona.file))
    registry.load()
    item, why = find_persona(registry, args.persona)
    if item is None:
        print(f"× {why}")
        return 1
    if args.verify:
        return do_verify(item, args)
    if why:
        print(f"× {item.name}：{why}")
        print("（盘点用 --list；补好素材再来）")
        return 1

    print(f"角色：{item.name}（{item.id}）")
    print(f"素材：{item.audio_dir}  {len(item.clips)} 条 / {item.seconds:.1f} 秒 / 文本覆盖 {item.text_coverage:.0%}")
    print(f"模型将装到：{item.model_dir}")
    return do_train(item, args)


if __name__ == "__main__":
    sys.exit(main())
