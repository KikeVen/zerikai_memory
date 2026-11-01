"""Ollaya transport: send one batched TypeSafe system_one request to winnow:e4b.
Uses the TypeSafe SDK pointed at the local Ollaya server; raises OllayaUnavailable
on any failure (connection, timeout, HTTP 422, non-JSON body, missing answers).
Never caches a client singleton — Ollaya does not need keep-alive at the SDK layer.
"""

import logging

import config

logger = logging.getLogger(__name__)


class OllayaUnavailable(Exception):
    """Raised when Ollaya is unavailable or the request cannot be completed.
    Signals fail-open: the caller logs at WARN and falls through to lexical rerank.
    Never propagates up; always caught and handled by the calling pipeline stage.
    """


def _dump_answer(answer) -> dict:
    """Serialize one TypeSafe answer object to a plain dict for normalize.to_compose.
    Extracts type, noul, score, choice, probabilities from the answer object using
    getattr with None defaults; works for both typed SDK objects and plain dicts.
    """
    if isinstance(answer, dict):
        return answer
    return {
        "type": getattr(answer, "type", None),
        "noul": getattr(answer, "noul", None),
        "score": getattr(answer, "score", None),
        "choice": getattr(answer, "choice", None),
        "probabilities": getattr(answer, "probabilities", None),
    }


def call(state: dict, questions: dict) -> dict:
    """Send one batched TypeSafe system_one request to Ollaya (winnow:e4b); returns raw answers.
    Builds a fresh TypeSafeClient from config.OLLAYA_* per call with RetryPolicy(max_retries=0)
    because Ollaya STATE_TRUNCATED is not transient. No client caching, no retries.
    Args:
        state: JSON-serialisable state dict forwarded to TypeSafeClient.system_one.
        questions: Question set from ollaya.questions.evidence_questions.
    Returns:
        dict with keys "model" (str), "answers" ({name: {type, noul/score/choice,
        probabilities}}), and "usage" ({input_tokens, output_tokens}).
    Raises:
        OllayaUnavailable: On ImportError (typesafe_sdk missing), client-init failure,
            connection error, timeout, HTTP 422 (STATE_TRUNCATED / INVALID_REQUEST /
            TOO_MANY_OPTIONS), non-JSON body, or a response missing 'answers'.
    """
    try:
        from typesafe_sdk import RetryPolicy, TypeSafeClient
    except ImportError as exc:
        raise OllayaUnavailable(f"typesafe_sdk not installed: {exc}") from exc

    try:
        client = TypeSafeClient(
            api_key=config.OLLAYA_API_KEY,
            base_url=config.OLLAYA_BASE_URL,
            model=config.OLLAYA_MODEL,
            timeout=config.OLLAYA_TIMEOUT_SECONDS,
            # no retries — Ollaya STATE_TRUNCATED is not transient
            retry=RetryPolicy(max_retries=0),
        )
    except Exception as exc:
        raise OllayaUnavailable(f"Ollaya client init failed: {exc}") from exc

    try:
        response = client.system_one(state=state, questions=questions)
    except Exception as exc:
        # Covers connection errors, timeouts, HTTP 4xx/5xx, and parse failures.
        raise OllayaUnavailable(f"Ollaya system_one call failed: {exc}") from exc

    answers_raw = getattr(response, "answers", None)
    if not answers_raw:
        raise OllayaUnavailable("Ollaya response missing 'answers'")

    answers = {name: _dump_answer(ans) for name, ans in answers_raw.items()}

    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": getattr(usage_obj, "input_tokens", None) or 0,
        "output_tokens": getattr(usage_obj, "output_tokens", None) or 0,
    }

    return {
        "model": getattr(response, "model", config.OLLAYA_MODEL),
        "answers": answers,
        "usage": usage,
    }
