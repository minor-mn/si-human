"""会話に使う LLM（Claude / OpenAI）の切り替え層。

会話履歴は LLM に依存しない形（Turn のリスト）で持ち、問い合わせる直前に各 API の形式へ変換する。
変換は毎回同じ結果になるので、どちらの API でもプロンプトキャッシュが効く。
"""

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

log = logging.getLogger(__name__)

REFUSAL_REPLY = "ごめんなさい、その話題にはお答えできないです。"

Provider = Literal["claude", "openai"]
PROVIDERS: tuple[Provider, ...] = ("claude", "openai")


@dataclass
class Part:
    type: Literal["text", "image"]
    data: str  # text ならテキスト、image なら JPEG の base64


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    parts: list[Part]


class LLM(Protocol):
    name: str

    def stream(self, system: str, turns: list[Turn], cache_key: str) -> AsyncIterator[str]:
        """応答テキストを断片ごとに流す。"""
        ...


class ClaudeLLM:
    def __init__(self) -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
        self.effort = os.environ.get("CLAUDE_EFFORT", "low")
        self.name = f"claude ({self.model})"

    @staticmethod
    def _to_messages(turns: list[Turn]) -> list[dict]:
        messages = []
        for turn in turns:
            if turn.role == "assistant":
                messages.append({"role": "assistant", "content": turn.parts[0].data})
                continue
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": p.data}}
                if p.type == "image"
                else {"type": "text", "text": p.data}
                for p in turn.parts
            ]
            messages.append({"role": "user", "content": content})
        return messages

    async def stream(self, system: str, turns: list[Turn], cache_key: str) -> AsyncIterator[str]:
        kwargs: dict = {}
        if not self.model.startswith("claude-haiku"):
            # 安全分類器による拒否時、サーバー側で別モデルに自動で振り替える（Haiku には無い機能）
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        async with self.client.beta.messages.stream(
            model=self.model,
            # 音声で読み上げる 1〜3 文には十分。思考トークンもここに含まれる
            max_tokens=4096,
            system=system,
            messages=self._to_messages(turns),
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # 直近までの履歴を自動でキャッシュ
            **kwargs,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()

        u = final.usage
        log.info(
            "claude stop=%s in=%s cache_read=%s cache_write=%s out=%s",
            final.stop_reason,
            u.input_tokens,
            u.cache_read_input_tokens,
            u.cache_creation_input_tokens,
            u.output_tokens,
        )
        if final.stop_reason == "refusal":
            yield REFUSAL_REPLY


class OpenAILLM:
    def __init__(self) -> None:
        import openai

        self.client = openai.AsyncOpenAI()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
        self.effort = os.environ.get("OPENAI_REASONING_EFFORT", "low")
        self.name = f"openai ({self.model})"

    @staticmethod
    def _to_input(turns: list[Turn]) -> list[dict]:
        items = []
        for turn in turns:
            if turn.role == "assistant":
                items.append({"role": "assistant", "content": turn.parts[0].data})
                continue
            content = [
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{p.data}", "detail": "low"}
                if p.type == "image"
                else {"type": "input_text", "text": p.data}
                for p in turn.parts
            ]
            items.append({"role": "user", "content": content})
        return items

    async def stream(self, system: str, turns: list[Turn], cache_key: str) -> AsyncIterator[str]:
        stream = await self.client.responses.create(
            model=self.model,
            instructions=system,
            input=self._to_input(turns),
            reasoning={"effort": self.effort},
            max_output_tokens=4096,
            # 同じ会話のリクエストを同じキャッシュに寄せる
            prompt_cache_key=cache_key,
            # 履歴はこちらで持つので OpenAI 側には保存しない
            store=False,
            stream=True,
        )
        refused = False
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield event.delta
            elif event.type == "response.refusal.delta":
                refused = True
            elif event.type == "response.completed":
                u = event.response.usage
                log.info(
                    "openai in=%s cached=%s out=%s",
                    u.input_tokens,
                    u.input_tokens_details.cached_tokens,
                    u.output_tokens,
                )
        if refused:
            yield REFUSAL_REPLY


_instances: dict[Provider, LLM] = {}


def default_provider() -> Provider:
    provider = os.environ.get("LLM_PROVIDER", "claude")
    if provider not in PROVIDERS:
        raise RuntimeError(f"LLM_PROVIDER は {' / '.join(PROVIDERS)} のどれかにしてください: {provider}")
    return provider


def get_llm(provider: Provider) -> LLM:
    # クライアントは最初に使うときに作る（使わない側の API キーは不要）
    if provider not in _instances:
        _instances[provider] = ClaudeLLM() if provider == "claude" else OpenAILLM()
    return _instances[provider]
