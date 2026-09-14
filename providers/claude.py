from __future__ import annotations
from collections.abc import AsyncIterator
import anthropic

from .base import (
    BaseProvider,
    ChatRequest,
    ChatResponse,
    StreamChunk,
    ANTHROPIC_STRIP_PARAMS,
)


def _filter_extra(extra: dict) -> dict:
    return {k: v for k, v in extra.items() if k not in ANTHROPIC_STRIP_PARAMS}


class ClaudeProvider(BaseProvider):
    def __init__(
        self, name: str, api_key: str, base_url: str | None = None, **kwargs
    ):
        super().__init__(name, api_key, base_url, **kwargs)
        client_kwargs: dict = {"api_key": api_key, "timeout": 120.0}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = anthropic.AsyncAnthropic(**client_kwargs)

    @staticmethod
    def _convert_messages(messages: list) -> tuple[str, list[dict]]:
        """Separate system prompt and convert to Claude format."""
        system = ""
        claude_msgs = []
        for m in messages:
            if m.role == "system":
                system = m.content if isinstance(m.content, str) else str(m.content)
            else:
                claude_msgs.append({"role": m.role, "content": m.content})
        return system, claude_msgs

    async def chat(self, req: ChatRequest) -> ChatResponse:
        system, msgs = self._convert_messages(req.messages)
        kwargs: dict = {
            "model": req.model,
            "messages": msgs,
            "max_tokens": req.max_tokens,
        }
        if system:
            kwargs["system"] = system
        if req.temperature != 0.7:
            kwargs["temperature"] = req.temperature
        kwargs.update(_filter_extra(req.extra))

        resp = await self.client.messages.create(**kwargs)
        content = resp.content[0].text if resp.content else ""
        usage = {
            "prompt_tokens": resp.usage.input_tokens,
            "completion_tokens": resp.usage.output_tokens,
            "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0)
            or 0,
            "cache_creation_input_tokens": getattr(
                resp.usage, "cache_creation_input_tokens", 0
            )
            or 0,
        }
        return ChatResponse(
            content=content,
            model=resp.model,
            provider=self.name,
            usage=usage,
            finish_reason=resp.stop_reason or "stop",
        )

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        system, msgs = self._convert_messages(req.messages)
        kwargs: dict = {
            "model": req.model,
            "messages": msgs,
            "max_tokens": req.max_tokens,
        }
        if system:
            kwargs["system"] = system
        if req.temperature != 0.7:
            kwargs["temperature"] = req.temperature
        kwargs.update(_filter_extra(req.extra))

        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield StreamChunk(
                    delta=text,
                    model=req.model,
                    provider=self.name,
                )
            final = await stream.get_final_message()
            usage = {
                "prompt_tokens": final.usage.input_tokens,
                "completion_tokens": final.usage.output_tokens,
                "total_tokens": final.usage.input_tokens + final.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    final.usage, "cache_read_input_tokens", 0
                )
                or 0,
                "cache_creation_input_tokens": getattr(
                    final.usage, "cache_creation_input_tokens", 0
                )
                or 0,
            }
            yield StreamChunk(
                delta="",
                model=final.model,
                provider=self.name,
                finish_reason=final.stop_reason or "stop",
                usage=usage,
            )

    async def list_models(self) -> list[str]:
        return []
