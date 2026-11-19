"""把人格文件里的旧声线字段搬成新的 `"tts": {...}`（一次性迁移工具）。

旧的写法是 8 个平铺字段（`backend` / `voice` / `voice_ref` / `voice_ref_text` /
`voice_refs` / `voice_model` / `tts_voice`），新写法只有一份结构化描述
（见 `voice_loop/tts/spec.py::VoiceSpec`）。映射规则：

| 旧 | 新 |
| --- | --- |
| `backend: "piper"` + `voice: "x"` | `tts.provider="piper"`, `tts.voice_id="x"` |
| `backend: "zipvoice"` + `voice_ref/voice_ref_text` | `tts.ref_audio/ref_text`（+ `styles` ← `voice_refs`） |
| `backend: "zipvoice"` + `voice_model` | `tts.model` |
| `backend: "api"` + `tts_voice` | `tts.provider="deepseek"`, `tts.voice_id="stella"` |
| `voice`（piper 声线，但 provider 不是 piper） | `tts.fallback=["piper:那个声线"]` |

用法：

    python scripts/migrate_tts_format.py --dry-run     # 只报告
    python scripts/migrate_tts_format.py              # 真写（留 .bak-tts-<时间>）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEGACY = ("backend", "voice", "voice_ref", "voice_ref_text", "voice_refs",
          "voice_model", "tts_voice")
PROVIDER_MAP = {"api": "deepseek", "": "", "piper": "piper", "zipvoice": "zipvoice"}


def migrate(raw: dict) -> tuple[dict, bool]:
    """返回 ``(新的人格 json, 有没有改动)``。已经是新格式就原样返回。"""
    if not any(key in raw for key in LEGACY):
        return raw, False
    data = dict(raw)
    provider = PROVIDER_MAP.get(str(data.pop("backend", "") or "").strip().lower())
    if provider is None:                       # 不认识的旧后端名：当成「跟全局」，但留个记号
        provider = ""
    voice = str(data.pop("voice", "") or "").strip()
    ref = str(data.pop("voice_ref", "") or "").strip()
    ref_text = str(data.pop("voice_ref_text", "") or "").strip()
    styles = data.pop("voice_refs", None)
    model = str(data.pop("voice_model", "") or "").strip()
    api_voice = str(data.pop("tts_voice", "") or "").strip().lower()

    tts: dict = {}
    if provider:
        tts["provider"] = provider
    # 参考音频/模型/分风格参考是「数据」，不管 provider 写没写都保留 ——
    # 没写 provider 意味着「跟全局」，而全局可能正是克隆后端（amiya 就是这种）。
    if model:
        tts["model"] = model
    if ref:
        tts["ref_audio"] = ref
    if ref_text:
        tts["ref_text"] = ref_text
    if isinstance(styles, dict) and styles:
        tts["styles"] = styles
    if provider == "piper":
        if voice:
            tts["voice_id"] = voice
    elif provider == "deepseek":
        if api_voice:
            tts["voice_id"] = api_voice
        if voice:                              # 云端挂掉时用的本地嗓子
            tts["fallback"] = [f"piper:{voice}"]
    elif voice:                                # 跟全局 / 克隆：piper 声线当退路
        tts["fallback"] = [f"piper:{voice}"]

    if tts:
        data["tts"] = tts
    else:
        data.pop("tts", None)
    return data, True


def main() -> int:
    ap = argparse.ArgumentParser(description="把人格文件的旧声线字段搬成 tts 结构")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--dir", default="data/personas", help="人格文件目录")
    args = ap.parse_args()

    room = ROOT / args.dir
    files = sorted(room.glob("*.json"))
    if not files:
        print(f"{room} 下没有 json")
        return 1
    changed = 0
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"× {path.name}：读不动（{exc}）")
            continue
        data, touched = migrate(raw)
        if not touched:
            print(f"· {path.name}：已经是新格式")
            continue
        changed += 1
        print(f"{'（试运行）' if args.dry_run else ''}√ {path.name}："
              f"tts = {json.dumps(data.get('tts') or {}, ensure_ascii=False)}")
        if args.dry_run:
            continue
        backup = path.with_suffix(path.suffix + ".bak-tts")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n共 {len(files)} 个文件，{changed} 个需要迁移"
          + ("（试运行，没写）" if args.dry_run else "（已写，备份 *.bak-tts）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
