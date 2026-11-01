"""Ollaya model warm-up and release helpers for VRAM management.
pin() sends a keep_alive request so Ollaya holds the model between queries;
release() sends keep_alive=0 so the model is evicted after the current request.
Both use httpx (already a project dependency) and fail open — VRAM management
errors are logged at WARN and never propagate into the query path.
"""

import logging

import httpx

import config

logger = logging.getLogger(__name__)

# Ollaya server base URL with any trailing slash stripped, derived from
# config.OLLAYA_BASE_URL (env OLLAYA_BASE_URL, default http://127.0.0.1:11435).
# Every warm-up request in this module is built from it.
_BASE = config.OLLAYA_BASE_URL.rstrip("/")
# Full Ollaya endpoint URL for warm-up and eviction requests: f"{_BASE}/api/decide".
# POSTed by pin() with a non-zero keep_alive and by release() with keep_alive=0.
_DECIDE_URL = f"{_BASE}/api/decide"


def pin(model: str, keep_alive) -> None:
    """Send a no-op decide request to pin the model in VRAM with the given keep_alive.
    Uses an empty state and no questions so Ollaya loads the model without running
    inference. Logs and returns silently on any HTTP or connection error.
    """
    try:
        httpx.post(
            _DECIDE_URL,
            json={"model": model, "keep_alive": keep_alive},
            timeout=config.OLLAYA_TIMEOUT_SECONDS,
            trust_env=False,
        ).raise_for_status()
        logger.debug("ollaya.warm.pin model=%s keep_alive=%s", model, keep_alive)
    except Exception as exc:
        logger.warning("ollaya.warm.pin failed (non-fatal): %s", exc)


def release(model: str) -> None:
    """Send keep_alive=0 to evict the model from VRAM after the current request.
    Allows the synthesis model to take the GPU on the next query. Logs and returns
    silently on any HTTP or connection error.
    """
    try:
        httpx.post(
            _DECIDE_URL,
            json={"model": model, "keep_alive": 0},
            timeout=config.OLLAYA_TIMEOUT_SECONDS,
            trust_env=False,
        ).raise_for_status()
        logger.debug("ollaya.warm.release model=%s", model)
    except Exception as exc:
        logger.warning("ollaya.warm.release failed (non-fatal): %s", exc)
