"""Ollama 本机大模型客户端（流式输出，便于边生成边朗读）。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import requests

from .settings import LlmConfig


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self.base = cfg.host.rstrip("/")
        self._history: list[dict] = []

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

    def warmup(self) -> float:
        """预热：把模型加载进内存，避免第一次对话首字延迟过长。"""
        import time

        t0 = time.perf_counter()
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_predict": 1, "num_ctx": self.cfg.num_ctx},
        }
        r = requests.post(f"{self.base}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
        return time.perf_counter() - t0

    def release(self) -> bool:
        """让 Ollama 立刻卸载模型（keep_alive=0），一般能腾出 4~5 GB 内存。

        回到待唤醒状态时调用，下次唤醒会上一次预热。
        """
        try:
            r = requests.post(
                f"{self.base}/api/generate",
                json={"model": self.cfg.model, "keep_alive": 0},
                timeout=15,
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def is_loaded(self) -> bool | None:
        """查询模型当前是否驻留在内存里（用于状态展示）。"""
        try:
            r = requests.get(f"{self.base}/api/ps", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return self.cfg.model in names
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- 对话
    def reset(self) -> None:
        self._history.clear()

    def _build_messages(self, user_text: str) -> list[dict]:
        msgs: list[dict] = []
        if self.cfg.system_prompt.strip():
            msgs.append({"role": "system", "content": self.cfg.system_prompt.strip()})
        keep = max(0, int(self.cfg.history_turns)) * 2
        if keep:
            msgs.extend(self._history[-keep:])
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def chat_stream(self, user_text: str) -> Iterator[str]:
        """流式返回回答增量。

        注意：本方法不修改历史记录，调用方需在收尾时调用 :meth:`commit`，
        这样即使中途被打断也能如实记录对话。
        """
        messages = self._build_messages(user_text)
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": self.cfg.temperature,
                "top_p": self.cfg.top_p,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": self.cfg.num_predict,
            },
        }
        answer: list[str] = []
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
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except OllamaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"调用 Ollama 失败：{exc}") from exc

    def commit(self, user_text: str, answer: str) -> None:
        """把这一轮写进对话历史。"""
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": answer or ""})

    def chat(self, user_text: str) -> str:
        answer = "".join(self.chat_stream(user_text)).strip()
        self.commit(user_text, answer)
        return answer
