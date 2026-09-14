from __future__ import annotations
from collections.abc import AsyncIterator
from typing import cast
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .base import (
    BaseProvider,
    ChatRequest,
    ChatResponse,
    StreamChunk,
    convert_content_to_openai,
)


class OpenAIProvider(BaseProvider):
    def __init__(
        self, name: str, api_key: str, base_url: str | None = None, **kwargs
    ):
        super().__init__(name, api_key, base_url, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            timeout=120.0,
        )

    @staticmethod
    def _convert_messages(messages: list) -> list[dict]:
        # Use convert_content_to_openai so image blocks are preserved as
        # image_url entries for vision-capable downstream models (e.g. qwen-vl,
        # qwen-max). Text-only content still collapses to a plain string.
        return [
            {"role": m.role, "content": convert_content_to_openai(m.content)}
            for m in messages
        ]

    async def chat(self, req: ChatRequest) -> ChatResponse:
        resp = await self.client.chat.completions.create(
            model=req.model,
            messages=cast(
                list[ChatCompletionMessageParam],
                self._convert_messages(req.messages),
            ),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            **req.extra,
        )
        choice = resp.choices[0]
        usage = self._extract_usage(resp.usage) if resp.usage else {}
        return ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            provider=self.name,
            usage=usage,
            finish_reason=choice.finish_reason or "stop",
        )

    @staticmethod
    def _extract_usage(u) -> dict:
        """Extract usage in Anthropic semantics (cache-miss only input_tokens).

        OpenAI's prompt_tokens includes cache hits; we subtract them so the
        downstream `_normalize_usage` / stats code sees a uniform shape.
        Note: OpenAI protocol has no cache_creation concept (caching is
        implicit server-side), so only cache_read is handled here.
        """
        prompt = u.prompt_tokens or 0
        cache_read = (
            getattr(u, "prompt_cache_hit_tokens", None)
            or (
                getattr(u, "prompt_tokens_details", None)
                and u.prompt_tokens_details.cached_tokens
            )
            or 0
        )
        out = {
            "prompt_tokens": prompt,
            "completion_tokens": u.completion_tokens or 0,
            "total_tokens": u.total_tokens or 0,
        }
        if cache_read:
            out["cache_read_input_tokens"] = cache_read
            out["prompt_tokens"] = max(0, prompt - cache_read)
        return out

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        extra = dict(req.extra)
        extra.setdefault("stream_options", {"include_usage": True})
        stream = await self.client.chat.completions.create(
            model=req.model,
            messages=cast(
                list[ChatCompletionMessageParam],
                self._convert_messages(req.messages),
            ),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            stream=True,
            **extra,
        )
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield StreamChunk(
                        delta=delta.content,
                        model=chunk.model,
                        provider=self.name,
                    )
                if chunk.choices[0].finish_reason:
                    yield StreamChunk(
                        delta="",
                        model=chunk.model,
                        provider=self.name,
                        finish_reason=chunk.choices[0].finish_reason,
                    )
            # Usage-only chunk (last chunk when stream_options.include_usage=True)
            if getattr(chunk, "usage", None):
                yield StreamChunk(
                    delta="",
                    model=chunk.model,
                    provider=self.name,
                    usage=self._extract_usage(chunk.usage),
                )

    async def list_models(self) -> list[str]:
        resp = await self.client.models.list()
        return [m.id for m in resp.data]
