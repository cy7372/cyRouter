from __future__ import annotations

import logging
import time

logger = logging.getLogger("cyRouter.breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-(provider, model) circuit breaker.

    Three states:
      CLOSED   — normal, requests pass through
      OPEN     — tripped, requests fail fast (0 token burn)
      HALF_OPEN — cooldown over, probing with limited requests

    Escalating cooldown (Envoy outlier-detection style): each consecutive
    trip lengthens the cooldown — linear ``base × trips`` (Envoy's
    base_ejection_time × consecutive ejections) or ``exponential``
    ``base × 2^(trips-1)`` — capped at ``cooldown_max_seconds``. A flapping
    upstream gets progressively longer timeouts instead of the same fixed
    window every cycle. Sustained health decays the trip counter one level
    per ``decay_seconds`` of clean CLOSED operation (Envoy decays the
    multiplier per healthy interval).

    Probe semantics:
    - HALF_OPEN (user-traffic) success closes the circuit but KEEPS the
      trip counter, so a recover-then-refail upstream still escalates.
    - A successful ACTIVE probe (background health check) fully resets the
      circuit via ``reset()`` — Envoy's "successful active health check
      unejects the host and clears all counters".

    State machines are indexed by ``"provider|model"``.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        half_open_max: int = 1,
        cooldown_max_seconds: float = 1800.0,
        cooldown_backoff: str = "linear",
        decay_seconds: float = 600.0,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max = half_open_max
        self.cooldown_max_seconds = cooldown_max_seconds
        self.cooldown_backoff = cooldown_backoff
        self.decay_seconds = decay_seconds
        self._states: dict[str, dict] = {}

    def _key(self, provider: str, model: str) -> str:
        return f"{provider}|{model}"

    def _cooldown_for(self, s: dict) -> float:
        """Effective cooldown for a state, escalated by consecutive trips."""
        n = max(1, s.get("trips", 1))
        if self.cooldown_backoff == "exponential":
            cd = self.cooldown_seconds * (2 ** (n - 1))
        else:  # linear — Envoy: base_ejection_time × consecutive ejections
            cd = self.cooldown_seconds * n
        return min(cd, self.cooldown_max_seconds)

    def _trip(self, k: str, s: dict, now: float):
        s["state"] = OPEN
        s["tripped_at"] = now
        s["trips"] = s.get("trips", 0) + 1
        s["decay_anchor"] = now
        s["failures"] = 0
        logger.warning(
            "[breaker] %s → OPEN (trip #%d, cooldown %.0fs)",
            k,
            s["trips"],
            self._cooldown_for(s),
        )

    def allow(self, provider: str, model: str) -> bool:
        """Return ``True`` if the request should proceed, ``False`` to reject."""
        k = self._key(provider, model)
        s = self._states.get(k)
        if not s or s["state"] == CLOSED:
            return True
        if s["state"] == OPEN:
            if time.time() - s["tripped_at"] >= self._cooldown_for(s):
                s["state"] = HALF_OPEN
                s["half_open_count"] = 0
                logger.info("[breaker] %s OPEN → HALF_OPEN", k)
                return True
            return False
        # HALF_OPEN
        if s["half_open_count"] < self.half_open_max:
            s["half_open_count"] += 1
            return True
        return False

    def is_open(self, provider: str, model: str) -> bool:
        """Read-only: True iff this circuit is OPEN and still cooling down.

        Unlike allow(), performs no state transition and consumes no
        half-open probe slot — safe for per-request health checks.
        """
        if not self.enabled:
            return False
        s = self._states.get(self._key(provider, model))
        if not s or s["state"] != OPEN:
            return False
        return time.time() - s["tripped_at"] < self._cooldown_for(s)

    def cooldown_remaining(self, provider: str, model: str) -> float:
        """Read-only seconds until OPEN → HALF_OPEN (0 when not cooling down)."""
        s = self._states.get(self._key(provider, model))
        if not s or s["state"] != OPEN:
            return 0.0
        return max(0.0, self._cooldown_for(s) - (time.time() - s["tripped_at"]))

    def report_success(self, provider: str, model: str):
        k = self._key(provider, model)
        s = self._states.get(k)
        if not s:
            return
        now = time.time()
        if s["state"] == OPEN:
            # A panic-mode request (last candidate routed despite the open
            # circuit) succeeded — upstream is healthy again. Same trust
            # level as a half-open probe success: close, keep trips.
            s["state"] = CLOSED
            s["failures"] = 0
            s["half_open_count"] = 0
            s["decay_anchor"] = now
            logger.info(
                "[breaker] %s OPEN → CLOSED via panic success (trips=%d kept)",
                k,
                s.get("trips", 0),
            )
            return
        if s["state"] == HALF_OPEN:
            # Probe passed: close, but KEEP the trip counter — a
            # recover-then-refail (flapping) upstream must keep escalating.
            # The decay below returns it to base over sustained health.
            s["state"] = CLOSED
            s["failures"] = 0
            s["half_open_count"] = 0
            s["decay_anchor"] = now
            logger.info(
                "[breaker] %s HALF_OPEN → CLOSED (trips=%d kept, decays)",
                k,
                s.get("trips", 0),
            )
            return
        # CLOSED: reset the consecutive-failure counter; decay one trip
        # level per decay_seconds of continuous clean service.
        s["failures"] = 0
        if s.get("trips", 0) > 0 and now - s.get("decay_anchor", now) >= (
            self.decay_seconds
        ):
            s["trips"] -= 1
            s["decay_anchor"] = now
            logger.info("[breaker] %s healthy decay → trips=%d", k, s["trips"])
            if s["trips"] <= 0:
                del self._states[k]

    def report_failure(self, provider: str, model: str):
        k = self._key(provider, model)
        now = time.time()
        s = self._states.get(k)
        if not s:
            s = self._states[k] = {
                "state": CLOSED,
                "failures": 0,
                "tripped_at": 0,
                "half_open_count": 0,
                "trips": 0,
                "decay_anchor": now,
            }
        if s["state"] == HALF_OPEN:
            logger.warning("[breaker] %s HALF_OPEN probe failed", k)
            self._trip(k, s, now)
            return
        s["failures"] += 1
        if s["failures"] >= self.failure_threshold:
            self._trip(k, s, now)

    def status(self) -> list[dict]:
        result = []
        for k, s in self._states.items():
            parts = k.split("|", 1)
            result.append(
                {
                    "provider": parts[0],
                    "model": parts[1] if len(parts) > 1 else "",
                    "state": s["state"],
                    "failures": s["failures"],
                    "trips": s.get("trips", 0),
                    "cooldown_s": (
                        round(self._cooldown_for(s)) if s["state"] == OPEN else None
                    ),
                    "tripped_at": s["tripped_at"],
                }
            )
        return result

    def reset(self, provider: str = "", model: str = ""):
        """Full reset — clears ALL counters including the trip escalation.

        This is the active-health-check uneject (Envoy semantics): a probe
        that proved the upstream healthy wipes its whole history.
        """
        if not provider:
            self._states.clear()
            logger.info("[breaker] all circuits reset")
            return
        self._states.pop(self._key(provider, model), None)
        logger.info("[breaker] %s|%s reset", provider, model)

    def reload(self, cfg: dict):
        cb = cfg.get("circuit_breaker", {})
        self.failure_threshold = 0  # disabled if config section missing
        if isinstance(cb, dict) and cb.get("enabled"):
            try:
                self.failure_threshold = int(cb.get("failure_threshold", 3))
                self.cooldown_seconds = float(cb.get("cooldown_seconds", 60))
                self.half_open_max = int(cb.get("half_open_max", 1))
                self.cooldown_max_seconds = float(cb.get("cooldown_max_seconds", 1800))
                self.decay_seconds = float(cb.get("decay_seconds", 600))
                backoff = str(cb.get("cooldown_backoff", "linear")).lower()
                self.cooldown_backoff = (
                    "exponential" if backoff == "exponential" else "linear"
                )
            except (TypeError, ValueError):
                logger.warning(
                    "[breaker] invalid circuit_breaker config, using defaults"
                )
                self.failure_threshold = 3
                self.cooldown_seconds = 60.0
                self.half_open_max = 1
                self.cooldown_max_seconds = 1800.0
                self.cooldown_backoff = "linear"
                self.decay_seconds = 600.0

    @property
    def enabled(self) -> bool:
        return self.failure_threshold > 0


_breaker = None


def get_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker()
        try:
            from .config import load_config
        except ImportError:
            from config import load_config
        _breaker.reload(load_config())
    return _breaker
