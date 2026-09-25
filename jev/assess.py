"""Async Jev assessment wrappers for the zerikai_memory retrieval pipeline.
Builds minimal state payloads for Stage A (query) and Stage B (evidence), invokes
the synchronous jev.client.call via asyncio.to_thread, and fails open by returning
None on any Jev unavailability or unexpected error.
"""

import asyncio
from contextvars import ContextVar
import logging

import config
from jev.client import JevUnavailable, call
from jev.questions import evidence_questions, query_questions

logger = logging.getLogger(__name__)
_tracking_workspace_id: ContextVar[str | None] = ContextVar(
    "jev_tracking_workspace_id", default=None
)


def set_tracking_workspace_id(workspace_id: str):
    """Set the workspace id used for Jev token tracking in the current async context.
    Stores it in the _tracking_workspace_id ContextVar and returns the token that
    reset_tracking_workspace_id must consume to restore the prior value.
    """
    return _tracking_workspace_id.set(workspace_id)


def reset_tracking_workspace_id(token) -> None:
    """Reset the workspace tracking context after a Jev call."""
    _tracking_workspace_id.reset(token)


def _track_jev_usage(operation: str, response: dict | None) -> None:
    """Persist Jev token usage for the active workspace when a response carries usage.
    Reads the workspace id from the _tracking_workspace_id ContextVar, then calls
    main._track_token_usage to write an operation/model/usage row to the shared
    token_usage table. Fails open: any tracking error is logged and swallowed.
    """
    workspace_id = _tracking_workspace_id.get()
    if not workspace_id or not isinstance(response, dict):
        return

    usage = response.get("usage")
    if not usage:
        return

    model = response.get("model") or config.TYPESAFE_MODEL
    try:
        from main import _track_token_usage

        _track_token_usage(workspace_id, operation, model, usage)
    except Exception as exc:
        logger.warning("Jev token tracking failed open: %s", exc)


def _extract_answers(response: dict | None) -> dict | None:
    """Normalize a Jev client response into the raw answers mapping compose expects.
    Returns response["answers"] when it is a dict; otherwise falls back to the raw
    response dict, and returns None when the response is None or not a dict.
    """
    if response is None:
        return None
    answers = response.get("answers") if isinstance(response, dict) else None
    if isinstance(answers, dict):
        return answers
    return response if isinstance(response, dict) else None


# --- TEMPORARY DEBUG (remove after threshold calibration) -----------------
# Logs every raw Jev answer (query + evidence judges) so thresholds can be
# tuned from observed probabilities instead of defaults. Safe to delete.
def _debug_answer_value(answer: object) -> str:
    """Render one typed Jev answer as a compact string for debug logging."""
    if answer is None:
        return "None"
    parts = []
    for field in ("noul", "score", "choice"):
        if hasattr(answer, field):
            parts.append(f"{field}={getattr(answer, field)!r}")
    probabilities = getattr(answer, "probabilities", None)
    if probabilities is not None:
        parts.append(f"probabilities={dict(probabilities)!r}")
    if not parts:
        parts.append(repr(answer))
    return " ".join(parts)


def _log_raw_answers(label: str, response: dict | None) -> None:
    """TEMPORARY DEBUG: log raw Jev query/evidence answers for threshold calibration.
    Extracts answers via _extract_answers and logs the model, answer count, and each
    named answer's value. Logs "no answers (fail-open)" and returns early when
    extraction yields None. Remove once thresholds are calibrated.
    """
    answers = _extract_answers(response)
    if answers is None:
        logger.info("JEV_DEBUG %s | no answers (fail-open)", label)
        return
    model = response.get("model") if isinstance(response, dict) else None
    logger.info(
        "JEV_DEBUG %s | model=%s | %d answers", label, model, len(answers)
    )
    for name, answer in answers.items():
        logger.info(
            "JEV_DEBUG %s | %s -> %s", label, name, _debug_answer_value(answer)
        )
# --- END TEMPORARY DEBUG --------------------------------------------------


def _evidence_state(query: str, passages: list[dict]) -> dict:
    """Build the trimmed state payload Jev evidence questions consume.
    Keeps only id, source_file, lineno, name, and text from each passage, copying
    them into a fresh dict-of-lists; the input passages list is not mutated.
    """
    return {
        "query": query,
        "passages": [
            {
                "id": passage.get("id"),
                "source_file": passage.get("source_file"),
                "lineno": passage.get("lineno"),
                "name": passage.get("name"),
                "text": passage.get("text"),
            }
            for passage in passages
        ],
    }


async def assess_query(query: str, brief: str, index_summary: dict) -> dict | None:
    """Return raw Stage A Jev answers for a query, or None on any failure.
    Returns None immediately when config.ENABLE_JEV is false. Otherwise calls the
    synchronous jev.client.call via asyncio.to_thread with query_questions().
    Side effects: tracks token usage and emits temporary debug logs. Fails open by
    returning None on JevUnavailable or any unexpected exception.
    """
    if not config.ENABLE_JEV:
        return None

    state = {
        "project_brief": brief,
        "query": query,
        "index_summary": index_summary,
    }

    try:
        response = await asyncio.to_thread(call, state, query_questions())
        _track_jev_usage("jev_query", response)
        _log_raw_answers("query", response)  # TEMPORARY DEBUG
        return _extract_answers(response)
    except JevUnavailable as exc:
        logger.warning("Jev query assessment unavailable: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Jev query assessment failed open: %s", exc)
        return None


async def assess_evidence(query: str, passages: list[dict]) -> dict | None:
    """Return raw Stage B Jev answers for passages, or None on any failure.
    Returns None immediately when config.ENABLE_JEV is false. Otherwise builds state
    via _evidence_state, derives passage_ids, and calls the synchronous jev.client.call
    via asyncio.to_thread with evidence_questions(passage_ids). Side effects: tracks
    token usage and emits temporary debug logs. Fails open by returning None on
    JevUnavailable or any unexpected exception.
    """
    if not config.ENABLE_JEV:
        return None

    state = _evidence_state(query, passages)
    passage_ids = [str(passage.get("id")) for passage in passages]

    try:
        response = await asyncio.to_thread(call, state, evidence_questions(passage_ids))
        _track_jev_usage("jev_evidence", response)
        _log_raw_answers("evidence", response)  # TEMPORARY DEBUG
        return _extract_answers(response)
    except JevUnavailable as exc:
        logger.warning("Jev evidence assessment unavailable: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Jev evidence assessment failed open: %s", exc)
        return None
