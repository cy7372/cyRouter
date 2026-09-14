from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger("cyRouter")


@dataclass
class Message:
    role: str
    content: str | list  # str for simple text, list for multi-block Anthropic content


def flatten_content(content: str | list) -> str:
    """Flatten multi-block content to plain text (for OpenAI providers).

    Note: image blocks are dropped here. Use convert_content_to_openai() for
    providers that support vision, which preserves images as image_url blocks.
    """
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                parts.append(
                    f"[Tool: {block.get('name', '')}({json_safe(block.get('input', {}))})]"
                )
            elif btype == "tool_result":
                rc = block.get("content", "")
                parts.append(
                    f"[Result: {rc if isinstance(rc, str) else json_safe(rc)}]"
                )
            elif btype == "image":
                # Silently drop — flatten_content is for text-only contexts.
                # Vision-capable providers should use convert_content_to_openai.
                pass
            else:
                logger.warning(
                    "flatten_content: dropping unsupported block type %r "
                    "(use vision-capable path for image blocks)",
                    btype,
                )
        else:
            parts.append(str(block))
    return "\n".join(parts)


def convert_content_to_openai(content: str | list) -> str | list:
    """Convert Anthropic content blocks to OpenAI chat format.

    Returns a plain string for text-only content, or a list of OpenAI
    content blocks when images are present:
      - text block  -> {"type": "text", "text": ...}
      - image block -> {"type": "image_url", "image_url": {"url": "data:..."}}
      - tool_use / tool_result -> flattened text placeholders (OpenAI tool
        messages are handled separately by the provider layer)

    Anthropic image source shapes supported:
      - {"type": "base64", "media_type": "...", "data": "..."}
      - {"type": "url", "url": "..."}
    """
    if isinstance(content, str):
        return content

    has_image = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            has_image = True
            break
    if not has_image:
        # No images — fall back to plain-text flattening (cheaper, and keeps
        # tool_use/tool_result placeholders that the provider expects).
        return flatten_content(content)

    parts: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"type": "text", "text": str(block)})
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append({"type": "text", "text": text})
        elif btype == "image":
            source = block.get("source") or {}
            stype = source.get("type")
            if stype == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                if data:
                    parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{data}",
                        },
                    })
                else:
                    logger.warning("image block has empty base64 data, skipping")
            elif stype == "url":
                url = source.get("url", "")
                if url:
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
                else:
                    logger.warning("image block has empty url, skipping")
            else:
                logger.warning(
                    "image block has unsupported source type %r, skipping", stype
                )
        elif btype == "tool_use":
            parts.append({
                "type": "text",
                "text": f"[Tool: {block.get('name', '')}({json_safe(block.get('input', {}))})]",
            })
        elif btype == "tool_result":
            rc = block.get("content", "")
            parts.append({
                "type": "text",
                "text": f"[Result: {rc if isinstance(rc, str) else json_safe(rc)}]",
            })
        else:
            logger.warning(
                "convert_content_to_openai: dropping unsupported block type %r",
                btype,
            )
    return parts


def json_safe(obj) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


# Parameters that may not be supported by all upstream APIs.
# Used by both ClaudeProvider and the Anthropic raw proxy in main.py.
ANTHROPIC_STRIP_PARAMS = {
    "thinking",
    "output_config",
    "context_management",
    "metadata",
    "service_tier",
    "top_k",
}


@dataclass
class ChatRequest:
    model: str
    messages: list[Message]
    temperature: float = 0.7
    max_tokens: int = 4096
    stream: bool = False
    extra: dict = field(default_factory=dict)
    conversation_id: str = ""


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)
    finish_reason: str = "stop"


@dataclass
class StreamChunk:
    delta: str
    model: str
    provider: str
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)


class BaseProvider(ABC):
    def __init__(
        self, name: str, api_key: str, base_url: str | None = None, **kwargs
    ):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.extra = kwargs

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse: ...

    # NOTE: typed as a regular ``def`` returning AsyncIterator, not
    # ``async def``. The concrete providers implement this as an async
    # generator (``async def ... yield``), and calling an async generator
    # function synchronously returns an AsyncIterator — no await needed.
    # Typing it ``async def`` here would make it a *coroutine* returning
    # AsyncIterator, which mismatches the async-generator overrides.
    @abstractmethod
    def chat_stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]: ...

    @abstractmethod
    async def list_models(self) -> list[str]: ...
