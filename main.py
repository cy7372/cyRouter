from __future__ import annotations

import argparse
import asyncio
import copy
import hmac
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    from .breaker import get_breaker
    from .builtin_meta import BUILTIN_MODEL_META
    from .builtin_pricing import (
        BUILTIN_PRICING,
        effective_pricing,
        effective_pricing_map,
    )
    from .builtin_providers import provider_templates_payload
    from .cache import get_cache
    from .config import (
        DATA_DIR,
        DEFAULT_PORT,
        add_provider,
        decrypt_value,
        encrypt_value,
        load_config,
        remove_provider,
        save_config,
    )
    from .pricing_sources import PRICING_SOURCES, extract_all
    from .providers.base import ChatRequest, Message, StreamChunk
    from .reasoning import get_reasoning_cache
    from .redact import (
        ContentRestorer,
        redact_body,
        redact_enabled,
        restore_obj,
    )
    from .router import (
        _get_provider_candidates,
        _get_provider_for_model,
        _log_request,
        _maybe_compact_async,
        _normalize_usage,
        current_key_name,
        get_log_entries,
    )
    from .version import __display_version__
except ImportError:
    from breaker import get_breaker
    from builtin_meta import BUILTIN_MODEL_META
    from builtin_pricing import (
        BUILTIN_PRICING,
        effective_pricing,
        effective_pricing_map,
    )
    from builtin_providers import provider_templates_payload
    from cache import get_cache
    from config import (
        DATA_DIR,
        DEFAULT_PORT,
        add_provider,
        decrypt_value,
        encrypt_value,
        load_config,
        remove_provider,
        save_config,
    )
    from pricing_sources import PRICING_SOURCES, extract_all
    from providers.base import ChatRequest, Message, StreamChunk
    from reasoning import get_reasoning_cache
    from redact import (
        ContentRestorer,
        redact_body,
        redact_enabled,
        restore_obj,
    )
    from router import (
        _get_provider_candidates,
        _get_provider_for_model,
        _log_request,
        _maybe_compact_async,
        _normalize_usage,
        current_key_name,
        get_log_entries,
    )
    from version import __display_version__

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("cyRouter")

# ── Update check（内置更新检查）────────────────────────────
# 启动后查一次 + 每 24h 查自托管 version.json（发布链 build.ps1 自动产出；
# nginx 白名单路径免鉴权）。结果挂在 /health.latest 供上游/监控消费。
# 检查失败静默（离线机器不影响服务）。env CYROUTER_UPDATE_URL 可覆盖端点。
_UPDATE_URL = os.environ.get(
    "CYROUTER_UPDATE_URL", "https://pub.cyyu.me:8443/release/files/version.json"
)
_update_state = {"latest_version": None, "update_available": False,
                 "checked_at": None, "error": None,
                 "hint": "Windows: Setup.exe 覆盖装 / Linux: installer.sh --update"}


def _version_tuple(s: str):
    """'1.2.0.16' → (1,2,0,16)；非数字段容错归 0。"""
    parts = []
    for seg in str(s).lstrip("v").split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


async def _update_check_once() -> None:
    try:
        client = _http_client or _make_async_client(trust_env=True)
        resp = await client.get(_UPDATE_URL, timeout=10.0)
        resp.raise_for_status()
        latest = resp.json().get("version")
        if not latest:
            raise ValueError("manifest 无 version 字段")
        _update_state["latest_version"] = latest
        _update_state["update_available"] = _version_tuple(latest) > _version_tuple(__display_version__)
        _update_state["checked_at"] = time.time()
        _update_state["error"] = None
        if _update_state["update_available"]:
            logger.info("更新可用: %s → %s（%s）", __display_version__, latest, _update_state["hint"])
    except Exception as exc:  # noqa: BLE001 — 更新检查失败不得影响服务
        _update_state["error"] = f"{type(exc).__name__}: {exc}"


async def _update_check_loop() -> None:
    while True:
        await _update_check_once()
        await asyncio.sleep(24 * 3600)

# Silence high-volume third-party loggers: httpx logs every HTTP request at INFO
# (tens of lines per client call), drowning real warnings in server-err.log.
# asyncio WARNING filters Windows ProactorBasePipeTransport connection-lost noise.
for _noisy in ("httpx", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_start_time = time.time()


def _resolve_web_dir() -> Path:
    """Locate the web/ UI directory across dev / onefile / onedir modes.

    - PyInstaller onefile: resources live under sys._MEIPASS (temp unpack dir)
    - PyInstaller onedir : resources sit beside the exe
    - Dev (python main.py): beside this file
    """
    if getattr(sys, "frozen", False):
        # _MEIPASS is the unpack dir in onefile; same as exe dir in onedir.
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = Path(base) / "web"
            if p.exists():
                return p
        # onedir fallback: exe's own directory
        return Path(sys.executable).resolve().parent / "web"
    return Path(__file__).parent / "web"


WEB_DIR = _resolve_web_dir()

# Shared httpx clients with connection pooling.
# Two instances split by network reachability:
#   * _http_client        — direct (trust_env=False). Domestic providers
#     (bigmodel/deepseek/dashscope/...) connect directly. This avoids a stale
#     HTTPS_PROXY env var in the servy session-0 process routing Chinese
#     endpoints through a dead local proxy, which caused ConnectTimeout.
#   * _http_client_proxied — honours env proxy (trust_env=True). Used by
#     international providers (e.g. OpenAI/Anthropic official endpoints) that
#     are unreachable without a proxy from China.
# Selection is automatic via _get_http_client_for_url(url).
_http_client: httpx.AsyncClient | None = None  # direct (domestic)
_http_client_proxied: httpx.AsyncClient | None = None  # via env proxy (intl)

# Remote model cache
_remote_models_cache: dict | None = None
_remote_models_cache_ts: float = 0.0

# Provider quota cache (avoids hitting zhipu's external API on every dashboard load)
_quota_cache: dict | None = None
_quota_cache_ts: float = 0.0
_QUOTA_CACHE_TTL = 300.0  # 5 minutes

# Failed models: {model_name: fail_ts}
# Populated when upstream returns quota/rate-limit errors so auto routing
# skips the model until cooldown expires.
_failed_models: dict[str, float] = {}
_FAILED_MODEL_COOLDOWN = 900.0  # 15 minutes


def _mark_model_failed(model: str):
    if model:
        _failed_models[model] = time.time()
        logger.warning(f"auto: model '{model}' marked failed")


def _is_model_failed(model: str) -> bool:
    if not model:
        return False
    ts = _failed_models.get(model)
    if ts is None:
        return False
    if (time.time() - ts) > _FAILED_MODEL_COOLDOWN:
        _failed_models.pop(model, None)
        return False
    return True


# Slow-but-successful models: {model: (mark_ts, level)} — latency health
# for auto routing (2026-09-14). Slow SUCCESSES are invisible to every
# failure-counting layer: quota marks need errors, the breaker needs
# failures, and an upstream serving 6~32s sluggish 200s sails through
# both (2026-09-14: qwen3.8-flash did exactly that, hanging every
# latency-sensitive consumer — pre-ask judge dialogs, status judges —
# while auto kept routing to it). Marked from small-output NON-STREAM
# completions only (actual usage tokens gate, see _note_latency): long
# generations and streaming TTFB (thinking models legitimately take 60s+
# to first token) can never trip it. Auto routes to the tier fallback for
# the cooldown; expiry restores the primary via the existing
# restore-to-primary path.
_slow_models: dict[str, tuple[float, int]] = {}
_SLOW_MARK_BASE_COOLDOWN = 120.0  # seconds; escalates x2 per repeat mark
_SLOW_MARK_MAX_LEVEL = 5  # cap: 120 * 2^5 = 64min ceiling


def _slow_cooldown_s(level: int) -> float:
    return _SLOW_MARK_BASE_COOLDOWN * (2 ** (max(level, 1) - 1))


def _mark_model_slow(model: str, elapsed_ms: int, threshold_ms: int) -> None:
    if not model:
        return
    now = time.time()
    level = 1
    prev = _slow_models.get(model)
    if prev is not None:
        prev_ts, prev_level = prev
        # Escalate while marks keep recurring inside ~4 base windows;
        # a long quiet stretch decays the level back to 1.
        level = (
            min(prev_level + 1, _SLOW_MARK_MAX_LEVEL)
            if (now - prev_ts) < _SLOW_MARK_BASE_COOLDOWN * 4
            else 1
        )
    _slow_models[model] = (now, level)
    logger.warning(
        "auto: model '%s' marked slow (%dms >= %dms, cooldown %.0fs)",
        model,
        elapsed_ms,
        threshold_ms,
        _slow_cooldown_s(level),
    )


def _is_model_slow(model: str) -> bool:
    if not model:
        return False
    ent = _slow_models.get(model)
    if ent is None:
        return False
    ts, level = ent
    if (time.time() - ts) > _slow_cooldown_s(level):
        _slow_models.pop(model, None)  # expired — self-heal on read
        return False
    return True


def _note_latency(model: str, elapsed_ms: int, usage: dict | None) -> None:
    """Latency health signal feeding _slow_marks. Called on non-stream
    SUCCESS paths only. Gates on the response's ACTUAL output tokens
    (usage), never on the request's max_tokens — auto_elevate_max_tokens
    rewrites body.max_tokens in place (8 -> 32768), which would silently
    opt every judge call out of the gate. Threshold via auto.slow_mark_ms
    (0 disables); token ceiling via auto.slow_mark_max_output_tokens.
    Never raises: latency health must not touch the request path.
    """
    try:
        if not model or usage is None or elapsed_ms is None:
            return
        out_tokens = 0
        if isinstance(usage, dict):
            out_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
        if out_tokens <= 0:
            return  # no/zero usage info — never mark on a guess
        cfg = load_config()
        auto_cfg = (cfg.get("auto") or {}) if isinstance(cfg, dict) else {}
        threshold_ms = int(auto_cfg.get("slow_mark_ms", _AUTO_DEFAULTS["slow_mark_ms"]))
        max_out = int(
            auto_cfg.get(
                "slow_mark_max_output_tokens",
                _AUTO_DEFAULTS["slow_mark_max_output_tokens"],
            )
        )
        if threshold_ms <= 0 or max_out <= 0:
            return
        if out_tokens > max_out:
            return  # real generation — latency is legit, not sickness
        if elapsed_ms >= threshold_ms:
            _mark_model_slow(model, elapsed_ms, threshold_ms)
    except Exception:
        pass


# Per-(provider, model) fatal-error tracking for provider fallback. When a
# provider hits a fatal error (quota exhausted / rate limited) for a model,
# skip it on subsequent requests until cooldown expires and go straight to the
# next fallback candidate — avoids paying one failing round-trip per request.
# Value is (failed_at, cooldown_seconds): quota exhaustion lasts much longer
# than rate limiting (quota typically resets per billing cycle).
_failed_provider_models: dict[tuple[str, str], tuple[float, float]] = {}


def _provider_fallback_cooldown(error_type: str = "") -> float:
    try:
        cfg = load_config()
        if error_type == "insufficient_quota":
            return float(cfg.get("provider_quota_cooldown", 3600))
        return float(cfg.get("provider_fallback_cooldown", 900))
    except Exception:
        return 3600.0 if error_type == "insufficient_quota" else 900.0


def _mark_provider_model_failed(
    provider_name: str, model: str, cooldown: float | None = None
):
    if provider_name and model:
        secs = cooldown if cooldown is not None else _provider_fallback_cooldown()
        _failed_provider_models[(provider_name, model)] = (time.time(), secs)
        logger.warning(
            "fallback: provider '%s' marked failed for model '%s' (cooldown %.0fs)",
            provider_name,
            model,
            secs,
        )


def _is_provider_model_failed(provider_name: str, model: str) -> bool:
    if not provider_name or not model:
        return False
    entry = _failed_provider_models.get((provider_name, model))
    if entry is None:
        return False
    ts, cooldown = entry
    if (time.time() - ts) > cooldown:
        _failed_provider_models.pop((provider_name, model), None)
        return False
    return True


# Background task registry — retain references so GC doesn't cancel fire-and-forget
# tasks mid-execution (per asyncio.create_task docs).
_background_tasks: set = set()


def _spawn_background(coro):
    """Schedule a fire-and-forget task that survives GC.

    Holds a strong reference until the task completes, then auto-discards.
    Exceptions are logged (not raised) so they don't propagate to the caller.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _get_proxy_url(provider_name: str | None = None) -> str | None:
    """Resolve the proxy URL for a provider's upstream.

    Priority (per provider):
      1. provider config ``proxy`` field — a string URL, or ``true`` to use
         the global proxy below.
      2. config.json top-level ``proxy`` field.
      3. HTTPS_PROXY/HTTP_PROXY env vars.

    The config fields are essential for the packaged service: cyRouter runs
    as a LocalSystem Windows service, which does NOT inherit the user's shell
    env (so ``trust_env=True`` alone never sees the user's proxy).  Without
    an explicit proxy some international providers (e.g. opencode-go's
    region-restricted models) get rejected with 403.
    """
    if provider_name:
        try:
            p_cfg = _find_provider_cfg(provider_name)
            pv = (p_cfg or {}).get("proxy")
            if isinstance(pv, str) and pv.strip():
                return pv.strip()
        except Exception:
            pass
    try:
        cfg = load_config()
        p = (cfg.get("proxy") or "").strip()
        if p:
            return p
    except Exception:
        pass
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


_DOMESTIC_HOSTS = frozenset(
    {
        "open.bigmodel.cn",  # 智谱 (Zhipu/GLM)
        "api.deepseek.com",  # DeepSeek
        "dashscope.aliyuncs.com",  # 阿里通义 (Qwen)
        "dashscope-intl.aliyuncs.com",  # 阿里国际 (still Alibaba infra)
        "token-plan.cn-beijing.maas.aliyuncs.com",  # 阿里百炼 Token Plan
        "api.moonshot.cn",  # 月之暗面 (Kimi)
        "api.minimaxi.com",  # MiniMax
        "yi.01.ai",  # 零一万物 (Yi)
        "api.baichuan-ai.com",  # 百川
        "api.lingyiwanwu.com",  # 百度/其他
    }
)


def _hostname(url: str | None) -> str:
    """Lowercased hostname of a URL ('' when unparsable)."""
    from urllib.parse import urlparse

    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _is_domestic_url(url: str | None) -> bool:
    """True if url points at a domestic (China) host reachable without proxy.

    Used to pick between the direct and proxied httpx clients. Domestic
    endpoints connect directly (faster, and immune to local-proxy outages);
    international endpoints honour HTTPS_PROXY.
    """
    if not url:
        return True  # safe default: unknown → direct (domestic is the norm)
    host = _hostname(url)
    if not host:
        return True
    if host in _DOMESTIC_HOSTS:
        return True
    # .cn / .com.cn TLD → domestic (covers most Chinese providers generically)
    if host.endswith(".cn") or host.endswith(".com.cn"):
        return True
    return False


def _provider_uses_proxy(provider_name: str | None, url: str | None = None) -> bool:
    """Decide whether a provider's upstream should go through a proxy.

    Rules:
      * provider config ``proxy`` explicitly set → follow it (true/false/URL).
      * otherwise automatic: domestic (China) hosts connect directly,
        international hosts use the proxy when one is configured.
    """
    if provider_name:
        try:
            p_cfg = _find_provider_cfg(provider_name)
            pv = (p_cfg or {}).get("proxy")
            if pv is True:
                return True
            if pv is False:
                return False
            if isinstance(pv, str) and pv.strip():
                return True
        except Exception:
            pass
    if _is_domestic_url(url):
        return False
    return _get_proxy_url(provider_name) is not None


# ── TCP keepalive（上游连接）──────────────────────────────────────
# 动机：kimi 上游存在 ~30s 零字节静默杀流（2026-08 取证：idle_gap≈30s，
# 无错误帧无 [DONE]，TCP 直接关闭）。杀流者可能是 L4 中间盒（NAT/云 LB
# 流状态表老化——openai-python #3269/#3503 同款问题，官方 SDK 靠缩短
# TCP keepalive 探测修复），也可能是 kimi 自己的 L7 网关按应用层空闲
# 计时。对前者，OS 默认 2h 才发首个探测 → 连接在中间盒眼里早已死；
# 收紧到 15s 首探测 / 5s 间隔（远小于 30s 杀流阈值）可持续刷新流状态。
# 对 L7 无效（探测包不携带应用字节）——由黑盒 close_style 取证区分
# （FIN=应用层主动关 / RST=中间盒或崩溃）。
# 开关：config.tcp_keepalive = {"enabled": bool, "idle_seconds": int,
# "interval_seconds": int, "count": int}，缺省全开 15/5/3。
_TCP_KEEPALIVE_DEFAULTS = {
    "enabled": True,
    "idle_seconds": 15,  # 静默 15s 后发首个探测（杀流阈值 ~30s，留一倍余量）
    "interval_seconds": 5,
    "count": 3,
}


def _keepalive_socket_options() -> list[tuple[int, int, int]]:
    """SO_KEEPALIVE + 收紧探测参数，跨平台（Windows/Linux/macOS）。

    Windows 10 1709+ 与 Linux 共享 TCP_KEEPIDLE/TCP_KEEPINTVL/TCP_KEEPCNT
    （单位秒，微软文档确认）；macOS 的首探测时间叫 TCP_KEEPALIVE。属性不存在
    时跳过，至少保证 SO_KEEPALIVE 本身打开。
    """
    cfg = {
        **_TCP_KEEPALIVE_DEFAULTS,
        **(load_config().get("tcp_keepalive") or {}),
    }
    opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    if not cfg.get("enabled", True):
        return opts
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(socket, "TCP_KEEPALIVE", None)  # macOS 命名
    if idle_opt is not None:
        opts.append((socket.IPPROTO_TCP, idle_opt, int(cfg["idle_seconds"])))
    intvl_opt = getattr(socket, "TCP_KEEPINTVL", None)
    if intvl_opt is not None:
        opts.append((socket.IPPROTO_TCP, intvl_opt, int(cfg["interval_seconds"])))
    cnt_opt = getattr(socket, "TCP_KEEPCNT", None)
    if cnt_opt is not None:
        opts.append((socket.IPPROTO_TCP, cnt_opt, min(int(cfg["count"]), 255)))
    return opts


def _make_async_client(
    *, trust_env: bool, proxy: str | None = None
) -> httpx.AsyncClient:
    """统一的上游 AsyncClient 工厂：socket 层带 TCP keepalive。

    proxy/trust_env 必须挂在 transport 上（httpx 不允许 transport 与
    AsyncClient(proxy=...) 同传）。超时与旧实现一致：read=300s 是给推理模型
    首字节前的长思考留余量（曾因 30s 假设引发真实负载 ReadTimeout）。
    """
    transport = httpx.AsyncHTTPTransport(
        trust_env=trust_env,
        proxy=proxy,
        socket_options=_keepalive_socket_options(),
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=15.0),
    )


def _stream_close_exc_style(exc: BaseException) -> str:
    """分类流读异常的连接关闭方式（FIN/RST 取证，配合 keepalive 实验）。

    'rst'        — 连接被重置（ConnectionResetError / WinError 10054）：
                   指向中间盒或上游 worker 崩溃，TCP keepalive 可能有效。
    'fin_abrupt' — 对端发了干净 FIN 但 HTTP 体不完整（httpcore
                   RemoteProtocolError "peer closed connection"）：应用层
                   主动关连接，keepalive 探测包救不了。
    'other'      — 其余（超时、TLS 错误等）。
    遍历异常链（raise-from / context），防环。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionResetError):
            return "rst"
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    text = str(exc).lower()
    if "connection reset" in text or "10054" in text or "econnreset" in text:
        return "rst"
    if "peer closed connection" in text:
        return "fin_abrupt"
    return "other"


def _get_http_client() -> httpx.AsyncClient:
    """Direct client (trust_env=False) for domestic providers."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = _make_async_client(trust_env=False)
    return _http_client


def _get_http_client_proxied() -> httpx.AsyncClient:
    """Proxied client for international providers.

    Uses the config ``proxy`` URL when present, else falls back to
    HTTPS_PROXY/HTTP_PROXY env vars.  See :func:`_get_proxy_url`.
    """
    global _http_client_proxied
    if _http_client_proxied is None or _http_client_proxied.is_closed:
        proxy = _get_proxy_url()
        if proxy:
            logger.info("proxied client using config proxy: %s", proxy)
            _http_client_proxied = _make_async_client(trust_env=True, proxy=proxy)
        else:
            logger.info("proxied client using env proxy")
            _http_client_proxied = _make_async_client(trust_env=True)
    return _http_client_proxied


def _get_http_client_for_url(
    url: str | None = None, provider_name: str | None = None
) -> httpx.AsyncClient:
    """Pick the httpx client for a provider request.

    A provider's explicit ``proxy`` config wins; otherwise domestic hosts
    connect directly and international hosts use the proxy (when configured).
    """
    if not _provider_uses_proxy(provider_name, url):
        return _get_http_client()
    return _get_http_client_proxied()


# Per-request timeout for non-streaming calls. The shared httpx client uses
# read=300s (for reasoning models that think long before first byte on
# streaming), but a non-streaming POST waiting 300s means a hung upstream
# ties up a connection for 5 minutes. 120s is generous for any real model.
_NONSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=15.0)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _http_client, _http_client_proxied
    _http_client = _make_async_client(trust_env=False)
    # Proxied client: apply the configured proxy when present. Essential
    # for the packaged LocalSystem service, which has no shell proxy env, so
    # a trust_env-only client would connect directly and time out on
    # international upstreams (e.g. opencode-go region-restricted models).
    # Falls back to trust_env=True so interactive/dev runs still pick up
    # HTTPS_PROXY. Must mirror _get_http_client_proxied()'s logic, otherwise
    # the eager singleton here starves that lazy builder of its config-proxy
    # branch (the bug that caused gpt-5.6-luna ConnectTimeout).
    _lifespan_proxy = _get_proxy_url()
    if _lifespan_proxy:
        _http_client_proxied = _make_async_client(trust_env=True, proxy=_lifespan_proxy)
    else:
        _http_client_proxied = _make_async_client(trust_env=True)
    # Fire-and-forget availability probing for multi-provider model chains
    # (opt-in via config "probe.enabled"; loop self-suspends when disabled).
    _spawn_background(_probe_loop())
    # Live usage readings for opted-in providers (local_quota.fetch_usages,
    # e.g. Kimi Code's /usages) — feeds the quota bars with real numbers
    # instead of local estimation.
    _spawn_background(_usages_fetch_loop())
    # Daily official-price check (config.pricing_check.auto, opt-in; loop
    # self-suspends when disabled). Refreshes the hint cache and, when
    # pricing_check.auto_adopt is on, adopts CNY single-value diffs into
    # existing user pricing entries within guard rails.
    _spawn_background(_pricing_check_loop())
    # Update check：启动即查 + 24h 周期（/health.latest 暴露）
    _spawn_background(_update_check_loop())

    yield
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    if _http_client_proxied and not _http_client_proxied.is_closed:
        await _http_client_proxied.aclose()
    _http_client_proxied = None


app = FastAPI(title="cyRouter", description="LLM API Router", lifespan=_lifespan)

_MAX_BODY = 10 * 1024 * 1024  # 10MB


def _build_cors_origins() -> list[str]:
    """Build CORS allowed origins from config, falling back to localhost defaults."""
    defaults = [
        "http://localhost",
        "http://localhost:20100",
        "http://127.0.0.1",
        "http://127.0.0.1:20100",
    ]
    try:
        cfg = load_config()
        extra = cfg.get("cors_origins", [])
        if isinstance(extra, list):
            return defaults + [str(o) for o in extra]
    except Exception:
        pass
    return defaults


app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health check ─────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __display_version__,
        "timestamp": time.time(),
        "uptime_seconds": int(time.time() - _start_time),
        "latest": _update_state,
    }


# ── Router key auth middleware ──────────────────────────────────────────


@app.middleware("http")
async def admin_api_guard(request: Request, call_next):
    """Require X-Admin-Token on every /api/* management endpoint.

    2026-09-14: D02 became a multi-user AI server — an unauthenticated
    localhost admin API means any local user can create/reveal/revoke keys
    and rewrite routing config.  Token auto-generated in config.json
    (config.load_config) or set via CYROUTER_ADMIN_TOKEN env.
    """
    if request.url.path.startswith("/api/"):
        admin = os.environ.get("CYROUTER_ADMIN_TOKEN") or load_config().get(
            "admin_token", ""
        )
        if not admin:
            return JSONResponse(
                {"error": "admin_token not configured"}, status_code=503
            )
        if request.headers.get("x-admin-token", "") != admin:
            return JSONResponse(
                {"error": "invalid or missing X-Admin-Token"}, status_code=403
            )
    return await call_next(request)


@app.middleware("http")
async def redaction_count_middleware(request: Request, call_next):
    """Stamp X-Cy-Redacted-Count on responses whose request body was masked.

    Set by ``_redact_request_body`` onto ``request.state.cy_redacted_n``.
    Header absent == nothing was redacted.  This is the observability half of
    the redaction design: masked surrogates are plausible-looking on purpose,
    so without this header an agent session whose context was rewritten in
    transit has no way to notice (2026-09-07 incident: tool-output UUIDs were
    silently swapped and it took ~40 minutes to trace back to this layer).
    """
    response = await call_next(request)
    n = getattr(request.state, "cy_redacted_n", None)
    if n:
        response.headers["X-Cy-Redacted-Count"] = str(n)
    return response


@app.middleware("http")
async def body_limit_middleware(request: Request, call_next):
    """Reject requests exceeding the max body size before reading the body."""
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > _MAX_BODY:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": "Request body too large",
                            "type": "payload_error",
                        }
                    },
                )
        except ValueError:
            pass
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/v1/", "/anthropic/")):
        cfg = load_config()
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token:
            token = request.headers.get("x-api-key", "")

        matched = False
        for k in cfg.get("api_keys", []):
            if isinstance(k, dict) and hmac.compare_digest(token, k.get("key", "")):
                matched = True
                request.state.key_name = k.get("name", "")
                break

        if not matched:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {"message": "Invalid router key", "type": "auth_error"}
                },
            )
        current_key_name.set(getattr(request.state, "key_name", ""))
    response = await call_next(request)
    await _maybe_compact_async()
    return response


async def _openai_compat_sse_stream(
    provider,
    req: ChatRequest,
    original_model: str,
    start_time: float,
    log_entry: dict,
    *,
    aiter_override=None,
):
    """Convert an Anthropic-compatible provider's stream to OpenAI SSE format.

    aiter_override: optional pre-built async iterator of StreamChunk. When
    set (e.g. a _chain_chunks wrapping a gated iterator), replaces
    provider.chat_stream(req). Used by the empty-output retry path so the
    first-token gate's buffered chunks are replayed before live iteration.
    """
    output_parts: list[str] = []
    usage_data: dict = {}
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    finish_reason_sent = False  # set when upstream emits a finish_reason chunk

    async def _flush(status: str, error: str | None = None):
        log_entry["status"] = status
        log_entry["output"] = "".join(output_parts)
        log_entry["latency_ms"] = int((time.time() - start_time) * 1000)
        if usage_data:
            log_entry["usage"] = _sdk_usage_to_log(usage_data)
        if error:
            log_entry["error"] = error
        await _log_request(log_entry)

    try:
        source = (
            aiter_override if aiter_override is not None else provider.chat_stream(req)
        )
        async for chunk in source:
            if chunk.delta:
                output_parts.append(chunk.delta)
                data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(start_time),
                    "model": original_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk.delta},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            if chunk.usage:
                usage_data = chunk.usage
            if chunk.finish_reason:
                finish_reason_sent = True
                data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(start_time),
                    "model": original_model,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": chunk.finish_reason}
                    ],
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        # Native passthrough: do NOT synthesize a finish_reason chunk when
        # upstream omitted one. Synthesizing "stop" masked upstream's
        # malformed stream as a clean completion. Forward [DONE] as-is;
        # clients observe upstream's true behaviour.
        # Final chunk with usage
        if usage_data:
            usage_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(start_time),
                "model": original_model,
                "choices": [],
                "usage": {
                    "prompt_tokens": usage_data.get("prompt_tokens", 0),
                    "completion_tokens": usage_data.get("completion_tokens", 0),
                    "total_tokens": usage_data.get("total_tokens", 0),
                },
            }
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        await _flush("ok" if finish_reason_sent else "finish_reason_missing")
    except asyncio.CancelledError:
        logger.info("stream cancelled by client (model=%s)", original_model)
        await _flush("cancelled")
        raise
    except Exception as e:
        logger.error(f"OpenAI compat stream error: {e}")
        await _flush("error", error=str(e))
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'router_error'}})}\n\n"


# ── OpenAI-compatible API ─────────────────────────────────────────────


def _openai_usage_to_anthropic(usage: dict) -> dict:
    """Convert a raw OpenAI-format upstream usage dict to Anthropic semantics.

    Only call this on usage dicts taken DIRECTLY from an upstream HTTP
    response (the raw-proxy paths). SDK providers are expected to return
    already-normalised usage and must not go through this function.

    OpenAI providers report cache hits under different field names:
      - DeepSeek: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
      - OpenAI standard: `prompt_tokens_details.cached_tokens`

    Two upstream conventions exist for prompt_tokens:
      - OpenAI: prompt_tokens INCLUDES cache-hit portion (total input)
      - DeepSeek: prompt_tokens EXCLUDES cache-hit portion (cache-miss only)

    We detect the DeepSeek convention when cache tokens >= prompt_tokens
    (they can't be a subset if they're larger), and normalise accordingly
    so the output always follows Anthropic semantics (input_tokens =
    cache-miss only, cache tokens tracked separately).

    No-op when the upstream reports no cache fields.
    """
    if not usage:
        return {}
    prompt = usage.get("prompt_tokens", 0) or 0

    cache_read = (
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

    if cache_read + cache_creation > 0 and cache_read >= prompt:
        # DeepSeek convention: prompt_tokens is cache-miss only, doesn't
        # include cache hits. The real cache-miss input is just prompt.
        input_tokens = prompt
    else:
        # OpenAI convention: prompt_tokens includes cache-hit portion.
        # Subtract to get cache-miss only (Anthropic semantics).
        input_tokens = max(0, prompt - cache_read - cache_creation)

    out = {
        "input_tokens": input_tokens,
        "output_tokens": usage.get("completion_tokens", 0) or 0,
    }
    if cache_read:
        out["cache_read_input_tokens"] = cache_read
    if cache_creation:
        out["cache_creation_input_tokens"] = cache_creation
    return out


def _sdk_usage_to_log(usage: dict) -> dict:
    """Convert a SDK provider's usage dict into the canonical log shape.

    Both OpenAIProvider and ClaudeProvider already normalise to Anthropic
    semantics (input_tokens = cache-miss only) before returning, so here we
    only need to copy fields through and rename `prompt_tokens` →
    `input_tokens` if that's the only key present.
    """
    if not usage:
        return {}
    out = {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0,
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0))
        or 0,
    }
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cc = usage.get("cache_creation_input_tokens", 0) or 0
    if cr:
        out["cache_read_input_tokens"] = cr
    if cc:
        out["cache_creation_input_tokens"] = cc
    return out


def _find_provider_cfg(provider_name: str) -> dict | None:
    cfg = load_config()
    for p_cfg in cfg.get("providers", []):
        if p_cfg["name"] == provider_name:
            return p_cfg
    return None


def _apply_param_clamp(body: dict, provider_name: str) -> None:
    """Apply a provider's `param_clamp` / `param_drop` / `param_map` config to the request body.

    Some upstreams hard-reject certain sampling params:
      - kimi's coding endpoint only allows temperature=1 (and the allowed
        value *changes* over time — was 0.6, now 1). Omitting temperature
        always works, so `param_drop` is more robust than `param_clamp` for
        kimi. Configure on the provider:

        "param_clamp": {"temperature": 0.6}   # force a fixed value
        "param_drop": ["temperature"]         # delete the field entirely

    Some upstreams reject only certain *values* while keeping the field
    (e.g. volcano's glm-5.3 rejects reasoning_effort:"none" — "this model
    always thinks" — but accepts low/high/max). Translate the value instead
    of dropping the field, so the client's intent (minimal thinking)
    survives failover to that provider:

        "param_map": {"reasoning_effort": {"none": "low"}}

    Cross-vocabulary thinking-disable translation (2026-09-14): GLM-5.x
    upstreams "always think" and only understand effort levels, but many
    clients express the disable intent in another dialect — anthropic-style
    ``thinking: {"type": "disabled"}`` or qwen-style ``enable_thinking:
    false``. Those fields are dropped/stripped upstream and the intent is
    silently lost: the model then burns the whole max_tokens budget on
    reasoning before emitting any visible content (pi's judge hit this
    30+/day as "truncated at max_tokens"). Opt-in per provider:

        "thinking_disable_translate": "none"   # volcano: param_map then → low
        "thinking_disable_translate": "low"    # zhipu: rejects "none" outright

    When the disable intent is detected and the client has not set
    ``reasoning_effort`` itself, it is translated to the configured effort
    level and the original fields removed. Runs BEFORE drop/map/clamp so
    the existing per-provider steps keep adapting the value.

    Order: `thinking_disable_translate` → `param_drop` → `param_map` →
    `param_clamp` (clamp stays the final authority). Only string values are
    translated; unmapped values pass through untouched. Mutates `body` in
    place.
    Called per-candidate so each provider in a chain can be tuned separately
    (e.g. kimi code + opencode-go are both kimi-backed and both need
    temperature dropped).
    """
    p_cfg = _find_provider_cfg(provider_name)
    if not p_cfg:
        return
    translate_effort = p_cfg.get("thinking_disable_translate")
    if translate_effort:
        disabled = False
        _t = body.get("thinking")
        if isinstance(_t, dict) and _t.get("type") == "disabled":
            disabled = True
        if body.get("enable_thinking") is False:
            disabled = True
        if disabled:
            if "reasoning_effort" not in body:
                body["reasoning_effort"] = str(translate_effort)
            body.pop("thinking", None)
            body.pop("enable_thinking", None)
    drop = p_cfg.get("param_drop")
    if isinstance(drop, list):
        for key in drop:
            body.pop(key, None)
    mapping = p_cfg.get("param_map")
    if isinstance(mapping, dict):
        for key, rules in mapping.items():
            if key in body and isinstance(rules, dict):
                cur = body[key]
                if isinstance(cur, str):
                    rewritten = rules.get(cur)
                    if rewritten is not None:
                        body[key] = rewritten
    clamp = p_cfg.get("param_clamp")
    if isinstance(clamp, dict):
        for key, val in clamp.items():
            body[key] = val


def _retry_after_seconds(status_code: int, upstream_headers) -> int | None:
    """Compute a Retry-After hint to send back to the client.

    Order of preference:
      1. Upstream's own Retry-After header (if present).
      2. Smart defaults by status: 429→15s (give zhipu time to recover from
         rate limiting — too short and clients retry-storm), 5xx→5s, 529→5s.
    Returns None when no hint is appropriate (e.g. 4xx client errors).
    """
    ra = upstream_headers.get("retry-after") if upstream_headers else None
    if ra:
        try:
            return max(1, int(ra))
        except (TypeError, ValueError):
            pass
    if status_code in (429,):
        return 15
    if status_code in (500, 502, 503, 529):
        return 5
    return None


# Patterns that signal rate-limit / overload / quota errors in response bodies.
# Used to detect "soft errors" where upstream returns HTTP 200 with an error
# payload (GLM/DeepSeek do this for 1305 / "访问量过大"), and to normalize
# ambiguous 4xx/5xx bodies into OpenAI-standard error.type values that
# opencode's retryTransient logic recognises.
_ERROR_CLASSIFICATION = [
    (
        (
            "rate_limit",
            "rate limit",
            "rate-limit",
            "too many requests",
            "该模型当前访问量过大",
            "访问量过大",
            "1305",
            "1304",
        ),
        429,
        "rate_limit_exceeded",
    ),
    (
        (
            "overloaded",
            "model is at capacity",
            "service unavailable",
            "currently unavailable",
            "服务暂时不可用",
            "模型当前访问量",
        ),
        503,
        "overloaded_error",
    ),
    (
        (
            "insufficient_quota",
            "quota exceeded",
            "exceeded your current quota",
            "余额不足",
            "account balance is insufficient",
        ),
        429,
        "insufficient_quota",
    ),
    (
        (
            # Kimi Code billing-cycle exhaustion: HTTP 403 + type
            # "access_terminated_error". Not matching insufficient_quota's
            # literal patterns, it used to fall through the 4xx passthrough —
            # never fatal-marked, so auto stayed pinned on kimi-k3 and the
            # provider chain never skipped "kimi code" (2026-08-14).
            "access_terminated_error",
            "reached your usage limit",
            "usage limit for this billing cycle",
            "quota will be refreshed",
        ),
        403,
        "insufficient_quota",
    ),
]


def _classify_error_body(body_text: str) -> tuple[int, str] | None:
    """Return (http_status, error_type) if body matches a known error pattern."""
    if not body_text:
        return None
    body_lower = body_text.lower()
    for patterns, status, etype in _ERROR_CLASSIFICATION:
        for p in patterns:
            if p.lower() in body_lower:
                return status, etype
    return None


def _normalize_upstream_error(
    body_text: str, status_code: int
) -> tuple[dict, int, str | None]:
    """Produce a normalized OpenAI-shape error from upstream body + status.

    Handles three cases:
      1. Soft error: status 200 but body has "error" JSON → infer status from body.
      2. Retryable status (408/409/429/5xx): set proper error.type if missing.
      3. Non-retryable 4xx: pass through with type=upstream_error.

    Returns (normalized_body, final_status, original_code).
    """
    try:
        body_json = json.loads(body_text)
    except Exception:
        body_json = {"error": {"message": body_text[:500]}}

    orig_error = body_json.get("error", {}) if isinstance(body_json, dict) else {}
    if not isinstance(orig_error, dict):
        orig_error = {"message": str(orig_error)}
    orig_code = str(orig_error.get("code", "")) if orig_error.get("code") else None
    orig_msg = orig_error.get("message") or body_text[:200]

    classified = _classify_error_body(body_text)
    if classified:
        final_status, error_type = classified
    elif status_code in (408, 409):
        final_status, error_type = status_code, "server_error"
    elif status_code == 429:
        final_status, error_type = 429, "rate_limit_exceeded"
    elif status_code in (500, 502):
        final_status, error_type = status_code, "server_error"
    elif status_code in (503, 529):
        # Anthropic uses 529 for overloaded; OpenAI clients only retry 5xx,
        # so map 529→503 for opencode compatibility.
        final_status, error_type = 503, "overloaded_error"
    elif status_code == 504:
        final_status, error_type = 504, "server_error"
    else:
        # Non-retryable 4xx (400/401/403/404/...): pass through verbatim.
        return (
            body_json
            if isinstance(body_json, dict)
            else {"error": {"message": str(body_json)}},
            status_code,
            orig_code,
        )

    new_error = {"type": error_type, "message": orig_msg}
    if orig_code:
        new_error["code"] = orig_code
    return {"error": new_error}, final_status, orig_code


# Fatal-error classification + marking is unified in _cb_fail (structured
# fields → keyword table → status), which every failure path funnels through.


def _upstream_error_response(resp, *, anthropic_shape: bool):
    """Build a JSONResponse that mirrors the upstream error so the client SDK
    can parse it natively, plus a Retry-After header when applicable.

    Uses _normalize_upstream_error so the body has a proper `error.type` field
    (rate_limit_exceeded / overloaded_error / insufficient_quota / ...) that
    opencode's retryTransient logic recognises, instead of relying on the
    upstream's own (often missing or non-standard) type field.

    For Anthropic-shape responses (Claude Code), maps OpenAI-style error types
    to Anthropic-standard ones so the Anthropic SDK recognises them and applies
    its built-in retry/backoff logic:
      - rate_limit_exceeded → rate_limit_error
      - overloaded_error    → overloaded_error (already standard)
      - server_error        → api_error

    Fatal-error marking is NOT done here — callers funnel through _cb_fail
    (2026-09-04: the old _check_fatal_error double-marked with a weaker
    keyword-only classifier ahead of _cb_fail's unified one).
    """
    norm_body, final_status, _ = _normalize_upstream_error(resp.text, resp.status_code)
    if anthropic_shape:
        # Anthropic wraps differently: {"type":"error","error":{...}}
        err_inner = norm_body.get("error", norm_body)
        if isinstance(err_inner, dict):
            # Map OpenAI-style types to Anthropic-standard so the SDK's
            # retry logic (keyed off error.type) actually fires.
            _ANTHROPIC_TYPE_MAP = {
                "rate_limit_exceeded": "rate_limit_error",
                "overloaded_error": "overloaded_error",
                "server_error": "api_error",
                "upstream_timeout": "overloaded_error",
            }
            current_type = err_inner.get("type")
            if current_type in _ANTHROPIC_TYPE_MAP:
                err_inner = {**err_inner, "type": _ANTHROPIC_TYPE_MAP[current_type]}
        err_body = {"type": "error", "error": err_inner}
    else:
        err_body = norm_body
    headers = {}
    ra = _retry_after_seconds(final_status, resp.headers)
    if ra is not None:
        headers["Retry-After"] = str(ra)
    return JSONResponse(err_body, status_code=final_status, headers=headers)


def _timeout_error_response(exc: Exception, *, anthropic_shape: bool):
    """Map httpx timeout/connect errors to a 504 + SDK-friendly body so the
    client treats it as a transient upstream issue and retries."""
    msg = str(exc) or exc.__class__.__name__
    if anthropic_shape:
        err_body = {
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": f"upstream timeout: {msg[:200]}",
            },
        }
    else:
        err_body = {
            "error": {
                "message": f"upstream timeout: {msg[:200]}",
                "type": "upstream_timeout",
            },
        }
    # 504 Gateway Timeout with Retry-After so SDKs back off rather than hammer
    # a hung upstream.
    return JSONResponse(err_body, status_code=504, headers={"Retry-After": "5"})


def _sdk_exception_response(exc: Exception, *, anthropic_shape: bool):
    """Convert an OpenAI/Anthropic SDK exception into a proper upstream-style
    error response. Without this, SDK errors (RateLimitError 429,
    InternalServerError 529, etc.) get squashed into a generic 502 Bad Gateway,
    which breaks client retry logic because the original status/code/type is lost.
    """
    # OpenAI SDK exceptions carry status_code + response body
    status_code = getattr(exc, "status_code", None) or 502
    raw_body = getattr(exc, "body", None)
    # Anthropic SDK uses .error attribute
    err_attr = getattr(exc, "error", None)

    if anthropic_shape:
        if isinstance(raw_body, dict) and "error" in raw_body:
            err_body = raw_body
        elif isinstance(err_attr, dict):
            err_body = {"type": "error", "error": err_attr}
        else:
            err_type = (
                "rate_limit_error"
                if status_code == 429
                else (
                    "overloaded_error"
                    if status_code in (502, 503, 529)
                    else "api_error"
                )
            )
            err_body = {
                "type": "error",
                "error": {"type": err_type, "message": str(exc)[:300]},
            }
    else:
        if isinstance(raw_body, dict) and "error" in raw_body:
            err_body = raw_body
        elif isinstance(raw_body, dict):
            err_body = {"error": raw_body}
        else:
            err_type = (
                "rate_limit_error"
                if status_code == 429
                else (
                    "overloaded_error"
                    if status_code in (502, 503, 529)
                    else "api_error"
                )
            )
            err_body = {"error": {"message": str(exc)[:300], "type": err_type}}

    headers = {}
    ra = _retry_after_seconds(status_code, None)
    if ra is not None:
        headers["Retry-After"] = str(ra)
    # Map 529→503 on the OpenAI path so ai-sdk/OpenAI SDK auto-retries.
    # See _upstream_error_response for rationale.
    final_status = status_code
    if not anthropic_shape and final_status == 529:
        final_status = 503
    return JSONResponse(err_body, status_code=final_status, headers=headers)


# ── Empty-output detection & retry helpers ────────────────────────────
# SDK retryTransient is HTTP-layer only (408/409/429/5xx) — a 200 response
# with empty content slips through as "success" and breaks the user's
# workflow. These helpers detect strictly-empty responses (per LiteLLM's
# reasoning_content standard: content + reasoning_content + tool_calls all
# empty) and let the endpoint handlers retry in-line.


class EmptyStreamError(Exception):
    """Sentinel raised by SDK-compat stream gates when no content arrives
    within the gate timeout. Caught by the retry loop in endpoint handlers."""


# ── Circuit breaker helpers ───────────────────────────────────────────
def _cb_check(
    provider: str, model: str, *, last_candidate: bool = False
) -> JSONResponse | None:
    """Return a 503 if the circuit is open, else None.

    Envoy panic-mode semantics: rejecting the LAST candidate would turn the
    breaker into a self-inflicted total outage (the 2026-08-26 incident: a
    single-provider model ate 5 instant 503s during a 120s OPEN window).
    When `last_candidate` is set we route anyway and let the real outcome
    speak — success closes the circuit early (report_success handles OPEN),
    failure escalates the cooldown. Fail-fast only happens when the chain
    still has alternatives to try.
    """
    b = get_breaker()
    if not b.enabled or b.allow(provider, model):
        return None
    if last_candidate:
        logger.info(
            "breaker: panic mode — last candidate %s|%s open, routing anyway",
            provider,
            model,
        )
        return None
    rem = int(b.cooldown_remaining(provider, model))
    return JSONResponse(
        {
            "error": {
                "type": "circuit_open",
                "message": (
                    f"provider {provider} is temporarily unavailable "
                    f"(circuit open, retry after ~{max(rem, 1)}s)"
                ),
            }
        },
        status_code=503,
        headers={"Retry-After": str(max(rem, 1))},
    )


def _cb_ok(provider: str, model: str):
    b = get_breaker()
    if b.enabled:
        b.report_success(provider, model)


# ── Unified upstream-failure classification (LiteLLM-style) ──────────
# One classifier, one decision table, every path. Structured error.type /
# error.code fields are the primary signal — they are API contract and far
# more stable than message copy (Kimi rewrites its billing-cycle message
# anytime; "access_terminated_error" is a type name they won't rename
# lightly). The legacy message keyword table stays as fallback, HTTP status
# is the last resort.
_QUOTA_TYPE_RE = re.compile(
    r"quota|terminat|exhaust|billing|insufficient|payment|余额不足",
    re.I,
)
_RATE_LIMIT_TYPE_RE = re.compile(r"rate.?limit|too.?many.?request", re.I)


def _classify_upstream_failure(
    *, status: int | None = None, exc: Exception | None = None, body_text: str = ""
) -> str:
    """Routing category of an upstream failure.

    Returns one of: quota / rate_limit / auth / not_found / timeout /
    server / network / client. See _cb_fail for the decision table.

    Precedence: structured error.type/error.code (body JSON or SDK
    exception attributes) → legacy message keyword table → HTTP status.
    No signal at all → "network": _cb_fail is only invoked on REAL
    failures (timeouts, stream kills, connection errors), which belong in
    the circuit breaker.
    """
    etype = ecode = ""
    code: int | None = None
    if body_text:
        try:
            j = json.loads(body_text)
            err = j.get("error") if isinstance(j, dict) else None
            if isinstance(err, dict):
                etype = str(err.get("type") or "")
                ecode = str(err.get("code") or "")
        except Exception:
            pass
    if exc is not None:
        code = getattr(exc, "status_code", None)
        if not etype:
            etype = str(getattr(exc, "type", "") or "")
        if not ecode:
            ecode = str(getattr(exc, "code", "") or "")
        exc_body = getattr(exc, "body", None)
        if isinstance(exc_body, dict):
            err = exc_body.get("error")
            if isinstance(err, dict):
                etype = etype or str(err.get("type") or "")
                ecode = ecode or str(err.get("code") or "")
    if status is not None:
        code = status

    # 1. Structured fields (API contract)
    field = f"{etype} {ecode}"
    if field.strip():
        if _RATE_LIMIT_TYPE_RE.search(field):
            return "rate_limit"
        if _QUOTA_TYPE_RE.search(field):
            return "quota"
    # 2. Legacy message keyword table
    if body_text:
        hit = _classify_error_body(body_text)
        if hit:
            _, error_type = hit
            return "quota" if error_type == "insufficient_quota" else "rate_limit"
    # 3. HTTP status
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code is not None:
            if code == 429:
                return "rate_limit"  # body didn't say quota → plain rate limit
            if code in (401, 403):
                return "auth"
            if code == 404:
                return "not_found"
            if code == 408:
                return "timeout"
            if 500 <= code < 600:
                return "server"
            if 400 <= code < 500:
                return "client"
    return "network"  # timeout / connection error / stream kill / unknown


def _cb_fail(
    provider: str,
    model: str,
    *,
    status: int | None = None,
    exc: Exception | None = None,
    body_text: str = "",
):
    """Unified failure handling: classify once, decide once.

    Every failure path (raw OpenAI / raw Anthropic / SDK / gate-probe)
    funnels here with whatever context it has; the decision table is
    LiteLLM-routing parity:

      quota          → provider 3600s mark + model mark (billing cycle)
      rate_limit     → provider 900s mark + model mark
      auth/not_found → provider 900s mark only (dead key / dead model for
                       THIS provider; LiteLLM cools 401/404 as
                       non-retryable errors — model health stays intact so
                       other providers on the chain keep serving)
      server/timeout/network → circuit breaker (escalating cooldown)
      client (other 4xx) → nothing: request-side problem, fail fast
    """
    cat = _classify_upstream_failure(status=status, exc=exc, body_text=body_text)
    if cat == "quota":
        _mark_provider_model_failed(
            provider,
            model,
            cooldown=_provider_fallback_cooldown("insufficient_quota"),
        )
        _mark_model_failed(model)
    elif cat == "rate_limit":
        _mark_provider_model_failed(provider, model)
        _mark_model_failed(model)
    elif cat in ("auth", "not_found"):
        _mark_provider_model_failed(provider, model)
    elif cat in ("server", "timeout", "network"):
        b = get_breaker()
        if b.enabled:
            b.report_failure(provider, model)
    # "client": not an upstream-health signal — do nothing.


def _cache_try(body: dict) -> JSONResponse | None:
    """Return a cached response if available, else None."""
    c = get_cache()
    if not c.enabled:
        return None
    cached = c.get(body)
    if cached is None:
        return None
    return JSONResponse(cached, headers={"X-Cache": "HIT"})


def _payload_looks_empty(data) -> bool:
    """Shape-agnostic empty guard for the cache layer.

    Probes the payload's shape by its distinguishing key (OpenAI: "choices";
    Anthropic: "content" list / type=="message"). Unknown shapes pass through
    untouched — the cache guard must never drop a response it can't classify.
    """
    if not isinstance(data, dict):
        return False
    if "choices" in data:
        return _is_empty_response(data, anthropic_shape=False)
    if "content" in data or data.get("type") == "message":
        return _is_empty_response(data, anthropic_shape=True)
    return False


def _cache_store(body: dict, response: JSONResponse):
    c = get_cache()
    if not c.enabled:
        return
    try:
        import json as _json

        raw = bytes(response.body).decode()
        data = _json.loads(raw)
        # Empty-response guard (2026-09-12): the ok_empty fall-through hands a
        # genuinely-empty 200 straight to the cache; served from cache it
        # would bypass ALL retry layers (cache sits first in the pipeline).
        if _payload_looks_empty(data):
            return
        c.set(body, data)
    except Exception:
        pass


def _redact_request_body(
    body: dict, request: Request | None = None
) -> tuple[dict, dict[str, str]]:
    """Mask embedded API keys/secrets before forwarding to the upstream provider.

    Returns ``(body_for_upstream, mapping)``. When redaction is disabled (or
    nothing matches) the body is returned unchanged and the mapping is empty.

    Callers invoke this *after* ``_cache_try`` so the cache key is derived from
    the pristine (original) body — two requests differing only in the real
    secret embedded in an otherwise-identical prompt won't share a cache entry.
    The returned body is a deep copy; the caller's ``body`` is never mutated.

    When ``request`` is given and anything was masked, the count is stashed on
    ``request.state.cy_redacted_n`` — the redaction-count middleware stamps it
    onto the response as ``X-Cy-Redacted-Count`` so clients (and agents) can
    see that this request's context was rewritten in transit.
    """
    cfg = load_config()
    if not redact_enabled(cfg):
        return body, {}
    rc = cfg.get("secret_redaction") or {}
    redacted, mapping = redact_body(body, rc)
    if mapping and request is not None:
        try:
            request.state.cy_redacted_n = len(mapping)
        except Exception:
            pass
    return redacted, mapping


async def _restored_stream(agen, redaction_map):
    """Wrap a stream generator: restore redacted secrets in streamed content.

    The upstream LLM may split a placeholder across many content deltas, each
    wrapped in its own SSE/JSON event. A byte-level wrapper cannot reassemble
    it: the JSON structural bytes between deltas (``"}}]\n\ndata: {..."})
    interrupt the placeholder's byte continuity, so neither a full-match
    ``replace`` nor a trailing-prefix hold-back ever sees the complete
    surrogate — the fragments leak straight to the client.

    Fix: buffer the byte stream into complete SSE lines, parse each ``data:``
    line's JSON, feed the extracted content text through per-stream
    :class:`ContentRestorer` instances (which concatenate clean deltas and
    hold back only the trailing surrogate-prefix run), and re-emit the line
    with restored content. Lines without content pass through verbatim.
    Agnostic to whether the inner generator yields ``str`` or ``bytes``, and
    to OpenAI vs Anthropic event shapes.
    """
    if not redaction_map:
        async for chunk in agen:
            b = (
                bytes(chunk)
                if isinstance(chunk, (bytes, bytearray))
                else str(chunk).encode()
            )
            if b:
                yield b
        return

    restorers: dict[str, ContentRestorer] = {}

    def _get(key: str) -> ContentRestorer:
        r = restorers.get(key)
        if r is None:
            r = ContentRestorer(redaction_map)
            restorers[key] = r
        return r

    buf = b""
    async for chunk in agen:
        b = (
            bytes(chunk)
            if isinstance(chunk, (bytes, bytearray))
            else str(chunk).encode()
        )
        buf += b
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            out = _restore_sse_line(line + b"\n", _get)
            if out:
                yield out
    if buf:
        out = _restore_sse_line(buf, _get)
        if out:
            yield out

    # Release any text still held back (e.g. a surrogate whose tail arrived in
    # the final delta). Rare; only happens when a stream ends mid-surrogate.
    for key, r in restorers.items():
        tail = r.flush()
        if tail:
            yield _tail_content_chunk(tail, key)


def _restore_sse_line(line_bytes: bytes, get_restorer) -> bytes:
    """Restore surrogate→original in the content fields of one SSE line.

    Returns the (possibly re-serialised) line bytes. Non-``data:`` lines and
    JSON-unparseable payloads pass through unchanged.
    """
    stripped = line_bytes.rstrip(b"\r\n")
    newline = line_bytes[len(stripped) :]
    payload = _sse_data_payload(stripped)
    if payload is None:
        return line_bytes
    if not payload or payload == b"[DONE]":
        return line_bytes
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line_bytes
    if not isinstance(data, dict):
        return line_bytes

    modified = False
    # OpenAI: choices[0].delta.{content, reasoning_content} and streamed
    # tool-call argument fragments.
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        if isinstance(delta, dict):
            for field in ("content", "reasoning_content"):
                v = delta.get(field)
                if isinstance(v, str) and v:
                    r = get_restorer(f"openai:{field}").feed(v)
                    if r != v:
                        delta[field] = r
                        modified = True
            # tool_calls[].function.arguments is assembled incrementally:
            # each delta carries a fragment, not the cumulative value. Restore
            # per call index so a surrogate split across fragments reassembles
            # — otherwise a redacted secret echoed into a tool call leaks the
            # surrogate to the client (e.g. a write_file tool call) while the
            # matching content text is correctly restored.
            tcs = delta.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str) and args:
                        idx = tc.get("index", 0)
                        r = get_restorer(f"openai:tc:{idx}").feed(args)
                        if r != args:
                            fn["arguments"] = r
                            modified = True
    # Anthropic: delta.text (text_delta) / delta.thinking (thinking_delta) /
    # delta.partial_json (input_json_delta — streamed tool-use input).
    delta = data.get("delta")
    if isinstance(delta, dict):
        dtype = delta.get("type")
        field = None
        if dtype == "text_delta":
            field = "text"
        elif dtype == "thinking_delta":
            field = "thinking"
        if field is not None:
            v = delta.get(field)
            if isinstance(v, str) and v:
                r = get_restorer(f"anthropic:{field}").feed(v)
                if r != v:
                    delta[field] = r
                    modified = True
        elif dtype == "input_json_delta":
            pj = delta.get("partial_json")
            if isinstance(pj, str) and pj:
                idx = data.get("index", 0)
                r = get_restorer(f"anthropic:tc:{idx}").feed(pj)
                if r != pj:
                    delta["partial_json"] = r
                    modified = True

    if not modified:
        return line_bytes
    new_payload = json.dumps(data, ensure_ascii=False)
    return b"data: " + new_payload.encode("utf-8") + newline


def _tail_content_chunk(text: str, key: str) -> bytes:
    """Emit held-back text as a final delta in the stream's format.

    Only reached when a stream ends with a not-yet-complete surrogate prefix
    in the buffer. The text is either a (truncated) surrogate fragment or
    coincidental prose — never the real secret — so emitting it leaks nothing.
    """
    # Streamed tool-call argument fragment (OpenAI / Anthropic). The key is
    # "<provider>:tc:<index>"; re-emit it as the same incremental argument
    # shape the upstream used so the client reassembles it identically.
    if ":tc:" in key:
        provider, _, idx = key.partition(":tc:")
        try:
            idx_i = int(idx)
        except (TypeError, ValueError):
            idx_i = 0
        if provider == "anthropic":
            evt = {
                "type": "content_block_delta",
                "index": idx_i,
                "delta": {"type": "input_json_delta", "partial_json": text},
            }
            return (
                b"event: content_block_delta\ndata: "
                + json.dumps(evt, ensure_ascii=False).encode("utf-8")
                + b"\n\n"
            )
        evt = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": idx_i, "function": {"arguments": text}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
        return b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8") + b"\n\n"
    if key.startswith("anthropic:"):
        field = "text" if key.endswith(":text") else "thinking"
        dtype = "text_delta" if field == "text" else "thinking_delta"
        evt = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": dtype, field: text},
        }
        return (
            b"event: content_block_delta\ndata: "
            + json.dumps(evt, ensure_ascii=False).encode("utf-8")
            + b"\n\n"
        )
    field = "content" if key.endswith(":content") else "reasoning_content"
    evt = {"choices": [{"index": 0, "delta": {field: text}, "finish_reason": None}]}
    return b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _effective_gate_timeout(body: dict, cfg: dict) -> float:
    """Gate timeout, extended when thinking may be active.

    SDK providers (ClaudeProvider) use text_stream which filters out
    thinking events.  If thinking is enabled (or not explicitly disabled),
    the gate deadline must be at least as long as thinking_only_timeout so
    legitimate thinking phases don't trigger empty-retry loops.
    """
    base = float(cfg.get("empty_retry_timeout", 90))
    thinking = body.get("thinking")
    disabled = isinstance(thinking, dict) and thinking.get("type") == "disabled"
    if disabled:
        return base
    thinking_to = float(cfg.get("thinking_only_timeout", 120) or 0)
    return max(base, thinking_to)


def _is_empty_response(result: dict, *, anthropic_shape: bool) -> bool:
    """Return True iff the response carries NO user-visible content.

    Strict definition (matches LiteLLM's industry standard and avoids
    mis-retrying GLM-5.x thinking-mode responses whose text lands in
    reasoning_content / thinking blocks):
      - OpenAI shape: choices[0].message.{content, reasoning_content,
        tool_calls} ALL empty/missing.
      - Anthropic shape: every block in the content list is empty — no
        type=text with non-empty text, no type=tool_use, no type=thinking
        with non-empty thinking. A redacted_thinking block is treated as
        actionable whenever present (its payload is hidden, not absent).
    Tool-call-only responses are NOT empty (legitimate tool-only turn).
    """
    if not isinstance(result, dict):
        return True
    if anthropic_shape:
        blocks = result.get("content")
        if not isinstance(blocks, list) or not blocks:
            return True
        # A response is non-empty if it has ANY content block with substance:
        # text, tool_use, or thinking (thinking is a first-class content block
        # per Anthropic protocol, matching OpenAI path's reasoning_content
        # handling). Only truly empty blocks → empty response.
        has_actionable = False
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text" and b.get("text"):
                has_actionable = True
                break
            if btype == "tool_use":
                has_actionable = True
                break
            if btype == "thinking" and b.get("thinking"):
                has_actionable = True
                break
            if btype == "redacted_thinking":
                # redacted payload is hidden, not absent — presence = content.
                has_actionable = True
                break
        return not has_actionable
    # OpenAI shape
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    if msg.get("content"):
        return False
    if msg.get("reasoning_content"):
        return False
    tool_calls = msg.get("tool_calls")
    return not (isinstance(tool_calls, list) and tool_calls)


def _sse_data_payload(line: bytes) -> bytes | None:
    """Payload bytes of an SSE ``data:`` line — the space is OPTIONAL.

    WHATWG SSE spec: after the field colon, "If value starts with a U+0020
    SPACE character, remove it from value" — exactly one optional space.
    Most upstreams emit ``data: {...}``, but kimi coding's Anthropic
    endpoint emits ``data:{...}`` (2026-09-08 incident: every line failed
    the hard-coded ``b"data: "`` prefixes, the first-token gate read 11KB
    of buffered bytes as "no content", retried 3× and surfaced
    empty_response 502 for every Anthropic-protocol streaming request
    through that provider). Returns None for non-data lines.
    """
    if line.startswith(b"data: "):
        return line[6:]
    if line.startswith(b"data:"):
        return line[5:]
    return None


def _sse_line_has_content(line: bytes, *, anthropic_shape: bool) -> bool:
    """Return True iff one SSE `data:` line carries a content-bearing event.

    OpenAI shape: delta.content / delta.reasoning_content truthy, or
      delta.tool_calls non-empty list.
    Anthropic shape: delta.type ∈ {text_delta, thinking_delta,
      input_json_delta}, or content_block.type == tool_use.

    NOTE: deliberately NOT reusing _observe_line (main.py:1653+) — it does
    not recognise thinking_delta, which would cause thinking-mode streams
    to be mis-classified as empty. _observe_line's gap is a pre-existing
    logging-only issue, left untouched here.
    """
    payload = _sse_data_payload(line)
    if payload is None:
        return False
    try:
        data = json.loads(payload.decode(errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if anthropic_shape:
        delta = data.get("delta", {}) or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            return bool(delta.get("text"))
        if dtype == "thinking_delta":
            # thinking is a legitimate content block (Anthropic protocol).
            # Treat as content so the stream gate doesn't time out and
            # trigger 4.5min of pointless retries. Matches OpenAI path's
            # handling of reasoning_content.
            return True
        if dtype == "input_json_delta":
            # partial_json may legitimately be empty on the first delta of a
            # tool-use arg — the type itself signals "content has started".
            return True
        cb = data.get("content_block", {}) or {}
        return cb.get("type") == "tool_use"
    # OpenAI shape — delta is the choices[0].delta object
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta", {}) or {}
        if delta.get("content"):
            return True
        if delta.get("reasoning_content"):
            return True
        tc = delta.get("tool_calls")
        if isinstance(tc, list) and tc:
            return True
    return False


def _empty_response_jsonresponse(
    *,
    anthropic_shape: bool,
    attempts: int = 0,
) -> JSONResponse:
    """Terminal 502 returned after all empty-output retries are exhausted.

    Body shape mirrors _normalize_upstream_error so downstream SDKs see a
    consistent error.type field. attempts is included in the message for
    diagnosis (how many tries we made before giving up).
    """
    suffix = f" after {attempts} attempts" if attempts > 0 else ""
    if anthropic_shape:
        err_body = {
            "type": "error",
            "error": {
                "type": "empty_response",
                "message": f"upstream returned empty content{suffix}",
            },
        }
    else:
        err_body = {
            "error": {
                "type": "empty_response",
                "message": f"upstream returned empty content{suffix}",
            },
        }
    return JSONResponse(err_body, status_code=502)


def _annotate_final_error(resp, provider_name: str, tried_providers: list):
    """供应商链穷尽（最终候选也失败）时的错误归因——客户端透明性。

    链上有多个 provider 时，最终错误响应原本只带上游原始信息，客户端无法
    分辨错误来自哪家、前面已失败过几家。这里：
      1. 加 X-CyRouter-Provider / X-CyRouter-Tried 响应头（机器可查）
      2. 在 error.message 末尾追加可读归因（pi/opencode 会直接显示 message）
    上游原始 status/type/结构完全保留，只在 message 末尾追加一句。
    """
    if not isinstance(resp, JSONResponse) or resp.status_code < 400:
        return resp
    resp.headers["X-CyRouter-Provider"] = provider_name
    if len(tried_providers) > 1:
        resp.headers["X-CyRouter-Tried"] = " -> ".join(tried_providers)
    try:
        data = json.loads(bytes(resp.body).decode("utf-8"))
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            tried_txt = (
                f"；链上已尝试 {' → '.join(tried_providers)}"
                if len(tried_providers) > 1
                else ""
            )
            err["message"] += (
                f"（cyRouter：此错误来自供应商「{provider_name}」{tried_txt}）"
            )
            # Strip stale content-length/content-type inherited from the
            # *original* error response: we just appended text to
            # error.message, so the body is now longer than the old
            # content-length. Carrying the old value makes uvicorn raise
            # "Response content longer than Content-Length". Let
            # JSONResponse recompute both headers from the new body.
            new_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "content-type")
            }
            return JSONResponse(data, status_code=resp.status_code, headers=new_headers)
    except Exception:
        pass
    return resp


def _truncation_notice_text(
    *,
    provider_label: str,
    elapsed: float,
    idle_gap: float,
    up_headers: dict,
    close_style: str = "fin",
) -> str:
    """截断流里注入的 ⚠️ 诊断文本（OpenAI / Anthropic 路径共用，极简单行）。

    格式：who + 耗时｜分类｜[request-id]｜已触发自动恢复。
    分类：idle_gap ≥ 25s → 疑似网关空闲超时杀流；< 25s → 疑似上游崩溃或过载。
    close_style 是 TCP 层关闭方式取证（2026-09 keepalive 实验对照信号）：
    FIN = 对端应用层主动关（keepalive 无效）；RST = 连接被重置（中间盒或
    崩溃，keepalive 可能有效）；仅在非默认时标注，避免噪声。
    细节（产出字符数、max_tokens、请求体、上游响应头）一律在 data/blackbox/ 黑盒里。
    """
    if idle_gap >= 25:
        mode = f"断流前 {idle_gap:.0f}s 零字节，疑似网关空闲超时杀流"
    else:
        mode = f"断流前 {idle_gap:.1f}s 仍有数据，疑似上游崩溃或过载"
    _style_tag = {"rst": "（RST）", "fin_abrupt": "（FIN）"}.get(close_style, "")
    mode += _style_tag
    req_id = next(
        (
            v
            for k, v in up_headers.items()
            if "request" in k.lower() and "id" in k.lower()
        ),
        None,
    )
    rid = f"｜request-id {req_id} 可报障" if req_id else ""
    return (
        f"\n\n> ⚠️ cyRouter：{provider_label} 静默断流，耗时 {elapsed:.1f}s"
        f"｜{mode}"
        f"{rid}"
        f"｜已触发自动恢复"
    )


# ── Streaming pre-probe ────────────────────────────────────────────────
# Streaming responses (FastAPI StreamingResponse) commit to HTTP 200 the
# moment they're returned — the status code can't be changed mid-stream.
# That means errors detected inside the stream generator arrive as in-band
# SSE error events on a 200 response, and opencode/ai-sdk's retryTransient
# (which is HTTP-layer: fires on 408/409/429/5xx) never triggers.
#
# _probe_upstream_stream solves this by sending the upstream request with
# stream=True, reading response headers + the first body chunk, and deciding
# BEFORE we return StreamingResponse:
#   - non-200 → normalise body → return JSONResponse with proper status
#   - 200 + JSON error body (GLM/DeepSeek "soft-error" 1305/1304) → same
#   - 200 + genuine SSE → hand the partially-consumed stream back to the
#     caller, who wraps it in a StreamingResponse that forwards first_chunk
#     then the remaining bytes.


class ProbedStream:
    """Result of probing an upstream stream request.

    Exactly one of `error_response` / streaming fields is set:
      - error_response: JSONResponse to return directly (status 429/503/...).
      - first_chunk + byte_iter + response: hand these to the stream forwarder.
        Caller MUST close `response` when done (the forwarder does this in its
        finally block).
    """

    __slots__ = ("error_response", "first_chunk", "byte_iter", "response")

    def __init__(self):
        self.error_response: JSONResponse | None = None
        self.first_chunk: bytes = b""
        self.byte_iter: AsyncIterator[bytes] | None = None  # upstream byte iterator
        self.response: httpx.Response | None = None


async def _probe_upstream_stream(
    upstream_url: str,
    headers: dict,
    body: dict,
    *,
    anthropic_shape: bool,
    log_entry: dict,
    start_time: float,
    provider_name: str = "",
    model: str = "",
) -> ProbedStream:
    """Probe an upstream streaming response for errors before forwarding.

    Returns a ProbedStream. If .error_response is set, the caller returns it
    directly. Otherwise the caller forwards .first_chunk + .byte_iter as a
    StreamingResponse and closes .response when done.
    """
    result = ProbedStream()
    client = _get_http_client_for_url(upstream_url, provider_name)
    req = client.build_request("POST", upstream_url, headers=headers, json=body)
    response = await client.send(req, stream=True)

    async def _finalize_error(
        body_text: str, status_code: int, *, is_soft: bool
    ) -> JSONResponse:
        """Build normalised error JSONResponse and close the upstream stream."""
        await response.aclose()
        # Fatal-error marking is the caller's job via _cb_fail (unified
        # classifier) — no separate guard here.
        norm_body, final_status, _ = _normalize_upstream_error(body_text, status_code)
        err = norm_body.get("error", {}) if isinstance(norm_body, dict) else {}
        log_entry["status"] = "error"
        prefix = "[soft-error-200] " if is_soft else ""
        log_entry["error"] = (
            f"{prefix}{final_status} {err.get('type', '?')}: {err.get('message', '')[:200]}"
        )
        log_entry["latency_ms"] = int((time.time() - start_time) * 1000)
        await _log_request(log_entry)
        out_headers = {}
        ra = _retry_after_seconds(final_status, response.headers)
        if ra is not None:
            out_headers["Retry-After"] = str(ra)
        if anthropic_shape:
            err_inner = (
                norm_body.get("error", norm_body)
                if isinstance(norm_body, dict)
                else {"message": str(norm_body)}
            )
            return JSONResponse(
                {"type": "error", "error": err_inner},
                status_code=final_status,
                headers=out_headers,
            )
        return JSONResponse(norm_body, status_code=final_status, headers=out_headers)

    try:
        # Case 1: non-200 status — read full body and return normalised error.
        if response.status_code != 200:
            error_bytes = await response.aread()
            error_text = error_bytes.decode(errors="replace")
            result.error_response = await _finalize_error(
                error_text, response.status_code, is_soft=False
            )
            return result

        # Case 2: HTTP 200 — peek first chunk to detect soft-error (JSON body
        # instead of SSE). GLM's 1305/1304 and DeepSeek sometimes return this
        # shape: the connection is HTTP 200 but the body is a single JSON line
        # like {"error":{"code":"1305","message":"该模型当前访问量过大"}}.
        byte_iter = response.aiter_bytes()
        first_chunk = b""
        try:
            first_chunk = await asyncio.wait_for(byte_iter.__anext__(), timeout=60.0)
        except StopAsyncIteration:
            pass  # upstream closed without sending anything; treat as empty
        except asyncio.TimeoutError:
            pass  # no data yet but stream alive — proceed (forwarder will heartbeat)

        if first_chunk:
            text = first_chunk.decode(errors="replace").lstrip()
            # Real SSE chunks start with "data:", ":", "event:", "id:" or blank.
            # A leading "{" strongly suggests a bare JSON body (soft-error).
            if text.startswith("{"):
                # Soft-error JSON bodies are small (<1KB typically) but may
                # arrive split across chunks. Read up to 8KB total looking for
                # a parseable error JSON; if we see a newline first or exceed
                # the budget, fall through to streaming (it's real SSE).
                accumulated = first_chunk
                peek = None
                for _ in range(8):
                    try:
                        peek = json.loads(accumulated.decode(errors="replace").lstrip())
                        break
                    except json.JSONDecodeError:
                        # Incomplete JSON — try to read more bytes.
                        if b"\n" in accumulated:
                            # A newline means this is SSE data, not bare JSON.
                            peek = None
                            break
                        try:
                            more = await asyncio.wait_for(
                                byte_iter.__anext__(), timeout=5.0
                            )
                            accumulated += more
                            if len(accumulated) > 8192:
                                break  # too big — not a soft-error
                        except (StopAsyncIteration, asyncio.TimeoutError):
                            break
                # Heuristic: JSON with "error" key and no "choices" / SSE "data"
                # field is a soft-error. Read the rest and finalise.
                if (
                    isinstance(peek, dict)
                    and "error" in peek
                    and "choices" not in peek
                    and "data" not in peek
                ):
                    rest = b""
                    async for chunk in byte_iter:
                        rest += chunk
                    full_text = (accumulated + rest).decode(errors="replace")
                    result.error_response = await _finalize_error(
                        full_text, 200, is_soft=True
                    )
                    return result
                # Not a confirmed soft-error: rebase first_chunk to include
                # whatever we read while probing, so the stream forwarder sees
                # the full upstream byte sequence.
                first_chunk = accumulated

        # Case 3: SSE error event — upstream sent an in-band error via SSE
        # (e.g. zhipu rate_limit_error 1302/1305 as event:error). These
        # arrive as HTTP 200 + SSE, so the HTTP-layer error detection above
        # can't catch them. Detect by looking for "type":"error" in the
        # first chunk without "type":"message_start" — a normal stream always
        # starts with message_start.
        if (
            first_chunk
            and b'"type":"error"' in first_chunk
            and b'"type":"message_start"' not in first_chunk
        ):
            rest = b""
            async for chunk in byte_iter:
                rest += chunk
                if len(rest) > 8192:
                    break
            full_text = (first_chunk + rest).decode(errors="replace")
            result.error_response = await _finalize_error(full_text, 502, is_soft=False)
            return result

        # Case 4: genuine SSE stream — hand off to caller for forwarding.
        result.first_chunk = first_chunk
        result.byte_iter = byte_iter
        result.response = response
        return result
    except Exception:
        # Any failure during probe: close the stream and re-raise so the
        # caller can return a generic error response.
        with suppress(Exception):
            await response.aclose()
        raise


async def _gate_probe_for_content(
    probed: ProbedStream,
    *,
    anthropic_shape: bool,
    gate_timeout: float,
    log_entry: dict,
    start_time: float,
) -> ProbedStream:
    """First-token gate for raw-proxy streams.

    Buffers upstream bytes until a content-bearing SSE event is observed
    (text_delta / thinking_delta / input_json_delta / tool_use for Anthropic;
    delta.content / delta.reasoning_content / delta.tool_calls for OpenAI).
    Once seen, repacks probed.first_chunk with the full buffered prefix and
    returns — the caller then forwards via _raw_*_sse_stream as usual.

    On timeout / EOF without content: closes the upstream, sets
    probed.error_response to a 502 empty_response, writes a retry_empty log
    line, and returns. The caller's retry loop decides whether to retry or
    surface the error to the client.

    Wall-clock deadline (not per-chunk timeout) so that trickle-ping streams
    can't extend the gate indefinitely.
    """
    deadline = time.time() + gate_timeout
    accumulated = bytearray(probed.first_chunk)
    byte_iter = probed.byte_iter
    # Contract: the gate runs only after a successful probe populated both
    # the byte iterator and the underlying response. Narrow here once so the
    # loop below can call __anext__ / aclose without per-call None guards.
    assert byte_iter is not None
    assert probed.response is not None

    def _scan() -> bool:
        buf = bytes(accumulated)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if _sse_line_has_content(line, anthropic_shape=anthropic_shape):
                return True
        return False

    # Cheap first scan: probe already gave us some bytes.
    if _scan():
        probed.first_chunk = bytes(accumulated)
        return probed

    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(byte_iter.__anext__(), timeout=remaining)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            accumulated.extend(chunk)
            if _scan():
                probed.first_chunk = bytes(accumulated)
                return probed
    except (Exception, asyncio.CancelledError):
        # Close on ANY unexpected error (network, cancel) — the normal
        # timeout/EOF path falls through to the close below; this branch
        # only fires for httpx.ReadError / RemoteProtocolError etc.
        # that would otherwise leak probed.response.
        with suppress(Exception):
            await probed.response.aclose()
        raise

    # Timeout or upstream EOF without any content event.
    with suppress(Exception):
        await probed.response.aclose()
    log_entry["status"] = "retry_empty"
    log_entry["error"] = (
        f"first-token-gate timeout after {gate_timeout}s "
        f"without content (anthropic_shape={anthropic_shape}, "
        f"buffered={len(accumulated)}b)"
    )
    log_entry["latency_ms"] = int((time.time() - start_time) * 1000)
    await _log_request(log_entry)
    probed.error_response = _empty_response_jsonresponse(
        anthropic_shape=anthropic_shape,
    )
    probed.first_chunk = b""
    probed.byte_iter = None
    probed.response = None
    return probed


async def _gate_sdk_stream_for_content(
    aiter,
    *,
    gate_timeout: float,
) -> tuple[list, object]:
    """First-token gate for SDK-compat (provider.chat_stream) iterators.

    Awaits StreamChunks from `aiter` until one with truthy .delta arrives
    (the first content-bearing chunk). Returns (buffered_chunks, aiter)
    where buffered_chunks is a non-empty list ending in that first content
    chunk, and aiter is the same iterator positioned just after the buffer.

    On timeout / EOF without content: closes aiter and raises
    EmptyStreamError. The caller's retry loop catches this and either
    retries or returns _empty_response_jsonresponse. On any OTHER
    exception (network error, cancel) aiter is also closed before
    re-raising, so the underlying provider stream never leaks.

    NOT an async generator: must complete before the caller returns
    StreamingResponse, since the gate decision has to happen pre-commit.
    """
    deadline = time.time() + gate_timeout
    buffered: list = []
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise EmptyStreamError(
                    f"gate timeout after {gate_timeout}s without content"
                )
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
            except asyncio.TimeoutError:
                raise EmptyStreamError(
                    f"gate timeout after {gate_timeout}s without content"
                ) from None
            except StopAsyncIteration:
                raise EmptyStreamError(
                    "upstream stream ended without content"
                ) from None
            buffered.append(chunk)
            if chunk.delta:
                return buffered, aiter
    except (Exception, asyncio.CancelledError):
        # Close on ANY exit path (timeout, EOF, network error, cancel):
        # otherwise httpx.ReadError / openai.APIConnectionError raised by
        # aiter.__anext__() would leak the underlying provider stream and
        # its httpx connection until GC reclaims it.
        with suppress(Exception):
            await aiter.aclose()
        raise


async def _chain_chunks(
    first_chunks: list[StreamChunk], live_aiter
) -> AsyncGenerator[StreamChunk, None]:
    """Async generator: yield buffered chunks first, then continue with the
    live iterator. Used to feed a primed iterator back into the SSE
    forwarder (_openai_compat_sse_stream).

    Closes live_aiter on every exit path (normal completion, consumer
    cancellation, error). Without this, a client disconnect mid-stream
    would leave the underlying provider stream open until GC reclaims it.
    """
    try:
        for c in first_chunks:
            yield c
        async for c in live_aiter:
            yield c
    finally:
        with suppress(Exception):
            await live_aiter.aclose()


async def _make_prepended_line_iter(
    first_chunk: bytes,
    byte_iter: AsyncIterator[bytes],
    response: httpx.Response,
):
    """Async generator yielding decoded lines from a partially-consumed stream.

    Yields the lines hidden inside first_chunk first, then continues with the
    remaining bytes from byte_iter. Closes `response` in a finally block so
    the upstream connection is always released even on client disconnect.
    """
    buffer = b""
    try:
        if first_chunk:
            buffer = first_chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode(errors="replace")
        async for chunk in byte_iter:
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode(errors="replace")
        if buffer:
            yield buffer.decode(errors="replace")
    finally:
        with suppress(Exception):
            await response.aclose()


def _get_openai_upstreams(
    model: str, force_provider: str | None = None
) -> list[tuple[str, str, str, str]]:
    """Ordered OpenAI-compatible upstream candidates for a model.

    Returns a list of (base_url, api_key, mapped_model, provider_name) in
    priority order: the first entry is the primary provider, the rest are
    fallbacks from the `model_fallbacks` config. The request layer tries them
    in sequence, moving to the next when the current one errors before
    producing any content (e.g. quota exhausted). Providers in fatal-error
    cooldown are skipped unless they are the only remaining candidate.
    For dual-protocol providers, uses base_urls.openai from config.
    """
    from providers.claude import ClaudeProvider

    out: list[tuple[str, str, str, str]] = []
    for provider, mapped in _get_provider_candidates(model):
        if force_provider and provider.name != force_provider:
            continue
        if isinstance(provider, ClaudeProvider):
            p_cfg = _find_provider_cfg(provider.name)
            openai_url = p_cfg.get("base_urls", {}).get("openai") if p_cfg else None
            if not openai_url:
                continue
            base_url = openai_url
        else:
            base_url = provider.base_url or "https://api.openai.com/v1"
        out.append((base_url, provider.api_key, (mapped or model), provider.name))
    # Skip providers in fatal-error cooldown — but only when alternatives
    # remain (never leave a model with zero candidates).
    if len(out) > 1:
        filtered = [u for u in out if not _is_provider_model_failed(u[3], model)]
        if filtered:
            out = filtered
    return out


def _get_openai_upstream(
    model: str, force_provider: str | None = None
) -> tuple[str, str, str, str] | None:
    """Find upstream for an OpenAI-compatible raw proxy request.
    Returns (base_url, api_key, mapped_model, provider_name) or None.
    For dual-protocol providers, uses base_urls.openai from config.
    """
    upstreams = _get_openai_upstreams(model, force_provider=force_provider)
    return upstreams[0] if upstreams else None


def _new_attempt_log(
    *,
    model: str,
    provider: str,
    input_msgs,
    stream: bool,
    session_id: str,
    trace_id: str,
    attempt: int,
    timestamp: float | None = None,
    messages: int | None = None,
    input_override=None,
) -> dict:
    """Fresh attempt-log entry shared by the stream/non-stream/SDK retry loops.

    Previously duplicated at 5 sites; a single shape keeps the request-log
    schema consistent across all retry paths.
    """
    return {
        "timestamp": timestamp if timestamp is not None else time.time(),
        "model": model,
        "provider": provider,
        "messages": messages if messages is not None else len(input_msgs),
        "stream": stream,
        "input": input_override if input_override is not None else input_msgs,
        "output": "",
        "status": "pending",
        "session_id": session_id,
        "trace_id": trace_id,
        "attempt_index": attempt,
    }


async def _openai_raw_stream_once(
    *,
    upstream_url: str,
    headers: dict,
    body: dict,
    model: str,
    base_url: str,
    provider_name: str,
    start: float,
    input_msgs: list,
    session_id: str,
    session_key: str = "",
    trace_id: str,
    redaction_map,
    max_attempts: int,
    gate_timeout: float,
) -> tuple:
    """Streaming attempt against ONE OpenAI-compatible upstream.

    Runs the usual empty-retry loop for a single provider. Returns
    (response, should_fallback): should_fallback=True means the upstream
    errored BEFORE producing any content, so the caller may try the next
    fallback provider. In that case the response is the exact error the
    client would see if this were the last candidate.
    """
    for attempt in range(max_attempts):
        attempt_log = _new_attempt_log(
            model=model,
            provider=base_url,
            input_msgs=input_msgs,
            stream=True,
            session_id=session_id,
            trace_id=trace_id,
            attempt=attempt,
        )
        # Pre-probe upstream: detect non-200 and soft-error (HTTP 200 + JSON
        # error body) BEFORE returning StreamingResponse. Without this,
        # errors arrive as in-band SSE events on a 200 response and
        # opencode's retryTransient never fires (HTTP-layer: 408/409/429/5xx).
        try:
            probed = await _probe_upstream_stream(
                upstream_url,
                headers,
                body,
                anthropic_shape=False,
                log_entry=attempt_log,
                start_time=start,
                provider_name=provider_name,
                model=model,
            )
        except httpx.TimeoutException as e:
            attempt_log["status"] = "error"
            attempt_log["error"] = f"timeout: {e}"
            attempt_log["latency_ms"] = int((time.time() - start) * 1000)
            await _log_request(attempt_log)
            _cb_fail(provider_name, model)
            return _timeout_error_response(e, anthropic_shape=False), True
        except Exception as e:
            attempt_log["status"] = "error"
            attempt_log["error"] = str(e)
            attempt_log["latency_ms"] = int((time.time() - start) * 1000)
            await _log_request(attempt_log)
            _cb_fail(provider_name, model, exc=e)
            return _sdk_exception_response(e, anthropic_shape=False), True
        if probed.error_response is not None:
            _gate_body = ""
            try:
                _gate_body = bytes(probed.error_response.body).decode("utf-8")
            except Exception:
                pass
            _cb_fail(
                provider_name,
                model,
                status=probed.error_response.status_code,
                body_text=_gate_body,
            )
            return probed.error_response, True
        # First-token gate: detect empty upstream streams (HTTP 200 with
        # only message_start/message_stop and zero content deltas) and
        # retry. Wall-clock timeout (default 90s) — GLM thinking mode can
        # take 60s+ before the first text_delta, so we don't short-circuit.
        probed = await _gate_probe_for_content(
            probed,
            anthropic_shape=False,
            gate_timeout=gate_timeout,
            log_entry=attempt_log,
            start_time=start,
        )
        if probed.error_response is not None:
            # Gate already wrote a retry_empty log line and closed upstream.
            if attempt < max_attempts - 1:
                continue
            _cb_fail(provider_name, model)
            return (
                _empty_response_jsonresponse(
                    anthropic_shape=False,
                    attempts=max_attempts,
                ),
                True,
            )
        _cb_ok(provider_name, model)
        # Probe contract: error_response is None guarantees byte_iter +
        # response are populated. Narrow for the type checker.
        assert probed.byte_iter is not None and probed.response is not None
        return (
            StreamingResponse(
                _restored_stream(
                    _raw_openai_sse_stream(
                        probed.first_chunk,
                        probed.byte_iter,
                        probed.response,
                        model,
                        start,
                        attempt_log,
                        body,
                        provider_name,
                        session_key=session_key,
                    ),
                    redaction_map,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-CyRouter-Provider": provider_name,
                },
            ),
            False,
        )
    _cb_fail(provider_name, model)
    return (
        _empty_response_jsonresponse(anthropic_shape=False, attempts=max_attempts),
        True,
    )


async def _openai_raw_nonstream_once(
    *,
    upstream_url: str,
    headers: dict,
    body: dict,
    model: str,
    base_url: str,
    provider_name: str,
    start: float,
    input_msgs: list,
    session_id: str,
    trace_id: str,
    redaction_map,
    orig_body: dict,
    max_attempts: int,
) -> tuple:
    """Non-streaming attempt against ONE OpenAI-compatible upstream.

    Returns (response, should_fallback); see _openai_raw_stream_once.
    """
    for attempt in range(max_attempts):
        attempt_log = _new_attempt_log(
            model=model,
            provider=base_url,
            input_msgs=input_msgs,
            stream=False,
            session_id=session_id,
            trace_id=trace_id,
            attempt=attempt,
        )
        try:
            client = _get_http_client_for_url(upstream_url, provider_name)
            resp = await client.post(
                upstream_url, headers=headers, json=body, timeout=_NONSTREAM_TIMEOUT
            )
            body_text = resp.text

            # Detect "soft error": GLM/DeepSeek sometimes return HTTP 200 with
            # an error JSON body (e.g. 1305 "该模型当前访问量过大") instead of a
            # proper 4xx/5xx. opencode's retryTransient only fires on
            # 408/409/429/5xx, so re-classify so the client SDK retries.
            is_soft_error = (
                resp.status_code == 200
                and '"error"' in body_text
                and '"choices"' not in body_text
            )
            if resp.status_code != 200 or is_soft_error:
                # Quota/rate-limit marking is unified inside _cb_fail
                # (classification + decision table), no separate guard.
                norm_body, final_status, _ = _normalize_upstream_error(
                    body_text, resp.status_code
                )
                attempt_log["status"] = "error"
                err = norm_body.get("error", {})
                attempt_log["error"] = (
                    f"{final_status} {err.get('type', '?')}: {err.get('message', '')[:200]}"
                )
                if is_soft_error:
                    attempt_log["error"] = "[soft-error-200] " + attempt_log["error"]
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                await _log_request(attempt_log)
                _cb_fail(provider_name, model, status=final_status, body_text=body_text)
                out_headers = {}
                ra = _retry_after_seconds(final_status, resp.headers)
                if ra is not None:
                    out_headers["Retry-After"] = str(ra)
                return (
                    JSONResponse(
                        norm_body, status_code=final_status, headers=out_headers
                    ),
                    True,
                )
            result = resp.json()

            # Reasoning replay capture: cache the real reasoning of a
            # tool-call round so later turns that omit it can be replayed.
            try:
                _choices = result.get("choices") or []
                if _choices and isinstance(_choices[0], dict):
                    _msg = _choices[0].get("message") or {}
                    _reasoning = _msg.get("reasoning_content") or ""
                    _ids = [
                        tc.get("id")
                        for tc in (_msg.get("tool_calls") or [])
                        if isinstance(tc, dict) and tc.get("id")
                    ]
                    if _reasoning.strip() and _ids:
                        get_reasoning_cache().store(_ids, _reasoning)
            except Exception:
                logger.debug("reasoning capture failed", exc_info=True)

            # Empty-output check: 200 but content+reasoning_content+tool_calls
            # all empty → retry (up to max_attempts). HTTP errors above don't
            # hit this branch. Final attempt: fall through so client sees the
            # genuine empty 200 (no synthesised 502), but the log marks it
            # ok_empty so dashboards can distinguish it from real ok.
            is_empty = _is_empty_response(result, anthropic_shape=False)
            if is_empty and attempt < max_attempts - 1:
                attempt_log["status"] = "retry_empty"
                attempt_log["error"] = "empty response from upstream, retrying"
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                if result.get("choices"):
                    choice0 = result["choices"][0]
                    msg = (
                        choice0.get("message", {}) if isinstance(choice0, dict) else {}
                    )
                    if msg.get("reasoning_content"):
                        attempt_log["reasoning"] = msg["reasoning_content"]
                    fr = (
                        choice0.get("finish_reason")
                        if isinstance(choice0, dict)
                        else None
                    )
                    if fr:
                        attempt_log["finish_reason"] = fr
                await _log_request(attempt_log)
                continue

            if "model" in result:
                result["model"] = model
            attempt_log["status"] = "ok_empty" if is_empty else "ok"
            if is_empty:
                attempt_log["error"] = "empty response after exhausting retries"
            attempt_log["latency_ms"] = int((time.time() - start) * 1000)
            if result.get("choices"):
                choice0 = result["choices"][0]
                msg = choice0.get("message", {})
                attempt_log["output"] = msg.get("content", "")
                if msg.get("reasoning_content"):
                    attempt_log["reasoning"] = msg["reasoning_content"]
                if choice0.get("finish_reason"):
                    attempt_log["finish_reason"] = choice0["finish_reason"]
            usage = result.get("usage")
            if usage:
                attempt_log["usage"] = _openai_usage_to_anthropic(usage)
            await _log_request(attempt_log)
            if is_empty:
                _cb_fail(provider_name, model)
            else:
                _cb_ok(provider_name, model)
                _note_latency(model, attempt_log["latency_ms"], usage)
            if redaction_map:
                result = restore_obj(result, redaction_map)
            r = JSONResponse(result)
            _cache_store(orig_body, r)
            return r, False
        except HTTPException:
            raise
        except httpx.TimeoutException as e:
            attempt_log["status"] = "error"
            attempt_log["error"] = f"timeout: {e}"
            attempt_log["latency_ms"] = int((time.time() - start) * 1000)
            await _log_request(attempt_log)
            _cb_fail(provider_name, model)
            return _timeout_error_response(e, anthropic_shape=False), True
        except Exception as e:
            attempt_log["status"] = "error"
            attempt_log["error"] = str(e)
            attempt_log["latency_ms"] = int((time.time() - start) * 1000)
            await _log_request(attempt_log)
            _cb_fail(provider_name, model, exc=e)
            return _sdk_exception_response(e, anthropic_shape=False), True
    _cb_fail(provider_name, model)
    return (
        _empty_response_jsonresponse(anthropic_shape=False, attempts=max_attempts),
        True,
    )


@app.post("/v1/responses")
@app.post("/v1/responses/{path:path}")
async def openai_responses(request: Request, path: str = ""):
    """OpenAI Responses API proxy (used by gpt-5.6-luna on opencode-go).

    Transparent passthrough to an OpenAI-compatible upstream that supports
    the Responses protocol (``<base_url>/responses``).  Unlike chat
    completions this is a thin relay: no empty-retry / gating / reasoning
    replay — Responses items carry their own structure and the upstream
    semantics differ (item arrays, function_call items, etc.).  We do keep
    secret redaction (mask embedded keys before forwarding, restore on the
    way back) and the standard request log.
    """
    body = await request.json()
    model = body.get("model", "")

    session_id = request.headers.get("x-cyrouter-session-id", "")
    trace_id = request.headers.get("x-cyrouter-trace-id", "")
    model_override = request.headers.get("x-cyrouter-model-override", "")
    if model_override:
        model = body["model"] = model_override
    force_provider = request.headers.get("x-cyrouter-provider", "") or None

    upstream = _get_openai_upstream(model, force_provider=force_provider)
    if not upstream:
        raise HTTPException(404, f"No provider for model: {model} (Responses API)")
    base_url, api_key, mapped_model, provider_name = upstream
    body["model"] = mapped_model

    body, redaction_map = _redact_request_body(body, request)

    is_stream = bool(body.get("stream"))
    # "input" may be a plain string or a message list; derive a count for
    # the log.  Prefer len() of the list when present.
    _in = body.get("input")
    n_msgs = len(_in) if isinstance(_in, list) else (1 if _in else 0)

    start = time.time()
    attempt_log = _new_attempt_log(
        model=model,
        provider=base_url,
        input_msgs=None,
        stream=is_stream,
        session_id=session_id,
        trace_id=trace_id,
        attempt=0,
        timestamp=start,
        messages=n_msgs,
        input_override=_in,
    )

    upstream_url = f"{base_url.rstrip('/')}/responses"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    logger.info(
        "responses-debug url=%s headers=%s body=%s",
        upstream_url,
        {
            k: (v[:12] + "..." if k == "Authorization" else v)
            for k, v in headers.items()
        },
        json.dumps(body, ensure_ascii=False)[:300],
    )
    client = _get_http_client_for_url(upstream_url, provider_name)
    try:
        resp = await client.post(
            upstream_url, headers=headers, json=body, timeout=_NONSTREAM_TIMEOUT
        )
        try:
            logger.info(
                "responses-debug2 status=%s req_headers=%s body_len=%d body=%s",
                resp.status_code,
                dict(resp.request.headers),
                len(resp.request.content or b""),
                (resp.request.content or b"")[:200],
            )
        except Exception:
            pass
    except httpx.TimeoutException as e:
        attempt_log["status"] = "error"
        attempt_log["error"] = f"timeout: {e}"
        attempt_log["latency_ms"] = int((time.time() - start) * 1000)
        await _log_request(attempt_log)
        return _timeout_error_response(e, anthropic_shape=False)
    except Exception as e:
        attempt_log["status"] = "error"
        attempt_log["error"] = str(e)
        attempt_log["latency_ms"] = int((time.time() - start) * 1000)
        await _log_request(attempt_log)
        return _sdk_exception_response(e, anthropic_shape=False)

    body_text = resp.text
    if resp.status_code != 200:
        # fatal marking unified inside _cb_fail below
        norm_body, final_status, _ = _normalize_upstream_error(
            body_text, resp.status_code
        )
        attempt_log["status"] = "error"
        err = norm_body.get("error", {})
        attempt_log["error"] = (
            f"{final_status} {err.get('type', '?')}: {err.get('message', '')[:200]}"
        )
        attempt_log["latency_ms"] = int((time.time() - start) * 1000)
        await _log_request(attempt_log)
        _cb_fail(provider_name, model, status=final_status, body_text=body_text)
        out_headers = {}
        ra = _retry_after_seconds(final_status, resp.headers)
        if ra is not None:
            out_headers["Retry-After"] = str(ra)
        return JSONResponse(norm_body, status_code=final_status, headers=out_headers)

    if is_stream:
        # Relay the SSE byte stream, restoring redacted secrets in content.
        async def _gen():
            async for chunk in resp.aiter_bytes():
                yield chunk

        attempt_log["status"] = "ok"
        attempt_log["latency_ms"] = int((time.time() - start) * 1000)
        await _log_request(attempt_log)
        _cb_ok(provider_name, model)
        return StreamingResponse(
            _restored_stream(_gen(), redaction_map),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-CyRouter-Provider": provider_name,
            },
        )

    result = resp.json()
    if "model" in result:
        result["model"] = model
    attempt_log["status"] = "ok"
    attempt_log["latency_ms"] = int((time.time() - start) * 1000)
    # Extract a short text summary for the log.
    try:
        outs = result.get("output") or []
        texts = []
        for item in outs:
            if isinstance(item, dict) and item.get("type") == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        texts.append(c.get("text", ""))
        attempt_log["output"] = "".join(texts)[:2000]
    except Exception:
        pass
    usage = result.get("usage")
    if usage:
        attempt_log["usage"] = _openai_usage_to_anthropic(usage)
    await _log_request(attempt_log)
    _cb_ok(provider_name, model)
    _note_latency(model, attempt_log["latency_ms"], usage)
    if redaction_map:
        result = restore_obj(result, redaction_map)
    return JSONResponse(result)


def _estimate_request_tokens(body: dict) -> int:
    """Rough input-token estimate for the context preflight.

    CJK chars ≈1 token each; other chars ≈1 token per 4 (code/prose).
    Counts message text, tool_calls, replayed reasoning_content and tool
    definitions — the dominant bulk in agent traffic. Per-message role /
    structural overhead (~5%) is covered by the conservative 4-char divisor.
    """

    def _weight(s: str) -> int:
        cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        return cjk + (len(s) - cjk) // 4

    total = 0
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            total += _weight(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    if part.get("type") in ("image_url", "input_image", "image"):
                        total += 1500  # typical vision token cost
                    else:
                        total += _weight(str(part.get("text") or ""))
        if m.get("tool_calls"):
            total += _weight(json.dumps(m["tool_calls"], ensure_ascii=False))
        if m.get("reasoning_content"):
            total += _weight(str(m["reasoning_content"]))
    if body.get("tools"):
        total += _weight(json.dumps(body["tools"], ensure_ascii=False))
    return total


def _context_preflight(model: str, body: dict):
    """Reject requests whose estimated input exceeds the model's context.

    Without this, an oversized session hits the upstream's silent input
    truncation: the model sees a beheaded history, has ~zero output headroom
    and returns finish=length after 1-4 tokens — which clients surface as a
    confusing "truncated" error and auto-compact retry loops that can still
    overflow (observed on kimi-k3: 277k→163k session vs k3's ~128k window).
    A clear 400 tells the user to compact instead. The limit comes from the
    unified resolver (model_overrides > model_meta > pricing.max_context >
    heuristic), so every model with a known context — not just kimi-k3 — is
    protected; kimi-k3 additionally wired max_context=256000 which now lives
    in model_meta.
    """
    cfg = load_config()
    limit = _resolve_model_limit(model, cfg)["context"]
    if limit <= 0:
        return None
    est = _estimate_request_tokens(body)
    if est <= limit:
        return None
    msg = (
        f"context_length_exceeded: estimated input ~{est:,} tokens exceeds "
        f"{model}'s {limit:,}-token context window. Compact the session or "
        f"start a new one. (rough estimate: CJK=1 tok/ch, other=1 tok/4ch)"
    )
    logger.warning(
        "context preflight reject: model=%s est=%d limit=%d", model, est, limit
    )
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": msg,
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        },
    )


def _resp_payload_is_empty(resp, *, anthropic_shape: bool) -> bool:
    """True iff resp is a completed (non-streaming) JSONResponse whose payload
    carries no user-visible content — the upstream 200-but-empty soft failure.

    StreamingResponse has no .body and is skipped by construction (a
    mid-stream drop can never be re-dispatched anyway). Re-uses
    _is_empty_response as the single authoritative definition, so
    tool_calls / reasoning_content / thinking blocks all count as content.
    """
    try:
        if getattr(resp, "status_code", 0) != 200:
            return False
        body = getattr(resp, "body", None)
        if not isinstance(body, (bytes, bytearray)):
            return False
        payload = json.loads(bytes(body).decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            return False
        # Shape guard: _is_empty_response treats a missing shape-defining
        # field as empty, so probe only payloads that actually belong to the
        # caller's protocol (OpenAI: "choices"; Anthropic: "content" list).
        # A cross-shaped payload is never classified (no false positives).
        if anthropic_shape:
            if not isinstance(payload.get("content"), list):
                return False
        else:
            if "choices" not in payload:
                return False
        return _is_empty_response(payload, anthropic_shape=anthropic_shape)
    except Exception:
        return False


async def _run_auto_reroute(
    request: Any, body: dict, impl, *, anthropic_shape: bool = False, **impl_kwargs
):
    """Dispatch an API request, rerouting `auto` once per fatally-dead model.

    (Typing: `request` is Any — the wrapper only touches ``request.state``
    and hands the request through to `impl`; unit tests pass a stub.)

    2026-08-30 incident: kimi-k3's weekly quota wall (403 insufficient_quota)
    hit while glm-5.3 was transiently failed-marked — the discovering request
    ate the raw 403 even though auto switched every LATER request to glm.
    Fatal errors (quota / rate-limit marks, dead provider chains) are
    pre-content: nothing was streamed and no side effects happened, so the
    request can be transparently re-resolved on a live model and retried.

    Reroute only when: the request asked for `auto`, the impl resolved a
    concrete model (not client-pinned via x-cyrouter-model-override /
    x-cyrouter-provider), and the response is an HTTP error with the
    resolved model now unhealthy (the failure marked it dead — quota /
    rate-limit / whole chain down), OR the response is a completed
    non-streaming 200 with genuinely empty content (ok_empty fall-through
    after in-provider retries — 2026-09-12 yinor incident). Empty-200
    reroutes do NOT mark the model dead globally: one empty answer must not
    cool a model for everyone; the breaker already recorded _cb_fail inside
    the impl. Non-fatal 4xx passthroughs and mid-stream drops (status 200,
    already streaming) never reroute. Hard cap: 3 dead models per request,
    then the last response is returned truthfully.
    """
    is_auto = (
        isinstance(body, dict)
        and isinstance(body.get("model"), str)
        and body["model"].lower() == "auto"
    )
    if not is_auto:
        return await impl(request, body, **impl_kwargs)
    pristine = copy.deepcopy(body)
    tried: set[str] = set()
    while True:
        resp = await impl(
            request,
            copy.deepcopy(pristine),
            _auto_exclude=(set(tried) or None),
            **impl_kwargs,
        )
        resolved = getattr(request.state, "cy_auto_resolved", "") or ""
        empty_200 = _resp_payload_is_empty(resp, anthropic_shape=anthropic_shape)
        if (
            (getattr(resp, "status_code", 200) < 400 and not empty_200)
            or not resolved
            or resolved in tried
            or (not empty_200 and _is_healthy(resolved))
            or len(tried) >= 3
        ):
            return resp
        tried.add(resolved)
        logger.warning(
            "auto: in-request reroute after %s of '%s' "
            "(%d model(s) tried this request)",
            "empty 200 response" if empty_200 else "fatal failure",
            resolved,
            len(tried),
        )


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    body = await request.json()
    return await _run_auto_reroute(request, body, _openai_chat_once)


async def _openai_chat_once(request: Request, body: dict, _auto_exclude=None):
    # Pure passthrough: forward the request messages unchanged.
    # (_strip_empty_messages was removed — it never fired on this traffic:
    # pi's content:"None" bug produces a truthy string, not an empty message,
    # so the function was a no-op here.)

    model = body.get("model", "")

    # "auto" virtual model: resolve before any upstream lookup.
    # _auto_exclude: models that already died fatally during THIS request
    # (in-request reroute) — resolution skips them.
    request.state.cy_auto_resolved = ""
    if isinstance(model, str) and model.lower() == "auto":
        cfg = load_config()
        body["_session_id"] = request.headers.get(
            "x-cyrouter-session-id", ""
        ) or request.headers.get("x-conversation-id", "")
        model = body["model"] = _resolve_auto_model(body, cfg, exclude=_auto_exclude)
        body.pop("_session_id", None)
        request.state.cy_auto_resolved = model

    # Parse CyRouter control headers (Helicone-style).
    session_id = request.headers.get("x-cyrouter-session-id", "")
    trace_id = request.headers.get("x-cyrouter-trace-id", "")
    model_override = request.headers.get("x-cyrouter-model-override", "")
    if model_override:
        model = body["model"] = model_override
        # auto may have injected a thinking field for the resolved model;
        # clear it so the override target doesn't receive a stale field
        # meant for a different model family.
        body.pop("thinking", None)
        # Client pinned a concrete model — in-request auto reroute must not
        # swap it out from under them.
        request.state.cy_auto_resolved = ""

    # Session key for the session-level provider penalty window: header
    # first, derived prefix-hash fallback (pi/opencode send no headers).
    # Computed on the pristine body BEFORE tool-adjacency repair and
    # redaction so it matches the auto stickiness key exactly.
    cy_session_key = (
        request.headers.get("x-cyrouter-session-id", "")
        or request.headers.get("x-conversation-id", "")
        or _auto_session_key(body)
    )

    # Tool-adjacency repair: hoist tool results back next to their
    # assistant when a mid-turn injected message (pi-processes lifecycle
    # events, etc.) broke the assistant(tool_calls)→tool adjacency that
    # kimi code strictly validates. Runs before the cache lookup so the
    # cache key is the canonical repaired form.
    _apply_tool_adjacency_repair(body)

    if not body.get("stream"):
        cached = _cache_try(body)
        if cached is not None:
            return cached

    # Reasoning replay runs BEFORE redaction: tool_call ids in the history
    # get masked by _redact_request_body, so cache lookups must see the
    # pristine body. Injected reasoning is redacted along with the rest.
    _ensure_reasoning_content(body)

    # Secret redaction runs AFTER the cache lookup so the cache key is the
    # pristine body (two prompts differing only in the embedded real key stay
    # distinct). orig_body feeds _cache_store; the redacted copy goes upstream.
    orig_body = body
    body, _redaction_map = _redact_request_body(body, request)

    # Context preflight: reject obviously oversized sessions with a clear 400
    # before the upstream silently truncates input and returns a confusing
    # 1-token finish=length. Opt-in via pricing[model].max_context.
    reject = _context_preflight(model, body)
    if reject is not None:
        return reject

    # Try raw proxy first (OpenAI-compatible upstream)
    force_provider = request.headers.get("x-cyrouter-provider", "") or None
    if force_provider:
        # Client pinned a provider — rerouting to another model would break
        # the pin (its chain may not include the forced provider).
        request.state.cy_auto_resolved = ""
    upstreams = _get_openai_upstreams(model, force_provider=force_provider)
    if not force_provider:
        _order = _session_penalty_reorder(
            cy_session_key, model, [u[3] for u in upstreams]
        )
        if _order is not None:
            upstreams = [upstreams[i] for i in _order]
            logger.info(
                "session %s…: penalty reorder chain → [%s]",
                cy_session_key[:8],
                ", ".join(u[3] for u in upstreams),
            )

    # If no raw proxy upstream, try SDK-based compat (Anthropic providers)
    if not upstreams:
        from providers.claude import ClaudeProvider

        provider, mapped = _get_provider_for_model(model)
        if force_provider and (not provider or provider.name != force_provider):
            raise HTTPException(
                404, f"No provider for model: {model} (force={force_provider})"
            )
        if not provider or not isinstance(provider, ClaudeProvider):
            raise HTTPException(404, f"No provider for model: {model}")

        # Single-provider SDK path: this provider IS the last candidate —
        # panic mode applies (never hard-wall on the only serving option).
        reject = _cb_check(provider.name, model, last_candidate=True)
        if reject is not None:
            return reject

        # Build ChatRequest from OpenAI-format body
        messages = [
            Message(role=m.get("role", "user"), content=m.get("content", ""))
            for m in body.get("messages", [])
        ]
        req = ChatRequest(
            model=mapped or model,
            messages=messages,
            temperature=body.get("temperature", 0.7),
            max_tokens=_elevate_max_tokens(model, body.get("max_tokens", 4096)),
            stream=body.get("stream", False),
            extra={
                k: v
                for k, v in body.items()
                if k not in ("model", "messages", "temperature", "max_tokens", "stream")
            },
            conversation_id=request.headers.get("x-conversation-id", ""),
        )
        start = time.time()
        input_msgs = [
            {
                "role": m.get("role", ""),
                "content": m.get("content", "")
                if isinstance(m.get("content"), str)
                else str(m.get("content", "")),
            }
            for m in body.get("messages", [])
        ]

        if req.stream:
            cfg = load_config()
            max_attempts = int(cfg.get("empty_retry_attempts", 3))
            gate_timeout = _effective_gate_timeout(body, cfg)
            for attempt in range(max_attempts):
                attempt_log = _new_attempt_log(
                    model=model,
                    provider=provider.name,
                    input_msgs=input_msgs,
                    stream=True,
                    session_id=session_id,
                    trace_id=trace_id,
                    attempt=attempt,
                )
                try:
                    buffered, live_aiter = await _gate_sdk_stream_for_content(
                        provider.chat_stream(req),
                        gate_timeout=gate_timeout,
                    )
                except EmptyStreamError as e:
                    attempt_log["status"] = "retry_empty"
                    attempt_log["error"] = f"sdk gate: {e}"
                    attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                    await _log_request(attempt_log)
                    if attempt < max_attempts - 1:
                        continue
                    _cb_fail(provider.name, model)
                    return _empty_response_jsonresponse(
                        anthropic_shape=False,
                        attempts=max_attempts,
                    )
                except httpx.TimeoutException as e:
                    attempt_log["status"] = "error"
                    attempt_log["error"] = f"timeout: {e}"
                    attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                    await _log_request(attempt_log)
                    _cb_fail(provider.name, model)
                    return _timeout_error_response(e, anthropic_shape=False)
                except Exception as e:
                    attempt_log["status"] = "error"
                    attempt_log["error"] = str(e)
                    attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                    await _log_request(attempt_log)
                    _cb_fail(provider.name, model, exc=e)
                    return _sdk_exception_response(e, anthropic_shape=False)
                # Gate passed — feed buffered + live back into the SSE forwarder.
                primed = _chain_chunks(buffered, live_aiter)
                _cb_ok(provider.name, model)
                return StreamingResponse(
                    _restored_stream(
                        _openai_compat_sse_stream(
                            provider,
                            req,
                            model,
                            start,
                            attempt_log,
                            aiter_override=primed,
                        ),
                        _redaction_map,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                        "X-CyRouter-Provider": provider.name,
                    },
                )
            _cb_fail(provider.name, model)
            return _empty_response_jsonresponse(
                anthropic_shape=False,
                attempts=max_attempts,
            )

        # Non-streaming via SDK
        cfg = load_config()
        max_attempts = int(cfg.get("empty_retry_attempts", 3))
        for attempt in range(max_attempts):
            attempt_log = _new_attempt_log(
                model=model,
                provider=provider.name,
                input_msgs=input_msgs,
                stream=False,
                session_id=session_id,
                trace_id=trace_id,
                attempt=attempt,
            )
            try:
                resp = await provider.chat(req)
                result = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(start),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": resp.content},
                            "finish_reason": resp.finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": resp.usage.get("prompt_tokens", 0),
                        "completion_tokens": resp.usage.get("completion_tokens", 0),
                        "total_tokens": resp.usage.get("total_tokens", 0),
                    },
                }
                # Empty-output retry: SDK ChatResponse only carries content
                # (no reasoning_content / tool_calls surface here), so empty
                # is simply resp.content being blank. On the final attempt the
                # genuine empty 200 is still returned to the client (per the
                # project's "no synthesised errors" design), but the log marks
                # it ok_empty so dashboards can distinguish it from real ok.
                if not resp.content:
                    if attempt < max_attempts - 1:
                        attempt_log["status"] = "retry_empty"
                        attempt_log["error"] = "empty response from upstream, retrying"
                        attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                        if resp.usage:
                            attempt_log["usage"] = _sdk_usage_to_log(resp.usage)
                        if resp.finish_reason:
                            attempt_log["finish_reason"] = resp.finish_reason
                        await _log_request(attempt_log)
                        continue
                    attempt_log["status"] = "ok_empty"
                    attempt_log["error"] = "empty response after exhausting retries"
                else:
                    attempt_log["status"] = "ok"
                attempt_log["output"] = resp.content
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                if resp.usage:
                    attempt_log["usage"] = _sdk_usage_to_log(resp.usage)
                await _log_request(attempt_log)
                if attempt_log["status"] == "ok_empty":
                    _cb_fail(provider.name, model)
                else:
                    _cb_ok(provider.name, model)
                    _note_latency(model, attempt_log["latency_ms"], resp.usage)
                if _redaction_map:
                    result = restore_obj(result, _redaction_map)
                r = JSONResponse(result)
                _cache_store(orig_body, r)
                return r
            except httpx.TimeoutException as e:
                attempt_log["status"] = "error"
                attempt_log["error"] = f"timeout: {e}"
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                await _log_request(attempt_log)
                _cb_fail(provider.name, model)
                return _timeout_error_response(e, anthropic_shape=False)
            except Exception as e:
                attempt_log["status"] = "error"
                attempt_log["error"] = str(e)
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                await _log_request(attempt_log)
                _cb_fail(provider.name, model, exc=e)
                return _sdk_exception_response(e, anthropic_shape=False)
        _cb_fail(provider.name, model)
        return _empty_response_jsonresponse(
            anthropic_shape=False,
            attempts=max_attempts,
        )

    # ── Raw proxy for OpenAI-compatible upstream ──
    # Provider fallback: `upstreams` is an ordered candidate list (primary +
    # model_fallbacks). On any error BEFORE content starts flowing, the next
    # candidate is tried. Once streaming to the client has begun, no fallback
    # is possible (output already sent).
    # Elevate max_tokens to the model's true output ceiling — OPT-IN via
    # config `auto_elevate_max_tokens: true`. Off by default = NATIVE
    # passthrough (forward the client's max_tokens unchanged, or omit it).
    # Thinking models count reasoning against max_tokens; a client default of
    # 4096/8192 can be exhausted by reasoning alone. BUT over-elevating past
    # the upstream's real tolerance triggers mid-stream truncation: GLM coding
    # plan silently drops the connection (no finish_reason) when max_tokens
    # exceeds ~65536, even though it advertises 128K output. So elevation must
    # stay explicit — never silently inflate here. See _elevate_max_tokens()
    # for the SDK path's equivalent opt-in logic.
    try:
        if load_config().get("auto_elevate_max_tokens", False):
            _mt_ceiling = _model_output_ceiling(model)
            if _mt_ceiling:
                _cur_mt = body.get("max_tokens")
                if (
                    not isinstance(_cur_mt, int)
                    or _cur_mt <= 0
                    or _cur_mt < _mt_ceiling
                ):
                    body["max_tokens"] = _mt_ceiling
    except Exception as _e:
        logger.warning("auto_elevate_max_tokens skipped due to error: %s", _e)

    # Pure passthrough: do not strip tool_choice for DeepSeek. If the
    # upstream rejects thinking + tool_choice together, that 400 is
    # surfaced to the client as-is — cyRouter is a transparent forwarder,
    # not a compatibility translator.

    is_stream = body.get("stream", False)
    start = time.time()
    input_msgs = [
        {
            "role": m.get("role", ""),
            "content": m.get("content", "")
            if isinstance(m.get("content"), str)
            else str(m.get("content", "")),
        }
        for m in body.get("messages", [])
    ]

    # Diagnostic: GLM-5.x upstream truncation glitch — log the exact params
    # the client requested so the next "stream ended without [DONE]" warning
    # is paired with its trigger. See comment block ~line 1947 for the bug.
    if "glm-5" in (model or "").lower():
        logger.info(
            "glm-5.x upstream params: model=%s max_tokens=%r thinking=%r "
            "stream=%s msgs=%d tools=%d",
            model,
            body.get("max_tokens"),
            body.get("thinking"),
            is_stream,
            len(body.get("messages", [])),
            len(body.get("tools", [])),
        )

    cfg = load_config()
    max_attempts = int(cfg.get("empty_retry_attempts", 3))
    gate_timeout = float(cfg.get("empty_retry_timeout", 90))

    n_candidates = len(upstreams)
    _tried_providers: list = []
    for cand_i, (base_url, api_key, mapped_model, raw_provider_name) in enumerate(
        upstreams
    ):
        is_last_candidate = cand_i == n_candidates - 1
        body["model"] = mapped_model
        _apply_param_clamp(body, raw_provider_name)
        _tried_providers.append(raw_provider_name)

        reject = _cb_check(raw_provider_name, model, last_candidate=is_last_candidate)
        if reject is not None:
            if is_last_candidate:
                return reject
            continue

        upstream_url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        if is_stream:
            resp, should_fallback = await _openai_raw_stream_once(
                upstream_url=upstream_url,
                headers=headers,
                body=body,
                model=model,
                base_url=base_url,
                provider_name=raw_provider_name,
                start=start,
                input_msgs=input_msgs,
                session_id=session_id,
                session_key=cy_session_key,
                trace_id=trace_id,
                redaction_map=_redaction_map,
                max_attempts=max_attempts,
                gate_timeout=gate_timeout,
            )
        else:
            resp, should_fallback = await _openai_raw_nonstream_once(
                upstream_url=upstream_url,
                headers=headers,
                body=body,
                model=model,
                base_url=base_url,
                provider_name=raw_provider_name,
                start=start,
                input_msgs=input_msgs,
                session_id=session_id,
                trace_id=trace_id,
                redaction_map=_redaction_map,
                orig_body=orig_body,
                max_attempts=max_attempts,
            )

        if should_fallback and not is_last_candidate:
            logger.info(
                "provider fallback: model=%s provider=%s failed pre-content, "
                "trying next candidate",
                model,
                raw_provider_name,
            )
            # Pre-content failure for THIS session — remember it so this
            # session's next request doesn't pay the dead first attempt
            # again (global breaker may stay closed under parallel traffic).
            _session_penalty_record(cy_session_key, raw_provider_name, model)
            continue
        if not should_fallback:
            # Upstream produced content — clear any stale global "model
            # failed" mark that an earlier candidate's fatal error may have
            # set, so auto routing sees the model as healthy again.
            _failed_models.pop(model, None)
            return resp
        # 最后一个候选也失败：把归因（哪家 provider、链上试过几家）写进错误响应
        return _annotate_final_error(resp, raw_provider_name, _tried_providers)


def _dump_truncation_blackbox(
    *, model: str, base_url: str, request_body: dict | None, trigger: dict
) -> None:
    """Persist the full secrets-redacted request body on upstream truncation.

    The normal request_log stores only a simplified input (role + content,
    capped), which cannot reproduce an upstream truncation glitch. This black
    box keeps the complete request exactly as sent upstream (secrets already
    masked by _redact_request_body), so the failure can be replayed by POSTing
    the `request` object to <base_url>/chat/completions.

    Only fires on the rare truncation event, so it never bloats under load.
    """
    if request_body is None:
        return
    try:
        from datetime import datetime

        bb_dir = DATA_DIR / "blackbox"
        bb_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now()
        safe = "".join(c if c.isalnum() else "-" for c in (model or "unknown"))[:40]
        path = bb_dir / (
            f"truncated-{safe}-"
            f"{ts.strftime('%Y%m%d-%H%M%S')}-{ts.microsecond // 1000:03d}.json"
        )
        envelope = {
            "captured_at": ts.isoformat(timespec="seconds"),
            "model": model,
            "provider": base_url,
            "trigger": trigger,
            "note": (
                "Full request body (secrets redacted) captured at upstream "
                "truncation for post-mortem reproduction. Re-send a POST to "
                "<provider>/chat/completions with the 'request' object below."
            ),
            "request": request_body,
        }
        blob = json.dumps(envelope, ensure_ascii=False)
        if len(blob) > 50 * 1024 * 1024:
            logger.warning(
                "truncation blackbox skipped: body too large (%d bytes, model=%s)",
                len(blob),
                model,
            )
            return
        path.write_text(blob, encoding="utf-8")
        logger.warning(
            "truncation blackbox written: %s (%d bytes, model=%s)",
            path,
            len(blob),
            model,
        )
    except Exception as e:
        logger.error("truncation blackbox write failed: %s", e)


async def _raw_openai_sse_stream(
    first_chunk: bytes,
    byte_iter: AsyncIterator[bytes],
    response: httpx.Response,
    original_model: str,
    start_time: float,
    log_entry: dict,
    request_body: dict | None = None,
    provider_name: str = "",
    session_key: str = "",
):
    """Forward raw OpenAI SSE stream chunks (already probed for errors).

    Takes a partially-consumed upstream stream from _probe_upstream_stream:
    first_chunk is the bytes already read during probing, byte_iter yields the
    remaining bytes, response is the underlying httpx stream (closed in
    _make_prepended_line_iter's finally block).

    Pre-probe guarantees (handled by the caller):
      - HTTP status is 200
      - First chunk is NOT a soft-error JSON (it's genuine SSE data or empty)
    So this function only forwards chunks with model-name rewrite + logging.
    """
    output_parts: list[str] = []
    reasoning_parts: list[
        str
    ] = []  # GLM/DeepSeek thinking content (delta.reasoning_content)
    tool_call_ids: list[str] = []  # ids of tool_calls in this response
    usage_data: dict = {}
    last_finish_reason: str | None = (
        None  # track across chunks (usually only final chunk has it)
    )
    saw_done = False  # set on a normal terminator: [DONE] or an empty-choices
    # chunk (opencode-go ends streams with {"choices":[],"cost":"0"} instead of
    # [DONE]). An end with saw_done still False is a true upstream truncation.
    forwarded_done = False  # whether we've emitted a [DONE] to the client
    # This is model-specific: kimi-k3 on the same provider emits a normal
    # finish_reason, while gpt-5.6-luna does not. Keep the compatibility
    # behavior scoped to an explicit model list, not the whole provider.
    _p_cfg = _find_provider_cfg(provider_name) if provider_name else None
    synth_models = (_p_cfg or {}).get("synthesize_finish_models") or []
    synthesize_finish = original_model in synth_models

    async def _flush(status: str, error: str | None = None):
        log_entry["status"] = status
        log_entry["output"] = "".join(output_parts)
        if reasoning_parts:
            log_entry["reasoning"] = "".join(reasoning_parts)
            # Reasoning replay capture: a successful tool-call round's real
            # reasoning is cached per tool_call_id so later turns whose
            # client omitted reasoning_content can be replayed (DeepSeek
            # reasoning continuity).
            if status == "ok" and tool_call_ids:
                stored = get_reasoning_cache().store(
                    tool_call_ids, "".join(reasoning_parts)
                )
                if stored:
                    logger.info(
                        "reasoning cache: stored reasoning for %d tool_call ids",
                        stored,
                    )
        if last_finish_reason:
            log_entry["finish_reason"] = last_finish_reason
        log_entry["latency_ms"] = int((time.time() - start_time) * 1000)
        if usage_data:
            log_entry["usage"] = _openai_usage_to_anthropic(usage_data)
        if error:
            log_entry["error"] = error
        await _log_request(log_entry)

    # The router never terminates an active stream — pure passthrough.
    # Regex to rewrite the model field in OpenAI SSE chunks. Operates on the
    # original payload string so we avoid json re-serialisation — the rest of
    # the chunk is forwarded byte-for-byte (field order, spacing, escaping).
    import re as _re

    _model_re = _re.compile(r'"model"\s*:\s*"[^"]*"')

    # 取证：记录最后一次从上游收到任何字节的时刻。静默断流（无 [DONE] 的
    # EOF）发生时，idle gap 可区分两种根因：≈30s+ 无字节 → 上游网关空闲
    # 超时杀流（模型长思考暂停输出）；≈0 → 活跃传输中被杀（上游 worker
    # 崩溃/过载）。心跳分支不更新此值——心跳是我们发给客户端的，不代表
    # 上游有字节。
    _last_byte_ts = start_time

    try:
        line_iter = _make_prepended_line_iter(first_chunk, byte_iter, response)
        while True:
            try:
                line = await asyncio.wait_for(line_iter.__anext__(), timeout=30.0)
            except asyncio.TimeoutError:
                # No active termination — a 30s line gap only emits an SSE
                # comment to keep the connection alive (pure passthrough).
                yield ": heartbeat\n\n"
                continue
            except StopAsyncIteration:
                break

            _last_byte_ts = time.time()  # 空行（keepalive）也算上游字节
            if not line:
                yield "\n"
                continue
            payload = ""
            if line.startswith("data: "):
                payload = line[6:]
            elif line.startswith("data:"):
                payload = line[5:]
            if payload and original_model:
                if payload.strip() == "[DONE]":
                    # Native passthrough: forward upstream's [DONE] verbatim.
                    # Do NOT synthesize a finish_reason chunk when upstream
                    # omitted one — synthesizing "stop" masked upstream's
                    # malformed stream as a clean completion. Clients see the
                    # true (possibly finish_reason-less) stream.
                    saw_done = True
                    forwarded_done = True
                    yield line + "\n"
                    break
                try:
                    data = json.loads(payload)
                    # Side-channel observation for logging — does NOT modify
                    # the forwarded bytes. The model field is rewritten below
                    # via regex on the original payload so the upstream's exact
                    # byte representation (field order, spacing, escaping) is
                    # preserved instead of being lost to json re-serialisation.
                    choices = data.get("choices")
                    if choices:
                        choice0 = choices[0]
                        delta = choice0.get("delta", {})
                        if delta.get("content"):
                            output_parts.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            if isinstance(tc, dict) and tc.get("id"):
                                tool_call_ids.append(tc["id"])
                        # Track finish_reason (usually arrives in the last chunk
                        # before [DONE]). Useful for diagnosing "model stopped early":
                        # stop=no tool_calls = model bailed on agent loop.
                        if choice0.get("finish_reason"):
                            last_finish_reason = choice0["finish_reason"]
                    elif synthesize_finish and isinstance(choices, list):
                        # This model terminates with an empty-choices chunk
                        # (e.g. {"choices":[],"cost":"0"}) instead of the
                        # standard data: [DONE]. Treat it as normal termination
                        # only for the explicitly configured model.
                        saw_done = True
                    if data.get("usage"):
                        usage_data = data["usage"]
                    # Byte-level model rewrite: regex-replace only the model
                    # value in the original payload string (count=1 — model is
                    # a top-level scalar). Avoids full json re-serialisation so
                    # the rest of the chunk is forwarded verbatim, matching the
                    # Anthropic path's byte-passthrough philosophy.
                    if data.get("model") and data["model"] != original_model:
                        payload = _model_re.sub(
                            f'"model":"{original_model}"', payload, count=1
                        )
                        line = f"data: {payload}"
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
            yield line + "\n"
    except asyncio.CancelledError:
        logger.info("stream cancelled by client (model=%s)", original_model)
        await _flush("cancelled")
        raise
    except Exception as e:
        _close_style = _stream_close_exc_style(e)
        logger.error("OpenAI raw proxy stream error (close=%s): %s", _close_style, e)
        await _flush("error", error=str(e))
        # FIN/RST 取证：连接被重置（RST）或对端 FIN 但 HTTP 体不完整
        # （fin_abrupt）的杀流走异常分支而不是 truncation 分支，单独留黑盒
        # 样本，与 close_style=fin 的静默断流对照，定位杀流者层级
        # （L4 中间盒 vs L7 网关）。客户端侧行为不变（router_error 透传）。
        if _close_style in ("rst", "fin_abrupt"):
            try:
                _dump_truncation_blackbox(
                    model=original_model,
                    base_url=log_entry.get("provider", ""),
                    request_body=request_body,
                    trigger={
                        "close_style": _close_style,
                        "error": str(e)[:300],
                        "finish_reason": last_finish_reason,
                        "content_chars": sum(len(s) for s in output_parts),
                        "reasoning_chars": sum(len(s) for s in reasoning_parts),
                        "elapsed_s": round(time.time() - start_time, 1),
                        "idle_gap_s": round(time.time() - _last_byte_ts, 1),
                        "provider": provider_name,
                    },
                )
            except Exception:
                logger.warning("blackbox dump failed (rst path)", exc_info=True)
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'router_error'}})}\n\n"
        return

    # Stream ended. If upstream sent a normal [DONE] we already forwarded it.
    # An abnormal end — StopAsyncIteration with no [DONE] — means the upstream
    # truncated the stream. Synthesize a finish_reason="length" terminator so
    # downstream clients (pi) treat it as a recoverable length-stop and
    # auto-compact + retry, instead of surfacing an opaque
    # "Stream ended without finish_reason" that neither the client nor the
    # model can recover from. Root cause of the truncation (glm-5.2 under
    # oversized context) is preserved in the log status and blackbox.
    if not saw_done:
        _elapsed = time.time() - start_time
        _cc = sum(len(s) for s in output_parts)
        _rc = sum(len(s) for s in reasoning_parts)
        _idle_gap = time.time() - _last_byte_ts
        # 上游响应头里的追踪 id（kimi/Moonshot request-id、cf-ray 等）——
        # 报障凭证。headers 在响应建立时就绪，流关闭后仍可读。
        _up_headers = {
            k: v
            for k, v in response.headers.items()
            if ("request" in k.lower() and "id" in k.lower())
            or "trace" in k.lower()
            or "ray" in k.lower()
            or k.lower() in ("server", "via")
        }
        logger.warning(
            "openai stream ended without [DONE] (model=%s, finish_reason=%s) "
            "— upstream truncation, synthesizing finish_reason=length for "
            "client auto-compaction",
            original_model,
            last_finish_reason or "<none>",
        )
        _dump_truncation_blackbox(
            model=original_model,
            base_url=log_entry.get("provider", ""),
            request_body=request_body,
            trigger={
                "saw_done": saw_done,
                "finish_reason": last_finish_reason,
                "content_chars": _cc,
                "reasoning_chars": _rc,
                "max_tokens": request_body.get("max_tokens") if request_body else None,
                "elapsed_s": round(_elapsed, 1),
                "provider": provider_name,
                "idle_gap_s": round(_idle_gap, 1),
                "upstream_headers": _up_headers or None,
                # 干净 EOF（StopAsyncIteration）且无 [DONE]：对端 FIN，应用层
                # 主动关——TCP keepalive 探测包无法阻止这类杀流。
                "close_style": "fin",
            },
        )
        # Upstream fault: report to the breaker so repeated truncations trip
        # OPEN (failure_threshold consecutive kills → escalating isolation),
        # and release session
        # stickiness so the next auto request can fall back to a healthier
        # model instead of re-locking onto this one.
        if provider_name:
            _cb_fail(provider_name, original_model)
        # Session penalty window: this provider just killed THIS session's
        # stream — demote it for this session's next requests so the
        # client's auto-recovery (pi compact-and-retry) doesn't slam back
        # into the same dying primary. The global breaker may never trip
        # here: other sessions' successes keep resetting its consecutive
        # counter (2026-09-07 incident).
        _session_penalty_record(session_key, provider_name, original_model)
        sid = log_entry.get("session_id", "")
        if sid:
            _session_invalidate(sid)
        # 诊断透传：上游静默断流不发任何错误帧，客户端（pi）只会看到
        # 一句通用的 "Response was truncated before completion"，所有失败
        # 模式归一成同一句话 → 无法排查。这里在合成 finish=length 之前注入
        # 一段可读的 cyRouter 诊断 delta，让用户能在会话里直接看到断流的
        # 极简现场（谁、耗时、分类、能否报障；细节查 data/blackbox/）。
        # 开关：config.truncation_notice（默认 true）。
        try:
            if load_config().get("truncation_notice", True):
                _notice = _truncation_notice_text(
                    provider_label=provider_name or log_entry.get("provider", "?"),
                    elapsed=_elapsed,
                    idle_gap=_idle_gap,
                    up_headers=_up_headers,
                )
                _nchunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(start_time),
                    "model": original_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": _notice},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(_nchunk, ensure_ascii=False)}\n\n"
        except Exception:
            logger.warning("truncation notice injection failed (openai)", exc_info=True)
        # Synthesize a length-stop terminator. pi maps finish_reason="length"
        # to stopReason="length"; isRecoverableLength(length, output<maxTokens)
        # then fires → auto-compaction with willRetry=true (compact + retry
        # once). This breaks the "once a session hits this it keeps happening"
        # dead-loop because the retried request runs against a compacted
        # (smaller) context.
        synth = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(start_time),
            "model": original_model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
        }
        yield f"data: {json.dumps(synth, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        await _flush("truncated")
    else:
        # Normal terminator seen ([DONE] or an empty-choices chunk). If the
        # upstream sent no standard [DONE] (opencode-go ends on an empty-choices
        # chunk), synthesize one so downstream OpenAI clients close the stream
        # cleanly instead of hanging or surfacing a spurious truncation error.
        if synthesize_finish and last_finish_reason is None:
            # OpenCode Go omits finish_reason entirely. Emit a clean
            # finish_reason="stop" chunk so clients treat the stream as a
            # normal completion instead of erroring/retrying.
            _synth = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(start_time),
                "model": original_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(_synth, ensure_ascii=False)}\n\n"
            last_finish_reason = "stop"
        if not forwarded_done:
            yield "data: [DONE]\n\n"
        await _flush("ok" if last_finish_reason else "finish_reason_missing")


# ── Route inspection ──────────────────────────────────────────────────


@app.get("/v1/routes/{model:path}")
async def openai_route_lookup(model: str):
    from router import _get_provider_candidates

    cands = _get_provider_candidates(model)
    if not cands:
        raise HTTPException(404, f"No provider for model: {model}")
    provider, mapped = cands[0]
    return {
        "model": model,
        "mapped_model": mapped,
        "provider": provider.name,
        "provider_type": provider.__class__.__name__,
        # Full provider chain (ordered); the first is the default provider.
        "providers": [p.name for p, _ in cands],
    }


# ── "auto" virtual model ───────────────────────────────────────────────
# When the client requests model "auto", cyRouter picks a concrete model
# and optionally tweaks the thinking field. No keyword heuristics, no
# token-count thresholds, no session stickiness — just two tiers + fallback
# + thinking control by message length.

_AUTO_DEFAULTS = {
    "simple_model": "deepseek-v4-flash",
    "complex_model": "glm-5.2",
    "simple_fallback": "",
    "complex_fallback": "",
    "vision_model": "",
    "vision_fallback": "",
    "thinking_threshold_chars": 200,
    # 小输出预算强制 flash 档的阈值（请求 max_tokens 小于此值且无 tools →
    # simple 档）；0 = 关闭。主对话 auto maxTokens=32768 远高于此。
    "max_tokens_simple_threshold": 16384,
    # 上下文大小选档：总请求 token 估算超过此值强制 complex 档。coding
    # agent 会话的尾消息常是「继续」之类的短句，但历史才是大头——用弱
    # 模型处理巨型上下文，能力与需求不匹配。
    "context_threshold_tokens": 32768,
    # prompt-cache 感知重选：粘性 TTL(10min) 过期后，上游缓存往往还活着
    # （缓存 TTL 是小时级）。重选时对比「留旧模型(热缓存价) vs 跳新模型
    # （首轮全价重算前缀）」的实际成本，选省的——缓存连续性远比模型单
    # 价差值钱（实测 cache_read 占 84~95%，cache_input 是 input 的 1/5~1/120）。
    "cache_aware_routing": True,
    "est_cache_ratio": 0.85,  # 稳态缓存命中率假设（实测 glm 95.5% / kimi 84.3%）
    "est_output_tokens": 2048,  # 每轮输出量估算（成本模拟用）
    # 配额压力降权（2026-08-26）：模型链上任一供应商的实测周池（
    # _usages_cache，来自 /usages 接口）使用率 >= 阈值时，把该模型降为
    # 次级候选——优先 fallback 模型；粘性会话在压力下也会被释放。
    # 阈值 0 = 禁用。这平滑了「额度耗尽才硬切」为「接近耗尽提前让路」。
    "quota_pressure_threshold": 90.0,
    # 延迟感知路由（2026-09-14）：小输出（实际输出 token ≤ slow_mark_max_output_tokens）
    # 的非流式请求完成耗时 ≥ slow_mark_ms（毫秒）→ 模型 slow-mark，auto 冷却期内
    # 改走 fallback。慢而成功的上游对配额标记和失败计数熔断器都隐形，此为第三维
    # 健康信号（2026-09-14 qwen3.8-flash 6~32s 慢 200 实证）。0 = 禁用。
    "slow_mark_ms": 4000,
    "slow_mark_max_output_tokens": 512,
}


def _get_model_map(cfg: dict) -> dict[str, list[str]]:
    """Unified model -> ordered provider-chain map.

    Reads cfg["models"] — the single source of truth where each model id
    (or glob pattern) maps to an ordered list of providers, tried in
    sequence on pre-content failure. (2026-09-04: legacy per-provider
    `models` lists and the `model_fallbacks` overlay were dead code and
    have been removed.)
    """
    m = cfg.get("models")
    if isinstance(m, dict) and m:
        return {k: (list(v) if isinstance(v, list) else [v]) for k, v in m.items()}
    return {}


def _get_available_models(cfg: dict) -> set[str]:
    # Concrete models only — glob chain keys aren't selectable model ids.
    return {m for m in _get_model_map(cfg) if "*" not in m and "?" not in m}


def _has_image_input(body: dict) -> bool:
    for m in body.get("messages", []):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for b in content:
                # Anthropic: image / OpenAI chat: image_url / OpenAI responses: input_image
                if isinstance(b, dict) and b.get("type") in (
                    "image",
                    "image_url",
                    "input_image",
                ):
                    return True
    return False


def _model_supports_vision(m: str, cfg: dict) -> bool:
    """Whether a model accepts image input, using the SAME resolution chain
    as /api/models capabilities.vision: model_overrides > model_meta (local
    authority) > BUILTIN_MODEL_META > name heuristic (vl/vision). Kept in
    one place so the router and the advertised capabilities can't drift apart.
    """
    name = (m or "").lower()
    if "embedding" in name:
        return False
    ov = (cfg.get("model_overrides") or {}).get(m) or {}
    if "vision" in ov:
        return bool(ov["vision"])
    meta = (cfg.get("model_meta") or {}).get(m) or {}
    if "vision" in meta:
        return bool(meta["vision"])
    builtin = BUILTIN_MODEL_META.get(m) or {}
    if "vision" in builtin:
        return bool(builtin["vision"])
    return "-vl" in name or "vision" in name or "vl-" in name


def _strip_image_blocks(body: dict, note: str) -> int:
    """Replace image content blocks with a text placeholder (in-place).

    Used by the vision-degraded fallback: when every vision-capable model
    is unavailable (e.g. kimi quota-exhausted) and auto.vision_fallback
    points at a TEXT-ONLY model, keeping the image blocks would make the
    upstream reject the whole request with 400 code 1210
    ("messages.content.type 参数非法，取值范围 ['text']") — the exact
    2026-08-30 incident. Stripping lets the text turn proceed instead of
    hard-erroring, with an honest note that the model cannot see images.

    Handles both wire formats (image / image_url / input_image blocks).
    Returns the number of image blocks replaced.
    """
    n = 0
    for m in body.get("messages", []):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for i, b in enumerate(content):
            if isinstance(b, dict) and b.get("type") in (
                "image",
                "image_url",
                "input_image",
            ):
                content[i] = {"type": "text", "text": note}
                n += 1
    return n


def _last_user_text(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
            return " ".join(parts)
    return ""


def _message_text(m: dict) -> str:
    """Extract the text of a message (content str or text-block list)."""
    content = m.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _auto_session_key(body: dict) -> str:
    """Fallback session key when the client sends no session header:
    hash(system-prompt prefix + first user-message prefix).

    Aligned with prompt-cache prefix semantics — upstream cache keys are the
    request prefix itself:
    - same conversation appending turns → prefix unchanged → same key
      (cache still hot, stickiness valid)
    - compaction / new conversation → prefix changes → new key (cache was
      dead anyway, re-picking loses nothing)
    Before this, stickiness relied entirely on client headers
    (x-cyrouter-session-id / x-conversation-id), which pi/opencode never
    send — stickiness never engaged and auto re-picked every turn,
    flip-flopping across tiers and re-warming the prefix cache at full
    price on every flip.
    """
    import hashlib

    msgs = body.get("messages") or []
    sys_txt = ""
    user_txt = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system" and not sys_txt:
            sys_txt = _message_text(m)[:2048]
        elif role == "user":
            user_txt = _message_text(m)[:2048]
            break
    if not (sys_txt or user_txt):
        return ""
    h = hashlib.sha256((sys_txt + "\x00" + user_txt).encode("utf-8", "ignore"))
    return "auto-" + h.hexdigest()[:16]


def _model_has_live_provider(model: str) -> bool:
    """True iff at least one provider on the model's chain can serve now.

    A provider counts as live when it is neither circuit-open (breaker)
    nor in fatal-error cooldown (quota/rate-limit mark) for this model.
    ALL-down semantics: only when EVERY provider on the chain is down is
    the model itself considered unavailable. A single tripped provider on
    a multi-provider chain must NOT flip auto's model choice — the chain
    absorbs that one level down (same model, next provider), and judging
    the whole model unhealthy on one OPEN provider was the pre-2026-08-13
    is_model_open bug. But when the chain has nothing live left (e.g.
    single-provider glm-5.3 with zhipu-coding circuit-open), auto must
    move to the fallback model instead of returning a guaranteed 503.
    """
    try:
        cands = _get_provider_candidates(model)
    except Exception:
        return True  # resolution hiccup — don't block routing on it
    if not cands:
        return False
    b = get_breaker()
    for provider, _mapped in cands:
        if _is_provider_model_failed(provider.name, model):
            continue
        if b.is_open(provider.name, model):
            continue
        return True
    return False


def _is_healthy(candidate: str) -> bool:
    """True iff model is available for auto routing.

    Two layers:
    1. Quota-exhaustion marks (``_mark_model_failed``: insufficient_quota /
       rate_limit_exceeded) — model-level cooldown.
    2. ALL providers on the model's chain down (circuit-open or fatal
       cooldown) — the provider chain can no longer absorb the failure, so
       the model itself is unavailable and auto moves to the fallback
       model (2026-08-13: single-provider glm-5.3 kept being picked while
       zhipu-coding was circuit-open → guaranteed 503s).

    Transient single-provider failures still do NOT flip auto's model —
    those are handled by the provider chain one level down.

    Plus latency health (2026-09-14): slow-marked models (small-output
    non-stream completions persistently over auto.slow_mark_ms) are
    skipped for the slow-mark cooldown — a sluggish-but-successful
    upstream is exactly what latency-sensitive consumers can't ride.
    """
    return (
        bool(candidate)
        and not _is_model_failed(candidate)
        and not _is_model_slow(candidate)
        and _model_has_live_provider(candidate)
    )


def _model_quota_pressure(model: str) -> float | None:
    """Live weekly-pool usage % for the model's chain, or None if unknown.

    Reads _usages_cache (populated by the /usages fetch loop). MIN across
    fresh provider readings: any chain provider with headroom can serve the
    model without pressure. None = no fresh data → no deprioritization
    (a provider without the /usages endpoint must never penalize its model).
    """
    try:
        cands = _get_provider_candidates(model)
    except Exception:
        return None
    best: float | None = None
    for provider, _mapped in cands:
        entry = _usages_cache.get(provider.name)
        if not entry:
            continue
        if entry.get("subscription") == "exhausted":
            # Confirmed subscription exhaustion = zero headroom until renewed.
            # No freshness check: the entry only exists while 429s persist; a
            # successful probe (renewal) replaces it immediately. Participates
            # in the MIN (another healthy provider keeps the model usable).
            if best is None:
                best = 100.0
            continue
        if (time.time() - entry.get("fetched_at", 0)) > _USAGES_STALE_SECONDS:
            continue
        weekly = entry.get("weekly") or {}
        pct = weekly.get("used_pct")
        if pct is None:
            continue
        if best is None or pct < best:
            best = pct
    return best


def _pick_model(target: str, fallback: str, available: set[str]) -> str:
    """Return the first of [target, fallback] that exists and is healthy.

    "Healthy" = present in `available`, not in the 15-min failed-model
    cooldown, and not circuit-open. Falls back to `target` if neither
    qualifies (caller gets a 404 or a 503 from the breaker).
    """
    for candidate in (target, fallback):
        if candidate and candidate in available and _is_healthy(candidate):
            return candidate
    return target  # give up — caller gets a 404 or breaker 503


# Session stickiness: once a session picks a model, keep using it so the
# conversation doesn't flip between simple/complex mid-stream. Invalidated
# when the model becomes unhealthy or after the TTL.
_session_model: dict[str, tuple[str, float]] = {}
_SESSION_STICKY_TTL = 600.0  # 10 minutes


def _session_set(session_id: str, model: str):
    if session_id and model:
        _session_model[session_id] = (model, time.time())


def _session_invalidate(session_id: str):
    _session_model.pop(session_id, None)


# ── Session-level provider penalty window ──────────────────────────────
# 2026-09-07 事故：zhipu-coding|glm-5.3 在 17 分钟内静默杀流 10 次（上游
# 网关 ~30s 零字节空闲超时，长思考不吐字必杀），但 6 个并行 auto 会话的
# 短回复成功不断把全局熔断器的「连续失败」计数清零（trips 永远 0↔1 震
# 荡，电路从不开）→ 被杀的会话重试时永远撞回同一家主供应商。根因：杀
# 流是按「请求形状」选择性发生的，而全局计数被其他会话的成功掩护。
# 对策：EOF 杀流 / pre-content 失败后，对 (session_key, model) 记一条
# 「惩罚」——时间窗内（默认 600s）该会话的供应商链把肇事者稳定排到
# 最后（不硬拒，单候选照用，保住 panic 语义）。成功请求无权清除惩罚，
# 只随时间过期：这是与「计数」语义的本质区别——连续计数会被会话自己
# 的短请求/compact 摘要成功重置，且 compact 会换 derived key 使计数归零。
_session_penalty: dict[tuple[str, str], tuple[str, float]] = {}
_SESSION_PENALTY_DEFAULT_TTL = 600.0


def _session_penalty_cfg(cfg: dict | None = None) -> tuple[bool, float]:
    """(enabled, duration_seconds) for the session penalty window."""
    sp = (cfg or load_config()).get("session_penalty")
    if not isinstance(sp, dict):
        return True, _SESSION_PENALTY_DEFAULT_TTL
    return (
        bool(sp.get("enabled", True)),
        float(sp.get("duration_seconds", _SESSION_PENALTY_DEFAULT_TTL)),
    )


def _session_penalty_record(session_key: str, provider: str, model: str) -> None:
    """Remember that `provider` just failed THIS session+model (silent
    stream kill or pre-content failure). Demote it to the last chain
    candidate for this session's subsequent requests; successes do NOT
    clear a penalty — only the time window does."""
    if not (session_key and provider and model):
        return
    enabled, _ = _session_penalty_cfg()
    if not enabled:
        return
    now = time.time()
    # Opportunistic pruning so the dict can't grow unbounded across sessions.
    if len(_session_penalty) > 256:
        stale = [
            k
            for k, (_, ts) in _session_penalty.items()
            if now - ts > _SESSION_PENALTY_DEFAULT_TTL
        ]
        for k in stale:
            _session_penalty.pop(k, None)
    _session_penalty[(session_key, model)] = (provider, now)
    logger.info(
        "session %s…: penalty %s|%s — demoted to last candidate for the penalty window",
        session_key[:8],
        provider,
        model,
    )


def _session_penalty_active(session_key: str, model: str) -> str | None:
    """Provider currently penalized for this session+model, or None."""
    if not session_key:
        return None
    ent = _session_penalty.get((session_key, model))
    if not ent:
        return None
    provider, ts = ent
    enabled, duration = _session_penalty_cfg()
    if not enabled or time.time() - ts > duration:
        _session_penalty.pop((session_key, model), None)
        return None
    return provider


def _session_penalty_reorder(
    session_key: str, model: str, names: list[str]
) -> list[int] | None:
    """Index permutation moving the penalized provider last (stable).

    None when nothing should change: no penalty / expired / provider not on
    this chain / fewer than 2 candidates / it already is last."""
    if len(names) < 2:
        return None
    p = _session_penalty_active(session_key, model)
    if not p or p not in names:
        return None
    order = [i for i, n in enumerate(names) if n != p] + [
        i for i, n in enumerate(names) if n == p
    ]
    if order == list(range(len(names))):
        return None
    return order


def _auto_tier_of(model: str, s: dict) -> int | None:
    """Tier rank of a model in the auto config: 0=simple, 1=complex,
    None=not a tier model (e.g. vision_model, or config changed since the
    session picked it). Distinguishes upgrades from downgrades: upgrades
    always switch immediately (quality first); same-tier/downgrade gets
    sticky/cost retention. Complex checked first so a model configured as
    both tiers ranks complex — harmless, target is the same model.
    """
    if model and model in (s.get("complex_model"), s.get("complex_fallback")):
        return 1
    if model and model in (s.get("simple_model"), s.get("simple_fallback")):
        return 0
    return None


def _auto_turn_cost(
    model_id: str, ctx_tokens: int, out_tokens: int, cfg: dict, cache_ratio: float
) -> float | None:
    """Estimated cost (RMB) of one turn on `model_id`.

    cache_ratio = fraction of the input context served from the upstream
    prompt cache: ~0.85 when staying on the warm model, 0.0 on a switch
    (first turn re-processes the whole prefix at full price — after that
    the new model's cache is warm too, so switching is a one-time loss).
    pricing units are RMB/M tokens. None = model has no pricing data
    (caller falls back to rule-based routing).
    """
    # config pricing > builtin catalog — fresh installs get builtin prices
    # instead of silently falling back to rule-based routing (2026-09-10).
    p = effective_pricing(cfg, model_id)
    if not isinstance(p, dict) or not p.get("input"):
        return None
    if p.get("plan_monthly"):
        # 套餐内边际成本≈0 —— 留/切都优先于按量计费模型。额度耗尽时该模型
        # 会被健康检查标记失败而退出候选，不会长期依赖这个 0。
        return 0.0
    ip = float(p.get("input") or 0)
    cp = float(p.get("cache_input") or ip)  # no cache price defined = no discount
    op = float(p.get("output") or 0)
    return (
        ctx_tokens * (cache_ratio * cp + (1.0 - cache_ratio) * ip) + out_tokens * op
    ) / 1e6


def _resolve_auto_model(body: dict, cfg: dict, exclude: set[str] | None = None) -> str:
    """Pick a model for the `auto` virtual model. May mutate `body`.

    `exclude` is used by the in-request reroute path: models that already
    failed fatally during THIS request are filtered out of availability so
    the retry lands on a live model instead of the dead one (2026-08-30
    incident: kimi-k3 weekly quota wall hit while glm-5.3 was transiently
    failed-marked — the discovering request ate a raw 403 even though auto
    switched every later request to glm).
    """
    auto_cfg = (cfg.get("auto") or {}) if isinstance(cfg, dict) else {}
    s = {**_AUTO_DEFAULTS, **auto_cfg}
    avail = _get_available_models(cfg)
    if exclude:
        avail = {m for m in avail if m not in exclude}

    has_tools = bool(body.get("tools"))
    has_images = _has_image_input(body)
    last_len = len(_last_user_text(body))
    # Session key: client header wins; otherwise derive from the request
    # prefix (aligned with upstream prompt-cache keys — see _auto_session_key).
    session_id = body.get("_session_id", "") or _auto_session_key(body)

    # Image input is a HARD constraint: the turn must land on a
    # vision-capable, healthy model — or degrade explicitly. Checked before
    # stickiness so an image turn never reuses a non-vision sticky model.
    # Candidate order:
    #   1. auto.vision_model (if healthy)
    #   2. any vision-capable tier candidate (simple/complex + fallbacks)
    #   3. auto.vision_fallback — degraded mode: if it supports vision use
    #      it as-is; if it is text-only (e.g. glm-5.3-flash), STRIP the
    #      image blocks (replaced by an honest placeholder) so the text
    #      turn proceeds. Without the strip, a text-only upstream rejects
    #      the request with an opaque 400 (zhipu code 1210) — the
    #      2026-08-30 incident.
    #   4. explicit 503: client gets a truthful "no vision model available"
    #      instead of a bogus 400.
    if has_images:
        vm = s["vision_model"]
        # 2026-09-11 事故：vision_model 被配成纯文本模型（kimi 过期后改配时
        # 误指 glm-5.3）时，这里只查健康不查能力，图片盲发给纯文本模型 →
        # volcano-apig 400 code 1210「messages.content.type 参数非法」。
        # 图片是硬约束：任何一步选模型都必须验 vision 能力（第 2/3 步
        # 早已如此，第 1 步补齐）。
        vm_vision = _model_supports_vision(vm, cfg) if vm else False
        if vm and not vm_vision:
            logger.warning(
                f"auto: vision_model '{vm}' does not support image input "
                f"(capabilities.vision=false) — skipping it for image turns; "
                f"fix auto.vision_model to a vision-capable model"
            )
        if vm and vm_vision and vm in avail and _is_healthy(vm):
            logger.info(f"auto: image → {vm}")
            _session_set(session_id, vm)
            return vm
        vision_candidates = [
            m
            for m in (
                vm,
                s["simple_model"],
                s["simple_fallback"],
                s["complex_model"],
                s["complex_fallback"],
            )
            if m
        ]
        seen: set[str] = set()
        for cand in vision_candidates:
            if cand in seen:
                continue
            seen.add(cand)
            if (
                cand in avail
                and _is_healthy(cand)
                and _model_supports_vision(cand, cfg)
            ):
                logger.warning(
                    f"auto: vision_model '{vm or '∅'}' unavailable, "
                    f"image → vision-capable {cand}"
                )
                _session_set(session_id, cand)
                return cand
        vf = s.get("vision_fallback") or ""
        if vf and vf in avail and _is_healthy(vf):
            if _model_supports_vision(vf, cfg):
                logger.warning(
                    f"auto: vision_model '{vm or '∅'}' unavailable, "
                    f"image → vision-capable vision_fallback {vf}"
                )
                _session_set(session_id, vf)
                return vf
            note = (
                "[图片内容已省略：视觉模型暂时不可用（额度冷却/熔断），"
                "本轮降级为纯文本处理，模型无法看到图片。请稍后重试图片请求。]"
            )
            n = _strip_image_blocks(body, note)
            logger.warning(
                f"auto: vision_model '{vm or '∅'}' unavailable → "
                f"degraded to vision_fallback {vf} "
                f"(stripped {n} image block(s), model cannot see images)"
            )
            _session_set(session_id, vf)
            return vf
        _names = "、".join(seen) or "无候选"
        raise HTTPException(
            status_code=503,
            detail=(
                f"auto 图片路由失败：视觉模型不可用（可能处于额度冷却/熔断中），"
                f"且候选 {_names} 均不支持图片输入。请稍后重试或检查 "
                f"auto.vision_model 配置及该模型供应商额度。"
            ),
        )

    # ── 选档：输出预算是任务规模的直接信号 ──────────────────────────────
    # 「简单任务」的真实画像是 output 小 + 无 tools；prompt 长度只是素材量
    # （judge/记忆提取/标题类辅助任务 = 长 input + 小 output，提取长文是素材
    # 长不是任务难）。用尾消息长度 <200 判定会把它们误判进 complex 档的
    # reasoning 大模型，而 reasoning 的 thinking 阶段与输出共享 max_tokens，
    # 小预算必被吃光 → 空 content / finish=length（2026-09-08 根因：yinor
    # 「LLM 返回空响应」+ pi session-llm「truncated at max_tokens」，辅助链
    # max_tokens 2048/8192 全被 complex 档 glm-5.3 吃爆）。
    # max_tokens_simple_threshold 默认 16384（主对话 auto maxTokens=32768 远
    # 高于此），0 = 关闭。
    ctx_tokens = _estimate_request_tokens(body)
    _budget_thr = int(s.get("max_tokens_simple_threshold") or 16384)
    _out_budget = body.get("max_tokens")
    small_budget = (
        _budget_thr > 0
        and isinstance(_out_budget, int)
        and 0 < _out_budget < _budget_thr
    )
    is_simple = not has_tools and (
        small_budget
        or (
            last_len < s["thinking_threshold_chars"]
            and ctx_tokens < int(s.get("context_threshold_tokens") or 0)
        )
    )
    target = _pick_model(
        s["simple_model"] if is_simple else s["complex_model"],
        s["simple_fallback"] if is_simple else s["complex_fallback"],
        avail,
    )

    # Quota-pressure deprioritization: a fresh live /usages reading showing
    # the model's plan pool nearly exhausted makes it a secondary pick —
    # smooth handoff BEFORE the hard 403 wall (the fatal mark only fires
    # after the first failed request). Disabled at threshold 0 or when no
    # provider on the chain reports live usage.
    _pressure_thr = float(s.get("quota_pressure_threshold") or 0)

    def _pressured(m: str) -> bool:
        if _pressure_thr <= 0 or not m:
            return False
        p = _model_quota_pressure(m)
        return p is not None and p >= _pressure_thr

    if _pressured(target):
        alt = s["simple_fallback"] if is_simple else s["complex_fallback"]
        if (
            alt
            and alt != target
            and alt in avail
            and _is_healthy(alt)
            and not _pressured(alt)
        ):
            logger.info(
                "auto: quota pressure %s≥%.0f%% → prefer fallback %s",
                target,
                _pressure_thr,
                alt,
            )
            target = alt

    # Unified sticky / cache-aware decision (heuristic routing only —
    # skipped for image turns, which are a hard constraint).
    # Principle: an UPGRADE (rule wants a higher tier than the session's
    # current model) always switches immediately — quality first. Same-tier
    # or downgrade re-picks may stay on the current model: unconditionally
    # within the sticky TTL, cost-compared after it (the upstream prompt
    # cache outlives the TTL by hours; switching re-warms at full price).
    #
    # sticky 的适用域是「同前缀长会话」（cache 连续性是粘性存在的全部理
    # 由）。小预算辅助请求（judge/提取/标题）单发单收、prompt 每次不同、
    # 无前缀连续性——粘性对它们只有害（可能被同 key 的大预算请求粘到大
    # 模型）而无益（无 cache 可续），不进入 sticky 系统（不读旧记录，
    # 返回时也不写记录，见函数尾部）。
    if not has_images and not small_budget:
        prev = _session_model.get(session_id)
        last_model, last_ts = prev if prev else ("", 0.0)
        if last_model == target and last_model:
            _session_set(session_id, target)  # refresh TTL
            logger.info(f"auto: session {session_id[:8]} sticky → {target}")
            return target
        if (
            last_model
            and last_model in avail
            and _is_healthy(last_model)
            and not _pressured(last_model)
        ):
            sticky_tier = _auto_tier_of(last_model, s)
            rule_tier = 0 if is_simple else 1
            # Restore-to-primary: the session is sitting on a FALLBACK model
            # (reached only because the primary was down) and the rule pick
            # is the primary again — switch back immediately, bypassing
            # sticky TTL and cache-aware stay. Without this, plan-monthly
            # pairs (stay ¥0 <= switch ¥0) pinned the session on the
            # fallback forever after the primary recovered.
            primary = s["simple_model"] if is_simple else s["complex_model"]
            restore_to_primary = (
                last_model != primary
                and target == primary
                and last_model in (s["simple_fallback"], s["complex_fallback"])
            )
            if sticky_tier is None:
                # Non-tier sticky (vision_model, or the slot was reconfigured
                # away since the session picked it): stickiness doesn't apply,
                # re-pick. Without this, text turns after an image turn kept
                # routing to the vision model for the whole sticky TTL (and
                # forever once cache-aware also favors staying).
                logger.info(
                    "auto: session %s drop non-tier sticky %s",
                    session_id[:8],
                    last_model,
                )
            elif rule_tier > sticky_tier:
                logger.info(
                    "auto: upgrade %s → %s (tier %d→%d, last=%dc, ctx=%d)",
                    last_model,
                    target,
                    sticky_tier,
                    rule_tier,
                    last_len,
                    ctx_tokens,
                )
            elif restore_to_primary:
                logger.info(
                    "auto: restore %s → %s (primary healthy again)",
                    last_model,
                    target,
                )
            elif (time.time() - last_ts) <= _SESSION_STICKY_TTL:
                logger.info(f"auto: session {session_id[:8]} sticky → {last_model}")
                _session_set(session_id, last_model)
                return last_model
            elif s.get("cache_aware_routing"):
                out_est = int(s.get("est_output_tokens") or 2048)
                ratio = float(s.get("est_cache_ratio") or 0.85)
                stay = _auto_turn_cost(last_model, ctx_tokens, out_est, cfg, ratio)
                switch = _auto_turn_cost(target, ctx_tokens, out_est, cfg, 0.0)
                if stay is not None and switch is not None and stay <= switch:
                    logger.info(
                        "auto: cache-aware stay %s (¥%.4f) < switch %s (¥%.4f), ctx=%d",
                        last_model,
                        stay,
                        target,
                        switch,
                        ctx_tokens,
                    )
                    _session_set(session_id, last_model)
                    return last_model
                logger.info(
                    "auto: cache-aware switch %s → %s (stay=%s switch=%s, ctx=%d)",
                    last_model,
                    target,
                    f"¥{stay:.4f}" if stay is not None else "n/a",
                    f"¥{switch:.4f}" if switch is not None else "n/a",
                    ctx_tokens,
                )

    # Pure passthrough: do not disable thinking for GLM. Previously this
    # stripped thinking on short GLM-5.x turns to avoid the upstream
    # truncation glitch — that was papering over an upstream bug. The
    # router now forwards the client's thinking setting unchanged.
    logger.info(
        f"auto: → {target} (tools={has_tools}, last={last_len}c, ctx={ctx_tokens}, budget={_out_budget})"
    )

    if not small_budget:
        # 长会话请求才留粘性记录；小预算辅助请求不污染 sticky（见上方注释）
        _session_set(session_id, target)
    return target


# ── Anthropic-compatible API (raw HTTP proxy) ─────────────────────────


def _strip_cch(text: str) -> str:
    """Remove Claude Code Attribution Header lines from text.

    Claude Code prepends a unique `anthropic-attribution: cch=<id>` line to
    the system prompt on every request.  Third-party upstreams use the prefix
    of the prompt body as a cache key, so this ever-changing header destroys
    prompt caching.  Strip it so the cache key stays stable.
    """
    import re

    return re.sub(
        r"^anthropic-attribution:.*\n?", "", text, count=1, flags=re.MULTILINE
    )


def _strip_cch_from_body(body: dict):
    """Strip CCH from the system prompt in an Anthropic-format request body (in-place)."""
    system = body.get("system")
    if isinstance(system, str):
        body["system"] = _strip_cch(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = _strip_cch(block.get("text", ""))


def _repair_tool_adjacency(messages) -> tuple[list, int, int]:
    """Hoist tool results back to their assistant when a non-tool message
    got injected between an assistant's tool_calls and its tool results.

    Real-world trigger (2026-08-28 DancherLink incident): the pi-processes
    extension injects process lifecycle events (e.g. ``<process_event
    type="lifecycle" kind="crash">) as user-role custom messages MID-TURN —
    landing exactly between an assistant's ``taskkill`` tool_call and its
    tool result. kimi code strictly validates OpenAI adjacency (tool
    messages must immediately follow the assistant carrying the matching
    ``tool_calls``) and rejects the WHOLE request:

        400 messages with role 'tool' must be a response to a preceeding
        message with 'tool_calls'

    Tolerant upstreams (GLM, ...) accept the shape, so a session can carry
    the broken junction silently until auto-routing lands it on kimi.

    Repair semantics: within one assistant tool-turn, reorder to
    ``[assistant(tool_calls), tool results..., intruding messages...]`` —
    tool results keep their relative order, intruders keep theirs, nothing
    is dropped. Turns whose tool results never arrive (aborted calls) are
    left untouched; tool messages referencing foreign ids are never moved.

    Returns ``(repaired_list, n_moved_tools, n_intruders)``.
    """
    if not isinstance(messages, list):
        return messages, 0, 0
    out = list(messages)
    moved = intruders_n = 0
    i = 0
    while i < len(out):
        m = out[i]
        if not (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and isinstance(m.get("tool_calls"), list)
            and m["tool_calls"]
        ):
            i += 1
            continue
        ids = {
            tc.get("id")
            for tc in m["tool_calls"]
            if isinstance(tc, dict) and tc.get("id")
        }
        if not ids:
            i += 1
            continue
        # Walk the turn segment after this assistant, collecting its tool
        # results and any user/system intruders, until the turn really ends
        # (another assistant, or a tool message answering someone else).
        tools: list[int] = []
        intruders: list[int] = []
        p = i + 1
        while p < len(out):
            mm = out[p]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool" and mm.get("tool_call_id") in ids:
                tools.append(p)
            elif role in ("user", "system", "developer"):
                intruders.append(p)
            else:
                break
            p += 1
        if tools and intruders and min(intruders) < max(tools):
            out[i + 1 : p] = [out[x] for x in tools] + [out[x] for x in intruders]
            moved += len(tools)
            intruders_n += len(intruders)
            i = p
            continue
        i += 1
    return out, moved, intruders_n


def _apply_tool_adjacency_repair(body: dict, cfg: dict | None = None) -> None:
    """In-place tool-adjacency repair on an OpenAI-format request body.

    Opt-out via ``config.message_repair.tool_adjacency = false``.
    """
    cfg = cfg or load_config()
    mr = cfg.get("message_repair") or {}
    if mr.get("tool_adjacency", True) is False:
        return
    msgs = body.get("messages")
    repaired, moved, intruders = _repair_tool_adjacency(msgs)
    if moved:
        body["messages"] = repaired
        logger.info(
            "tool-adjacency repair: hoisted %d tool result(s) above %d "
            "mid-turn message(s) that broke assistant(tool_calls)→tool "
            "adjacency",
            moved,
            intruders,
        )


def _ensure_reasoning_content(body: dict) -> None:
    """Ensure assistant messages carry reasoning_content (DeepSeek/GLM).

    Clients (pi 0.83.0, opencode, ...) commonly discard reasoning_content
    from thinking-model responses. DeepSeek returns 400 when the field is
    missing on tool-call rounds — and worse, an empty or missing value
    breaks reasoning continuity: the model confabulates confidently
    instead of continuing its prior train of thought.

    Strategy: replay the REAL reasoning from the tool_call_id-keyed cache
    whenever possible; fall back to "" only when no cache entry exists
    (still avoids the 400; a miss is logged for observability).
    """
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return
    cache = get_reasoning_cache()
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if m.get("reasoning_content"):
            continue  # client sent real reasoning — pass through untouched
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            replay = cache.lookup_by_tool_calls(tool_calls)
            if replay:
                m["reasoning_content"] = replay
                continue
            logger.info(
                "reasoning replay miss: assistant turn with %d tool_calls "
                "has no cached reasoning",
                len(tool_calls),
            )
        m["reasoning_content"] = ""


def _prepare_anthropic_body(body: dict) -> dict:
    """Return a deep copy of the request body for upstream forwarding.

    Transparent passthrough — previous optimisations (stripping thinking blocks,
    downgrading tool_choice, removing unsupported params) were based on wrong
    assumptions and broke opencode's direct-connect workflow with GLM. Verified
    that opencode → GLM works without cyRouter, so cyRouter must not modify
    the body beyond what's strictly necessary.

    Only transformation kept: strip Claude Code Attribution Header (CCH) from
    system prompt. This is unique to Claude Code and destroys prompt caching
    if left in place. opencode doesn't send it, so this is a no-op for opencode.
    """
    body = json.loads(json.dumps(body))
    _strip_cch_from_body(body)
    return body


def _get_upstreams_for_model(
    model: str, force_provider: str | None = None
) -> list[tuple]:
    """Ordered Anthropic-compatible upstream candidates for a model.

    Returns a list of (base_url, api_key, mapped_model, provider_type,
    provider) in priority order: primary first, then `model_fallbacks`
    candidates. Only providers that actually support the Anthropic protocol
    are included — either natively (ClaudeProvider) or via a declared
    anthropic base URL (dual-protocol providers like zhipu / deepseek).
    OpenAI-only providers (e.g. qwen) are skipped; callers reject the request
    with a clear error when the list is empty rather than routing through the
    broken Anthropic→OpenAI translation layer. Providers in fatal-error
    cooldown are dropped when alternatives remain.
    """
    from providers.claude import ClaudeProvider

    out: list[tuple] = []
    for provider, mapped in _get_provider_candidates(model):
        if force_provider and provider.name != force_provider:
            continue
        api_key = provider.api_key
        p_cfg = _find_provider_cfg(provider.name)

        # Dual-protocol: explicit anthropic URL in config → raw proxy
        if p_cfg:
            anthropic_url = p_cfg.get("base_urls", {}).get("anthropic")
            if anthropic_url:
                out.append(
                    (anthropic_url, api_key, (mapped or model), "anthropic", provider)
                )
                continue

        # Native Anthropic provider (Claude SDK)
        if isinstance(provider, ClaudeProvider):
            base_url = provider.base_url or "https://api.anthropic.com"
            out.append((base_url, api_key, (mapped or model), "anthropic", provider))
        # OpenAI-only provider — no Anthropic protocol support; skip.
    if len(out) > 1:
        filtered = [u for u in out if not _is_provider_model_failed(u[4].name, model)]
        if filtered:
            out = filtered
    return out


def _build_log_entry(body: dict, provider_base_url: str, is_stream: bool) -> dict:
    """Build a log entry for the Anthropic proxy path."""
    from providers.base import flatten_content

    messages = body.get("messages", [])
    return {
        "timestamp": time.time(),
        "model": body.get("model", ""),
        "provider": provider_base_url,
        "messages": len(messages),
        "stream": is_stream,
        "input": [
            {
                "role": m.get("role", ""),
                "content": flatten_content(m.get("content", "")),
            }
            for m in messages
        ],
        "output": "",
        "status": "pending",
    }


async def _finish_log(
    log_entry: dict,
    status: str,
    start: float,
    result: dict | None = None,
    error: str | None = None,
):
    """Complete a log entry with status, latency, and optionally output/usage."""
    log_entry["status"] = status
    log_entry["latency_ms"] = int((time.time() - start) * 1000)
    if error:
        log_entry["error"] = error
    if result:
        content = result.get("content", [])
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(f"[Tool: {block.get('name', '')}]")
            log_entry["output"] = "".join(parts)
        elif isinstance(content, str):
            log_entry["output"] = content
        usage = _normalize_usage(result.get("usage") or {})
        if usage:
            log_usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
            if "cache_read_input_tokens" in usage:
                log_usage["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" in usage:
                log_usage["cache_creation_input_tokens"] = usage[
                    "cache_creation_input_tokens"
                ]
            log_entry["usage"] = log_usage
    await _log_request(log_entry)


async def _raw_anthropic_sse_stream(
    first_chunk: bytes,
    byte_iter: AsyncIterator[bytes],
    response: httpx.Response,
    original_model: str,
    start_time: float,
    log_entry: dict,
    provider_name: str = "",
    request_body: dict | None = None,
    session_key: str = "",
):
    """Byte-level passthrough with side-channel observation.

    Forwards upstream bytes verbatim to the client — no JSON re-serialisation,
    no id/stop_reason normalisation (those broke opencode's direct workflow
    with GLM). The only byte modification is replacing the "model" field in
    message_start so the client sees its requested model name.

    Concurrently observes the byte stream (parsing SSE lines from a side buffer)
    to populate log_entry — usage tokens, output text, stream integrity status.
    This observation never modifies the forwarded bytes.

    Stream integrity protection: if upstream sends message_start but never
    sends message_stop (truncated stream), synthesise an in-band `event: error`
    so downstream SDKs hit their retry path instead of hanging.
    """
    import re

    _model_pattern = re.compile(rb'"model":"[^"]*"')

    output_parts: list[str] = []
    reasoning_parts: list[str] = []  # thinking_delta content for logging
    usage_tokens: dict = {}
    sent_message_start = False
    sent_message_stop = False
    stream_error: str | None = None  # set when upstream sends event:error
    line_buffer = b""
    # 取证：最后一次收到上游字节的时刻（idle gap 分类断流根因，见 OpenAI 路径）。
    # 心跳不更新它——那是我们发给客户端的，不是上游字节。
    _last_byte_ts = start_time
    # 当前打开的 content_block index——截流合成终止序列时按真实 index 闭合
    _open_block_index: int | None = None

    def _observe_line(line: bytes) -> None:
        """Parse a complete SSE line for logging. Does NOT modify forwarded bytes."""
        nonlocal sent_message_start, sent_message_stop, stream_error, _open_block_index
        payload = _sse_data_payload(line)
        if payload is None:
            return
        try:
            data = json.loads(payload.decode(errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        et = data.get("type", "")
        if et == "error":
            err = data.get("error", {})
            if isinstance(err, dict):
                stream_error = f"upstream {err.get('type', 'error')}: {err.get('message', '')[:200]}"
            else:
                stream_error = str(err)[:200]
            return
        if et == "message_start":
            sent_message_start = True
            u = (data.get("message") or {}).get("usage", {}) or {}
            if "input_tokens" in u:
                usage_tokens["input_tokens"] = u["input_tokens"]
            if "cache_creation_input_tokens" in u:
                usage_tokens["cache_creation_input_tokens"] = u[
                    "cache_creation_input_tokens"
                ]
            if "cache_read_input_tokens" in u:
                usage_tokens["cache_read_input_tokens"] = u["cache_read_input_tokens"]
        elif et == "message_delta":
            u = data.get("usage", {}) or {}
            if "output_tokens" in u:
                usage_tokens["output_tokens"] = u["output_tokens"]
            if "input_tokens" in u:
                usage_tokens["input_tokens"] = u["input_tokens"]
            if "cache_read_input_tokens" in u:
                usage_tokens["cache_read_input_tokens"] = u["cache_read_input_tokens"]
        elif et == "message_stop":
            sent_message_stop = True
        elif et == "content_block_start":
            _open_block_index = data.get("index")
        elif et == "content_block_stop":
            _open_block_index = None

        delta = data.get("delta", {}) or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            output_parts.append(delta.get("text", ""))
        elif dtype == "input_json_delta":
            output_parts.append(f"[tool_args:{delta.get('partial_json', '')}]")
        if dtype == "thinking_delta":
            thinking = delta.get("thinking", "")
            if thinking:
                reasoning_parts.append(thinking)

        cb = data.get("content_block", {}) or {}
        if cb.get("type") == "tool_use":
            output_parts.append(f"[Tool: {cb.get('name', '')}]")

    def _observe_chunk(chunk: bytes) -> None:
        """Feed chunk into line buffer, observe any complete lines."""
        nonlocal line_buffer
        line_buffer += chunk
        while b"\n" in line_buffer:
            line, line_buffer = line_buffer.split(b"\n", 1)
            _observe_line(line)

    async def _flush(status: str, error: str | None = None):
        log_entry["status"] = status
        log_entry["output"] = "".join(output_parts)
        if reasoning_parts:
            log_entry["reasoning"] = "".join(reasoning_parts)
        log_entry["usage"] = _normalize_usage(usage_tokens)
        log_entry["latency_ms"] = int((time.time() - start_time) * 1000)
        if error:
            log_entry["error"] = error
        await _log_request(log_entry)

    # The router never terminates an active stream — pure passthrough.

    try:
        if first_chunk:
            chunk = first_chunk
            # message_start is always in first_chunk (probe guarantees 200 + first byte)
            if b'"type":"message_start"' in chunk and original_model:
                replacement = f'"model":"{original_model}"'.encode()
                chunk = _model_pattern.sub(replacement, chunk, count=1)
            _observe_chunk(chunk)
            _last_byte_ts = time.time()  # 取证
            yield chunk

        # Stream remaining chunks with heartbeat: if upstream is silent for >30s
        # (common in GLM thinking mode between message_start and first delta),
        # emit a SSE comment line to keep the client connection alive.
        while True:
            try:
                chunk = await asyncio.wait_for(byte_iter.__anext__(), timeout=30.0)
            except asyncio.TimeoutError:
                # No active termination — a 30s chunk gap only emits an SSE
                # comment to keep the connection alive (pure passthrough).
                yield b": heartbeat\n\n"
                continue
            except StopAsyncIteration:
                break
            _last_byte_ts = time.time()  # 取证（心跳分支不更新——那是我们发的）
            _observe_chunk(chunk)
            yield chunk

        # Flush any trailing partial line (last line without \n terminator)
        if line_buffer:
            _observe_line(line_buffer)
            line_buffer = b""
    except asyncio.CancelledError:
        logger.info("stream cancelled by client (model=%s)", original_model)
        await _flush("cancelled")
        raise
    except Exception as e:
        _close_style = _stream_close_exc_style(e)
        logger.error("Anthropic raw proxy stream error (close=%s): %s", _close_style, e)
        await _flush("error", error=str(e))
        # FIN/RST 取证（与 OpenAI 路径对齐）：RST / fin_abrupt 杀流走异常
        # 分支，单独留黑盒样本定位杀流者层级；客户端行为不变。
        if _close_style in ("rst", "fin_abrupt"):
            try:
                _dump_truncation_blackbox(
                    model=original_model,
                    base_url=log_entry.get("provider", ""),
                    request_body=request_body,
                    trigger={
                        "close_style": _close_style,
                        "shape": "anthropic",
                        "error": str(e)[:300],
                        "content_chars": sum(len(s) for s in output_parts),
                        "reasoning_chars": sum(len(s) for s in reasoning_parts),
                        "elapsed_s": round(time.time() - start_time, 1),
                        "idle_gap_s": round(time.time() - _last_byte_ts, 1),
                        "provider": provider_name,
                    },
                )
            except Exception:
                logger.warning(
                    "blackbox dump failed (anthropic rst path)", exc_info=True
                )
        error_event = {
            "type": "error",
            "error": {"type": "router_error", "message": str(e)},
        }
        yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
        return
    finally:
        # Critical: release the upstream connection back to httpx pool.
        with suppress(Exception):
            await response.aclose()

    # Stream integrity / error detection.
    # Case 1: upstream sent an error event (e.g. zhipu rate_limit_error 1302)
    # without ever sending message_start. Log as error so the dashboard
    # reflects reality — the client already received the error event in-band.
    if stream_error and not sent_message_start:
        await _flush("error", error=stream_error)
        return

    # Case 2: upstream sent message_start but never sent message_stop
    # (truncated stream). Synthesize a complete terminator sequence with
    # stop_reason="max_tokens" so downstream clients (pi) map it to
    # stopReason="length" and auto-compact + retry, instead of surfacing
    # an opaque "Anthropic stream ended before message_stop" error that
    # neither the client nor the model can recover from.
    if sent_message_start and not sent_message_stop:
        diagnostic = "upstream stream truncated (no message_stop received)"
        logger.warning(f"[stream-integrity] {diagnostic} model={original_model}")
        # ── 取证（与 OpenAI 路径对齐）：idle gap 区分「网关空闲超时杀流」与
        # 「活跃传输中被杀」；上游响应头 request-id 是向供应商报障的凭证。
        _elapsed = time.time() - start_time
        _idle_gap = time.time() - _last_byte_ts
        _cc = sum(len(s) for s in output_parts)
        _rc = sum(len(s) for s in reasoning_parts)
        _up_headers = {
            k: v
            for k, v in response.headers.items()
            if ("request" in k.lower() and "id" in k.lower())
            or "trace" in k.lower()
            or "ray" in k.lower()
            or k.lower() in ("server", "via")
        }
        _dump_truncation_blackbox(
            model=original_model,
            base_url=log_entry.get("provider", ""),
            request_body=request_body,
            trigger={
                "saw_done": False,
                "shape": "anthropic",
                "diagnostic": diagnostic,
                "content_chars": _cc,
                "reasoning_chars": _rc,
                "max_tokens": request_body.get("max_tokens") if request_body else None,
                "elapsed_s": round(_elapsed, 1),
                "provider": provider_name,
                "idle_gap_s": round(_idle_gap, 1),
                "upstream_headers": _up_headers or None,
                # 干净 EOF（StopAsyncIteration）且无 message_stop：对端 FIN，
                # 应用层主动关——TCP keepalive 探测包无法阻止这类杀流。
                "close_style": "fin",
            },
        )
        # Upstream fault: report to the breaker + release session stickiness
        # (see _raw_openai_sse_stream for rationale).
        if provider_name:
            _cb_fail(provider_name, original_model)
        # Session penalty window (see _raw_openai_sse_stream for rationale):
        # demote this provider for THIS session's next requests.
        _session_penalty_record(session_key, provider_name, original_model)
        sid = log_entry.get("session_id", "")
        if sid:
            _session_invalidate(sid)
        await _flush("truncated", error=diagnostic)
        # Synthesize a length-stop terminator. pi maps Anthropic
        # stop_reason="max_tokens" to stopReason="length";
        # isRecoverableLength(length, output<maxTokens) then fires →
        # auto-compaction with willRetry=true (compact + retry once). This
        # breaks the truncation dead-loop because the retried request runs
        # against a compacted (smaller) context.
        # 闭合仍在打开的 content_block（用真实 index，而非盲猜 0）
        _next_index = 0
        if _open_block_index is not None:
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': _open_block_index})}\n\n"
            _next_index = _open_block_index + 1
        # 诊断透传（config.truncation_notice，默认 true）：上游静默断流不发
        # 任何错误帧，客户端只会看到 stop_reason=max_tokens，与真正的
        # max_tokens 截断无法区分。注入一个文本块说明断流现场。
        try:
            if load_config().get("truncation_notice", True):
                _notice = _truncation_notice_text(
                    provider_label=provider_name or log_entry.get("provider", "?"),
                    elapsed=_elapsed,
                    idle_gap=_idle_gap,
                    up_headers=_up_headers,
                )
                _bs = {
                    "type": "content_block_start",
                    "index": _next_index,
                    "content_block": {"type": "text", "text": ""},
                }
                yield f"event: content_block_start\ndata: {json.dumps(_bs, ensure_ascii=False)}\n\n"
                _bd = {
                    "type": "content_block_delta",
                    "index": _next_index,
                    "delta": {"type": "text_delta", "text": _notice},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(_bd, ensure_ascii=False)}\n\n"
                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': _next_index})}\n\n"
        except Exception:
            logger.warning(
                "truncation notice injection failed (anthropic)", exc_info=True
            )
        msg_delta = {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
            "usage": {"output_tokens": usage_tokens.get("output_tokens", 0)},
        }
        yield f"event: message_delta\ndata: {json.dumps(msg_delta, ensure_ascii=False)}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
        return

    await _flush("ok")


async def _anthropic_raw_stream_once(
    *,
    upstream_url: str,
    headers: dict,
    body: dict,
    model: str,
    base_url: str,
    provider_name: str,
    start: float,
    trace_id: str,
    session_key: str = "",
    redaction_map,
    max_attempts: int,
    gate_timeout: float,
) -> tuple:
    """Streaming attempt against ONE Anthropic-compatible upstream.

    Returns (response, should_fallback); see _openai_raw_stream_once.
    """
    for attempt in range(max_attempts):
        attempt_log = _build_log_entry(body, base_url, True)
        attempt_log["attempt_index"] = attempt
        attempt_log["trace_id"] = trace_id
        # Pre-probe upstream (same rationale as the OpenAI path):
        # without this, GLM/DeepSeek 1305 soft-errors arrive as in-band
        # SSE events on a 200 response and Anthropic SDK's retry logic
        # (which keys off HTTP status + error.type) never fires.
        try:
            probed = await _probe_upstream_stream(
                upstream_url,
                headers,
                body,
                anthropic_shape=True,
                log_entry=attempt_log,
                start_time=start,
                provider_name=provider_name,
                model=model,
            )
        except httpx.TimeoutException as e:
            await _finish_log(attempt_log, "error", start, error=f"timeout: {e}")
            _cb_fail(provider_name, model)
            return _timeout_error_response(e, anthropic_shape=True), True
        except Exception as e:
            await _finish_log(attempt_log, "error", start, error=str(e))
            _cb_fail(provider_name, model, exc=e)
            return _sdk_exception_response(e, anthropic_shape=True), True
        if probed.error_response is not None:
            _gate_body = ""
            try:
                _gate_body = bytes(probed.error_response.body).decode("utf-8")
            except Exception:
                pass
            _cb_fail(
                provider_name,
                model,
                status=probed.error_response.status_code,
                body_text=_gate_body,
            )
            return probed.error_response, True
        # First-token gate: same rationale as OpenAI raw-proxy path.
        # Catches "200 + only message_start/message_stop, zero deltas"
        # and retries. Anthropic shape detects text_delta /
        # thinking_delta / input_json_delta / tool_use.
        probed = await _gate_probe_for_content(
            probed,
            anthropic_shape=True,
            gate_timeout=gate_timeout,
            log_entry=attempt_log,
            start_time=start,
        )
        if probed.error_response is not None:
            if attempt < max_attempts - 1:
                continue
            _cb_fail(provider_name, model)
            return (
                _empty_response_jsonresponse(
                    anthropic_shape=True,
                    attempts=max_attempts,
                ),
                True,
            )
        _cb_ok(provider_name, model)
        # Probe contract: error_response is None guarantees byte_iter +
        # response are populated. Narrow for the type checker.
        assert probed.byte_iter is not None and probed.response is not None
        return (
            StreamingResponse(
                _restored_stream(
                    _raw_anthropic_sse_stream(
                        probed.first_chunk,
                        probed.byte_iter,
                        probed.response,
                        model,
                        start,
                        attempt_log,
                        provider_name,
                        body,
                        session_key=session_key,
                    ),
                    redaction_map,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-CyRouter-Provider": provider_name,
                },
            ),
            False,
        )
    _cb_fail(provider_name, model)
    return (
        _empty_response_jsonresponse(anthropic_shape=True, attempts=max_attempts),
        True,
    )


async def _anthropic_raw_nonstream_once(
    *,
    upstream_url: str,
    headers: dict,
    body: dict,
    model: str,
    base_url: str,
    provider_name: str,
    start: float,
    trace_id: str,
    redaction_map,
    orig_body: dict,
    max_attempts: int,
) -> tuple:
    """Non-streaming attempt against ONE Anthropic-compatible upstream.

    Returns (response, should_fallback); see _openai_raw_stream_once.
    """
    for attempt in range(max_attempts):
        attempt_log = _build_log_entry(body, base_url, False)
        attempt_log["attempt_index"] = attempt
        attempt_log["trace_id"] = trace_id
        try:
            client = _get_http_client_for_url(upstream_url, provider_name)
            resp = await client.post(
                upstream_url,
                headers=headers,
                json=body,
                timeout=_NONSTREAM_TIMEOUT,
            )
            if resp.status_code != 200:
                # Pass upstream error body through verbatim so Anthropic SDK
                # can recognise overloaded_error/rate_limit_error and apply
                # proper retry/backoff. Wrapping in HTTPException would nest
                # the body inside `{"detail": "..."}` and break SDK parsing.
                await _finish_log(attempt_log, "error", start, error=resp.text[:500])
                _cb_fail(
                    provider_name, model, status=resp.status_code, body_text=resp.text
                )
                return (
                    _upstream_error_response(
                        resp,
                        anthropic_shape=True,
                    ),
                    True,
                )
            result = resp.json()
            # Empty-output check: 200 but content blocks are all empty
            # (no text/tool_use/thinking with non-empty payload) → retry.
            # Final attempt: pass the genuine empty 200 through (no
            # synthesised 502), but mark the log ok_empty so dashboards
            # can distinguish it from real ok.
            is_empty = _is_empty_response(result, anthropic_shape=True)
            if is_empty and attempt < max_attempts - 1:
                attempt_log["status"] = "retry_empty"
                attempt_log["error"] = "empty response from upstream, retrying"
                attempt_log["latency_ms"] = int((time.time() - start) * 1000)
                await _log_request(attempt_log)
                continue
            result["model"] = model
            if is_empty:
                await _finish_log(
                    attempt_log,
                    "ok_empty",
                    start,
                    result,
                    error="empty response after exhausting retries",
                )
                _cb_fail(provider_name, model)
            else:
                await _finish_log(attempt_log, "ok", start, result)
                _cb_ok(provider_name, model)
                _note_latency(
                    model,
                    int((time.time() - start) * 1000),
                    result.get("usage"),
                )
            if redaction_map:
                result = restore_obj(result, redaction_map)
            r = JSONResponse(result)
            _cache_store(orig_body, r)
            return r, False
        except httpx.TimeoutException as e:
            await _finish_log(attempt_log, "error", start, error=f"timeout: {e}")
            _cb_fail(provider_name, model)
            return _timeout_error_response(e, anthropic_shape=True), True
        except Exception as e:
            await _finish_log(attempt_log, "error", start, error=str(e))
            _cb_fail(provider_name, model, exc=e)
            return _sdk_exception_response(e, anthropic_shape=True), True
    _cb_fail(provider_name, model)
    return (
        _empty_response_jsonresponse(anthropic_shape=True, attempts=max_attempts),
        True,
    )


@app.post("/anthropic/v1/messages")
@app.post("/anthropic/v1/messages/{path:path}")
async def anthropic_messages(request: Request, path: str = ""):
    body = await request.json()
    return await _run_auto_reroute(
        request, body, _anthropic_messages_once, path=path, anthropic_shape=True
    )


async def _anthropic_messages_once(
    request: Request, body: dict, path: str = "", _auto_exclude=None
):
    model = body.get("model", "")

    # "auto" virtual model: resolve to concrete model before upstream lookup.
    # Mutates body. _auto_exclude: models that already died fatally during
    # THIS request (in-request reroute) — resolution skips them.
    request.state.cy_auto_resolved = ""
    if isinstance(model, str) and model.lower() == "auto":
        cfg = load_config()
        body["_session_id"] = request.headers.get(
            "x-cyrouter-session-id", ""
        ) or request.headers.get("x-conversation-id", "")
        model = body["model"] = _resolve_auto_model(body, cfg, exclude=_auto_exclude)
        body.pop("_session_id", None)
        request.state.cy_auto_resolved = model

    force_provider = request.headers.get("x-cyrouter-provider", "") or None
    if force_provider:
        # Client pinned a provider — rerouting to another model would break
        # the pin (its chain may not include the forced provider).
        request.state.cy_auto_resolved = ""
    # Session key for the session-level provider penalty window (header
    # first, derived prefix-hash fallback). Computed on the pristine body
    # BEFORE redaction/_prepare_anthropic_body so it matches the auto
    # stickiness key exactly.
    cy_session_key = (
        request.headers.get("x-cyrouter-session-id", "")
        or request.headers.get("x-conversation-id", "")
        or _auto_session_key(body)
    )
    upstreams = _get_upstreams_for_model(model, force_provider=force_provider)
    if not upstreams:
        raise HTTPException(404, f"No provider for model: {model}")
    if not force_provider:
        _order = _session_penalty_reorder(
            cy_session_key, model, [u[4].name for u in upstreams]
        )
        if _order is not None:
            upstreams = [upstreams[i] for i in _order]
            logger.info(
                "session %s…: penalty reorder chain → [%s]",
                cy_session_key[:8],
                ", ".join(u[4].name for u in upstreams),
            )

    is_stream = body.get("stream", False)
    start = time.time()
    trace_id = request.headers.get("x-cyrouter-trace-id", "")

    if not is_stream:
        cached = _cache_try(body)
        if cached is not None:
            return cached

    # Reasoning replay runs BEFORE redaction (tool_call ids get masked).
    _ensure_reasoning_content(body)

    # Secret redaction AFTER cache lookup (cache key = pristine body).
    orig_body = body
    body, _redaction_map = _redact_request_body(body, request)

    # ── Claude-compatible upstream: raw SSE proxy ──
    # Provider fallback: upstreams is an ordered candidate list (primary +
    # model_fallbacks); every candidate speaks the Anthropic protocol. On any
    # error BEFORE content starts flowing, the next candidate is tried.
    body = _prepare_anthropic_body(body)

    anthropic_version = request.headers.get("anthropic-version", "2023-06-01")
    beta = request.headers.get("anthropic-beta")

    cfg = load_config()
    max_attempts = int(cfg.get("empty_retry_attempts", 3))
    gate_timeout = float(cfg.get("empty_retry_timeout", 90))

    n_candidates = len(upstreams)
    _tried_providers: list = []
    for cand_i, (base_url, api_key, mapped_model, _ptype, provider) in enumerate(
        upstreams
    ):
        is_last_candidate = cand_i == n_candidates - 1
        body["model"] = mapped_model
        _apply_param_clamp(body, provider.name)
        _tried_providers.append(provider.name)

        reject = _cb_check(provider.name, model, last_candidate=is_last_candidate)
        if reject is not None:
            if is_last_candidate:
                return reject
            continue

        upstream_url = f"{base_url.rstrip('/')}/v1/messages"
        # Dual auth: x-api-key (Anthropic standard) + Authorization Bearer
        # for gateways whose consumer auth only reads Bearer (e.g. Volcengine
        # API Gateway /compatible). Verified harmless on zhipu / deepseek /
        # kimi coding endpoints (2026-09-07) — they ignore the extra header.
        headers = {
            "content-type": "application/json",
            "anthropic-version": anthropic_version,
            "x-api-key": api_key,
            "authorization": f"Bearer {api_key}",
        }
        if beta:
            headers["anthropic-beta"] = beta

        if is_stream:
            resp, should_fallback = await _anthropic_raw_stream_once(
                upstream_url=upstream_url,
                headers=headers,
                body=body,
                model=model,
                base_url=base_url,
                provider_name=provider.name,
                start=start,
                trace_id=trace_id,
                session_key=cy_session_key,
                redaction_map=_redaction_map,
                max_attempts=max_attempts,
                gate_timeout=gate_timeout,
            )
        else:
            resp, should_fallback = await _anthropic_raw_nonstream_once(
                upstream_url=upstream_url,
                headers=headers,
                body=body,
                model=model,
                base_url=base_url,
                provider_name=provider.name,
                start=start,
                trace_id=trace_id,
                redaction_map=_redaction_map,
                orig_body=orig_body,
                max_attempts=max_attempts,
            )

        if should_fallback and not is_last_candidate:
            logger.info(
                "provider fallback: model=%s provider=%s failed pre-content, "
                "trying next candidate",
                model,
                provider.name,
            )
            # Pre-content failure for THIS session — remember it so this
            # session's next request doesn't pay the dead first attempt
            # again (global breaker may stay closed under parallel traffic).
            _session_penalty_record(cy_session_key, provider.name, model)
            continue
        if not should_fallback:
            # Upstream produced content — clear any stale global "model
            # failed" mark an earlier candidate's fatal error may have set.
            _failed_models.pop(model, None)
            return resp
        # 最后一个候选也失败：归因写进错误响应（哪家 provider、试过几家）
        return _annotate_final_error(resp, provider.name, _tried_providers)


@app.get("/v1/models")
async def openai_list_models():
    """Pure OpenAI-standard model list.

    Only the four schema fields (id, object, created, owned_by). No
    extension keys — contextWindow/maxOutputTokens live in /api.json for
    clients that want them; strict OpenAI clients get a clean shape here.
    """
    cfg = load_config()
    data = []
    for m, chain in _get_model_map(cfg).items():
        if "*" in m or "?" in m:
            continue  # glob chain keys aren't real model ids
        data.append(
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": chain[0] if chain else "",
            }
        )
    # model_aliases are NOT exposed here — they're transparent reroutes,
    # not discoverable models. Clients sending an aliased name still work
    # because /v1/chat/completions resolves the alias at request time.
    return {"object": "list", "data": data}


@app.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    """Transparent proxy for OpenAI-compatible embedding requests.

    Finds the upstream provider for the requested model and forwards the
    embedding request. No caching, no retry, no breaker — embedding calls
    are cheap and deterministic (no reasoning/tool-calling complexity).
    """
    body = await request.json()
    model = body.get("model", "")

    force_provider = request.headers.get("x-cyrouter-provider", "") or None
    upstream = _get_openai_upstream(model, force_provider=force_provider)
    if not upstream:
        raise HTTPException(404, f"No provider for embedding model: {model}")

    base_url, api_key, mapped_model, provider_name = upstream
    body["model"] = mapped_model

    # Outbound-only redaction: mask any embedded secrets in the input text
    # before it reaches the provider. The response is a numeric vector, so
    # there is nothing to restore on the way back.
    body, _ = _redact_request_body(body, request)

    upstream_url = f"{base_url.rstrip('/')}/embeddings"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    client = _get_http_client_for_url(upstream_url, provider_name)
    try:
        resp = await client.post(
            upstream_url, headers=headers, json=body, timeout=_NONSTREAM_TIMEOUT
        )
    except httpx.TimeoutException as e:
        return _timeout_error_response(e, anthropic_shape=False)
    except Exception as e:
        return _sdk_exception_response(e, anthropic_shape=False)

    if resp.status_code >= 400:
        return _upstream_error_response(resp, anthropic_shape=False)

    return JSONResponse(content=resp.json(), status_code=resp.status_code)


# ── Background provider probing ──────────────────────────────────
# Optional (config "probe": {"enabled": true, "interval_seconds": 900}).
# Periodically sends a minimal completion request to the primary provider
# of every multi-provider model chain so quota exhaustion / outages are
# detected and cooled down BEFORE a real user request pays for the
# discovery. Each probe costs a few tokens — disabled by default.
_probe_last_results: dict[tuple[str, str], dict] = {}


def _probe_targets(cfg: dict) -> list[tuple[str, str]]:
    """(model, primary_provider) pairs worth probing: concrete models with
    a chain of >=2 providers whose primary isn't already in cooldown
    (probing a known-dead provider wastes the request; cooldown expiry is
    re-checked by real traffic or the next probe round).
    """
    out: list[tuple[str, str]] = []
    for model, chain in _get_model_map(cfg).items():
        if "*" in model or "?" in model or len(chain) < 2:
            continue
        primary = chain[0]
        if _is_provider_model_failed(primary, model):
            continue
        out.append((model, primary))
    return out


async def _probe_provider_model(model: str, provider_name: str) -> tuple[bool, str]:
    """Send a minimal request to (provider, model). Returns (ok, detail).

    Failures funnel through _cb_fail (unified classification + decision
    table): quota/rate-limit marks divert user traffic immediately,
    server/network failures count toward the circuit breaker.
    """
    cfg = load_config()
    p_cfg = next(
        (p for p in cfg.get("providers", []) if p["name"] == provider_name), None
    )
    if p_cfg is None:
        return False, "provider missing"
    api_key = p_cfg.get("api_key", "")
    if p_cfg.get("encrypted"):
        try:
            api_key = decrypt_value(api_key)
        except Exception:
            return False, "key decrypt failed"
    base_urls = p_cfg.get("base_urls") or {}
    openai_url = base_urls.get("openai") or (
        p_cfg.get("base_url") if p_cfg.get("type", "openai") == "openai" else None
    )
    try:
        if openai_url:
            url = f"{openai_url.rstrip('/')}/chat/completions"
            headers = {
                "content-type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            body = {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
        elif base_urls.get("anthropic"):
            url = f"{base_urls['anthropic'].rstrip('/')}/v1/messages"
            headers = {
                "content-type": "application/json",
                "x-api-key": api_key,
                "authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
            }
            body = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        else:
            return False, "no probe-capable endpoint"
        client = _get_http_client_for_url(url, provider_name)
        resp = await client.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code == 200:
            text = resp.text
            # Soft-error: 200 + error JSON body (GLM/DeepSeek style)
            if (
                '"error"' in text
                and '"choices"' not in text
                and '"content"' not in text
            ):
                _cb_fail(provider_name, model, status=200, body_text=text)
                return False, "soft error"
            return True, "ok"
        _cb_fail(provider_name, model, status=resp.status_code, body_text=resp.text)
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        _cb_fail(provider_name, model, exc=e)
        return False, f"{type(e).__name__}: {str(e)[:80]}"


# ── Live usage readings (Kimi /usages et al.) ─────────────────────────
# Per-provider live usage cache, populated by _usages_fetch_loop. Keyed by
# provider name. Readings come from the provider's own usage endpoint
# (Kimi Code: GET {openai_base}/usages with the API key — the same endpoint
# the official CLI's /usage command uses; percentages on a 100 scale).
# The quota endpoint prefers these over local estimation when fresh.
_usages_cache: dict[str, dict] = {}
_USAGES_FETCH_INTERVAL = 300.0  # loop cadence
_USAGES_STALE_SECONDS = 900.0  # older than this → fall back to estimation

# ── Subscription-exhausted detection (2026-09-11) ─────────────────────────
# A Kimi Code subscription that expired (no credits left) is visible in the
# /usages endpoint itself: while a merely-drained weekly pool still answers
# 200 with percentage=100, an expired subscription 429s even /usages with
#   {"code":"resource_exhausted","message":"insufficient balance",
#    "details":[{"debug":{"reason":"REASON_QUOTA_EXCEEDED",
#      "localizedMessage":{"message":"Credits used up."}}}]}  (observed live)
# Consecutive balance-429s confirm exhaustion; any 200 clears it (renewal
# takes effect on the next successful probe).
_USAGES_EXHAUSTED_CONFIRM = 3  # consecutive balance-429s → exhausted
_USAGES_EXHAUSTED_BACKOFF = 1800.0  # probe recovery this often once exhausted
_usages_exhausted_streak: dict[str, dict] = {}  # name → {count, since}
_usages_next_attempt: dict[str, float] = {}  # name → next probe ts (backoff)

_USAGES_STATE_PATH = DATA_DIR / "usages_state.json"


def _persist_usages_state() -> None:
    """Persist subscription-exhausted markers to disk.

    2026-09-11: the verdict lived only in `_usages_cache`, so a restart
    forgot it and the UI fell back to a misleading local-estimate
    percentage for ~15min until the 3-streak re-confirmed. An expired
    subscription is a persistent fact — survive restarts. Renewal (a live
    200) clears the file entry again."""
    state = {
        name: entry
        for name, entry in _usages_cache.items()
        if isinstance(entry, dict) and entry.get("subscription") == "exhausted"
    }
    try:
        _USAGES_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        logger.warning("usages: failed to persist exhausted state", exc_info=True)


def _load_persisted_usages_state() -> None:
    """Restore exhausted markers at startup (see _persist_usages_state)."""
    try:
        raw = json.loads(_USAGES_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception:
        logger.warning("usages: unreadable persisted state, ignoring", exc_info=True)
        return
    if not isinstance(raw, dict):
        return
    for name, entry in raw.items():
        if isinstance(entry, dict) and entry.get("subscription") == "exhausted":
            _usages_cache[name] = dict(entry)
            logger.info(
                "usages: %s restored exhausted state from disk (since %s)",
                name,
                time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(entry.get("exhausted_since") or 0),
                ),
            )


_load_persisted_usages_state()


def _insufficient_balance_signal(body_text: str) -> bool:
    """True if a 429 body marks subscription-level credit exhaustion.

    Requires an explicit balance/credits marker — a bare 429 (rate window,
    gateway throttle, …) must never be counted toward exhaustion.
    """
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    msg = str(payload.get("message") or "").lower()
    if "insufficient balance" in msg or "credits used up" in msg:
        return True
    for d in payload.get("details") or []:
        if not isinstance(d, dict):
            continue
        dbg = d.get("debug") or {}
        loc = ((dbg.get("localizedMessage") or {}).get("message")) or ""
        if (
            str(dbg.get("reason")).upper() == "REASON_QUOTA_EXCEEDED"
            and "credits used up" in str(loc).lower()
        ):
            return True
    return False


def _iso_to_epoch(s: str | None) -> int:
    """Parse an ISO-8601 timestamp like 2026-08-31T04:59:15.611860Z → epoch."""
    if not s:
        return 0
    import datetime as _dt

    try:
        return int(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _parse_usages_payload(payload: dict) -> dict:
    """Normalize a /usages response to the cache entry shape.

    Observed Kimi shape (2026-08-26, percentage scale — limit="100"):
      usage:      {limit, used, remaining, resetTime}          # plan cycle
      limits[0]:  window {duration:300, timeUnit:TIME_UNIT_MINUTE}
                 detail {limit, remaining, resetTime}          # 5h rate window
      parallel:   {limit}, user.membership.level
    """
    weekly = rate = None
    u = payload.get("usage") or {}
    if isinstance(u, dict) and u.get("limit") is not None:
        try:
            weekly = {
                "used_pct": float(u.get("used") or 0.0),
                "remaining_pct": float(u.get("remaining") or 0.0),
                "limit_pct": float(u.get("limit") or 100.0),
                "reset_time": u.get("resetTime"),
            }
        except (TypeError, ValueError):
            weekly = None
    for lw in payload.get("limits") or []:
        if not isinstance(lw, dict):
            continue
        w = lw.get("window") or {}
        d = lw.get("detail") or {}
        dur = w.get("duration")
        unit = str(w.get("timeUnit") or "").upper()
        try:
            if "MINUTE" in unit and dur:
                hours = float(dur) / 60.0
            elif "HOUR" in unit and dur:
                hours = float(dur)
            else:
                continue
            rate = {
                "hours": hours,
                "limit_pct": float(d.get("limit") or 100.0),
                "remaining_pct": float(d.get("remaining") or 0.0),
                "reset_time": d.get("resetTime"),
            }
            break
        except (TypeError, ValueError):
            continue
    return {
        "weekly": weekly,
        "rate": rate,
        "parallel_limit": (payload.get("parallel") or {}).get("limit"),
        "membership": ((payload.get("user") or {}).get("membership") or {}).get(
            "level"
        ),
    }


async def _fetch_provider_usages(p_cfg: dict) -> bool:
    """GET {openai_base}/usages for one provider, refresh the cache.

    Returns True on a successful refresh. Failures keep the previous entry
    (staleness is visible via fetched_at) — callers fall back to estimation
    once past _USAGES_STALE_SECONDS.
    """
    name = p_cfg.get("name") or ""
    base = (p_cfg.get("base_urls") or {}).get("openai") or p_cfg.get("base_url")
    api_key = p_cfg.get("api_key", "")
    if p_cfg.get("encrypted"):
        try:
            api_key = decrypt_value(api_key)
        except Exception:
            logger.warning("usages: %s key decrypt failed", name)
            return False
    if not (name and base and api_key):
        return False
    url = base.rstrip("/") + "/usages"
    try:
        client = _get_http_client_for_url(url, name)
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        if resp.status_code == 429 and _insufficient_balance_signal(resp.text):
            streak = _usages_exhausted_streak.get(name) or {}
            streak["count"] = int(streak.get("count", 0)) + 1
            streak.setdefault("since", time.time())
            _usages_exhausted_streak[name] = streak
            was_exhausted = (_usages_cache.get(name) or {}).get(
                "subscription"
            ) == "exhausted"
            if was_exhausted:
                # Staying exhausted — keep the entry fresh, log at debug so a
                # long-expired subscription doesn't spam WARNING every probe.
                _usages_cache[name]["last_429_at"] = time.time()
                logger.debug("usages: %s still subscription-exhausted (HTTP 429)", name)
            elif streak["count"] >= _USAGES_EXHAUSTED_CONFIRM:
                _usages_cache[name] = {
                    "subscription": "exhausted",
                    "exhausted_since": streak["since"],
                    "last_429_at": time.time(),
                }
                _usages_next_attempt[name] = time.time() + _USAGES_EXHAUSTED_BACKOFF
                _persist_usages_state()
                logger.warning(
                    "usages: %s subscription exhausted (insufficient balance "
                    "×%d since %s) — probing recovery every %ds",
                    name,
                    streak["count"],
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(streak["since"])),
                    int(_USAGES_EXHAUSTED_BACKOFF),
                )
            else:
                logger.warning(
                    "usages: %s HTTP 429 insufficient balance (%d/%d)",
                    name,
                    streak["count"],
                    _USAGES_EXHAUSTED_CONFIRM,
                )
            return False
        if resp.status_code != 200:
            logger.warning("usages: %s HTTP %d", name, resp.status_code)
            return False
        parsed = _parse_usages_payload(resp.json())
        if not parsed["weekly"] and not parsed["rate"]:
            logger.warning("usages: %s unrecognized payload shape", name)
            return False
        if (_usages_cache.get(name) or {}).get(
            "subscription"
        ) == "exhausted" or _usages_exhausted_streak.get(name, {}).get("count", 0) > 0:
            logger.info("usages: %s subscription recovered (got a live reading)", name)
        _usages_exhausted_streak.pop(name, None)
        _usages_next_attempt.pop(name, None)
        entry = {"fetched_at": time.time(), **parsed}
        _usages_cache[name] = entry
        _persist_usages_state()  # renewal clears any persisted exhausted marker
        w = parsed["weekly"] or {}
        logger.info(
            "usages: %s weekly used=%s%% reset=%s",
            name,
            w.get("used_pct"),
            (w.get("reset_time") or "")[:19],
        )
        return True
    except Exception:
        logger.warning("usages: %s fetch failed", name, exc_info=True)
        return False


async def _usages_fetch_loop():
    """Periodically refresh live usage readings for opted-in providers.

    Opt-in: provider's local_quota.fetch_usages = true (the endpoint is
    provider-specific — Kimi's /usages — so it is never guessed).
    """
    while True:
        interval = _USAGES_FETCH_INTERVAL
        try:
            cfg = load_config()
            targets = [
                p
                for p in cfg.get("providers", [])
                if (p.get("local_quota") or {}).get("fetch_usages")
            ]
            if targets:
                for p in targets:
                    # Backoff for exhausted subscriptions: probe recovery at
                    # _USAGES_EXHAUSTED_BACKOFF cadence instead of every loop,
                    # so a long-expired plan doesn't keep hammering a 429.
                    if _usages_next_attempt.get(p.get("name") or "", 0) > time.time():
                        continue
                    await _fetch_provider_usages(p)
                    await asyncio.sleep(2)
            else:
                interval = 900.0  # nothing configured — poll config lazily
        except Exception:
            logger.warning("usages fetch loop error", exc_info=True)
        await asyncio.sleep(interval)


async def _probe_loop():
    """Background availability probing for multi-provider model chains."""
    while True:
        interval = 900.0
        try:
            cfg = load_config()
            probe_cfg = cfg.get("probe") or {}
            interval = max(60.0, float(probe_cfg.get("interval_seconds", 900)))
            if probe_cfg.get("enabled"):
                for model, provider_name in _probe_targets(cfg):
                    ok, detail = await _probe_provider_model(model, provider_name)
                    _probe_last_results[(provider_name, model)] = {
                        "ok": ok,
                        "detail": detail,
                        "ts": time.time(),
                    }
                    if ok:
                        # Early recovery: clear any stale failure mark, and
                        # uneject the circuit breaker (Envoy semantics: a
                        # successful ACTIVE health check resets all counters,
                        # including the trip escalation) instead of waiting
                        # out a cooldown the probe just proved unnecessary.
                        _failed_provider_models.pop((provider_name, model), None)
                        get_breaker().reset(provider_name, model)
                        logger.info(
                            "probe ok: model=%s provider=%s", model, provider_name
                        )
                    else:
                        logger.warning(
                            "probe FAILED: model=%s provider=%s (%s)",
                            model,
                            provider_name,
                            detail,
                        )
                    await asyncio.sleep(2)
        except Exception:
            logger.warning("probe loop error", exc_info=True)
        await asyncio.sleep(interval)


# ── Admin API ──────────────────────────────────────────────────────────


@app.get("/api/config")
async def api_get_config(request: Request):
    cfg = load_config()
    # Build a safe copy with masked keys — never mutate the loaded dict
    safe = json.loads(json.dumps(cfg))
    for p in safe.get("providers", []):
        if p.get("api_key"):
            p["api_key_masked"] = (
                p["api_key"][:8] + "..." if len(p["api_key"]) > 8 else "***"
            )
            del p["api_key"]
    # Detect reverse proxy prefix
    prefix = request.headers.get("x-script-name", "")
    if prefix:
        safe["proxy_prefix"] = prefix.rstrip("/")
    return safe


@app.put("/api/config")
async def api_save_config(request: Request):
    incoming = await request.json()
    existing = load_config()
    existing_cfg = {p["name"]: p for p in existing.get("providers", [])}

    # These settings belong to this local installation.  The web UI sends
    # the whole config object, so never let a client-side save overwrite them.
    for key in ("port", "host", "proxy", "api_keys", "proxy_prefix"):
        if key in existing:
            incoming[key] = existing[key]
        else:
            incoming.pop(key, None)

    # Preserve encrypted keys that the client didn't overwrite
    for p in incoming.get("providers", []):
        old = existing_cfg.get(p["name"])
        if old and old.get("encrypted"):
            if not p.get("api_key") or p.get("api_key") == old.get(
                "api_key_masked", ""
            ):
                # Client sent masked or empty — keep original encrypted key
                p["api_key"] = old["api_key"]
                p["encrypted"] = True
            elif not p.get("encrypted"):
                # Client sent a new plain key — encrypt it
                p["api_key"] = encrypt_value(p["api_key"])
                p["encrypted"] = True

    # Clean up masked fields before saving
    for p in incoming.get("providers", []):
        p.pop("api_key_masked", None)

    save_config(incoming)
    get_breaker().reload(incoming)
    return {"ok": True}


@app.get("/api/keys")
async def api_list_keys():
    """List all API keys (keys themselves are masked)."""
    cfg = load_config()
    result = []
    for k in cfg.get("api_keys", []):
        key = k.get("key", "")
        masked = key[:8] + "..." if len(key) > 8 else "***"
        result.append(
            {
                "name": k.get("name", ""),
                "key_masked": masked,
                "created_at": k.get("created_at", ""),
            }
        )
    return {"keys": result}


@app.post("/api/keys")
async def api_create_key(request: Request):
    import secrets as _sec
    from datetime import datetime, timezone

    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    key = "sk-" + _sec.token_urlsafe(32)

    cfg = load_config()
    api_keys = cfg.get("api_keys", [])
    for k in api_keys:
        if k.get("name") == name:
            raise HTTPException(400, f"key '{name}' already exists")
    api_keys.append(
        {
            "name": name,
            "key": key,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    cfg["api_keys"] = api_keys
    save_config(cfg)
    return {"name": name, "key": key}


@app.delete("/api/keys/{name}")
async def api_delete_key(name: str):
    cfg = load_config()
    api_keys = cfg.get("api_keys", [])
    before = len(api_keys)
    cfg["api_keys"] = [k for k in api_keys if k.get("name") != name]
    if len(cfg["api_keys"]) == before:
        raise HTTPException(404, f"key '{name}' not found")
    save_config(cfg)
    return {"ok": True}


@app.get("/api/keys/{name}/reveal")
async def api_reveal_key(name: str):
    """Return the full plaintext key for a given name (for 'view' in UI)."""
    cfg = load_config()
    for k in cfg.get("api_keys", []):
        if k.get("name") == name:
            return {"name": name, "key": k.get("key", "")}
    raise HTTPException(404, f"key '{name}' not found")


# ── "auto" virtual model config ────────────────────────────────────────
@app.get("/api/auto")
async def api_get_auto_config():
    """Return the current `auto` block + available model choices.

    Available models are sourced from all providers' model lists so the
    frontend can populate simple/complex model dropdowns.
    """
    cfg = load_config()
    auto_cfg = cfg.get("auto") or {}
    # Merge with defaults so the UI always shows every field.
    merged = {**_AUTO_DEFAULTS, **auto_cfg}
    # Collect candidate models (unified model -> provider-chain map).
    candidates = []
    for m, chain in _get_model_map(cfg).items():
        if "*" in m or "?" in m:
            continue
        candidates.append({"id": m, "provider": chain[0] if chain else ""})
    return {"config": merged, "models": candidates}


@app.put("/api/auto")
async def api_save_auto_config(request: Request):
    """Update only the `auto` block. Validates required fields.

    Accepts a partial update — missing keys keep their current value.
    """
    incoming = await request.json()
    cfg = load_config()
    current = cfg.get("auto") or {}
    # Merge: incoming overrides current; validate types.
    merged = {**_AUTO_DEFAULTS, **current}
    for key, val in incoming.items():
        if key in _AUTO_DEFAULTS:
            merged[key] = val
    # Type coercion / validation
    if not isinstance(merged.get("simple_model"), str):
        raise HTTPException(400, "simple_model must be a string")
    if not isinstance(merged.get("complex_model"), str):
        raise HTTPException(400, "complex_model must be a string")
    for fb_field in (
        "simple_fallback",
        "complex_fallback",
        "vision_model",
        "vision_fallback",
    ):
        val = merged.get(fb_field)
        if val is not None and not isinstance(val, str):
            raise HTTPException(400, f"{fb_field} must be a string or empty")
        if val is None:
            merged[fb_field] = ""
    try:
        merged["thinking_threshold_chars"] = int(merged["thinking_threshold_chars"])
    except (ValueError, TypeError):
        raise HTTPException(
            400, "thinking_threshold_chars must be an integer"
        ) from None
    cfg["auto"] = merged
    save_config(cfg)
    # Invalidate the usage cache since routing may change.
    global _USAGE_CACHE
    _USAGE_CACHE = None
    return {"ok": True, "config": merged}


@app.get("/api/providers/templates")
async def api_provider_templates():
    """内置常用供应商模板（静态、无 key）。

    模板永不落 config，只在 UI「从模板添加」时预填表单；用户保存后
    才成为 config 条目。suggested_models 全部在 BUILTIN_PRICING /
    BUILTIN_MODEL_META 里，添加后零配置可记账。
    """
    return provider_templates_payload()


@app.post("/api/providers")
async def api_add_provider(request: Request):
    data = await request.json()
    existing_cfg = load_config()
    existing_providers = {p["name"]: p for p in existing_cfg.get("providers", [])}
    old = existing_providers.get(data.get("name"))

    # New plain key — encrypt it
    if data.get("api_key") and not data.get("encrypted"):
        data["api_key"] = encrypt_value(data["api_key"])
        data["encrypted"] = True
    # Editing without changing key — preserve existing encrypted key
    elif not data.get("api_key") and old and old.get("encrypted"):
        data["api_key"] = old["api_key"]
        data["encrypted"] = True

    # Merge over the stored provider: the UI edit form only manages
    # connection fields (name/type/urls/key/proxy/console_url). Form-absent
    # extras (model_map, local_quota, …) must survive a UI edit instead of
    # being silently dropped.
    if old:
        merged = {**old, **data}
        # Single↔dual URL switch: keep only the shape the form sent.
        if "base_urls" in data:
            merged.pop("base_url", None)
        elif "base_url" in data:
            merged.pop("base_urls", None)
        # Explicit nulls (e.g. a cleared console_url) delete the key.
        data = {k: v for k, v in merged.items() if v is not None}

    add_provider(data)
    return {"ok": True}


@app.delete("/api/providers/{name}")
async def api_remove_provider(name: str):
    remove_provider(name)
    return {"ok": True}


def _machine_label() -> str:
    """Best-effort host name for export metadata (never a secret)."""
    with suppress(Exception):
        return socket.gethostname() or "unknown"
    return "unknown"


def _provider_identity(p: dict) -> dict:
    """Provider fields that are meaningful for import diffing.

    api_key/encrypted are excluded: local keys are Fernet ciphertext tied to
    this install's .key while import files carry plaintext, so comparing
    them always mismatches. Key-only changes are not previewed (the import
    file's key always wins on apply).
    """
    return {k: v for k, v in p.items() if k not in ("api_key", "encrypted")}


def _portable_import_diff(incoming: dict, existing: dict) -> dict:
    """Diff a portable config payload against the local config.

    Returned per-section lists name what the import WILL do:
    providers/models are wholesale-replaced by the file (missing entries
    get REMOVED — the classic silent-rollback hazard the UI previews),
    model_meta/pricing merge per model (file wins per model, nothing removed).
    """
    diff = {
        "providers": {"added": [], "changed": [], "removed": []},
        "models": {"added": [], "changed": [], "removed": []},
        "model_meta": {"added": [], "changed": []},
        "pricing": {"added": [], "changed": []},
    }
    in_p = {
        p.get("name"): _provider_identity(p)
        for p in incoming.get("providers", [])
        if isinstance(p, dict) and p.get("name")
    }
    ex_p = {
        p.get("name"): _provider_identity(p)
        for p in existing.get("providers", [])
        if isinstance(p, dict) and p.get("name")
    }
    for name, ident in in_p.items():
        if name not in ex_p:
            diff["providers"]["added"].append(name)
        elif ident != ex_p[name]:
            diff["providers"]["changed"].append(name)
    diff["providers"]["removed"] = [n for n in ex_p if n not in in_p]

    in_m, ex_m = incoming.get("models", {}), existing.get("models", {})
    diff["models"]["added"] = sorted(k for k in in_m if k not in ex_m)
    diff["models"]["changed"] = sorted(
        k for k in in_m if k in ex_m and in_m[k] != ex_m[k]
    )
    diff["models"]["removed"] = sorted(k for k in ex_m if k not in in_m)

    in_meta, ex_meta = incoming.get("model_meta", {}), existing.get("model_meta", {})
    diff["model_meta"]["added"] = sorted(k for k in in_meta if k not in ex_meta)
    diff["model_meta"]["changed"] = sorted(
        k for k in in_meta if k in ex_meta and in_meta[k] != ex_meta[k]
    )

    in_pr, ex_pr = incoming.get("pricing", {}), existing.get("pricing", {})
    diff["pricing"]["added"] = sorted(k for k in in_pr if k not in ex_pr)
    diff["pricing"]["changed"] = sorted(
        k for k in in_pr if k in ex_pr and in_pr[k] != ex_pr[k]
    )
    return diff


def _backup_meta_of(incoming: dict) -> dict | None:
    """Read the __meta__ envelope from an import payload, if present."""
    meta = incoming.get("__meta__")
    return meta if isinstance(meta, dict) else None


@app.get("/api/config/export")
async def api_export_config():
    """Export portable provider/model config wrapped in a metadata envelope.

    Installation-local settings (port, proxy, host, cyRouter client keys,
    and reverse-proxy details) must never leave this machine. Provider API
    keys are exported in plain text so the backup restores on another
    install (the importer re-encrypts with the target machine's .key).

    __meta__ carries format/schema_version/exported_at/source/machine/counts
    + warnings so the importer can preview staleness; schema_version=2 files
    are envelopes, pre-2026-09-08 files (no __meta__) still import as v1.
    """
    from datetime import datetime

    cfg = load_config()
    # Explicit allowlist: do not accidentally export newly-added local fields.
    # model_meta travels with the models it describes (context/max_output/
    # vision are model-intrinsic, machine-independent) — losing it on a
    # restore degrades every limit to the name heuristic and strips vision
    # from models like kimi-k3 whose name lacks -vl.
    export = {
        key: json.loads(json.dumps(cfg[key]))
        for key in ("providers", "models", "model_meta", "pricing")
        if key in cfg
    }
    warnings: list[str] = []
    for p in export.get("providers", []):
        if p.get("encrypted") and p.get("api_key"):
            try:
                p["api_key"] = decrypt_value(p["api_key"])
            except Exception:
                # .key mismatch or corrupt ciphertext. Exporting the ciphertext
                # labeled as plaintext would DOUBLE-ENCRYPT it on import, and
                # Fernet ciphertext is undecryptable on any other machine
                # anyway. Ship the provider key-less + a warning instead — the
                # user re-enters that one key after restore. (2026-09-08 fix;
                # the old suppress() path silently produced poisoned files.)
                p["api_key"] = None
                warnings.append(
                    f"供应商「{p.get('name')}」的 API key 无法用本机密钥解密，"
                    f"已导出为空 key，导入后需手动补填"
                )
            p["encrypted"] = False
    meta = {
        "format": "cyrouter-config",
        "schema_version": 2,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": f"cyRouter {__display_version__}",
        "machine": _machine_label(),
        "counts": {
            "providers": len(export.get("providers", [])),
            "models": len(export.get("models", {})),
            "model_meta": len(export.get("model_meta", {})),
            "pricing": len(export.get("pricing", {})),
        },
        "warnings": warnings,
    }
    return {"__meta__": meta, **export}


def _apply_portable_import(incoming: dict) -> None:
    """Encrypt plain keys and merge a portable payload into local config.

    Shared by /api/config/import and /api/config/clone. Only the portable
    allowlist (providers/models wholesale, model_meta per-model merge) is
    written; installation-local fields are copied over from the existing
    config so they cannot be overwritten by foreign/stale top-level keys.
    """
    # Encrypt any plain provider keys from the import
    for p in incoming.get("providers", []):
        if p.get("api_key") and not p.get("encrypted"):
            p["api_key"] = encrypt_value(p["api_key"])
            p["encrypted"] = True
    existing = load_config()
    portable_keys = ("providers", "models", "model_meta", "pricing")
    imported = {key: incoming[key] for key in portable_keys if key in incoming}

    # Installation-local settings remain exactly as configured on this machine.
    for key in ("port", "host", "proxy", "api_keys", "proxy_prefix"):
        if key in existing:
            imported[key] = existing[key]
    # Runtime policies are local too, but are preserved for compatibility.
    for key in (
        "auto",
        "circuit_breaker",
        "semantic_cache",
        "probe",
        "token_plan_quota",
    ):
        if key in existing:
            imported[key] = existing[key]

    imported.setdefault("pricing", existing.get("pricing", {}))
    # pricing merges PER MODEL like model_meta (file wins on conflict):
    # prices/plan flags are model-intrinsic, and a stale backup must not
    # delete pricing for locally-added models. (2026-09-09: pricing joined
    # the portable allowlist — a fresh install has NO pricing, so excluding
    # it left restored instances displaying 0/— for every cost.)
    if "pricing" in incoming:
        merged_pricing = dict(existing.get("pricing", {}))
        merged_pricing.update(incoming["pricing"])
        imported["pricing"] = merged_pricing
    imported.setdefault("model_overrides", existing.get("model_overrides", {}))
    imported.setdefault("models", existing.get("models", {}))
    # model_meta merges PER MODEL (file wins on conflict, locally-known
    # models absent from the file survive) — a wholesale replace would
    # recreate the very loss this whitelist entry exists to fix whenever
    # the importing machine knows newer models than the backup.
    if "model_meta" in imported:
        merged_meta = dict(existing.get("model_meta", {}))
        merged_meta.update(imported["model_meta"])
        imported["model_meta"] = merged_meta
    else:
        imported["model_meta"] = existing.get("model_meta", {})
    save_config(imported)
    get_breaker().reload(imported)
    get_cache().reload(imported)


@app.post("/api/config/import")
async def api_import_config(request: Request, dry_run: bool = False):
    """Import config from a backup file, encrypting plain API keys.

    dry_run=true returns the change preview without touching the disk —
    the UI runs it before the confirm dialog. The __meta__ envelope (if
    present) is echoed back for staleness display and is NEVER written.
    """
    incoming = await request.json()
    # Validate structure
    if not isinstance(incoming.get("providers"), list):
        raise HTTPException(400, "Invalid config: providers must be a list")
    existing = load_config()
    diff = _portable_import_diff(incoming, existing)
    if dry_run:
        return {"dry_run": True, "diff": diff, "backup_meta": _backup_meta_of(incoming)}
    _apply_portable_import(incoming)
    return {"ok": True, "diff": diff}


@app.post("/api/config/clone")
async def api_clone_config(request: Request):
    """Pull portable config from another running cyRouter instance.

    Typical use: seed a fresh packaged install (port 20108) from the dev
    instance (20100) on the same machine, or migrate between machines on
    the LAN — no manual config.json/.key copying (keys travel decrypted,
    re-encrypted against THIS install's .key on apply). dry_run=true
    previews the diff; the source router key is used only for the outbound
    request and is never stored.
    """
    body = await request.json()
    src = str(body.get("source_url") or "").strip().rstrip("/")
    src_key = str(body.get("source_key") or "").strip()
    dry_run = bool(body.get("dry_run"))
    if not src.startswith(("http://", "https://")):
        raise HTTPException(400, "source_url must be an http(s) URL")
    headers = (
        {"x-api-key": src_key, "authorization": f"Bearer {src_key}"} if src_key else {}
    )
    try:
        # Direct (trust_env=False) client: sibling instances are localhost/LAN;
        # an env proxy must never intercept an intra-host clone.
        r = await _get_http_client().get(
            f"{src}/api/config/export",
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"无法连接源实例 {src}: {e}") from e
    if r.status_code == 401:
        raise HTTPException(401, "源实例拒绝了此 router key（Invalid router key）")
    if r.status_code != 200:
        raise HTTPException(502, f"源实例返回 HTTP {r.status_code}")
    try:
        incoming = r.json()
    except ValueError as e:
        raise HTTPException(502, "源实例响应不是有效 JSON") from e
    if not isinstance(incoming.get("providers"), list):
        raise HTTPException(502, "源实例返回的配置缺少 providers 列表")
    existing = load_config()
    diff = _portable_import_diff(incoming, existing)
    if dry_run:
        return {
            "dry_run": True,
            "diff": diff,
            "backup_meta": _backup_meta_of(incoming),
        }
    _apply_portable_import(incoming)
    return {"ok": True, "diff": diff, "backup_meta": _backup_meta_of(incoming)}


@app.get("/api/cache")
async def api_cache_stats():
    return get_cache().stats()


@app.get("/api/breaker")
async def api_breaker_status():
    """List all circuit breaker states."""
    b = get_breaker()
    b.reload(load_config())
    return {"enabled": b.enabled, "circuits": b.status()}


@app.get("/api/probe/status")
async def api_probe_status():
    """Provider fallback cooldowns + last background probe results."""
    cfg = load_config()
    probe_cfg = cfg.get("probe") or {}
    now = time.time()
    return {
        "enabled": bool(probe_cfg.get("enabled")),
        "interval_seconds": probe_cfg.get("interval_seconds", 900),
        "cooldowns": [
            {
                "provider": p,
                "model": m,
                "age_seconds": int(now - ts),
                "cooldown_seconds": int(cd),
            }
            for (p, m), (ts, cd) in _failed_provider_models.items()
        ],
        "last_results": [
            {
                "provider": p,
                "model": m,
                "ok": r["ok"],
                "detail": r["detail"],
                "age_seconds": int(now - r["ts"]),
            }
            for (p, m), r in _probe_last_results.items()
        ],
    }


@app.get("/api/reasoning/cache")
async def api_reasoning_cache():
    """Reasoning replay cache stats (tool_call_id-keyed, in-memory)."""
    return get_reasoning_cache().stats()


@app.delete("/api/reasoning/cache")
async def api_reasoning_cache_clear():
    """Clear the reasoning replay cache (e.g. to recover from bad entries)."""
    get_reasoning_cache().clear()
    return {"ok": True}


@app.post("/api/breaker/reset")
async def api_breaker_reset(request: Request):
    """Reset all or a specific circuit.  Body: {"provider":"", "model":""}."""
    body = await request.json()
    p = body.get("provider", "")
    m = body.get("model", "")
    b = get_breaker()
    b.reload(load_config())
    b.reset(p, m)
    return {"ok": True}


@app.get("/api/logs")
async def api_get_logs(limit: int = 50, session_id: str | None = None):
    return {"logs": get_log_entries(limit, session_id=session_id)}


@app.get("/api/stats")
async def api_get_stats(session_id: str | None = None):
    """Pre-computed dashboard stats — avoids loading all logs client-side.

    Optional session_id filters entries by x-cyrouter-session-id header.
    """
    import calendar as _cal

    from router import (
        _resolve_model_alias,
        _token_plan_provider_urls,
        compute_token_cost,
        get_log_stats,
    )

    cfg = load_config()
    # config pricing > builtin catalog — with builtin fallback the 18
    # catalog models stop dropping into missing_pricing on fresh installs.
    pricing = effective_pricing_map(cfg)
    _plan_urls = _token_plan_provider_urls(cfg)

    import datetime as _dt

    now = time.time()
    today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = today.timestamp()
    tm = time.localtime(now)
    if tm.tm_hour < 1:
        today_start -= 86400

    # Days in current month for plan daily amortization
    days_in_month = _cal.monthrange(tm.tm_year, tm.tm_mon)[1]

    entries = get_log_stats(session_id=session_id)
    total_in = total_out = total_cache_read = token_cost = cache_saved_cost = 0
    plan_cost = (
        0  # sum of daily-amortized subscription costs for plan models used today
    )
    plan_models_used: set = set()
    tp_plan_saved = 0.0  # per-token value covered by the token plan today
    tp_plan_used = False
    missing_pricing: set = set()
    by_model: dict[str, dict] = {}
    by_key: dict[str, dict] = {}

    for e in entries:
        if e["timestamp"] < today_start:
            continue
        # Skip failed/truncated requests — they have no usage and would
        # inflate request counts vs daily_stats (which only logs status==ok).
        if e.get("status") != "ok":
            continue
        u = _normalize_usage(e.get("usage") or {})
        inp = u.get("input_tokens", u.get("prompt_tokens", 0))
        out = u.get("output_tokens", u.get("completion_tokens", 0))
        cr = u.get("cache_read_input_tokens", 0)
        cc = u.get("cache_creation_input_tokens", 0)
        total_in += inp
        total_out += out
        total_cache_read += cr
        model = e.get("model", "?")
        # Apply model_aliases so logs of rerouted requests (e.g. claude-haiku-*
        # → deepseek-v4-flash) are aggregated and priced as the actual model
        # that handled the traffic. Otherwise the dashboard would flag the
        # original client-facing name as "missing pricing".
        resolved = _resolve_model_alias(model, cfg)
        if resolved != model:
            model = resolved
        if model not in by_model:
            by_model[model] = {
                "count": 0,
                "in": 0,
                "out": 0,
                "cache_read": 0,
                "cc": 0,
                "cost": 0,
            }
        by_model[model]["count"] += 1
        by_model[model]["in"] += inp
        by_model[model]["out"] += out
        by_model[model]["cache_read"] += cr
        by_model[model]["cc"] += cc
        # Per-key aggregation
        kn = e.get("key_name") or "(未命名)"
        if kn not in by_key:
            by_key[kn] = {
                "requests": 0,
                "in": 0,
                "out": 0,
                "cache_read": 0,
                "cost": 0,
                "theoretical_cost": 0,
            }
        by_key[kn]["requests"] += 1
        by_key[kn]["in"] += inp
        by_key[kn]["out"] += out
        by_key[kn]["cache_read"] += cr
        p = pricing.get(model)
        if p:
            by_key[kn]["cost"] += compute_token_cost(inp, out, cr, cc, p, cached=True)
        if isinstance(p, dict) and p.get("plan_monthly"):
            plan_models_used.add(model)
            by_model[model]["plan"] = True
            by_model[model]["plan_monthly"] = p["plan_monthly"]
        elif p and e.get("provider", "") in _plan_urls:
            # Served by a subscription-plan provider (Aliyun Token Plan):
            # prepaid, excluded from out-of-pocket spend; tracked as plan
            # savings so 理论 = 实际 + 缓存省 + 套餐省 stays conserved.
            # Coverage is per-serving-provider: the same model may still be
            # pay-as-you-go when a fallback provider serves it.
            tp_plan_saved += compute_token_cost(inp, out, cr, cc, p, cached=True)
            tp_plan_used = True
        elif p:
            cost = compute_token_cost(inp, out, cr, cc, p, cached=True)
            token_cost += cost
            by_model[model]["cost"] += cost
        else:
            missing_pricing.add(model)
        if p and cr:
            actual_cache_price = p.get("cache_input", p["input"])
            cache_saved_cost += cr * (p["input"] - actual_cache_price) / 1_000_000

    # Calculate plan savings: what per-token would have cost vs. daily amortization
    plan_saved = 0.0
    for model in plan_models_used:
        d = by_model[model]
        p = pricing[model]
        daily = p["plan_monthly"] / days_in_month
        plan_cost += daily
        # What this usage would cost at per-token prices. Cache-read tokens
        # are billed at cache price, NOT full input — otherwise the cache
        # discount gets double-counted (it is already in cache_saved_cost).
        per_token = compute_token_cost(
            d["in"], d["out"], d["cache_read"], d.get("cc", 0), p, cached=True
        )
        # Plan savings = what the plan covers at per-token rates (gross, NOT
        # minus daily amortization). The monthly fee is a sunk cost excluded
        # from today's stats, so this keeps the identity
        #   理论 = 实际(非套餐) + 缓存省 + 套餐省  strictly conserved.
        saved_today = per_token
        plan_saved += saved_today
        by_model[model]["cost"] = (
            0  # plan: pre-paid subscription, not a real cost today
        )
        by_model[model]["plan_daily"] = round(daily, 2)
        by_model[model]["plan_saved"] = round(saved_today, 2)

    # Token-plan coverage: same semantics as monthly plans — prepaid by the
    # subscription, excluded from out-of-pocket, counted as plan savings.
    plan_saved += tp_plan_saved
    if tp_plan_used:
        _tp_cfg = cfg.get("token_plan_quota") or {}
        _monthly_fee = float(_tp_cfg.get("monthly_fee", 0))
        if _monthly_fee > 0:
            plan_cost += _monthly_fee / days_in_month

    total_cost = token_cost  # actual spend = per-token costs only (plan is pre-paid subscription)

    # Theoretical cost: what ALL models would cost at per-token rates (no plan, no cache discount)
    theoretical_cost = 0.0
    for e in entries:
        if e["timestamp"] < today_start:
            continue
        if e.get("status") != "ok":
            continue
        u = _normalize_usage(e.get("usage") or {})
        inp = u.get("input_tokens", u.get("prompt_tokens", 0))
        out = u.get("output_tokens", u.get("completion_tokens", 0))
        p = pricing.get(e.get("model", ""))
        if p:
            cost = compute_token_cost(inp, out, 0, 0, p, cached=False)
            theoretical_cost += cost
            model = _resolve_model_alias(e.get("model", "?"), cfg)
            if model in by_model:
                by_model[model].setdefault("theoretical_cost", 0)
                by_model[model]["theoretical_cost"] += cost
            kn = e.get("key_name") or "(未命名)"
            if kn in by_key:
                by_key[kn]["theoretical_cost"] += cost

    from stats import get_today_overview

    today_base = get_today_overview(cfg)

    return {
        "today": {
            **today_base,
            "plan_cost": plan_cost,
            "plan_models": list(plan_models_used),
            "days_in_month": days_in_month,
        },
        "by_model": by_model,
        "by_key": by_key,
        "providers": len(cfg.get("providers", [])),
        "models": len(_get_model_map(cfg)),
        "missing_pricing": sorted(missing_pricing),
    }


@app.get("/api/provider/quota")
async def api_provider_quota(refresh: bool = False):
    """Query provider quota/balance for supported providers.

    Cached for 5 minutes — the dashboard loads this on every visit, and the
    upstream zhipu API can take 0.3-1s. Cache avoids blocking the dashboard
    render on a slow external call. Force refresh via ?refresh=true.
    """
    global _quota_cache, _quota_cache_ts
    if (
        not refresh
        and _quota_cache is not None
        and (time.time() - _quota_cache_ts) < _QUOTA_CACHE_TTL
    ):
        return _quota_cache

    cfg = load_config()
    results: dict = {}
    for p_cfg in cfg.get("providers", []):
        ptype = p_cfg.get("type", "")
        name = p_cfg["name"]
        provider_name = p_cfg.get("provider", "")
        api_key = p_cfg.get("api_key", "")
        if p_cfg.get("encrypted"):
            api_key = decrypt_value(api_key)

        # Zhipu providers (legacy or new format)
        is_zhipu = ptype in ("zhipu-coding", "zhipu") or provider_name == "zhipu"
        if is_zhipu:
            try:
                client = _get_http_client_for_url(
                    "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
                )
                resp = await client.get(
                    "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                    headers={
                        "Authorization": api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    results[name] = data
            except Exception:
                pass

        # Aliyun Token Plan: local credits estimation. The service exposes no
        # quota API, so the dashboard bar is derived from the tracked cost of
        # requests this provider served (see router._load_token_plan_usage).
        tp_quota = cfg.get("token_plan_quota") or {}
        if name == tp_quota.get("provider"):
            try:
                results[name] = _estimate_token_plan_quota(cfg, p_cfg, name, tp_quota)
            except Exception:
                logger.warning("token plan quota estimation failed", exc_info=True)

        # Per-provider local quota estimation: token-denominated plan quotas
        # with no official usage API (e.g. Kimi Code's weekly token pool +
        # 5h rate window). Config lives on the provider as "local_quota";
        # weekly usage comes from daily_stats by_provider token counters,
        # the sub-day rate window from the request log.
        local_q = p_cfg.get("local_quota")
        if local_q:
            try:
                results[name] = _estimate_local_quota(cfg, p_cfg, name, local_q)
            except Exception:
                logger.warning("local quota estimation failed", exc_info=True)
    _quota_cache = results
    _quota_cache_ts = time.time()
    return results


def _estimate_token_plan_quota(
    cfg: dict, p_cfg: dict, name: str, tp_quota: dict
) -> dict:
    """Build a zhipu-shaped quota payload from locally tracked usage.

    Credits conversion uses `rmb_per_credit` (calibrated against the console
    “我的订阅” used-credits figure — measured error ≈1%). When `window_reset`
    is set (the exact reset timestamp shown in the console), the real window
    is used and nextResetTime is exact; otherwise a rolling window
    approximates it. Expired resets auto-advance in whole windows — accurate
    while usage is continuous; after a usage gap refresh `window_reset`
    from the console.
    """
    import datetime as _dt

    from router import _load_token_plan_usage

    window = int(tp_quota.get("window_days", 7))
    limit_credits = float(tp_quota.get("limit_credits", 0))
    rmb_per_credit = float(tp_quota.get("rmb_per_credit", 0))
    if limit_credits <= 0 or rmb_per_credit <= 0:
        return {}

    now = _dt.datetime.now()
    step = _dt.timedelta(days=window)
    # Shared window-reset parsing/advance (same as local_quota's rate window).
    reset_ts, window_start = _parse_reset(tp_quota.get("window_reset") or "", step, now)
    if window_start is None:
        window_start = now - step

    usage = _load_token_plan_usage()
    cost = 0.0
    reqs = 0
    day = window_start.date()
    while day <= now.date():
        d = usage.get(day.isoformat()) or {}
        cost += float(d.get("cost", 0.0))
        reqs += int(d.get("requests", 0))
        day += _dt.timedelta(days=1)

    est_credits = cost / rmb_per_credit
    pct = min(100.0, est_credits / limit_credits * 100.0)
    return {
        "limits": [
            {
                "type": "TOKENS_LIMIT",
                "percentage": round(pct, 1),
                "nextResetTime": int(reset_ts.timestamp()) if reset_ts else 0,
                "label": f"{window}天额度·估算",
            }
        ],
        "estimated": True,
        "detail": {
            "window_days": window,
            "window_start": window_start.strftime("%Y-%m-%d %H:%M"),
            "window_reset": (reset_ts.strftime("%Y-%m-%d %H:%M") if reset_ts else None),
            "cost_rmb": round(cost, 2),
            "est_credits_used": round(est_credits, 1),
            "limit_credits": limit_credits,
            "requests": reqs,
        },
    }


def _parse_reset(reset_str: str, step, now):
    """Parse a reset timestamp and auto-advance it in whole `step` windows
    until it is in the future. Returns (reset_ts, window_start) or (None, None).
    """
    import datetime as _dt

    reset_ts = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            reset_ts = _dt.datetime.strptime(reset_str.strip(), fmt)
            break
        except ValueError:
            continue
    if reset_ts is None:
        return None, None
    while reset_ts <= now:
        reset_ts += step
    return reset_ts, reset_ts - step


def _provider_hosts(p_cfg: dict) -> set:
    """Hostnames of all the provider's base_urls — request-log and
    daily_stats entries are keyed by the actual upstream URL, so matching by
    host covers both the OpenAI and Anthropic protocol endpoints."""

    hosts = set()
    urls = list((p_cfg.get("base_urls") or {}).values())
    if p_cfg.get("base_url"):
        urls.append(p_cfg["base_url"])
    for u in urls:
        h = _hostname(u)
        if h:
            hosts.add(h)
    return hosts


def _estimate_local_quota(cfg: dict, p_cfg: dict, name: str, lq: dict) -> dict:
    """Token-denominated local quota estimation (zhipu-shaped payload).

    For plan providers with no official usage API — built for Kimi Code,
    whose console shows a weekly token pool plus a 5-hour rate window.

    Config on the provider::

        "local_quota": {
            "window_days": 7,             # plan window (weekly)
            "limit_tokens": 4730000,      # plan token limit per window
            "window_reset": "…",          # exact reset from the console
            "rate_window_hours": 5,       # optional sub-day rate window
            "rate_limit_tokens": 1875000,
            "rate_window_reset": "…"
        }

    Token accounting: input + cache_read + cache_creation + output —
    cache buckets are INCLUDED: kimi 的池子按含缓存口径扣减（2026-08-10
    校准实锤：新周窗 3.3h 内控制台 19%，raw 口径仅 5.46M → 隐含池
    28.7M，与 Allegro 社区估计月池 23.75 亿差 20 倍；含缓存 62.9M →
    隐含 331M/周，量级吻合；早期 raw 口径严重低估，见 2026-08-09 的
    「按缓存读取计费」结论）。Limits were derived by back-calculation
    from console percentage readings, so they are approximate:
    recalibrate against the console after each window reset.

    Data sources: the plan window (days) sums daily_stats by_provider token
    counters; the rate window (hours) scans the request log because daily
    granularity is too coarse for a 5h block.
    """
    import datetime as _dt

    from router import _load_daily_stats, get_log_entries

    hosts = _provider_hosts(p_cfg)
    if not hosts:
        return {}

    def _host_of(url: str) -> str:
        return _hostname(url)

    # --- Live readings first (Kimi /usages et al.) — real percentages +
    # exact reset timestamps from the provider's own usage endpoint; local
    # token accounting below is the fallback when the reading is stale.
    live = _usages_cache.get(name)
    if live and live.get("subscription") == "exhausted":
        # Confirmed subscription exhaustion (consecutive insufficient-balance
        # 429s on /usages itself): local token estimates are meaningless —
        # the account has no credits left. Render a definitive red bar; the
        # reset time is unknown because only renewal (not time) restores it.
        since = live.get("exhausted_since") or 0
        return {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "percentage": 100.0,
                    "nextResetTime": 0,
                    "label": "\u8ba2\u9605\u5df2\u8fc7\u671f",
                }
            ],
            "estimated": False,
            "source": "usages",
            "subscription": "exhausted",
            "detail": {
                "exhausted_since": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(since)
                )
                if since
                else "",
                "hint": "Credits \u5df2\u8017\u5c3d\uff08\u8ba2\u9605\u5230\u671f\u672a\u7eed\u8d39\uff09\uff0c"
                "\u7eed\u8d39\u540e\u81ea\u52a8\u6062\u590d",
            },
        }
    if live and (time.time() - live.get("fetched_at", 0)) <= _USAGES_STALE_SECONDS:
        live_limits = []
        w = live.get("weekly") or {}
        if w:
            live_limits.append(
                {
                    "type": "TOKENS_LIMIT",
                    "percentage": round(w.get("used_pct", 0.0), 1),
                    "nextResetTime": _iso_to_epoch(w.get("reset_time")),
                    "label": "\u5468\u671f\u989d\u5ea6",
                }
            )
        r = live.get("rate") or {}
        if r:
            rate_used = max(
                0.0, r.get("limit_pct", 100.0) - r.get("remaining_pct", 0.0)
            )
            live_limits.append(
                {
                    "type": "TOKENS_LIMIT",
                    "percentage": round(rate_used, 1),
                    "nextResetTime": _iso_to_epoch(r.get("reset_time")),
                    "label": f"{r.get('hours', 0):g}h \u9891\u9650",
                }
            )
        if live_limits:
            return {
                "limits": live_limits,
                "estimated": False,
                "source": "usages",
                "fetched_at": live.get("fetched_at"),
                "detail": {
                    "membership": live.get("membership"),
                    "parallel_limit": live.get("parallel_limit"),
                    "weekly_reset": (w.get("reset_time") or "")[:19],
                    "rate_reset": (r.get("reset_time") or "")[:19],
                },
            }

    now = _dt.datetime.now()
    limits = []
    detail = {}

    # --- Plan window (e.g. weekly pool) — daily_stats by_provider ---------
    limit_tokens = float(lq.get("limit_tokens", 0))
    if limit_tokens > 0:
        window = int(lq.get("window_days", 7))
        step = _dt.timedelta(days=window)
        reset_ts, window_start = _parse_reset(lq.get("window_reset") or "", step, now)
        if window_start is None:
            reset_ts = None
            window_start = now - step
        stats = _load_daily_stats()
        used = reqs = 0
        day = window_start.date()
        while day <= now.date():
            d = stats.get(day.isoformat()) or {}
            for url, pe in (d.get("by_provider") or {}).items():
                if _host_of(url) in hosts:
                    # by_provider "in" is normalized (_normalize_usage merges
                    # the cache bucket for DeepSeek/Anthropic-style usage
                    # reports) — 含缓存口径，与 kimi 池子扣减一致。
                    used += int(pe.get("in", 0)) + int(pe.get("out", 0))
                    reqs += int(pe.get("requests", 0))
            day += _dt.timedelta(days=1)
        pct = min(100.0, used / limit_tokens * 100.0)
        limits.append(
            {
                "type": "TOKENS_LIMIT",
                "percentage": round(pct, 1),
                "nextResetTime": int(reset_ts.timestamp()) if reset_ts else 0,
                "label": f"{window}天额度·估算",
            }
        )
        detail["plan"] = {
            "window_days": window,
            "window_start": window_start.strftime("%Y-%m-%d %H:%M"),
            "window_reset": (reset_ts.strftime("%Y-%m-%d %H:%M") if reset_ts else None),
            "tokens_used": used,
            "limit_tokens": int(limit_tokens),
            "requests": reqs,
        }

    # --- Rate window (e.g. 5h block) — request log (sub-day granularity) --
    rate_limit = float(lq.get("rate_limit_tokens", 0))
    rate_hours = float(lq.get("rate_window_hours", 0))
    if rate_limit > 0 and rate_hours > 0:
        step = _dt.timedelta(hours=rate_hours)
        reset_ts, block_start = _parse_reset(
            lq.get("rate_window_reset") or "", step, now
        )
        if block_start is None:
            reset_ts = None
            block_start = now - step  # rolling approximation
        block_ts = block_start.timestamp()
        used = reqs = 0
        for e in get_log_entries(limit=2000):
            if e.get("status") != "ok" or e.get("timestamp", 0) < block_ts:
                continue
            if _host_of(e.get("provider") or "") not in hosts:
                continue
            u = e.get("usage") or {}
            # 含缓存口径：request_log 里是 DeepSeek 风格原始值（input=cache
            # miss，cache_read/creation 独立），需显式加上。
            used += (
                int(u.get("input_tokens", 0))
                + int(u.get("cache_read_input_tokens", 0))
                + int(u.get("cache_creation_input_tokens", 0))
                + int(u.get("output_tokens", 0))
            )
            reqs += 1
        pct = min(100.0, used / rate_limit * 100.0)
        limits.append(
            {
                "type": "TOKENS_LIMIT",
                "percentage": round(pct, 1),
                "nextResetTime": int(reset_ts.timestamp()) if reset_ts else 0,
                "label": f"{rate_hours:g}h 频限·估算",
            }
        )
        detail["rate"] = {
            "window_hours": rate_hours,
            "window_start": block_start.strftime("%Y-%m-%d %H:%M"),
            "window_reset": (reset_ts.strftime("%Y-%m-%d %H:%M") if reset_ts else None),
            "tokens_used": used,
            "limit_tokens": int(rate_limit),
            "requests": reqs,
        }

    if not limits:
        return {}
    return {"limits": limits, "estimated": True, "detail": detail}


@app.get("/api/stats/daily")
async def api_get_daily_stats(year: int = 0, month: int = 0):
    """Daily token/cost breakdown for a given month."""
    from router import get_daily_stats

    now = time.localtime()
    if year == 0:
        year = now.tm_year
    if month == 0:
        month = now.tm_mon
    return {"year": year, "month": month, "days": get_daily_stats(year, month)}


@app.get("/api/stats/recent")
async def api_get_recent_stats(days: int = 7, offset: int = 0):
    """Daily token/cost breakdown for N days ending `offset` days ago."""
    from router import get_recent_daily_stats

    if days <= 0 or days > 90:
        days = 7
    if offset < 0:
        offset = 0
    return {"days": get_recent_daily_stats(days, offset)}


# ── Usage view for OpenCode /usage command ──────────────────────────────
# Aggregates today's per-model stats + current-month totals + provider
# balances into a single response shaped for client display. Reuses the
# same pricing/usage logic as /api/stats and the quota cache from
# /api/provider/quota.
_USAGE_CACHE: dict | None = None
_USAGE_CACHE_TS: float = 0.0
_USAGE_CACHE_TTL = (
    45.0  # seconds — client throttles to ≥1/min, this absorbs concurrent sessions
)


@app.get("/api/usage")
async def api_get_usage(refresh: bool = False):
    """Account usage summary for OpenCode TUI integration.

    Returns today's cost/requests, current billing-cycle cost, per-model
    breakdown, and any provider balances CyRouter can query (currently
    Zhipu only). Cached ~45s to absorb concurrent client sessions.
    """
    import calendar as _cal
    import datetime as _dt

    from router import (
        _normalize_usage,
        _resolve_model_alias,
        _token_plan_provider_urls,
        compute_token_cost,
        get_daily_stats,
        get_log_stats,
    )

    global _USAGE_CACHE, _USAGE_CACHE_TS
    if (
        not refresh
        and _USAGE_CACHE is not None
        and (time.time() - _USAGE_CACHE_TS) < _USAGE_CACHE_TTL
    ):
        return _USAGE_CACHE

    cfg = load_config()
    # config pricing > builtin catalog (same effective view as /api/stats).
    pricing = effective_pricing_map(cfg)
    _plan_urls = _token_plan_provider_urls(cfg)
    now = time.time()
    tm = time.localtime(now)

    # ── Billing cycle: current calendar month (local time) ──
    days_in_month = _cal.monthrange(tm.tm_year, tm.tm_mon)[1]
    cycle_start = _dt.datetime(tm.tm_year, tm.tm_mon, 1)
    cycle_end = cycle_start + _dt.timedelta(days=days_in_month)
    cycle_start_iso = cycle_start.isoformat(timespec="seconds")
    cycle_end_iso = cycle_end.isoformat(timespec="seconds")

    # ── Today window (with 1h midnight buffer matching /api/stats) ──
    today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = today.timestamp()
    if tm.tm_hour < 1:
        today_start -= 86400

    # ── Today's per-model breakdown (mirrors /api/stats logic) ──
    entries = get_log_stats()
    by_model: dict[str, dict] = {}
    today_cost = 0.0
    today_requests = 0

    for e in entries:
        if e["timestamp"] < today_start:
            continue
        if e.get("status") != "ok":
            continue
        u = _normalize_usage(e.get("usage") or {})
        inp = u.get("input_tokens", u.get("prompt_tokens", 0))
        out = u.get("output_tokens", u.get("completion_tokens", 0))
        cr = u.get("cache_read_input_tokens", 0)
        cc = u.get("cache_creation_input_tokens", 0)
        model = _resolve_model_alias(e.get("model", "?"), cfg)
        if model not in by_model:
            by_model[model] = {
                "id": model,
                "name": model,
                "today_cost": 0.0,
                "today_requests": 0,
                "today_tokens": {"input": 0, "output": 0},
            }
        m = by_model[model]
        m["today_requests"] += 1
        m["today_tokens"]["input"] += inp
        m["today_tokens"]["output"] += out
        today_requests += 1
        p = pricing.get(model)
        # Requests served by a subscription-plan provider (Aliyun Token
        # Plan) are prepaid — excluded from out-of-pocket spend like
        # plan_monthly models.
        if (
            isinstance(p, dict)
            and not p.get("plan_monthly")
            and e.get("provider", "") not in _plan_urls
        ):
            cost = compute_token_cost(inp, out, cr, cc, p, cached=True)
            m["today_cost"] += cost
            today_cost += cost
        # plan_monthly / plan-provider requests: cost counted as 0 today

    # ── Current-month period cost from persisted daily stats ──
    month_days = get_daily_stats(tm.tm_year, tm.tm_mon)
    # pay_cost excludes plan_monthly models; cost includes amortized plan.
    # Use pay_cost for "real spend this period" semantics.
    period_cost = round(sum(d["pay_cost"] for d in month_days), 4)

    # ── Provider balances / quotas ──
    # Zhipu coding plan is subscription-based (no monetary balance). The quota
    # API returns `limits` (5h token window, weekly token window, monthly MCP
    # calls) + `level` (plan tier). We surface the weekly token quota as the
    # primary total/left since it's the binding constraint for coding use.
    balances: list[dict] = []
    quota = (
        _quota_cache
        if (
            _quota_cache is not None
            and (time.time() - _quota_cache_ts) < _QUOTA_CACHE_TTL
        )
        else None
    )
    if quota is None:
        # Lazy refresh: don't block on slow upstream; try once with short timeout.
        try:
            quota = await api_provider_quota(refresh=False)
        except Exception:
            quota = {}
    for p_cfg in cfg.get("providers", []):
        provider_name = p_cfg.get("provider", "") or p_cfg.get("name", "")
        is_zhipu = (
            p_cfg.get("type", "") in ("zhipu-coding", "zhipu")
            or provider_name == "zhipu"
        )
        if is_zhipu:
            q = (quota or {}).get(p_cfg["name"]) or {}
            limits = q.get("limits") or []
            # Parse token limits: sort by nextResetTime ascending →
            # [0] = 5-hour window, [1] = weekly window (per Zhipu convention).
            token_limits = sorted(
                [
                    lim
                    for lim in limits
                    if lim.get("type") == "TOKENS_LIMIT" and lim.get("nextResetTime")
                ],
                key=lambda lim: lim["nextResetTime"],
            )
            weekly = (
                token_limits[1]
                if len(token_limits) >= 2
                else (token_limits[0] if token_limits else {})
            )
            weekly_pct = weekly.get("percentage", 0) or 0
            # total/left expressed as percentage points (subscription has no
            # absolute token cap exposed; percentage is the only absolute).
            entry = {
                "provider": p_cfg["name"],
                "type": "subscription",
                "plan_level": q.get("level"),  # lite / pro / max
                "total": 100,  # percentage scale
                "left": max(0, 100 - weekly_pct),
                "currency": "percent",
                "available": bool(q),
            }
            # Detailed quota windows for clients that want them.
            detail = []
            for lim in token_limits:
                pct = lim.get("percentage", 0) or 0
                reset_ms = lim.get("nextResetTime")
                reset_iso = (
                    _dt.datetime.fromtimestamp(reset_ms / 1000).isoformat(
                        timespec="seconds"
                    )
                    if reset_ms
                    else None
                )
                detail.append(
                    {
                        "used_percent": pct,
                        "left_percent": max(0, 100 - pct),
                        "resets_at": reset_iso,
                    }
                )
            # Label windows by position (5h first, weekly second).
            if len(detail) >= 1:
                detail[0]["window"] = "5h"
            if len(detail) >= 2:
                detail[1]["window"] = "weekly"
            if detail:
                entry["quotas"] = detail
            balances.append(entry)

    result = {
        "account": {
            "plan": "cyrouter-local",
        },
        "balance": {
            "providers": balances,
            "currency": "CNY",
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        },
        "billing_cycle": {
            "start_at": cycle_start_iso,
            "end_at": cycle_end_iso,
            "reset_at": cycle_end_iso,
        },
        "usage": {
            "period_cost": period_cost,
            "today_cost": round(today_cost, 4),
            "today_requests": today_requests,
        },
        "models": sorted(
            by_model.values(),
            key=lambda x: x["today_cost"],
            reverse=True,
        ),
    }

    _USAGE_CACHE = result
    _USAGE_CACHE_TS = time.time()
    return result


@app.get("/api/models")
async def api_list_models():
    """Rich model catalog in cyRouter's own format.

    Distinct from /v1/models (pure OpenAI standard, no cost/limits) and
    /api.json (opencode convention whose `cost` is implicitly USD/M):
    this endpoint uses cyRouter-native field names and explicitly tags
    the pricing currency so there is no unit ambiguity.

    Back-compat: the top-level "__aliases__" key is preserved. The
    dashboard (web/index.html loadModels) consumes only that field —
    its model list comes from /api/config — so restructuring the rest
    of the body is safe.
    """
    cfg = load_config()
    # Pricing resolution mirrors limits: config entry wins wholesale over
    # the builtin catalog (2026-09-10) — fresh installs display real prices
    # without cloning a config first. builtin_pricing is also exposed
    # top-level so the UI can price models NOT yet added to config.
    overrides = cfg.get("model_overrides", {}) or {}

    models = []
    prov_by_name = {p["name"]: p for p in cfg.get("providers", [])}
    for m, chain in _get_model_map(cfg).items():
        if "*" in m or "?" in m or not chain:
            continue
        p = prov_by_name.get(chain[0])
        if p is None:
            continue
        proto = p.get("type", "openai")
        brand = p.get("provider") or p.get("name")
        name = m.lower()
        is_embedding = "embedding" in name
        # vision 解析链与 limit 同哲学：model_overrides > model_meta（本地权威）
        # > 名字启发式（_model_supports_vision 与 auto 图片路由共用同一函数，
        # 两侧永不漂移）。此前只看名字——kimi-k3 这类名字不含 vl/vision 的视觉
        # 模型被错标 false，pi（cyrouter-sync 读 /api/models）据此拦死图片输入。
        is_vision = _model_supports_vision(m, cfg)
        # Limits from the unified resolver (override > model_meta >
        # pricing.max_context > family heuristic) with per-field source
        # tags, so the dashboard can show where each value came from.
        limit = _resolve_model_limit(m, cfg)
        # Pricing in RMB / million tokens; source tag mirrors limit_source
        # so the UI can badge 内置 vs 本地 (config vs BUILTIN_PRICING).
        price = effective_pricing(cfg, m) or {}
        pricing_obj = None
        if price:
            pricing_obj = {
                "currency": "CNY",
                "unit": "per_million_tokens",
                "source": "config" if m in (cfg.get("pricing") or {}) else "builtin",
            }
            if "input" in price:
                pricing_obj["input"] = price["input"]
            if "output" in price:
                pricing_obj["output"] = price["output"]
            if "cache_input" in price:
                pricing_obj["cached_input"] = price["cache_input"]
            if "plan_monthly" in price:
                pricing_obj["plan_monthly"] = price["plan_monthly"]
        models.append(
            {
                "id": m,
                "provider": p["name"],
                "provider_brand": brand,
                "protocol": proto,
                "context_window": limit["context"],
                "max_output_tokens": limit["output"],
                "limit_source": {
                    "context": limit["context_source"],
                    "output": limit["output_source"],
                },
                "pricing": pricing_obj,
                "capabilities": {
                    "reasoning": not is_embedding,
                    "tool_call": not is_embedding,
                    "vision": is_vision,
                    "structured_output": not is_embedding,
                    "temperature": not is_embedding,
                },
            }
        )

    # Virtual "auto" model: expose it so clients can select it (pi discovers
    # models via this endpoint through cyrouter-sync). Limits are the MIN
    # across the configured tier candidates (simple/complex model +
    # fallbacks) — pi's auto-compact threshold then stays safe even when a
    # session has fallen back to the smallest-context candidate. vision=true
    # whenever a vision_model is configured (auto routes image turns there).
    auto_cfg = cfg.get("auto") or {}
    auto_cands = {
        auto_cfg.get(k)
        for k in (
            "simple_model",
            "simple_fallback",
            "complex_model",
            "complex_fallback",
        )
    }
    auto_cands = {c for c in auto_cands if c}
    if auto_cands:
        limits = [_resolve_model_limit(c, cfg) for c in auto_cands]
        models.append(
            {
                "id": "auto",
                "provider": "cyrouter",
                "provider_brand": "cyRouter",
                "protocol": "openai",
                "context_window": min(lim["context"] for lim in limits),
                "max_output_tokens": min(lim["output"] for lim in limits),
                "limit_source": {"context": "auto-min", "output": "auto-min"},
                "pricing": None,
                "capabilities": {
                    "reasoning": True,
                    "tool_call": True,
                    "vision": bool(auto_cfg.get("vision_model")),
                    "structured_output": True,
                    "temperature": True,
                },
            }
        )

    result: dict = {"models": models}
    # Builtin price catalog — display-only reference for models not yet in
    # config (add-model dropdown "未定价" tags). Never written back to
    # config; user entries always win wholesale per effective_pricing().
    result["builtin_pricing"] = BUILTIN_PRICING
    # Back-compat: dashboard consumes this for the alias rows.
    aliases = cfg.get("model_aliases") or {}
    if aliases:
        result["__aliases__"] = [
            {"pattern": k, "target": v} for k, v in aliases.items()
        ]
    return result


@app.get("/api/models/meta")
async def api_models_meta():
    """Resolved per-model limits with per-field sources (management UI)."""
    cfg = load_config()
    out = {}
    for m, chain in _get_model_map(cfg).items():
        if "*" in m or "?" in m or not chain:
            continue
        out[m] = _resolve_model_limit(m, cfg)
    return {"models": out}


@app.put("/api/models/meta/{model}")
async def api_put_model_meta(model: str, request: Request):
    """Set explicit limit overrides (writes model_overrides[model].limit).

    Body: {"context": N, "output": M} — null/absent leaves that field
    untouched; passing both null clears the override entirely.
    """
    data = await request.json()
    cfg = load_config()
    overrides = cfg.setdefault("model_overrides", {})
    ov = overrides.setdefault(model, {})
    limit = ov.setdefault("limit", {})
    if "context" in data:
        if data["context"] is None:
            limit.pop("context", None)
        else:
            limit["context"] = int(data["context"])
    if "output" in data:
        if data["output"] is None:
            limit.pop("output", None)
        else:
            limit["output"] = int(data["output"])
    if not limit:
        ov.pop("limit", None)
    if not ov:
        overrides.pop(model, None)
    save_config(cfg)
    return {"ok": True, **(_resolve_model_limit(model, cfg))}


@app.delete("/api/models/meta/{model}")
async def api_delete_model_meta(model: str):
    """Remove all overrides for a model (back to model_meta/heuristic)."""
    cfg = load_config()
    overrides = cfg.get("model_overrides") or {}
    if model in overrides:
        del overrides[model]
        save_config(cfg)
    return {"ok": True}


# ── 官网价格检查（提示通道，2026-09-10）──
# 设计评审结论：无人值守爬虫不可行（zhipu/kimi JS 渲染、页面结构漂移、
# deepseek USD 分时/阿里云阶梯多地域语义歧义），但「手动触发检查 +
# 只提示不生效」是安全的：爬错只影响提示，生效价永远是
# config.pricing > BUILTIN_PRICING（wholesale 不变）。

_PRICING_CHECK_FILE = DATA_DIR / "pricing_check.json"
# 手动检查与每日循环共用路径的串行化锁（fetch + 写缓存 + 自动采纳
# 原子化，避免交叉写与双份抓取）
_PRICING_CHECK_LOCK = asyncio.Lock()


def _pricing_verdict(fetched: dict | None, effective: dict | None) -> str:
    """same/differs 仅对 CNY 单一价；区间/外币/缺基准一律 info。"""
    if not fetched or not effective:
        return "info"
    fi, fo = fetched.get("input"), fetched.get("output")
    ei, eo = effective.get("input"), effective.get("output")
    if not (
        isinstance(fi, (int, float))
        and isinstance(fo, (int, float))
        and isinstance(ei, (int, float))
        and isinstance(eo, (int, float))
    ):
        return "info"  # 分时/阶梯区间（或缺失）
    if fetched.get("currency", "CNY") != "CNY":
        return "info"  # USD 等外币口径，不做判定
    return "same" if (abs(fi - ei) < 0.005 and abs(fo - eo) < 0.005) else "differs"


async def _fetch_pricing_pages() -> dict[str, str]:
    """并行拉取可抓源的定价页（代理感知；unavailable 源不拉）。"""
    pages: dict[str, str] = {}
    keys = [k for k, c in PRICING_SOURCES.items() if "extractor" in c]

    async def _one(key: str) -> None:
        url = PRICING_SOURCES[key]["url"]
        try:
            client = _get_http_client_for_url(url)
            r = await client.get(
                url, timeout=httpx.Timeout(20.0), follow_redirects=True
            )
            if r.status_code == 200 and len(r.text) > 1000:
                pages[key] = r.text
            else:
                logger.warning(
                    "pricing-check: %s -> HTTP %s (%d bytes)",
                    key,
                    r.status_code,
                    len(r.text),
                )
        except Exception as e:
            logger.warning("pricing-check: %s fetch failed: %s", key, e)

    if keys:
        await asyncio.gather(*(_one(k) for k in keys))
    return pages


def _auto_adopt_from_check(sources: dict) -> list[dict]:
    """护栏内自动采纳官网价（写入 config.pricing，2026-09-10 用户显式开启）。

    只在全部护栏通过时写入，逐条记录；任何存疑口径一律跳过留人工：
    - verdict=differs（CNY 单一数值且有生效价基准）；
    - 只更新 config.pricing 里**已有条目**的模型——builtin 兜底的模型
      不新建条目，否则会破坏「跟随内置」语义（config 条目永远赢，
      内置目录随版本更新就不跟进了）；
    - 有 promo（限时折扣）的不动——促销是临时口径，采纳会固化折扣价；
    - 跳变熔断：|new/old - 1| > max_ratio_change（默认 1.0，即最多翻倍
      或腰斩）拒绝——官网改版提取错位（如抓到别的档位）时防污染。

    ⚠️ 并发写安全（2026-09-10 冲突审计修复）：本函数在 fetch 之后才被
    调用，中间隔了最长 ~20s 的 await——期间用户在 UI 改动配置会
    save_config（换新缓存对象+写盘）。所以这里**重新 load_config 取
    最新快照**，只改 pricing 字段；若用检查开始时的旧快照整份写回，
    会把用户那 20 秒内的所有改动回滚掉。同名条目若已与官网价一致
    （用户恰好手改过）则跳过。
    """
    cfg = load_config()
    pc_cfg = cfg.get("pricing_check") or {}
    max_ratio = float(pc_cfg.get("max_ratio_change", 1.0))
    pricing = cfg.get("pricing") or {}
    adopted: list[dict] = []
    for src_key, src in sources.items():
        for model, entry in (src.get("models") or {}).items():
            if entry.get("verdict") != "differs":
                continue
            if entry.get("promo"):
                continue
            existing = pricing.get(model)
            if existing is None:
                continue  # builtin-only：不新建条目
            fi, fo = entry.get("input"), entry.get("output")
            ei, eo = existing.get("input"), existing.get("output")
            if not all(isinstance(v, (int, float)) and v > 0 for v in (fi, fo, ei, eo)):
                continue
            # fetch 期间用户恰好手动改成了官网价 — 已一致，跳过
            if abs(fi - ei) < 0.005 and abs(fo - eo) < 0.005:
                continue
            if not (
                1 / (1 + max_ratio) <= fi / ei <= 1 + max_ratio
                and 1 / (1 + max_ratio) <= fo / eo <= 1 + max_ratio
            ):
                logger.warning(
                    "pricing-check auto-adopt: %s skipped — jump %.2f→%.2f"
                    " / %.2f→%.2f exceeds max_ratio_change",
                    model,
                    ei,
                    fi,
                    eo,
                    fo,
                )
                continue
            new_entry = dict(existing)
            new_entry["input"] = fi
            new_entry["output"] = fo
            pricing[model] = new_entry
            adopted.append(
                {
                    "model": model,
                    "source": src_key,
                    "input": [ei, fi],
                    "output": [eo, fo],
                }
            )
            logger.info(
                "pricing-check auto-adopt: %s %.4f→%.4f / %.4f→%.4f (%s)",
                model,
                ei,
                fi,
                eo,
                fo,
                src_key,
            )
    if adopted:
        save_config(cfg)  # pricing 是 cfg 的子引用，已就地更新
    return adopted


async def _run_pricing_check(auto: bool = False) -> dict:
    """抓取官网定价页并出 verdict；auto=True 时按护栏自动采纳。

    手动（POST）与每日后台循环共用，整体持锁串行化（手动与自动同时
    触发时排队，避免双份抓取与交叉写缓存）。结果写
    data/pricing_check.json；只有抓取成功才写缓存，失败不清旧结果。
    """
    async with _PRICING_CHECK_LOCK:
        cfg = load_config()
        pages = await _fetch_pricing_pages()
        sources = extract_all(pages)
        eff = effective_pricing_map(cfg)
        for src in sources.values():
            for model, entry in (src.get("models") or {}).items():
                e = eff.get(model)
                entry["verdict"] = _pricing_verdict(entry, e)
                entry["effective"] = e or None
        auto_adopted: list[dict] = []
        if auto and (cfg.get("pricing_check") or {}).get("auto_adopt"):
            # 采纳在内部重新 load_config（fetch 期间可能有并发改动）
            auto_adopted = _auto_adopt_from_check(sources)
        payload = {
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": sources,
            "auto_adopted": auto_adopted,
            "disclaimer": (
                "检查结果仅供人工核对；自动采纳仅在护栏内写入已有用户条目"
                "（CNY 单值、无促销、跳变≤阈值）；生效价 = config > 内置目录"
            ),
        }
        try:
            _PRICING_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PRICING_CHECK_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("pricing-check: cache write failed: %s", e)
        return payload


def _pricing_check_cache_age_s() -> float:
    """检查缓存年龄（秒）；无缓存/解析失败返回 inf（视为需要重抓）。"""
    try:
        d = json.loads(_PRICING_CHECK_FILE.read_text(encoding="utf-8"))
        ts = time.mktime(time.strptime(d.get("fetched_at") or "", "%Y-%m-%d %H:%M:%S"))
        return max(time.time() - ts, 0.0)
    except Exception:
        return float("inf")


async def _pricing_check_loop():
    """每日自动检查官网现价（config.pricing_check.auto，默认关）。

    启动后延迟 5 分钟避开启动高峰；每 interval_hours（默认 24h）跑
    一次 _run_pricing_check(auto=True)。auto_adopt 未开启时等价于
    只刷新提示缓存。缓存仍新鲜（< 周期 80%）时跳过——避免重启风暴
    （一天多次 restart 就多次抓）。单次失败只记日志，下周期重试。
    """
    await asyncio.sleep(300)
    while True:
        interval = 86400.0
        try:
            pc_cfg = load_config().get("pricing_check") or {}
            if pc_cfg.get("auto"):
                interval = float(pc_cfg.get("interval_hours", 24)) * 3600.0
                if _pricing_check_cache_age_s() < interval * 0.8:
                    logger.info("pricing-check loop: cache fresh, skip")
                else:
                    await _run_pricing_check(auto=True)
            else:
                interval = 3600.0  # 未开启 — 懒轮询配置
        except Exception as e:
            logger.warning("pricing-check loop: %s", e)
        await asyncio.sleep(max(interval, 600.0))


@app.get("/api/pricing/check")
async def api_pricing_check_get():
    """上次官网价格检查结果（磁盘缓存，无则空壳）。"""
    try:
        return json.loads(_PRICING_CHECK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"fetched_at": None, "sources": {}}


@app.post("/api/pricing/check")
async def api_pricing_check_post():
    """手动触发官网定价页抓取（提示通道；自动采纳只在每日循环里做）。"""
    return await _run_pricing_check(auto=False)


_PRICE_FIELDS = ("input", "output", "cache_input", "plan_monthly")


@app.post("/api/pricing/adopt")
async def api_pricing_adopt(request: Request):
    """显式采纳：{"model": m, "from": "fetched"|"builtin"}。

    - fetched：取最近一次检查缓存里该模型的官网价（仅 CNY 单一值可采纳）
      写入 config.pricing[m]，仍是用户显式动作（写入后同名整体赢）；
    - builtin：「跟随内置」——删除 config.pricing[m] 的价格字段（保留
      max_context 等非价字段），让 BUILTIN_PRICING 兜底，此后内置目录
      更新自动跟进。
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    model = (body or {}).get("model") or ""
    src = (body or {}).get("from") or ""
    if not model or src not in ("fetched", "builtin"):
        return JSONResponse(
            {"error": "body: {model, from: 'fetched'|'builtin'}"}, status_code=400
        )
    cfg = load_config()
    pricing = cfg.setdefault("pricing", {})
    if src == "builtin":
        entry = pricing.get(model)
        if not entry:
            return JSONResponse(
                {"error": f"{model} 没有用户配置条目，无需跟随内置"},
                status_code=404,
            )
        kept = {k: v for k, v in entry.items() if k not in _PRICE_FIELDS}
        if kept:
            pricing[model] = kept
        else:
            pricing.pop(model, None)
        save_config(cfg)
        return {
            "ok": True,
            "model": model,
            "from": "builtin",
            "effective": effective_pricing(cfg, model),
        }
    # from = fetched
    fetched = None
    try:
        cached = json.loads(_PRICING_CHECK_FILE.read_text(encoding="utf-8"))
        for s in (cached.get("sources") or {}).values():
            m = (s.get("models") or {}).get(model)
            if m:
                fetched = m
                break
    except Exception:
        fetched = None
    if not fetched:
        return JSONResponse(
            {"error": "最近检查结果里没有该模型，请先执行检查"}, status_code=404
        )
    fi, fo = fetched.get("input"), fetched.get("output")
    if not (
        isinstance(fi, (int, float))
        and isinstance(fo, (int, float))
        and fetched.get("currency", "CNY") == "CNY"
    ):
        return JSONResponse(
            {"error": "该模型官网价是区间/外币/非数值，不支持一键采纳，请手工核对"},
            status_code=422,
        )
    entry = dict(pricing.get(model) or {})
    entry["input"] = fi
    entry["output"] = fo
    pricing[model] = entry
    save_config(cfg)
    return {"ok": True, "model": model, "from": "fetched", "adopted": entry}


def _resolve_model_limit(model_id: str, cfg: dict) -> dict:
    """Resolve {context, output} token limits with per-field source tags.

    Chain (first hit wins per field): model_overrides.limit > model_meta
    (local authoritative schema) > BUILTIN_MODEL_META (shipped catalog,
    versioned with cyRouter) > pricing.max_context (legacy knob) >
    family heuristic. models.dev is gone — it barely covered the
    coding-API models and regressed them to heuristics (kimi-k3 was
    128k/8192 until explicitly set), the source of the output-truncation
    bug. Tagging the winning source keeps the dashboard honest: a value
    can always be traced and overridden.
    """
    pricing = cfg.get("pricing", {}) or {}
    overrides = cfg.get("model_overrides", {}) or {}
    meta = (cfg.get("model_meta", {}) or {}).get(model_id) or {}
    ovl = (overrides.get(model_id) or {}).get("limit") or {}
    price = pricing.get(model_id) or {}
    h = _infer_model_limit(model_id)
    # User entries replace the builtin entry WHOLESALE — only consult the
    # builtin catalog for models the user has NOT defined (see
    # builtin_meta.py for the semantics rationale).
    builtin = (
        {}
        if model_id in (cfg.get("model_meta") or {})
        else BUILTIN_MODEL_META.get(model_id, {})
    )

    def pick(field: str, layers) -> tuple[int, str]:
        for src, val in layers:
            if val is not None:
                return int(val), src
        return h[field], "heuristic"

    ctx, ctx_src = pick(
        "context",
        [
            ("override", ovl.get("context")),
            ("model_meta", meta.get("context")),
            ("builtin", builtin.get("context")),
            (
                "max_context",
                int(price["max_context"]) if price.get("max_context") else None,
            ),
        ],
    )
    out, out_src = pick(
        "output",
        [
            ("override", ovl.get("output")),
            ("model_meta", meta.get("max_output")),
            ("builtin", builtin.get("max_output")),
        ],
    )
    return {
        "context": ctx,
        "output": out,
        "context_source": ctx_src,
        "output_source": out_src,
        "layers": {
            "override": {
                "context": ovl.get("context"),
                "output": ovl.get("output"),
            },
            "model_meta": {
                "context": meta.get("context"),
                "output": meta.get("max_output"),
            },
            "builtin": {
                "context": builtin.get("context"),
                "output": builtin.get("max_output"),
            },
            "max_context": (
                int(price["max_context"]) if price.get("max_context") else None
            ),
            "heuristic": {"context": h["context"], "output": h["output"]},
        },
    }


def _infer_model_limit(model_id: str) -> dict:
    """Infer {context, output} token limits from model family.

    Fallback when neither `model_meta` nor a config `model_overrides`
    entry supplies a limit. Coding agents read
    `limit.output` to cap their own reply length, so the old blanket
    default of 8192 starved long multi-file answers.

    Values reflect documented model-family ceilings (verified 2026-07):
      * DeepSeek V4 (pro/flash): 1M context, 384K output
        - api-docs.deepseek.com/quick_start/pricing
      * GLM-4.6/5.2 (zhipu coding): 128K output - z.ai official docs
      * Qwen3 new gen (qwen3/3.5/3.6/3.7): 1M context, 65K output
        - qwencloud.com + openrouter.ai model pages
      * Qwen legacy (qwen-max/plus/turbo, no version): 8K output
        - dashscope returns `Range of max_tokens should be [1, 8192]`
      * Legacy VL (qwen-vl-max): 8K output - legacy VL tier
    Per-model overrides in config.json still win over this heuristic.
    """
    name = (model_id or "").lower()
    # Embedding models have no text-output limit concept.
    if "embedding" in name:
        return {"context": 8192, "output": 0}
    # DeepSeek V4 family: 1M context, 384K (393216) output ceiling.
    if "deepseek" in name:
        return {"context": 1048576, "output": 393216}
    # VL models: Qwen3-VL new gen 65K output, legacy VL 8K.
    if "-vl" in name or "vision" in name or "vl-" in name:
        if "qwen3" in name:
            return {"context": 1048576, "output": 65536}
        return {"context": 32768, "output": 8192}
    # GLM (zhipu coding): GLM-5.2 1M context (verified 2026-07-26 binary probe:
    # 1029020 ok / ~1050000 rejected code 1261 "Prompt exceeds max length"),
    # 128K (131072) output - z.ai official docs.
    if "glm" in name:
        return {"context": 1048576, "output": 131072}
    # Qwen3 new gen (3/3.5/3.6/3.7): 1M context, 65K output.
    if "qwen3" in name:
        return {"context": 1048576, "output": 65536}
    # Qwen legacy (qwen-max/plus/turbo/flash/coder, no version): 8K output.
    if "qwen" in name:
        return {"context": 131072, "output": 8192}
    # Kimi For Coding (k3 / k3-256k): 256K context, 32K output. The generic
    # fallback's 8192 output starved long answers — pi read max_output_tokens
    # as its max_tokens ceiling, k3 hit exactly 8192 and returned
    # finish_reason=length → "Response was truncated before completion".
    if "kimi" in name:
        return {"context": 262144, "output": 32768}
    # Conservative fallback for unknown families.
    return {"context": 131072, "output": 8192}


def _model_output_ceiling(model_id: str) -> int:
    """Resolve the true output-token ceiling for a model (0 = unknown).

    Priority: config model_overrides.<model>.limit.output >
    family heuristic (_infer_model_limit).
    """
    try:
        name = (model_id or "").lower()
        if "embedding" in name:
            return 0
        cfg = load_config()
        overrides = cfg.get("model_overrides", {}) or {}
        ov = overrides.get(model_id, {}) or {}
        if ov:
            lim = ov.get("limit") or {}
            if isinstance(lim, dict) and lim.get("output"):
                return int(lim["output"])
        return int(_infer_model_limit(model_id).get("output", 0) or 0)
    except Exception:
        return 0


def _elevate_max_tokens(model_id: str, current) -> int:
    """Return max_tokens to forward upstream.

    By default this is a NATIVE passthrough: returns the client-supplied
    value unchanged (or 4096 when omitted). Raising max_tokens to the model
    ceiling is OPT-IN via config `auto_elevate_max_tokens: true`.

    When enabled, thinking models (GLM-5.2, DeepSeek-v4) count their reasoning
    chain against the max_tokens budget, so a client's legacy 4096/8192 default
    is consumed by reasoning alone, yielding finish_reason=length with a
    truncated reply. max_tokens is a ceiling, not a target: raising it never
    makes the model babble past its natural stop -- it only prevents the
    reasoning chain from starving the answer.

    current: client-supplied max_tokens (int), or None/0 when omitted.
    Returns the value to forward upstream.
    """
    cur = current if isinstance(current, int) and current > 0 else 4096
    # Native by default — only elevate when explicitly opted in.
    try:
        cfg = load_config()
        if not cfg.get("auto_elevate_max_tokens", False):
            return cur
    except Exception:
        return cur
    ceiling = _model_output_ceiling(model_id)
    if ceiling and cur < ceiling:
        logger.info(
            "elevate_max_tokens: %s %d -> %d (thinking-model budget)",
            model_id,
            cur,
            ceiling,
        )
        return ceiling
    return cur


@app.get("/api.json")
async def opencode_catalog():
    """opencode-compatible catalog served locally (own format, no models.dev).

    Metadata (limit, capabilities, ...) comes from the local authoritative
    source: config `model_meta` (per-model context/max_output/vision/...),
    overlaid by `model_overrides`; limits resolve through the same unified
    chain as /api/models (override > model_meta > pricing.max_context >
    heuristic). models.dev was dropped — it barely covers coding-API models
    (kimi-k3 etc.) and required a proxy fetch at startup; this kept hanging
    or empty. Cost always comes from local config.json pricing (RMB/M
    tokens — actual billing, never USD).

    Set OPENCODE_MODELS_URL=http://localhost:20100 so opencode fetches
    `${URL}/api.json` from here.
    """
    cfg = load_config()
    # config pricing > builtin catalog — opencode cost display works on
    # fresh installs without cloning a config first.
    pricing = effective_pricing_map(cfg)
    model_meta = cfg.get("model_meta", {}) or {}

    models = {}
    # Optional per-model metadata overrides (correct stale capability data,
    # e.g. vision flags): "model_overrides": { "qwen-vl-max": {
    #   "attachment": true, "modalities": {...} } }
    model_overrides = cfg.get("model_overrides", {}) or {}

    for m, chain in _get_model_map(cfg).items():
        if "*" in m or "?" in m:
            continue
        # Source priority: config override > model_meta (local, authoritative)
        # > defaults. models.dev no longer participates.
        meta = dict(model_meta.get(m, {}))
        override = model_overrides.get(m)
        if override:
            meta.update(override)
        meta.setdefault("id", m)
        meta.setdefault("name", m)
        meta.setdefault("attachment", bool(meta.get("vision")) or "-vl" in m)
        meta.setdefault("reasoning", True)
        meta.setdefault("tool_call", True)
        meta.setdefault("temperature", True)
        # opencode caps its reply by limit.output — resolved via the unified
        # chain so it can never regress to the stale 8192 heuristic again.
        meta["limit"] = _resolve_model_limit(m, cfg)
        meta.setdefault(
            "modalities",
            {
                "input": ["text"] + (["image"] if meta["attachment"] else []),
                "output": ["text"],
            },
        )
        meta.setdefault("release_date", "2025-01")
        meta.setdefault("last_updated", "2025-01")
        meta.setdefault("knowledge", "2024-06")
        meta.setdefault("family", "unknown")
        meta.setdefault("open_weights", False)

        # Cost ALWAYS from local pricing (RMB/M tokens — actual billing).
        price = pricing.get(m, {})
        if price:
            cost = {}
            if "input" in price:
                cost["input"] = price["input"]
            if "output" in price:
                cost["output"] = price["output"]
            if "cache_input" in price:
                cost["cache_read"] = price["cache_input"]
            if cost:
                meta["cost"] = cost

        models[m] = meta

    return {
        "cyrouter": {
            "id": "cyrouter",
            "name": "CyRouter",
            "api": "http://localhost:20100/v1",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["CYROUTER_API_KEY"],
            "models": models,
        }
    }


@app.get("/api/models/remote")
async def api_remote_models(refresh: bool = False):
    """Fetch available models from each provider's API (max 10 most relevant per provider).

    Results are cached in-memory for 5 minutes. Pass ?refresh=true to bust cache.
    """
    import time as _time

    global _remote_models_cache, _remote_models_cache_ts

    TTL = 300  # 5 minutes
    now = _time.time()
    if (
        _remote_models_cache is not None
        and (now - _remote_models_cache_ts) < TTL
        and not refresh
    ):
        return _remote_models_cache

    try:
        from .providers.claude import ClaudeProvider
        from .providers.openai import OpenAIProvider
        from .router import _build_provider
    except ImportError:
        from providers.claude import ClaudeProvider
        from providers.openai import OpenAIProvider
        from router import _build_provider

    cfg = load_config()

    import re

    non_chat = {
        "tts",
        "asr",
        "image",
        "video",
        "embed",
        "rerank",
        "whisper",
        "ocr",
        "speech",
        "voice",
        "audio",
        "dance",
        "music",
        "draw",
        "detect",
        "segment",
        "classify",
        "translate-",
        "paraformer",
        "funasr",
        "cosyvoice",
        "sensevoice",
        "realtime",
        "online",
        "offline",
    }

    def _score(s: str) -> int:
        s = s.lower()
        known = {
            "qwen-max": 5000,
            "qwen-plus": 4000,
            "qwen-turbo": 3000,
            "qwen3.6-plus": 5500,
            "qwen3.6-flash": 4500,
            "qwen3.6-max": 5000,
            "qwen3.7-plus": 5500,
            "qwen3.7-max": 5000,
            "qwen3.7-max-preview": 5200,
            "qwen3.6-max-preview": 5000,
            "qwen3-max": 4500,
            "glm-5.1": 6000,
            "glm-5": 5000,
            "glm-5-turbo": 4000,
        }
        if s in known:
            return known[s]
        clean = re.sub(r"-\d+b(?:-a\d+b)?|-a\d+b", "", s)
        nums = re.findall(r"(?:^|[.-])(\d+(?:\.\d+)?)", clean)
        score = 0
        for n in nums:
            try:
                score = score * 100 + int(float(n) * 100)
            except (TypeError, ValueError):
                continue
        for keyword, bonus in [
            ("max", 10),
            ("pro", 8),
            ("plus", 5),
            ("flash", 3),
            ("turbo", 1),
        ]:
            if keyword in s:
                score += bonus
        return score

    def _filter_models(name: str, models: list[str]) -> list[str]:
        if not models:
            return []
        models = [m for m in models if not any(kw in m.lower() for kw in non_chat)]
        grouped: dict[str, list[str]] = {}
        for m in models:
            base = re.sub(r"-\d{4}(?:-\d{2}){0,2}$", "", m)
            if any(
                bad in base.lower()
                for bad in (
                    "-deprecated",
                    "-legacy",
                    "-old",
                    "v1",
                    "v2",
                    "v3.0",
                    "v3.1",
                    "qwen2",
                    "qwen1",
                )
            ):
                continue
            grouped.setdefault(base, []).append(m)
        ranked = sorted(grouped.keys(), key=_score, reverse=True)[:10]
        models = [min(grouped[m], key=len) for m in ranked]
        if name == "qwen":
            third_party = {
                "zhipu",
                "glm-",
                "deepseek",
                "kimi",
                "minimax",
                "siliconflow",
                "vanchin",
            }
            models = [
                m for m in models if not any(kw in m.lower() for kw in third_party)
            ]
        return models

    async def _fetch_one(p_cfg: dict) -> tuple[str, list[str]]:
        name = p_cfg["name"]
        try:
            provider = _build_provider(p_cfg)
            if isinstance(provider, ClaudeProvider):
                openai_url = p_cfg.get("base_urls", {}).get("openai")
                if openai_url:
                    tmp = OpenAIProvider(
                        name=name, api_key=provider.api_key, base_url=openai_url
                    )
                    return name, _filter_models(name, await tmp.list_models())
                return name, []
            return name, _filter_models(name, await provider.list_models())
        except Exception:
            return name, []

    import asyncio

    tasks = [_fetch_one(p_cfg) for p_cfg in cfg.get("providers", [])]
    results = await asyncio.gather(*tasks)

    _remote_models_cache = dict(results)
    _remote_models_cache_ts = now
    return _remote_models_cache


@app.get("/", response_class=HTMLResponse)
async def serve_gui():
    index = WEB_DIR / "index.html"
    if index.exists():
        return HTMLResponse(
            index.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return HTMLResponse(
        "<h1>cyRouter</h1><p>Web UI not found. Place index.html in web/</p>"
    )


# ── Entry point ────────────────────────────────────────────────────────


def _open_browser_later(url: str, delay: float = 1.5):
    """Open the dashboard URL in the default browser after a short delay.

    The delay lets uvicorn bind the port first so the browser doesn't see
    a connection-refused on rapid machines.
    """
    import threading
    import webbrowser

    def _go():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as e:
            logger.warning(f"Failed to open browser: {e}")

    threading.Thread(target=_go, name="cyRouter-browser", daemon=True).start()


def main():
    cfg = load_config()
    cfg_host = cfg.get("host", "127.0.0.1")
    try:
        cfg_port = int(float(cfg.get("port", DEFAULT_PORT)))
    except (TypeError, ValueError):
        cfg_port = DEFAULT_PORT

    parser = argparse.ArgumentParser(description="cyRouter - LLM API Router")
    parser.add_argument("--host", default=cfg_host)
    parser.add_argument("--port", type=int, default=cfg_port)
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Disable system tray icon (frozen builds only)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the dashboard browser",
    )
    args = parser.parse_args()

    from router import rebuild_today_stats

    rebuild_today_stats()

    # PyInstaller onefile with console=False leaves sys.stdout/stderr as None
    # (no console attached). uvicorn's DefaultFormatter and Python's logging
    # call sys.stdout.isatty() during setup, which crashes on None. Substitute
    # a quiet black-hole stream so the rest of the app runs normally; the user
    # interacts via the tray + browser, not stdout.
    if getattr(sys, "frozen", False):
        import io as _io

        class _NullStream(_io.StringIO):
            def isatty(self) -> bool:
                return False

            def fileno(self):
                raise OSError("no fileno")

        if sys.stdout is None:
            sys.stdout = _NullStream()
        if sys.stderr is None:
            sys.stderr = _NullStream()

    import uvicorn

    # When frozen (exe), launch the tray icon and auto-open the browser so
    # end-users get a GUI-like experience without a console window.
    frozen = getattr(sys, "frozen", False)
    display_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    dashboard_url = f"http://{display_host}:{args.port}"

    # Startup banner — printed here (not in _lifespan) because main() holds the
    # authoritative --port/--host values. uvicorn is started with the string
    # "main:app", so it re-imports the module under the name `main` (distinct
    # from `__main__`); module globals set above are invisible to that import.
    logger.info(
        "\n"
        "═══════════════════════════════════════════════════════════\n"
        f"  cyRouter running at {dashboard_url}\n"
        f"  Open {dashboard_url} for admin UI\n"
        "═══════════════════════════════════════════════════════════"
    )

    quit_event = None
    if frozen and not args.no_tray:
        try:
            from tray import start_tray

            quit_event = start_tray(url=dashboard_url)
        except Exception as e:
            logger.warning(f"Tray icon unavailable: {e}")

    if not args.no_browser:
        _open_browser_later(dashboard_url)

    # uvicorn.run is blocking. When a tray quit-event exists, run uvicorn in
    # a daemon thread and poll the event on the main thread so clicking
    # "Quit" in the tray exits cleanly instead of leaving a zombie server.
    if quit_event is not None:
        import threading

        server = uvicorn.Server(
            uvicorn.Config(
                "cyRouter.main:app" if __package__ else "main:app",
                host=args.host,
                port=args.port,
                log_level="info",
                access_log=True,
            )
        )

        def _serve():
            try:
                server.run()
            except Exception as e:
                logger.error(f"uvicorn crashed: {e}")
                quit_event.set()

        t = threading.Thread(target=_serve, name="cyRouter-uvicorn", daemon=True)
        t.start()

        # Block until tray asks to quit
        quit_event.wait()
        logger.info("Tray quit requested; shutting down server...")
        server.should_exit = True
        t.join(timeout=10)
    else:
        uvicorn.run(
            "cyRouter.main:app" if __package__ else "main:app",
            host=args.host,
            port=args.port,
            log_level="info",
            access_log=True,
        )


if __name__ == "__main__":
    main()
