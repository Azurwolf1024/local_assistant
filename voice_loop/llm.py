"""大模型客户端。

两个后端，同一套接口：

- :class:`OllamaClient` —— 本机 Ollama（默认；全离线）
- :class:`OpenAICompatClient` —— OpenAI 兼容的云端接口（DeepSeek 等）

:class:`ChatClient` 是流水线真正拿到的那个门面：它按「当前角色」在两者之间切换，
本地不可用时也不会把嘴弄哑（见 ``configure`` / ``chat_events`` 的说明）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import requests

from .accel import llm_options
from .settings import ApiConfig, LlmConfig


class LlmError(RuntimeError):
    """两边共用的错误基类（流水线只需要认这一个）。"""


class OllamaError(LlmError):
    pass


class ApiError(LlmError):
    pass


def resolve_api_key(cfg: ApiConfig) -> str:
    """密钥优先级：``api_key`` → ``api_key_file`` → 环境变量。

    ⚠ 单独抽成函数：LLM 与 TTS 共用同一份密钥解析，
      免得两边对 ``env:`` / 文件 / 环境变量的优先级各写一套。
    """
    raw = str(getattr(cfg, "api_key", "") or "").strip()
    if raw.lower().startswith("env:"):
        return os.environ.get(raw[4:].strip(), "").strip()
    if raw:
        return raw
    path = str(getattr(cfg, "api_key_file", "") or "").strip()
    if path:
        try:
            for line in Path(path).expanduser().read_text(
                encoding="utf-8-sig", errors="ignore"
            ).splitlines():
                line = line.strip()
                if line:
                    return line
        except OSError:
            pass
    return os.environ.get(str(getattr(cfg, "api_key_env", "") or "").strip(), "").strip()


def _demote_extra_system(msgs: list[dict]) -> list[dict]:
    """把第一条之后的 system 就地改成 user（位置、内容都不动）。

    ★为什么★：有些本地 GGUF（例如从 unsloth 导入的 Qwen3.5）自带 Jinja 聊天模板，
    模板要求 system 只能是消息列表的第一条，否则 Ollama 直接 500：
    「System message must be at the beginning」。而本项目会把「此刻 + 工具规则」
    作为第二条 system 紧贴用户那句（实测这个位置工具调用最稳，见 README §1.2）。
    官方 Ollama 包里的模板不较真，本地导入的 GGUF 会。降级成 user 后语义不变、
    模板也能过（实测工具照常调）。
    """
    if not any(m.get("role") == "system" for m in msgs[1:]):
        return msgs
    out: list[dict] = []
    for i, m in enumerate(msgs):
        if i and m.get("role") == "system":
            m = {**m, "role": "user"}
        out.append(m)
    return out


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

    def _build_messages(self, user_text: str, images: list[str] | None = None,
                        system_override: str | None = None) -> list[dict]:
        msgs: list[dict] = []
        prompt = (self._system_prompt() if system_override is None
                  else system_override).strip()
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
        system_override: str | None = None,
    ) -> Iterator[dict]:
        """流式返回事件：``{"delta": "文字"}`` 或 ``{"tool_calls": [...]}``。

        为什么用事件而不是直接返回 tool_calls：Ollama 把工具调用放在**最后一个** chunk 里，
        而文字是边生成边到的。用事件流就能做到「模型要直接回答 -> 边说边播；
        模型要调工具 -> 一个字都没念，直接去执行」。

        ``extra_messages``：接在历史之后、本次输入之前（例如把工具结果喂回去）。
        ``system_override``：临时换掉 system prompt（人设「出戏」时重答用，
        见 ``pipeline`` 里的兜底重试）。
        """
        if messages is not None:
            msgs = [dict(m) for m in messages]
        else:
            msgs = self._build_messages(user_text, images, system_override)
            if extra_messages:
                msgs[-1:-1] = [dict(m) for m in extra_messages]
        if prefix_messages:
            # 放在最前面：例如「你可以调用工具」这类说明，跟用户的人设各自独立
            msgs = [dict(m) for m in prefix_messages] + msgs
        # 本地导入的 GGUF 模板要求 system 只能出现在第一条：多出来的降级成 user
        msgs = _demote_extra_system(msgs)
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


# =========================================================================== #
# 云端：OpenAI 兼容接口（DeepSeek / 通义 / 任何照着 OpenAI 协议做的服务）
# =========================================================================== #
class OpenAICompatClient:
    """``/chat/completions`` 的流式客户端（SSE）。

    只实现流水线用到的那一小块：``chat_events`` 吐出 ``{"delta": ...}`` 与
    ``{"tool_calls": [...]}``，工具定义沿用 OpenAI / Ollama 同一套 JSON 形状。
    """

    def __init__(self, cfg: ApiConfig) -> None:
        self.cfg = cfg
        self._history: list[dict] = []
        # 跟 OllamaClient 一样：运行时可换的人设与温度（None = 用配置里的）
        self.system_prompt: str | None = None
        self.temperature: float | None = None

    # ------------------------------------------------------------------ 基础
    @property
    def base(self) -> str:
        return (self.cfg.base_url or "https://api.deepseek.com").rstrip("/")

    def endpoint(self) -> str:
        base = self.base
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def api_key(self) -> str:
        """密钥（见 :func:`resolve_api_key` 的优先级）。"""
        return resolve_api_key(self.cfg)

    def ready(self) -> tuple[bool, str]:
        """配置够不够打这一枪。返回 ``(可以, 原因)``。"""
        if not bool(getattr(self.cfg, "enabled", True)):
            return False, "[api] enabled = false"
        if not self.api_key():
            env = str(self.cfg.api_key_env or "DEEPSEEK_API_KEY")
            path = str(getattr(self.cfg, "api_key_file", "") or "").strip()
            hint = f"写 [api] api_key、放一个 api_key_file（现在是 {path or '没填'}）" \
                if path else f"设置环境变量 {env}，或写 [api] api_key / api_key_file"
            return False, f"没有 API key（{hint}）"
        if not str(self.cfg.model or "").strip():
            return False, "[api] model 是空的"
        return True, ""

    def _system_prompt(self) -> str:
        if self.system_prompt is not None:
            return self.system_prompt
        return ""

    def ensure_model(self) -> None:
        ok, why = self.ready()
        if not ok:
            raise ApiError(why)

    def warmup(self, model: str | None = None) -> float:
        # 云端没有「加载进显存」这回事；能用的前提就是 key/地址配好了
        self.ensure_model()
        return 0.0

    def release(self, model: str | None = None) -> bool:
        return True

    def is_loaded(self, model: str | None = None) -> bool | None:
        return None

    # ---------------------------------------------------------------- 对话
    def reset(self) -> None:
        self._history.clear()

    def _build_messages(self, user_text: str, images: list[str] | None = None,
                        system_override: str | None = None) -> list[dict]:
        if images:
            raise ApiError("这个接口不支持看图（看图会继续走本地的视觉模型）")
        msgs: list[dict] = []
        prompt = (self._system_prompt() if system_override is None
                  else system_override).strip()
        if prompt:
            msgs.append({"role": "system", "content": prompt})
        if bool(getattr(self.cfg, "keep_all_history", False)):
            # ★正常 harness 的会话上下文★：自上次 reset（做梦 / 换角色）以来的
            # 全部消息原样带上，只在末尾追加新消息 —— 已缓存的前缀不会被挪走。
            msgs.extend(self._history)
        else:
            keep = max(0, int(self.cfg.history_turns)) * 2
            if keep:
                msgs.extend(self._history[-keep:])
        msgs.append({"role": "user", "content": user_text})
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
        for ev in self.chat_events(
            user_text, images=images, model=model, num_ctx=num_ctx, tools=tools,
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
        system_override: str | None = None,
    ) -> Iterator[dict]:
        """流式事件：``{"delta": 文字}`` / ``{"tool_calls": [...]}``。

        工具调用在 OpenAI 协议里是**分片**到的（先 name，再一段段 arguments），
        所以先把它们攒齐，流结束时一次性交出去 —— 形状和 Ollama 那份保持一致。
        """
        if messages is not None:
            msgs = [dict(m) for m in messages]
        else:
            msgs = self._build_messages(user_text, images, system_override)
            if extra_messages:
                msgs[-1:-1] = [dict(m) for m in extra_messages]
        if prefix_messages:
            msgs = [dict(m) for m in prefix_messages] + msgs

        payload: dict = {
            "model": (model or self.cfg.model),
            "messages": msgs,
            "stream": True,
            "temperature": (self.temperature if self.temperature is not None
                            else self.cfg.temperature),
            "top_p": self.cfg.top_p,
            "max_tokens": int(self.cfg.max_tokens),
            # ★要 usage 才会给缓存命中数★：deepseek 的 prompt_cache_hit_tokens 只有
            # 在 stream_options 里开口要的时候才随流回来（默认不回）。
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        ok, why = self.ready()
        if not ok:
            raise ApiError(why)

        calls: dict[int, dict] = {}
        headers = {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            with requests.post(
                self.endpoint(), json=payload, headers=headers, stream=True,
                timeout=(10, float(self.cfg.timeout)),
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.text[:400]
                    raise ApiError(f"API {resp.status_code}：{body}")
                for raw in resp.iter_lines(decode_unicode=False):
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        if line == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise ApiError(str(chunk["error"]))
                    usage = chunk.get("usage")
                    if isinstance(usage, dict) and usage:
                        # 有的服务把 usage 塞在最后一个 choices 为空的 chunk 里
                        yield {"usage": usage}
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0) or 0)
                        slot = calls.setdefault(
                            idx,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += str(fn["arguments"])
                    piece = delta.get("content")
                    if piece:
                        yield {"delta": piece}
                    if choices[0].get("finish_reason") in ("stop", "tool_calls"):
                        break
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"调用 API 失败（{self.base}）：{exc}") from exc

        if calls:
            yield {"tool_calls": [calls[i] for i in sorted(calls)]}

    def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> tuple[str, list[dict]]:
        content: list[str] = []
        calls: list[dict] = []
        for ev in self.chat_events(model=model, num_ctx=num_ctx, tools=tools,
                                   messages=messages):
            if "delta" in ev:
                content.append(ev["delta"])
            elif "tool_calls" in ev:
                calls.extend(ev["tool_calls"])
        return "".join(content).strip(), calls

    def commit(self, user_text: str, answer: str) -> None:
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": answer or ""})

    def commit_messages(self, messages: list[dict]) -> None:
        """把 harness 这一轮的真实轨迹（含 tool / assistant tool_calls）接进历史。

        只接非 system 消息；顺序原样保留，后续 build_messages 才能复用同一前缀。
        """
        for msg in messages or ():
            if str(msg.get("role") or "") in {"user", "assistant", "tool"}:
                self._history.append(dict(msg))

    def chat(self, user_text: str) -> str:
        answer = "".join(self.chat_stream(user_text)).strip()
        self.commit(user_text, answer)
        return answer


# =========================================================================== #
# 门面：按角色在「本地 / API」之间切换（流水线只认这一层）
# =========================================================================== #
class ChatClient:
    """统一门面，接口与 :class:`OllamaClient` 完全一样。

    为什么要有这一层：``pipeline`` 已经把 ``self.llm`` 用得到处都是
    （人设、温度、历史、预热、释放、看图），如果让各处自己判断走哪边，
    迟早会漏一处。这里把「谁是当前后端」收成一处，其余代码不需要知道。
    """

    def __init__(
        self,
        local_cfg: LlmConfig,
        api_cfg: ApiConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.local = OllamaClient(local_cfg)
        self.api_cfg = api_cfg or ApiConfig()
        self.log = logger or logging.getLogger("voice_loop")
        self._api: OpenAICompatClient | None = None
        self._engine = "local"
        self._last_used = "local"
        self._prompt_value: str | None = None
        self._temperature = None

    # ---------------------------------------------------------------- 身份
    @property
    def engine(self) -> str:
        return self._engine

    @property
    def system_prompt(self) -> str | None:
        return self._prompt_value

    @system_prompt.setter
    def system_prompt(self, value: str | None) -> None:
        self._prompt_value = value
        self.local.system_prompt = value
        if self._api is not None:
            self._api.system_prompt = value

    @property
    def temperature(self) -> float | None:
        return self._temperature

    @temperature.setter
    def temperature(self, value: float | None) -> None:
        self._temperature = value
        self.local.temperature = value
        if self._api is not None:
            self._api.temperature = value

    def _system_prompt(self) -> str:
        client = self._api if (self._engine == "api" and self._api) else self.local
        return client._system_prompt()

    @property
    def _history(self) -> list[dict]:
        """当前后端的历史（自测/调试会直接看它，跟 OllamaClient 同名）。"""
        client = self._api if (self._engine == "api" and self._api) else self.local
        return client._history

    def configure(
        self,
        engine: str | None,
        *,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> str:
        """把当前引擎切成 ``local`` 或 ``api``；返回**最终生效**的那个。

        角色卡上写 ``api`` 但 key / 地址没配好时，这里会**退回本地**并打一条警告 ——
        喊醒一个角色却一个字都说不出来，比退回本地糟得多。
        """
        want = str(engine or self.api_cfg.engine or "local").strip().lower()
        if want not in ("local", "api"):
            self.log.warning(f"[AI 引擎] 不认识 {want!r}，按本地处理")
            want = "local"
        if want == "api":
            if not bool(getattr(self.api_cfg, "enabled", True)):
                self.log.warning("[API] [api] enabled = false，这个角色改用本地模型")
                want = "local"
            else:
                overrides: dict = {}
                if model:
                    overrides["model"] = model
                if base_url:
                    overrides["base_url"] = base_url
                if api_key:
                    overrides["api_key"] = api_key
                cfg = dataclasses.replace(self.api_cfg, **overrides)
                client = self._api
                if client is None or client.cfg != cfg:
                    client = OpenAICompatClient(cfg)
                ok, why = client.ready()
                if not ok:
                    self.log.warning(f"[API] {why} —— 这个角色改用本地模型")
                    want = "local"
                else:
                    client.system_prompt = self._prompt_value
                    client.temperature = self._temperature
                    self._api = client
        if want != self._engine:
            self.reset()               # 换了脑子就不继承上一段对话（跟切角色同理）
            self.log.info(f"[AI 引擎] {'本地 Ollama' if want == 'local' else '云端 API'}"
                          + (f"（{self._api.cfg.model}）" if want == "api" and self._api else ""))
        self._engine = want
        self._last_used = want
        return want

    # ---------------------------------------------------------------- 生命周期
    def ensure_model(self) -> None:
        if self._engine == "api" and self._api is not None:
            self._api.ensure_model()
        else:
            self.local.ensure_model()

    def warmup(self, model: str | None = None) -> float:
        if self._engine == "api" and self._api is not None:
            return self._api.warmup(model)
        return self.local.warmup(model)

    def release(self, model: str | None = None) -> bool:
        # 就算当前角色用 API，本地模型仍要给看图/记忆总结用，所以照样释放
        return self.local.release(model)

    def is_loaded(self, model: str | None = None) -> bool | None:
        if self._engine == "api":
            return None
        return self.local.is_loaded(model)

    def list_models(self) -> list[str]:
        return self.local.list_models()

    def resolve_model(self, want: str) -> str:
        return self.local.resolve_model(want)

    # ---------------------------------------------------------------- 对话
    def reset(self) -> None:
        self.local.reset()
        if self._api is not None:
            self._api.reset()

    def build_messages(
        self,
        user_text: str,
        images: list[str] | None = None,
        extra_messages: list[dict] | None = None,
        prefix_messages: list[dict] | None = None,
        system_override: str | None = None,
    ) -> list[dict]:
        """按当前后端构造一份完整的 messages（harness agent 多轮要用）。"""
        client = self._api if (self._engine == "api" and self._api) else self.local
        msgs = [dict(m) for m in client._build_messages(user_text, images, system_override)]
        if extra_messages:
            msgs[-1:-1] = [dict(m) for m in extra_messages]
        if prefix_messages:
            msgs = [dict(m) for m in prefix_messages] + msgs
        return msgs

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
        system_override: str | None = None,
    ) -> Iterator[dict]:
        if self._engine == "api" and self._api is not None and not images:
            yielded = False
            try:
                for ev in self._api.chat_events(
                    user_text, images=images, model=model, num_ctx=num_ctx,
                    tools=tools, extra_messages=extra_messages, messages=messages,
                    prefix_messages=prefix_messages, system_override=system_override,
                ):
                    yielded = True
                    yield ev
                self._last_used = "api"
                return
            except LlmError as exc:
                if yielded:
                    # 已经念出去半句了，再切本地会重复；这一轮只能如实报错
                    raise
                self.log.warning(f"[API] 没通，这一轮改用本地模型：{exc}")
                self._api.reset()       # 云端那份历史接不上这一轮，清掉免得错位
                self._engine = "local"
        for ev in self.local.chat_events(
            user_text, images=images, model=model, num_ctx=num_ctx, tools=tools,
            extra_messages=extra_messages, messages=messages,
            prefix_messages=prefix_messages, system_override=system_override,
        ):
            yield ev
        self._last_used = "local"

    def chat_stream(
        self,
        user_text: str,
        images: list[str] | None = None,
        model: str | None = None,
        num_ctx: int | None = None,
        tools: list[dict] | None = None,
        extra_messages: list[dict] | None = None,
    ) -> Iterator[str]:
        for ev in self.chat_events(
            user_text, images=images, model=model, num_ctx=num_ctx, tools=tools,
            extra_messages=extra_messages,
        ):
            if "delta" in ev:
                yield ev["delta"]

    def commit(self, user_text: str, answer: str) -> None:
        if self._last_used == "api" and self._api is not None:
            self._api.commit(user_text, answer)
        else:
            self.local.commit(user_text, answer)

    def commit_messages(self, messages: list[dict]) -> None:
        client = self._api if (self._last_used == "api" and self._api is not None) else self.local
        commit = getattr(client, "commit_messages", None)
        if callable(commit):
            commit(messages)
        else:  # 老客户端没实现：退化成一条 user + 一条 answer
            user = next((str(m.get("content") or "") for m in messages
                         if m.get("role") == "user"), "")
            answer = next((str(m.get("content") or "") for m in reversed(messages)
                           if m.get("role") == "assistant"), "")
            self.commit(user, answer)

    def chat(self, user_text: str) -> str:
        answer = "".join(self.chat_stream(user_text)).strip()
        self.commit(user_text, answer)
        return answer

    def local_chat(self, user_text: str) -> str:
        """永远走本机的那一条（记忆总结、看图这类不该花云端 token 的活）。"""
        return self.local.chat(user_text)
