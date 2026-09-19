"""换模型前的体检：延迟 / 思考开销 / 选工具 / 看图（OCR）。

为什么要有它：换模型不能只看「新不新」。这台机器上真正决定体验的是
**首字延迟**和**看图识字的准度**，还有「它是不是默认在思考」（思考的 token
也要一个个生成，语音对话里就是白等几秒）。这个脚本把这几件事量出来。

跑法：
    python scripts/bench_llm.py qwen3.5:4b
    python scripts/bench_llm.py qwen2.5:7b qwen3.5:4b          # 横向对比（会来回换模型）
    python scripts/bench_llm.py qwen3.5:4b --think off         # 关掉思考再量一次
    python scripts/bench_llm.py qwen3.5:4b --no-vision         # 跳过看图那一段
    python scripts/bench_llm.py qwen3.5:4b --image shot.png    # 用自己的图

注意：脚本会**主动卸载**模型（keep_alive=0）来量冷启动，所以别在服务跑着的时候用，
会把正在用的模型踢出显存（服务下轮会自己再加载，只是慢一下）。
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics as stats
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_loop.settings import load_settings  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# Ollama 基础调用（故意不复用 voice_loop.llm：要单独控制 think / 看 thinking 字段）
# --------------------------------------------------------------------------- #
def api(base: str, path: str, payload: dict | None = None, timeout: float = 600):
    url = f"{base.rstrip('/')}{path}"
    if payload is None:
        r = requests.get(url, timeout=timeout)
    else:
        r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def unload(base: str, model: str) -> None:
    """把模型踢出内存，让下一次调用是真正的冷启动。"""
    try:
        requests.post(f"{base}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(40):                      # 最多等 10 秒，确认它真的走了
        try:
            names = [m.get("name", "") for m in api(base, "/api/ps").get("models", [])]
        except Exception:  # noqa: BLE001
            return
        if model not in names:
            return
        time.sleep(0.25)


def model_info(base: str, model: str) -> dict:
    try:
        r = api(base, "/api/show", {"model": model})
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 读不到 {model} 的信息：{exc}")
        return {}
    caps = r.get("capabilities") or []
    params = r.get("parameters") or ""
    n_ctx = ""
    for line in str(params).splitlines():
        if line.startswith("num_ctx"):
            n_ctx = line.split()[-1]
    size = r.get("size") or (r.get("details") or {}).get("parameter_size")
    return {"caps": caps, "ctx": n_ctx, "size": size, "details": r.get("details") or {}}


def timed_chat(
    base: str,
    model: str,
    messages: list[dict],
    think: bool | None = None,
    num_ctx: int = 4096,
    num_predict: int = 512,
    temperature: float = 0.7,
) -> dict:
    """跑一次流式对话，返回首字/总耗时/思考用了多少字。"""
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": "5m",
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if think is not None:
        payload["think"] = bool(think)
    t0 = time.perf_counter()
    first_text = first_any = None
    text: list[str] = []
    think_chars = 0
    calls: list[dict] = []
    with requests.post(f"{base}/api/chat", json=payload, stream=True, timeout=(10, 600)) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        for line in resp.iter_lines(decode_unicode=False):
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))
            msg = chunk.get("message") or {}
            if msg.get("thinking"):
                think_chars += len(msg["thinking"])
                first_any = first_any or time.perf_counter()
            if msg.get("content"):
                text.append(msg["content"])
                first_any = first_any or time.perf_counter()
                first_text = first_text or time.perf_counter()
            if msg.get("tool_calls"):
                calls.extend(msg["tool_calls"])
            if chunk.get("done"):
                break
    total = time.perf_counter() - t0
    return {
        "first": (first_text or first_any or 0) - t0,
        "first_any": (first_any or 0) - t0,
        "total": total,
        "text": "".join(text).strip(),
        "think_chars": think_chars,
        "tool_calls": calls,
    }


# --------------------------------------------------------------------------- #
def synth_image(path: Path, width: int = 2400, height: int = 1120) -> Path:
    """造一张「200% 缩放的大屏截图」：中文 + 代码 + 数字，用来量看图识字。

    故意做大（2400×1120）：项目的默认是压到最长边 1024 再喂给模型，
    所以要量「压到多少最划算」就得有得压。
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    def font(size: int):
        for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    k = width / 900.0
    def at(x, y, text, size=26, fill="black"):
        d.text((int(x * k), int(y * k)), text, fill=fill, font=font(int(size * k)))

    at(28, 24, "会议纪要", 30)
    at(28, 80, "本周五下午三点，A302 开组会。", 26)
    at(28, 130, "负责人：凯尔希    电话 13800001111", 26)
    d.rectangle([int(28 * k), int(190 * k), int(870 * k), int(380 * k)], outline="black",
                width=max(2, int(2 * k)))
    for i, line in enumerate([
        "def calc_total(items):",
        "    return sum(i['price'] for i in items)",
        "print(calc_total(CART))",
    ]):
        at(44, 205 + i * 40, line, 22)
    at(600, 130, "Alpha-7", 26)
    img.save(path, quality=92)
    return path


VISION_Q = "把图里的文字原样念出来，不要解释，不要补充。"
VISION_KEYS = ["会议纪要", "A302", "13800001111", "calc_total", "sum(i['price']"]


def bench_vision(base: str, model: str, img: Path, num_ctx: int = 4096,
                 max_side: int | None = None) -> dict:
    """把图压到最长边 max_side 再问一次（走项目自己的编码路径，保持一致）。"""
    from voice_loop import vision as vmod

    payload_img = img
    if max_side:
        from PIL import Image

        im = Image.open(img)
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
            payload_img = img.with_name(f"{img.stem}_{max_side}.jpg")
            im.convert("RGB").save(payload_img, quality=82)
        else:
            payload_img = img
    b64 = base64.b64encode(payload_img.read_bytes()).decode("ascii")
    msgs = [{"role": "user", "content": VISION_Q, "images": [b64]}]
    r = timed_chat(base, model, msgs, think=False, num_ctx=num_ctx, temperature=0.1)
    r["hit"] = [k for k in VISION_KEYS if k in r["text"]]
    r["side"] = max_side
    r["b64_kb"] = len(b64) * 3 // 4 // 1024
    return r


def bench_throughput(base: str, model: str, num_ctx: int, num_predict: int = 200) -> dict:
    """让 Ollama 自己报计数：生成吞吐 / 预填吞吐 / 加载耗时。

    为什么要它：流式首字会被「回答长短」和「思考」搅在一起，
    这里的 tokens/s 才是这个模型在这台机器上的真实速度。

    ★预填必须绕开前缀缓存★：prompt 开头塞一个随机串，再配一段一千多字的
    system prompt —— 否则 Ollama 直接复用上一次的 KV，报出来的
    `prompt_eval_count` 只有几十个 token，算出来的「预填速度」全是假的。
    （预填速度决定「读文件」「长 system prompt」要等多久，是本项目很关键的一个数。）
    """
    nonce = uuid.uuid4().hex[:8]
    filler = "罗德岛的基建分为贸易站、制造站、发电站、控制中枢与宿舍五个部分。" * 60
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": f"[{nonce}] {filler}"},
            {"role": "user", "content": "介绍一下罗德岛。"},
        ],
        "stream": False,
        "keep_alive": "5m",
        "think": False,
        "options": {"temperature": 0.7, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    r = api(base, "/api/chat", payload)

    def rate(count, dur_ns):
        try:
            count = float(count or 0)
            dur = float(dur_ns or 0) / 1e9
            return count / dur if dur > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    return {
        "gen_tps": rate(r.get("eval_count"), r.get("eval_duration")),
        "prompt_tps": rate(r.get("prompt_eval_count"), r.get("prompt_eval_duration")),
        "load": float(r.get("load_duration") or 0) / 1e9,
        "gen_count": int(r.get("eval_count") or 0),
        "prompt_count": int(r.get("prompt_eval_count") or 0),
    }


# --------------------------------------------------------------------------- #
def bench_one(base: str, model: str, args, vision_img: Path | None, llm_cfg) -> dict:
    print("\n" + "=" * 72)
    print(f" 模型：{model}")
    print("=" * 72)
    info = model_info(base, model)
    caps = info.get("caps") or []
    print(f"  能力：{'、'.join(caps) or '（读不到）'}")
    det = info.get("details") or {}
    if det:
        print(f"  细节：{det.get('parameter_size', '?')} / {det.get('quantization_level', '?')}"
              f" / family={det.get('family', '?')}")
    if info.get("ctx"):
        print(f"  模型自带 num_ctx：{info['ctx']}")

    out: dict = {"model": model, "caps": caps}

    # ---- 1. 第一次调用（冷启动：加载 + 预填 + 首字） ----
    print("\n  [1] 冷启动（先卸载，再量第一次）")
    cold_think = {"auto": None, "on": True, "off": False}[args.think]
    unload(base, model)
    cold = timed_chat(base, model, [{"role": "user", "content": "用一句话介绍罗德岛。"}],
                      think=cold_think, num_ctx=llm_cfg.num_ctx)
    print(f"      首字 {cold['first']:.2f}s / 总 {cold['total']:.2f}s   「{cold['text'][:40]}」")
    out["cold_first"] = cold["first"]

    # ---- 2. 热态：带真实 system prompt（预填量大） ----
    print("\n  [2] 热态（带 config.toml 里的 system prompt，最接近真实一轮）")
    msgs = [
        {"role": "system", "content": llm_cfg.system_prompt.strip()},
        {"role": "user", "content": "我今天有点累，还要不要继续写代码"},
    ]
    rounds = []
    think_arg = {"auto": None, "on": True, "off": False}[args.think]
    for i in range(args.rounds):
        r = timed_chat(base, model, msgs, think=think_arg, num_ctx=llm_cfg.num_ctx)
        rounds.append(r)
        note = "（首轮要预填 system prompt）" if i == 0 else "（已命中前缀缓存）"
        print(f"      第{i + 1}轮：首字 {r['first']:.2f}s / 总 {r['total']:.2f}s / "
              f"思考 {r['think_chars']} 字 {note}")
        print(f"         「{r['text'][:60]}」")
    # 体感看最后一轮：system prompt 一直不变，真实使用时都是缓存命中
    hot = rounds[-1]
    out["warm_first"] = hot["first"]
    out["warm_first_cold"] = rounds[0]["first"]
    out["warm_total"] = hot["total"]
    out["think_chars"] = hot["think_chars"]
    check("热态首字 < 1.5s（语音对话的体感线）", hot["first"] < 1.5,
          f"缓存后 {hot['first']:.2f}s（首轮预填 {rounds[0]['first']:.2f}s）")

    # ---- 3. 思考开销：默认 vs think=false ----
    print("\n  [3] 思考开销（同一个问题，一次默认、一次 think=false）")
    q = [{"role": "user", "content": "把「下周三下午三点半跟导师见面」记到日程里，"
                                     "我该怎么跟你说？"}]
    try:
        d = timed_chat(base, model, q, think=None, num_ctx=llm_cfg.num_ctx, num_predict=256)
        print(f"      默认    ：首字 {d['first']:.2f}s / 总 {d['total']:.2f}s / 思考 {d['think_chars']} 字")
        f = timed_chat(base, model, q, think=False, num_ctx=llm_cfg.num_ctx, num_predict=256)
        print(f"      think=假：首字 {f['first']:.2f}s / 总 {f['total']:.2f}s / 思考 {f['think_chars']} 字")
        out["default_total"] = d["total"]
        out["nothink_total"] = f["total"]
        if d["think_chars"]:
            check("think=false 真能省时间（思考字数为 0 且总耗时腰斩）",
                  f["think_chars"] == 0 and f["total"] < d["total"] * 0.75,
                  f"总耗时 {d['total']:.1f}s（思考 {d['think_chars']} 字）→ {f['total']:.1f}s")
        else:
            print("      （这个模型默认不思考，think 参数可省）")
    except Exception as exc:  # noqa: BLE001
        print(f"      [警告] 思考对比失败：{exc}")
        out["default_total"] = out["nothink_total"] = None

    # ---- 4. 看图 ----
    if vision_img is not None:
        vmodel = args.vision_model or model
        sizes = [int(x) for x in str(args.vision_sizes).replace(" ", "").split(",") if x] \
            if args.vision_sizes else [None]
        print(f"\n  [4] 看图（模型={vmodel}，合成的一张 2400×1120「大屏截图」，量识字准度）")
        try:
            for side in sizes:
                v = bench_vision(base, vmodel, vision_img, num_ctx=llm_cfg.num_ctx, max_side=side)
                tag = f"长边 {side}" if side else "原图"
                print(f"      {tag:9s} {v['b64_kb']:4d} KB  耗时 {v['total']:5.1f}s（首字 {v['first']:5.1f}s）"
                      f"  命中 {len(v['hit'])}/{len(VISION_KEYS)}")
                print(f"        {'、'.join(v['hit']) or '（一个都没读到）'}")
                out.setdefault("vision_sizes", []).append(
                    {"side": tag, "sec": v["total"], "hit": len(v["hit"]), "kb": v["b64_kb"]}
                )
                out["vision_total"] = v["total"]
                out["vision_hit"] = len(v["hit"])
                out["vision_model"] = vmodel
        except Exception as exc:  # noqa: BLE001
            print(f"      [警告] 看图失败（可能不是视觉模型）：{exc}")
            out["vision_total"] = None
            out["vision_hit"] = 0

    # ---- 5. 吞吐（Ollama 自报计数，跟回答长短无关） ----
    print("\n  [5] 吞吐（Ollama 自报的 tokens/s）")
    try:
        tp = bench_throughput(base, model, llm_cfg.num_ctx)
        print(f"      生成 {tp['gen_tps']:.1f} tok/s（{tp['gen_count']} 个 token）    "
              f"预填 {tp['prompt_tps']:.0f} tok/s（{tp['prompt_count']} 个）    "
              f"加载 {tp['load']:.1f}s")
        out["gen_tps"] = tp["gen_tps"]
        out["prompt_tps"] = tp["prompt_tps"]
        out["load"] = tp["load"]
    except Exception as exc:  # noqa: BLE001
        print(f"      [警告] 量吞吐失败：{exc}")
        out["gen_tps"] = out["prompt_tps"] = out["load"] = 0.0
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="换模型前的体检")
    ap.add_argument("models", nargs="*", help="要测的模型（默认测配置里那个）")
    ap.add_argument("--think", choices=["auto", "on", "off"], default="off",
                    help="第 [1][2] 段用什么思考设置（默认 off = 语音对话的现实场景；"
                         "第 [3] 段无论如何都会把 on/off 都量一遍）")
    ap.add_argument("--rounds", type=int, default=2, help="热态跑几轮取中位（默认 2）")
    ap.add_argument("--no-vision", action="store_true", help="跳过看图那一段")
    ap.add_argument("--image", default=None, help="用自己的图片；默认合成一张")
    ap.add_argument("--vision-model", default=None,
                    help="看图用另一个模型（例如文本用 qwen2.5:7b、看图用 qwen2.5vl:3b）")
    ap.add_argument("--vision-sizes", default="1024",
                    help="看图按这些最长边各量一遍（默认 1024；例：--vision-sizes 768,1024,1568）")
    args = ap.parse_args()

    settings = load_settings()
    base = settings.llm.host
    models = args.models or [settings.llm.model]

    try:
        api(base, "/api/tags")
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 连不上 Ollama（{base}）：{exc}")
        return 2
    have = [m.get("name", "") for m in api(base, "/api/tags").get("models", [])]

    img: Path | None = None
    if not args.no_vision:
        if args.image:
            img = Path(args.image)
            if not img.exists():
                print(f"[错误] 找不到图片：{img}")
                return 2
        else:
            img = synth_image(ROOT / "sessions" / "_bench_vision.png")
        print(f"看图用的图：{img}")

    print("=" * 72)
    print(" 模型体检（延迟 / 思考开销 / 看图）")
    print("=" * 72)
    print(f" 设置：think={args.think} rounds={args.rounds}  num_ctx={settings.llm.num_ctx}")

    rows: list[dict] = []
    for model in models:
        if model not in have:
            print(f"\n[跳过] Ollama 里没有 {model}（先 ollama pull {model}）")
            continue
        try:
            rows.append(bench_one(base, model, args, img, settings.llm))
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] {model} 跑挂了：{type(exc).__name__}: {exc}")

    if rows:
        print("\n" + "=" * 72)
        print(" 汇总（表格竖着短不了，重要的已加粗到下面）")
        print("=" * 72)
        head = (f"{'模型':22s} {'生成':>9s} {'预填':>9s} {'加载':>6s} {'冷启动':>7s} "
                f"{'热首字':>7s} {'首轮预填':>8s} {'看图':>7s} {'识字':>6s} {'思考':>5s}")
        print(" " + head)
        for r in rows:
            print(" " + f"{r['model']:22s} "
                  f"{r.get('gen_tps', 0):6.1f}t/s "
                  f"{r.get('prompt_tps', 0):6.0f}t/s "
                  f"{r.get('load', 0):5.1f}s "
                  f"{r.get('cold_first', 0):6.2f}s "
                  f"{r.get('warm_first', 0):6.2f}s "
                  f"{r.get('warm_first_cold', 0):7.2f}s "
                  f"{(r.get('vision_total') or 0):6.1f}s "
                  f"{r.get('vision_hit', 0)}/{len(VISION_KEYS)} "
                  f"{r.get('think_chars', 0):5d}")
        print("\n 说明：生成/预填 = Ollama 自报的 tok/s（跟回答长短无关，模型快慢看这个）；")
        print("       冷启动 = 加载模型 + 首字；热首字 = system prompt 命中前缀缓存后的首字；")
        print("       首轮预填 = 刚加载完、缓存还没建立那一次；思考 = 默认模式下思考了多少字。")
        for r in rows:
            if r.get("vision_sizes"):
                detail = "  ".join(f"{s['side']} {s['sec']:.1f}s {s['hit']}/5 {s['kb']}KB"
                                    for s in r["vision_sizes"])
                print(f"       看图（{r.get('vision_model')}）：{detail}")
        print("       「识字」是那张合成截图里 5 个关键词认出几个（1/5 也不算合格）。")

    print("\n" + "=" * 72)
    if _failures:
        print(f" {len(_failures)} 项未通过：{_failures}")
    else:
        print(" 全部通过 √")
    print("=" * 72)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
