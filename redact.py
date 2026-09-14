"""Secret redaction / reversible masking for request bodies.

Scans request-body text for likely API keys / tokens, replaces each with a
plausible-looking *placeholder key* before the body is forwarded to the
upstream provider, then restores the originals in the response. This prevents
secrets that a client embeds in a prompt / code / config snippet from leaking
to the model provider, while keeping the round-trip transparent to the client.

Strategy (aggressive mode, default):
  * Known credential prefixes (full token matched):
      sk-, sk-ant-, sk-or-, sk-proj-, AIza…, hf_…, ghp_/gho_/ghs_/ghr_/ghu_…,
      github_pat_…, xai-…, AKIA…, pplx-…
  * Generic high-entropy runs:  ``[A-Za-z0-9_]{28,128}`` whose Shannon
    entropy >= 3.0, after dropping shapes that are almost never secrets
    (pure hex of length >= 16 = MD5 / SHA / undashed UUID / hex ids, dashed
    UUIDs, pure digits, near-constant runs).  Hyphens are deliberately NOT
    in the generic class: hyphenated tokens are either known-prefix
    credentials (matched whole by _KNOWN_RES) or structured identifiers
    (dashed UUIDs, dates, composite ids) — with '-' in the class every
    episode/primary-key id in agent tool output was masked as a "secret"
    (observed in the wild 2026-09-07: a whole debugging session was misled
    into believing the machine's stdlib uuid.py was poisoned).

Masking design (middle-mask):
  Each detected secret is replaced by a *surrogate* that keeps the real key's
  provider structural prefix (+ a 2-char sample) and its last 4 chars verbatim,
  replacing only the high-entropy middle with a random run drawn from the same
  alphabet real keys use (``[A-Za-z0-9]``). The surrogate is the same length as
  the original and reads as a plausible credential, so the model can still tell
  the key's shape (provider / version segment / tail fingerprint) while the
  secret's high-entropy core never leaves this process. Restore is an exact
byte-for-byte replacement keyed on the surrogate. Idempotency relies on the
per-request ``mapping`` (a candidate already equal to a generated surrogate
is skipped).

All public entry points are synchronous and allocation-light; they operate on
plain JSON-derived structures (dict / list / str / scalar).
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import math
import re
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger("cyRouter.redact")

# ── Surrogate (masked token) ─────────────────────────────────────────
# A detected secret is replaced by a same-length surrogate that keeps the
# provider structural prefix (+ a short sample) and the last few chars,
# masking only the high-entropy middle with random [A-Za-z0-9].

# Provider structural prefixes, longest first so the most specific wins.
_STRUCTURAL_PREFIXES: tuple[str, ...] = (
    "sk-ant-api03-",
    "sk-ant-",
    "sk-or-",
    "sk-proj-",
    "sk-",
    "AIza",
    "hf_",
    "ghp_",
    "gho_",
    "ghs_",
    "ghr_",
    "ghu_",
    "github_pat_",
    "xai-",
    "AKIA",
    "LTAI",
    "AKID",
    "AKLT",
    "pplx-",
    "cr_",
)
# Real-key chars kept after the structural prefix (a sample so the model sees
# the key's actual opening) and at the very tail (a fingerprint).
_PREFIX_SAMPLE = 2
_TAIL_KEEP = 4
# Minimum length of the random middle; shorter secrets fall back to head-only.
_MIN_MASK_LEN = 6
_MASK_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _structural_prefix_len(secret: str) -> int:
    """Number of leading chars of ``secret`` to keep verbatim in the surrogate."""
    n = len(secret)
    for p in _STRUCTURAL_PREFIXES:
        if secret.startswith(p):
            return min(len(p) + _PREFIX_SAMPLE, max(0, n - _TAIL_KEEP))
    # Generic high-entropy secret with no known prefix: keep a short head.
    return min(_PREFIX_SAMPLE + 4, max(0, n - _TAIL_KEEP))


# Deterministic masking seed. The SAME real secret always maps to the SAME
# surrogate for an entire calendar day, even across restarts — so a secret
# embedded in a multi-turn agent conversation looks visually consistent to
# the model on every turn (and survives a cyRouter restart mid-conversation).
# The seed rotates daily so a surrogate that leaked (e.g. into a log) is only
# valid for one day.  Determinism replaces the old per-process random cache.
_SEED_CACHE: tuple[str, bytes] | None = None


def _daily_seed() -> bytes:
    """Return today's deterministic masking seed (ISO date bytes, cached)."""
    global _SEED_CACHE
    iso = datetime.date.today().isoformat()
    if _SEED_CACHE is None or _SEED_CACHE[0] != iso:
        _SEED_CACHE = (iso, iso.encode("ascii"))
    return _SEED_CACHE[1]


def _deterministic_middle(secret: str, mid_len: int) -> str:
    """Derive the masked middle deterministically from (secret, daily seed).

    Pure function: identical inputs always yield an identical output, with no
    randomness and no global mutable state (beyond the cached date). Uses
    SHA-256 in counter mode to expand enough keystream, mapping each byte into
    the mask alphabet. A typical key needs a single hash; even a 128-char run
    needs at most two.
    """
    secret_b = secret.encode("utf-8")
    seed = _daily_seed()
    out: list[str] = []
    counter = 0
    while len(out) < mid_len:
        digest = hashlib.sha256(
            seed + b"|redact|" + secret_b + counter.to_bytes(4, "big")
        ).digest()
        for byte in digest:
            if len(out) < mid_len:
                out.append(_MASK_ALPHABET[byte % len(_MASK_ALPHABET)])
        counter += 1
    return "".join(out)


def _new_masked(secret: str, existing_values) -> str:
    """Build a same-length, legit-looking surrogate with a deterministic middle.

    The middle is derived from (daily seed, secret) — never random — so a
    given secret maps to one fixed surrogate for the whole day across all
    requests and restarts. The surrogate keeps the real key's structural
    prefix (+ a short sample) and its last few chars, masking only the
    high-entropy middle. ``existing_values`` guards against an in-request
    collision (astronomically unlikely with a deterministic derivation, but
    kept for safety by salting the derived middle).
    """
    n = len(secret)
    head = _structural_prefix_len(secret)
    tail = _TAIL_KEEP if n - head >= _MIN_MASK_LEN + _TAIL_KEEP else 0
    mid_len = n - head - tail
    if mid_len < _MIN_MASK_LEN:
        # Secret too short to keep a tail and still mask — keep head only.
        tail = 0
        head = max(0, n - _MIN_MASK_LEN)
        mid_len = n - head
    keep_head = secret[:head]
    keep_tail = secret[n - tail :] if tail else ""
    salt = 0
    while True:
        basis = secret if salt == 0 else secret + "\x00" + str(salt)
        masked = keep_head + _deterministic_middle(basis, mid_len) + keep_tail
        if masked != secret and masked not in existing_values:
            return masked
        salt += 1
        if salt > 64:  # hard guard against a pathological loop
            return masked


# ── Detection patterns ────────────────────────────────────────────────
# Order within _KNOWN_RES does not matter — overlaps are resolved by the
# "longest span wins" rule in _scan().  Each pattern matches the FULL token
# (prefix + body) so the body isn't re-matched by the generic entropy rule.
_KNOWN_RES: list[re.Pattern] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),  # OpenAI classic
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key (~39 chars total)
    re.compile(r"hf_[A-Za-z0-9]{20,}"),  # HuggingFace
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT (classic)
    re.compile(r"gho_[A-Za-z0-9]{36}"),  # GitHub OAuth
    re.compile(r"ghs_[A-Za-z0-9]{36}"),  # GitHub server-to-server
    re.compile(r"ghr_[A-Za-z0-9]{36}"),  # GitHub refresh
    re.compile(r"ghu_[A-Za-z0-9]{36}"),  # GitHub user-to-server
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),  # GitHub fine-grained PAT
    re.compile(r"xai-[A-Za-z0-9]{20,}"),  # xAI / Grok
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"LTAI[A-Za-z0-9]{12,40}"),  # Alibaba Cloud AccessKey ID
    re.compile(r"AKID[A-Za-z0-9]{13,40}"),  # Tencent Cloud SecretId
    re.compile(r"AKLT[A-Za-z0-9]{13,40}"),  # Volcano Engine access key
    re.compile(r"pplx-[A-Za-z0-9]{20,}"),  # Perplexity
    re.compile(r"cr_[A-Za-z0-9]{20,}"),  # generic "cr_" credential
]

# Generic high-entropy token.  Lower bound 28 covers Alibaba Cloud
# AccessKey Secret (30 chars); the benign-filter still rejects pure-hex
# digests / pure digits / low-entropy runs, so the shorter floor rarely
# false-positives.  Upper bound avoids swallowing huge base64 blobs (which
# are data, not credentials) in one bite.
#
# NO HYPHEN in the class (2026-09-07): with '-' included a dashed UUID
# (36 chars, 8-4-4-4-12) is one contiguous run, fails the pure-hex benign
# exemption (dashes), and — unless its letter subsequence happens to look
# English — gets masked as a "secret", corrupting every episode/primary-key
# id in agent tool output.  The per-uuid outcome looked random (an
# English-likeness coin flip per token), which made it nearly undiagnosable.
# Hyphenated credentials are matched whole by _KNOWN_RES prefixes instead.
# Accepted cost: url-safe-base64 secrets without a known prefix are no
# longer caught here — structural identifiers vastly outnumber that class.
_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9_]{28,128}")

_MIN_ENTROPY = 3.0  # bits/char; real keys usually 4.0+, English prose ~3.0

# English morphemes common in long code identifiers but vanishingly rare in
# random secrets. Used by _is_likely_benign to spare PascalCase/camelCase
# symbols from the generic entropy detector.
_BENIGN_MORPHEMES: tuple[str, ...] = (
    "tion",
    "ing",
    "ment",
    "work",
    "oint",
    "enter",
    "anim",
    "ener",
)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


# ── English-likeness (char-bigram Markov model) ─────────────────────
# Distinguishes English-like code identifiers from random secrets far more
# robustly than vowel-ratio heuristics: identifiers follow English character
# transition statistics (sp-ri-ng, ve-ct-or), random base62/base64 secrets
# don't (qj, zx, k7-style transitions). Same family as rrenaud's
# Gibberish-Detector / DGA-domain classifiers. Model file trained on
# Norvig's big.txt (see AGENTS.md); 8KB JSON, lazy-loaded once.
_BIGRAM_MODEL_PATH = Path(__file__).with_name("redact_bigrams.json")
_bigram_model: dict | None = None
_bigram_model_loaded = False

# Calibrated offline against identifier vs synthetic-secret corpora with
# MEDIAN scoring (identifiers ≥-2.59, secrets ≤-2.80 — see AGENTS.md).
# Median resists outlier bigrams: a single rare pair (qq in QQml, xcb_) must
# not drag an otherwise-English identifier below the threshold, while random
# secrets have mostly-rare transitions and stay low. Missing/unscorable →
# returns a very low score so the benign check stays silent (safe side:
# more redaction, never less).
_ENGLISH_LIKENESS_THRESHOLD = -2.7


def _load_bigram_model() -> dict | None:
    global _bigram_model, _bigram_model_loaded
    if _bigram_model_loaded:
        return _bigram_model
    _bigram_model_loaded = True
    try:
        with open(_BIGRAM_MODEL_PATH, encoding="utf-8") as f:
            _bigram_model = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "redact: bigram model %s unavailable — english-likeness check disabled",
            _BIGRAM_MODEL_PATH,
        )
        _bigram_model = None
    return _bigram_model


def _english_likeness(token: str) -> float:
    """Median per-transition log10 probability of the token's a-z letters
    under the English+pinyin bigram model. Higher = more language-like.
    Returns -10 (never benign) when the model is unavailable or the token
    has too few letters to score reliably."""
    model = _load_bigram_model()
    if not model:
        return -10.0
    w = re.sub(r"[^a-z]", "", token.lower())
    if len(w) < 8:
        return -10.0
    logp = model["logprob"]
    floor = model["floor"]
    transitions = [logp.get(w[i : i + 2], floor) for i in range(len(w) - 1)]
    return statistics.median(transitions)


def _is_likely_benign(token: str) -> bool:
    """Heuristic reject for high-entropy matches that are probably not secrets.

    Only applied to *generic* entropy hits — anything matched by a known
    credential prefix is always redacted regardless of shape.
    """
    # Dashed UUID (8-4-4-4-12), either case: agent tool output is full of
    # episode ids / primary keys / request ids in this exact shape.  Kept
    # even though the generic RE no longer matches hyphens — belt-and-
    # suspenders against the class ever being widened again.
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        token,
        re.IGNORECASE,
    ):
        return True
    # Structured tool-call ids (OpenAI call_…, Anthropic toolu_…, chatcmpl-…):
    # agent traffic is full of them (every tool_use round-trip carries one)
    # and they are never credentials. Surfaced by the entropy audit log on
    # day one of the observability layer (2026-09-07).
    if re.fullmatch(r"(call|toolu|msg|fc)_[A-Za-z0-9]+", token):
        return True
    if re.fullmatch(r"chatcmpl-[A-Za-z0-9]+", token):
        return True
    # Pure hex digests, any length >= 16: MD5 (32), git SHA-1 (40),
    # SHA-256 (64), undashed UUIDs (32/36), hex-encoded ids.  Real secrets
    # are base62/base64 to pack entropy — pure hex is data, not credentials.
    if re.fullmatch(r"[0-9a-f]{16,128}", token, re.IGNORECASE):
        return True
    # Pure digits (account/order ids, timestamps).
    if token.isdigit():
        return True
    # Near-constant runs (e.g. "aaaaaaaa...", "0123456789" repeats).
    if len(set(token)) <= 3:
        return True
    # English-like code identifiers (PascalCase/camelCase symbols, Unreal-style
    # signal names). Without a benign path here the generic detector watermarks
    # code symbols in the model's view — observed in the wild: 28+ char
    # identifiers (signal/class names, pi-lens rule ids) were swapped for
    # surrogates and the model concluded the files on disk were corrupted.
    # Fast path: common English morphemes (random-secret collision ≈0.03-0.1%).
    lowered = token.lower()
    if any(m in lowered for m in _BENIGN_MORPHEMES):
        return True
    # Primary: char-bigram Markov English-likeness. Robust to consonant-heavy
    # symbols (GtkWidgetPath, strcmp) where vowel-ratio heuristics fail.
    if _english_likeness(token) > _ENGLISH_LIKENESS_THRESHOLD:
        return True
    # Low entropy → likely structured/sequential, not a random secret.
    if _shannon_entropy(token) < _MIN_ENTROPY:
        return True
    return False


def build_patterns(
    custom_patterns: Iterable[str] | None = None,
) -> list[re.Pattern]:
    """Compile user-supplied extra regexes; invalid ones are skipped + logged."""
    pats: list[re.Pattern] = []
    if custom_patterns:
        for src in custom_patterns:
            if not isinstance(src, str) or not src.strip():
                continue
            try:
                pats.append(re.compile(src))
            except re.error as e:
                logger.warning(
                    "secret_redaction: skipping invalid regex %r: %s", src, e
                )
    return pats


def _scan(
    text: str,
    custom_res: list[re.Pattern],
) -> list[tuple[int, int, str, str]]:
    """Find all secret-candidate spans, resolving overlaps (longest wins).

    Returns ``(start, end, matched_text, source)`` with source in
    ``{"known","entropy","custom"}``.
    """
    spans: list[tuple[int, int, str, str]] = []
    for rx in _KNOWN_RES:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), m.group(), "known"))
    for m in _HIGH_ENTROPY_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(), "entropy"))
    for rx in custom_res:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), m.group(), "custom"))

    if not spans:
        return []
    # Longest span first, then earliest start — so a known-prefix full-token
    # match dominates the generic entropy match that overlaps its tail.
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    chosen: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for st, en, tok, src in spans:
        if any(not (en <= a or st >= b) for a, b in occupied):
            continue
        chosen.append((st, en, tok, src))
        occupied.append((st, en))
    return chosen


def _redact_text(
    text: str,
    custom_res: list[re.Pattern],
    mapping: dict[str, str],
    exclude_substrings: Iterable[str] | None,
) -> str:
    """Replace every detected secret in ``text`` with its placeholder."""
    chosen = _scan(text, custom_res)
    if not chosen:
        return text
    exclude = tuple(s for s in (exclude_substrings or []) if s)
    new_tokens: list[str] = []
    for _st, _en, tok, src in chosen:
        # Never re-redact our own surrogates (idempotency): a candidate
        # already produced this request is skipped.
        if tok in mapping.values():
            continue
        # Generic entropy hits must pass the benign filter; known/custom hits
        # are redacted unconditionally (an explicit prefix is a strong signal).
        if src == "entropy" and _is_likely_benign(tok):
            continue
        if exclude and any(ex in tok for ex in exclude):
            continue
        if tok not in mapping:
            mapping[tok] = _new_masked(tok, mapping.values())
            # Audit trail: entropy-class hits are the false-positive-prone
            # ones (identifiers mistaken for secrets) — warn so a polluted
            # agent session is visible in logs within seconds.  Known/custom
            # prefixes are expected behavior, info suffices.  Only the 6-char
            # head is logged — the same head the surrogate itself exposes to
            # the provider, so no new disclosure.
            if src == "entropy":
                logger.warning(
                    "redact: entropy-class mask len=%d head=%s "
                    "(possible identifier false-positive)",
                    len(tok),
                    tok[:6],
                )
            else:
                logger.info(
                    "redact: %s-class mask len=%d head=%s",
                    src,
                    len(tok),
                    tok[:6],
                )
        new_tokens.append(tok)
    if not new_tokens:
        return text
    # Replace longest originals first so a shorter token can't be a substring
    # of a longer one and corrupt its placeholder.
    for tok in sorted(new_tokens, key=len, reverse=True):
        text = text.replace(tok, mapping[tok])
    return text


# Base64 data URLs (e.g. data:image/png;base64,iVBORw0KGgo...) carry inline
# binary payloads. Their high-entropy body triggers _HIGH_ENTROPY_RE, so
# _redact_text would shatter the blob into N placeholders and corrupt the
# image. Skip redaction for any string that embeds a base64 data URL.
_BASE64_DATA_URL_RE = re.compile(r"data:[^;]+;base64,", re.IGNORECASE)


def _looks_like_base64_payload(s: str) -> bool:
    """True if the string contains a base64 data URL (multimodal image/audio/etc).

    Such payloads must never be redacted — the high-entropy base64 body would
    match the generic secret detector and be destroyed. A real API key is a
    short opaque token, never an embedded data URL.
    """
    return bool(_BASE64_DATA_URL_RE.search(s))


def _is_anthropic_image_source(parent: dict) -> bool:
    """True if the dict is an Anthropic multimodal source object:

        {"type": "base64", "media_type": "image/png", "data": "<bare base64>"}

    Anthropic wire format carries the image as a BARE base64 ``data`` field
    (no data: URL prefix), so ``_looks_like_base64_payload`` does not cover
    it and the generic high-entropy detector used to shatter the payload —
    the upstream then failed to sniff the (corrupted) magic bytes and
    rejected the request with "unsupported image format:
    application/octet-stream" (2026-09-08 incident, kimi coding endpoint).
    A real secret never appears inside a base64+media_type source object,
    so the structural skip is safe.
    """
    return parent.get("type") == "base64" and isinstance(parent.get("media_type"), str)


def _walk(
    obj,
    custom_res: list[re.Pattern],
    mapping: dict[str, str],
    exclude_substrings: Iterable[str] | None,
) -> None:
    """Recursively redact every string value inside a JSON-derived structure.

    Skips strings carrying base64 data URLs — and the bare ``data`` field of
    Anthropic image source blocks — to avoid corrupting multimodal image/
    audio payloads (the base64 body is high-entropy and would otherwise be
    shattered into redaction placeholders).
    """
    if isinstance(obj, dict):
        skip_bare_data = _is_anthropic_image_source(obj)
        for k, v in obj.items():
            if isinstance(v, str):
                if skip_bare_data and k == "data":
                    continue
                if not _looks_like_base64_payload(v):
                    obj[k] = _redact_text(v, custom_res, mapping, exclude_substrings)
            else:
                _walk(v, custom_res, mapping, exclude_substrings)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                if not _looks_like_base64_payload(v):
                    obj[i] = _redact_text(v, custom_res, mapping, exclude_substrings)
            else:
                _walk(v, custom_res, mapping, exclude_substrings)
    # scalars (int/float/bool/None) are left untouched


def redact_body(
    body: dict,
    cfg: dict | None = None,
    mapping: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str]]:
    """Return ``(redacted_deep_copy, mapping)``.

    ``mapping`` maps ``original_secret -> placeholder`` and is also what
    :func:`restore_text` / :func:`restore_bytes` consume. The caller's
    ``body`` is never mutated, so it can still serve as a cache key.
    """
    rc = cfg or {}
    custom_res = build_patterns(rc.get("custom_patterns"))
    exclude_substrings = rc.get("exclude_substrings")
    mapping = {} if mapping is None else mapping
    redacted = copy.deepcopy(body)
    _walk(redacted, custom_res, mapping, exclude_substrings)
    return redacted, mapping


def _truncation_suffix_after(text: str, start: int, prefix_len: int) -> bool:
    """True if ``text[start + prefix_len:]`` begins a truncation marker.

    AI models habitually abbreviate long secrets as ``sk-xxxx...`` (ellipsis),
    ``sk-xxxx (省略)``, or just cut the line.  Exact-match restore cannot
    recover those — the placeholder is never fully present.  This predicate
    detects the *leading run* of a placeholder followed by a truncation
    boundary so restore can substitute the full original secret.
    """
    after = text[start + prefix_len :]
    if not after:
        return True
    if after.startswith("...") or after.startswith("…"):
        return True
    # A placeholder is all [A-Za-z0-9_-]; anything else right after the run
    # means the run is a prefix, not a full match.  ASCII letters/digits,
    # '_' and '-' continue the token, so they are NOT a truncation boundary.
    return not (after[0].isalnum() or after[0] in "_-")


def restore_text(text: str, mapping: dict[str, str]) -> str:
    """Replace placeholders back to originals in a string.

    In addition to exact matches, recovers *truncated* placeholder mentions:
    AI models often abbreviate long secrets (``sk-xxxx...``), so a leading
    run of a placeholder followed by a truncation boundary (ellipsis,
    punctuation, end-of-line) is replaced with the full original secret.
    """
    if not mapping:
        return text
    # Longest placeholder first (all share the prefix + 8 hex tail, so ties are
    # impossible, but the ordering keeps the invariant explicit).
    items = sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True)
    for _original, placeholder in items:
        if placeholder in text:
            text = text.replace(placeholder, _original)
    # Truncated-prefix recovery: a leading run of a placeholder followed by a
    # truncation boundary is replaced by the full original.  Minimum run
    # length avoids false positives on short coincidental runs ("sk-",
    # "sk-ab").  Order longest-first so a longer placeholder's prefix wins
    # over a shorter one's full run.
    min_run = 12
    for _original, placeholder in sorted(
        mapping.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        # Only attempt recovery when the placeholder is long enough that a
        # truncated mention is plausible and the run is distinctive.
        if len(placeholder) < min_run + 1:
            continue
        head = placeholder[:min_run]
        idx = text.find(head)
        while idx != -1:
            # Extend the matched run over every character that continues the
            # placeholder (alnum / _ / -), so the boundary check sees the
            # FULL leading run, not a fixed-length slice.
            end = idx + len(head)
            while (
                end < len(text)
                and (text[end].isalnum() or text[end] in "_-")
                and end < idx + len(placeholder)
            ):
                end += 1
            if _truncation_suffix_after(text, idx, end - idx):
                text = text[:idx] + _original + text[end:]
                idx = text.find(head, idx + len(_original))
            else:
                idx = text.find(head, idx + 1)
    return text


def restore_bytes(data: bytes, mapping: dict[str, str]) -> bytes:
    """Replace placeholders back to originals in a byte string (SSE stream)."""
    if not mapping:
        return data
    items = sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True)
    for original, placeholder in items:
        ph = placeholder.encode()
        if ph in data:
            data = data.replace(ph, original.encode())
    return data


def restore_obj(obj: Any, mapping: dict[str, str]) -> Any:
    """Recursively restore placeholders inside a JSON-derived response object.

    Used for non-streaming responses where we have the parsed ``result`` dict:
    avoids a full re-serialise / re-parse round-trip. Scalars pass through
    unchanged; dict / list are rebuilt shallowly per level.
    """
    if not mapping:
        return obj
    if isinstance(obj, str):
        return restore_text(obj, mapping)
    if isinstance(obj, dict):
        return {k: restore_obj(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [restore_obj(v, mapping) for v in obj]
    return obj


class StreamingRestorer:
    """Stateful placeholder restorer for raw byte streams.

    HTTP streaming chunks are split on arbitrary byte boundaries, so a single
    surrogate can straddle two chunks. This restorer holds back any trailing
    run that could be the *start* of a surrogate, releasing it once the next
    chunk either completes the surrogate (→ restored) or diverges from it
    (→ emitted verbatim).

    Use for the byte-level passthrough path (``_raw_anthropic_sse_stream``).
    Line/JSON-reassembling paths don't need it — their yield units already
    contain whole placeholders.
    """

    def __init__(self, mapping: dict[str, str] | None):
        self.mapping = dict(mapping) if mapping else {}
        self.pending = b""
        # Pre-encode every surrogate value once; feed() checks whether the
        # buffer tail is a prefix of any surrogate so a chunk boundary landing
        # inside it is held back and restored once it completes.
        self._ph_bytes = [ph.encode("ascii") for ph in self.mapping.values()]

    def feed(self, data: bytes) -> bytes:
        if not self._ph_bytes:
            return data
        buf = self.pending + data
        buf = restore_bytes(buf, self.mapping)
        # Hold back the longest trailing run that matches a prefix of any
        # placeholder — it may be the leading bytes of a placeholder whose
        # tail arrives in the next chunk.
        hold = 0
        for phb in self._ph_bytes:
            maxhold = min(len(phb), len(buf))
            for i in range(maxhold, hold, -1):  # only care about beating hold
                if buf[-i:] == phb[:i]:
                    hold = i
                    break
        if hold:
            out = buf[:-hold]
            self.pending = buf[-hold:]
        else:
            out = buf
            self.pending = b""
        return out

    def flush(self) -> bytes:
        """Emit any still-held bytes (call once when the stream ends normally)."""
        out = self.pending
        self.pending = b""
        return out


class ContentRestorer:
    """Restore placeholders → originals in streamed CONTENT TEXT.

    A surrogate may be split across many content deltas by the upstream LLM
    tokenizer. Byte/line-level restore (``StreamingRestorer``) cannot
    reassemble it because each delta is wrapped in its own SSE/JSON event —
    the structural bytes between deltas interrupt the placeholder's byte
    continuity, so neither a full-match ``replace`` nor a trailing-prefix
    hold-back ever sees the complete surrogate.

    This class operates on the *clean text* already extracted from those
    events: it concatenates deltas into a contiguous buffer, restores any
    fully-arrived surrogate, and holds back a trailing run that could be the
    start of a not-yet-complete surrogate so it can be restored once the next
    delta(s) complete it.

    Surrogates are same-length as their originals and use only ``[A-Za-z0-9]``,
    so the hold-back window is bounded by the longest surrogate and never
    swallows arbitrary prose.
    """

    def __init__(self, mapping: dict[str, str] | None):
        # mapping is {original: surrogate}; invert for restore lookups.
        inv = {sur: orig for orig, sur in (mapping or {}).items()}
        # Longest first so a longer surrogate is tried before its own prefix.
        self._surrogates = sorted(inv, key=len, reverse=True)
        self._orig: dict[str, str] = inv
        self._buf = ""

    def feed(self, text: str) -> str:
        """Consume one content delta; return the text safe to emit now."""
        if not self._surrogates or not text:
            return text
        self._buf += text
        # Restore any fully-arrived surrogate (longest first).
        for sur in self._surrogates:
            if sur in self._buf:
                self._buf = self._buf.replace(sur, self._orig[sur])
        # Truncated-prefix recovery: a leading run of a surrogate followed by
        # a truncation boundary (ellipsis / punctuation / end) is replaced
        # with the full original.  Minimum run guards against short
        # coincidental runs ("sk-").
        min_run = 12
        for sur in sorted(self._surrogates, key=len, reverse=True):
            if len(sur) < min_run + 1:
                continue
            head = sur[:min_run]
            idx = self._buf.find(head)
            while idx != -1:
                # Extend the matched run over every char that continues the
                # surrogate, so the boundary check sees the full leading run.
                end = idx + min_run
                while (
                    end < len(self._buf)
                    and end < idx + len(sur)
                    and (self._buf[end].isalnum() or self._buf[end] in "_-")
                ):
                    end += 1
                after = self._buf[end:]
                # Only restore on an EXPLICIT truncation boundary (ellipsis /
                # non-token punctuation). A bare end-of-buffer is NOT a
                # truncation here — the surrogate may simply be split across
                # deltas and more content is coming; hold-back + flush handle
                # the true stream-end case.
                if (
                    after.startswith("...")
                    or after.startswith("…")
                    or (after and not after[0].isalnum() and after[0] not in "_-")
                ):
                    self._buf = self._buf[:idx] + self._orig[sur] + self._buf[end:]
                    idx = self._buf.find(head, idx + len(self._orig[sur]))
                elif after and (after[0].isalnum() or after[0] in "_-"):
                    idx = self._buf.find(head, idx + 1)
                else:
                    # Partial surrogate run at the very end of the buffer:
                    # treat as NOT yet truncation — wait for the next delta.
                    idx = self._buf.find(head, idx + 1)
        # Hold back the longest trailing run that is a prefix of some
        # surrogate, so a surrogate split across deltas completes next feed.
        hold = 0
        for sur in self._surrogates:
            window = min(len(sur), len(self._buf))
            for i in range(window, hold, -1):
                if self._buf[-i:] == sur[:i]:
                    hold = i
                    break
        if hold:
            out, self._buf = self._buf[:-hold], self._buf[-hold:]
        else:
            out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        """Release any still-held text (call once when the stream ends)."""
        out = self._buf
        self._buf = ""
        if not self._surrogates:
            return out
        # Stream ended with a truncated surrogate mention still held back
        # (e.g. the model wrote "sk-xxxx..." and the ellipsis was the last
        # delta).  Restore the leading run to the full original.
        min_run = 12
        for sur in sorted(self._surrogates, key=len, reverse=True):
            if len(sur) < min_run + 1 or not out:
                continue
            head = sur[:min_run]
            idx = out.find(head)
            while idx != -1:
                end = idx + min_run
                while (
                    end < len(out)
                    and end < idx + len(sur)
                    and (out[end].isalnum() or out[end] in "_-")
                ):
                    end += 1
                after = out[end:]
                if (
                    after.startswith("...")
                    or after.startswith("…")
                    or not after
                    or (after and not after[0].isalnum() and after[0] not in "_-")
                ):
                    out = out[:idx] + self._orig[sur] + out[end:]
                    idx = out.find(head, idx + len(self._orig[sur]))
                else:
                    idx = out.find(head, idx + 1)
        return out


def redact_enabled(cfg: dict | None) -> bool:
    """True when redaction is switched on (defaults to ON for new installs)."""
    rc = (cfg or {}).get("secret_redaction")
    if not isinstance(rc, dict):
        return True  # default-on
    return bool(rc.get("enabled", True))
