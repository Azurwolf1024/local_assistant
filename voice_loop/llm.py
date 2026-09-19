"""Ollama 本机大模型客户端（流式输出，便于边生成边朗读）。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import requests

from .accel import llm_options
from .settings import LlmConfig


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self.base = cfg.host.rstrip("/")
        self._history: list[dict] = []
        # ★运行时可换的两项★（多角色用）：None = 用 cfg 里的值。
        # 单独存一份而不是直接改 cfg，是因为 settings.llm 是共享对象，
        # 改它会牵连到评估脚本 / 自检里读同一份配置的地方。
        self.system_prompt: str | None = None
        self.temperature: float | None = None

    # ------------------------------------------------------------------ 基础
    def list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(
                f"无法连接 Ollama（{self.base}）。请确认已运行 `ollama serve`。原始错误：{exc}"
            ) from exc
        return [m.get("name", "") for m in r.json().get("models", [])]

    def _system_prompt(self) -> str:
        """当前生效的人设：运行时指的（角色）优先，否则用 config.toml 里那段。"""
        if self.system_prompt is not None:
            return self.system_prompt
        return self.cfg.system_prompt

    def _think_param(self) -> bool | None:
        """把 ``[llm] think`` 翻成请求参数；None = 不传这个字段。

        为什么要它：qwen3.5 这类带 thinking 的模型**默认会先想一两千字再开口**，
        实测同一次回答 16.8s → 7.2s（关掉思考后），而语音对话里这几秒用户只能干等。
        不支持的模型（qwen2.5 等）收到这个字段不会报错，实测会被忽略。
        """
        mode = str(getattr(self.cfg, "think", "off") or "off").strip().lower()
        if mode in ("off", "false", "0", "no"):
            return False
        if mode in ("on", "true", "1", "yes"):
            return True
        return None

    def ensure_model(self) -> None:
        models = self.list_models()
        want = self.cfg.model
        if want in models:
            return
        # 允许 “qwen2.5:7b” 与 “qwen2.5:7b-instruct” 之类的宽松匹配
        stem = want.split(":")[0]
        matches = [m for m in models if m.split(":")[0] == stem]
        if matches:
            print(f"[llm] 未找到 {want}，改用最接近的 {matches[0]}", file=sys.stderr)
            self.cfg.model = matches[0]
            return
        raise OllamaError(
            f"Ollama 中没有模型 {want}。请先执行：ollama pull {want}\n当前已有：{models}"
        )

    def resolve_model(self, want: str) -> str:
        """把配置里的模型名换成真正存在的那个（宽松匹配），不存在就报清楚怎么装。

        看图专用：视觉模型可以是另一个（qwen2.5vl 等），也可以是自带视觉的
        qwen3.5:4b；用纯文本模型看图只会得到一堆编造的内容，所以这里宁可报错。
        """
        want = (want or "").strip() or self.cfg.model
        models = self.list_models()
        if want in models:
            return want
        stem = want.split(":")[0]
        matches = [m for m in models if m.split(":")[0] == stem]
        if matches:
            return matches[0]
        raise OllamaError(
            f"Ollama 里没有能看图的模型 {want}（纯文本模型看图只会编）。"
            f"先在终端装一个：ollama pull {want}。当前已有：{models}"
        )

    def warmup(self, model: str | None = None) -> float:
        """预热：把模型加载进内存，避免第一次对话首字延迟过长。"""
        import time

        t0 = time.perf_counter()
        payload = {
            "model": model or self.cfg.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_predict": 1, "num_ctx": self.cfg.num_ctx, **llm_options(self.cfg)},
        }
        think = self._think_param()
        if think is not None:
            payload["think"] = think        # 预热也别让它先思考，否则白烧 token
        r = requests.post(f"{self.base}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
        return time.perf_counter() - t0

    def release(self, model: str | None = None) -> bool:
        """让 Ollama 立刻卸载模型（keep_alive=0），一般能腾出 4~5 GB 内存。

        回到待唤醒状态时调用，下次唤醒会上一次预热。视觉模型（6 GB 上下）
        尤其值得卸：看完图就让它走。
        """
        try:
            r = requests.post(
                f"{self.base}/api/generate",
                json={"model": model or self.cfg.model, "keep_alive": 0},
                timeout=15,
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def is_loaded(self, model: str | None = None) -> bool | None:
        """查询模型当前是否驻留在内存里（用于状态展示）。"""
        try:
            r = requests.get(f"{self.base}/api/ps", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return (model or self.cfg.model) in names
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- 对话
    def reset(self) -> None:
        self._history.clear()

    def _build_messages(self, user_text: str, images: list[str] | None = None) -> list[dict]:
        msgs: list[dict] = []
        prompt = self._system_prompt().strip()
        if prompt:
            msgs.append({"role": "system", "content": prompt})
        keep = max(0, int(self.cfg.history_turns)) * 2
        if keep:
            msgs.extend(self._history[-keep:])
        last: dict = {"role": "user", "content": user_text}
        if images:
            # Ollama 的约定：images 是 base64 字符串数组（不带 data: 前缀）
            last["images"] = list(images)
        msgs.append(last)
        return msgs

    def chat_stream(
        self,
        user_text: str,
        images: list[str] | None = None,
        model: str | None = None,
        num_ctx: int | None = None,
        tools: list[dict] | None = None,
        extra_messages: list[dict] | None = None,
    ) -> Iterator[str]:
        """流式返回回答增量（只出文字，工具调用事件被丢掉）。

        要拿到 ``tool_calls`` 用 :meth:`chat_events`。

        注意：本方法不修改历史记录，调用方需在收尾时调用 :meth:`commit`，
        这样即使中途被打断也能如实记录对话。
        """
        for ev in self.chat_events(
            user_text,
            images=images,
            model=model,
            num_ctx=num_ctx,
            tools=tools,
            extra_messages=extra_messages,
        ):
            if "delta" in ev:
                yield ev["delta"]

    def chat_events(
        self,
        user_text: str = "",
        images: list[str] | None = None,
        model: str | None = None,
        num_ctx: int | None = None,
        tools: list[dict] | None = None,
        extra_messages: list[dict] | None = None,
        messages: list[dict] | None = None,
        prefix_messages: list[dict] | None = None,
    ) -> Iterator[dict]:
        """流式返回事件：``{"delta": "文字"}`` 或 ``{"tool_calls": [...]}``。

        为什么用事件而不是直接返回 tool_calls：Ollama 把工具调用放在**最后一个** chunk 里，
        而文字是边生成边到的。用事件流就能做到「模型要直接回答 -> 边说边播；
        模型要调工具 -> 一个字都没念，直接去执行」。

        ``extra_messages``：接在历史之后、本次输入之前（例如把工具结果喂回去）。
        """
        if messages is not None:
            msgs = [dict(m) for m in messages]
        else:
            msgs = self._build_messages(user_text, images)
            if extra_messages:
                msgs[-1:-1] = [dict(m) for m in extra_messages]
        if prefix_messages:
            # 放在最前面：例如「你可以调用工具」这类说明，跟用户的人设各自独立
            msgs = [dict(m) for m in prefix_messages] + msgs
        payload = {
            "model": model or self.cfg.model,
            "messages": msgs,
            "stream": True,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": (self.temperature if self.temperature is not None
                                else self.cfg.temperature),
                "top_p": self.cfg.top_p,
                "num_ctx": int(num_ctx or self.cfg.num_ctx),
                "num_predict": self.cfg.num_predict,
                **llm_options(self.cfg),
            },
        }
        if tools:
            payload["tools"] = tools
        think = self._think_param()
        if think is not None:
            payload["think"] = think
        try:
            with requests.post(
                f"{self.base}/api/chat", json=payload, stream=True, timeout=(5, 300)
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=False):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise OllamaError(str(chunk["error"]))
                    message = chunk.get("message") or {}
                    calls = message.get("tool_calls") or []
                    if calls:
                        yield {"tool_calls": calls}
                    piece = message.get("content", "")
                    if piece:
                        yield {"delta": piece}
                    if chunk.get("done"):
                        break
        except OllamaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"调用 Ollama 失败：{exc}") from exc

    def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> tuple[str, list[dict]]:
        """不走流式版：一次拿到「文字」和「工具调用」（评估脚本用，方便）。"""
        content: list[str] = []
        calls: list[dict] = []
        for ev in self.chat_events(
            model=model, num_ctx=num_ctx, tools=tools, messages=messages
        ):
            if "delta" in ev:
                content.append(ev["delta"])
            elif "tool_calls" in ev:
                calls.extend(ev["tool_calls"])
        return "".join(content).strip(), calls

    def commit(self, user_text: str, answer: str) -> None:
        """把这一轮写进对话历史。"""
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": answer or ""})

    def chat(self, user_text: str) -> str:
        answer = "".join(self.chat_stream(user_text)).strip()
        self.commit(user_text, answer)
        return answer
