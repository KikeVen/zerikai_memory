"""Pure composition helpers for Jev query and evidence judgments.
Transforms raw TypeSafe answers plus retrieved passages into small, stable
dicts that later pipeline stages can consume. No I/O, no imports from main.py,
and fail-open when Jev answers are unavailable.
"""

from math import inf


def _value(answer: object, field: str, default: float | str | None = None):
    """Read a field from a TypeSafe SDK answer object or dict-like structure.
    Branches on answer type: returns getattr(answer, field) when the attribute
    exists (typed Noul/Choice/Score response), answer.get(field, default) for a
    plain dict, and default when answer is None or the field is absent.
    Always returns a value; never raises KeyError or AttributeError.
    """
    if answer is None:
        return default
    if hasattr(answer, field):
        return getattr(answer, field)
    if isinstance(answer, dict):
        return answer.get(field, default)
    return default


def _distance(passage: dict) -> float:
    """Return the passage distance used for ranking tie-breaks in compose_evidence.
    Reads the "distance" key and falls back to "l2_distance" (ChromaDB vector
    distance), returning math.inf when neither is present so the passage sorts
    last under ascending distance ordering. No side effects.
    """
    distance = passage.get("distance")
    if distance is None:
        distance = passage.get("l2_distance")
    if distance is None:
        return inf
    return float(distance)


def compose_query(
    answers: dict | None,
    *,
    domain_min: float,
    spec_max: float,
    answerability_min: float,
) -> dict:
    """Compose Stage A Jev answers into a routing decision.
    Consumes TypeSafe answers (in_domain, specificity, answerable_from_code,
    intent); returns proceed, no_answer, intent, reason. Fails open when answers
    is None. Branch order: out_of_domain if in_domain < domain_min, too_vague
    if specificity > spec_max, not_answerable_from_code if answerable_from_code
    < answerability_min; else reason "proceed".
    Args:
        answers: Raw Stage A TypeSafe answers keyed by question name, or None.
        domain_min: Minimum in_domain Noul score required to proceed.
        spec_max: Maximum specificity Score allowed before the query is too vague.
        answerability_min: Minimum answerable_from_code Noul score required.
    Returns:
        dict: Routing decision with keys proceed (bool), no_answer (bool),
              intent (Choice value or None), and reason (str).
    """
    if answers is None:
        return {
            "proceed": True,
            "no_answer": False,
            "intent": None,
            "reason": "fail_open",
        }

    in_domain = float(_value(answers.get("in_domain"), "noul", 0.0) or 0.0)
    specificity = float(_value(answers.get("specificity"), "score", 0.0) or 0.0)
    answerable = float(
        _value(answers.get("answerable_from_code"), "noul", 0.0) or 0.0
    )
    intent = _value(answers.get("intent"), "choice", None)

    if in_domain < domain_min:
        return {
            "proceed": False,
            "no_answer": True,
            "intent": intent,
            "reason": "out_of_domain",
        }

    if specificity > spec_max:
        return {
            "proceed": False,
            "no_answer": True,
            "intent": intent,
            "reason": "too_vague",
        }

    if answerable < answerability_min:
        return {
            "proceed": False,
            "no_answer": True,
            "intent": intent,
            "reason": "not_answerable_from_code",
        }

    return {
        "proceed": True,
        "no_answer": False,
        "intent": intent,
        "reason": "proceed",
    }


def compose_evidence(
    answers: dict | None,
    passages: list[dict],
    *,
    rel_min: float,
    evid_min: float,
    inj_max: float,
    contra_min: float,
    answerability_min: float,
) -> dict:
    """Compose Stage B Jev evidence answers into per-passage keep/drop decisions.
    Fails open when answers is None. Keeps when relevance>=rel_min, evidence>=
    evid_min and injection<inj_max. Ranks by 0.50*relevance+0.35*evidence-
    0.15*injection and flags contradicts>=contra_min. Sorts included
    by rank desc then ascending ChromaDB l2_distance. Status "partial" on
    conflicts or low answerability, else "supported".
    Args:
        answers: Raw Stage B TypeSafe answers keyed by "{question}::{passage_id}"
                 (relevant, evidence, contradicts, injection) plus "answerable"
                 and "coverage", or None to fail open.
        passages: Retrieved passage dicts; each "id" keys the per-passage answers.
        rel_min: Minimum relevant Noul score to keep a passage.
        evid_min: Minimum evidence Noul score to keep a passage.
        inj_max: Maximum injection Noul score allowed; at or above it, drop.
        contra_min: Minimum contradicts Noul score to flag a passage as conflict.
        answerability_min: Minimum answerable Noul score; below it, status
                          downgrades to "partial".
    Returns:
        dict: included/dropped/flagged lists of enriched passages, conflicts
              (int), injection_dropped (int), security_note (str),
              no_answer (bool), status ("supported"/"partial"),
              coverage ("N/3" clamped 0-3, or None), and reason (str).
    """
    if answers is None:
        included = list(passages)
        return {
            "included": included,
            "dropped": [],
            "flagged": [],
            "conflicts": 0,
            "injection_dropped": 0,
            "security_note": "",
            "no_answer": False,
            "status": "supported",
            "coverage": None,
            "reason": "fail_open",
        }

    included = []
    dropped = []
    flagged = []
    injection_dropped = 0

    for passage in passages:
        passage_id = passage.get("id")
        relevant = float(
            _value(answers.get(f"relevant::{passage_id}"), "noul", 0.0) or 0.0
        )
        evidence = float(
            _value(answers.get(f"evidence::{passage_id}"), "noul", 0.0) or 0.0
        )
        contradicts = float(
            _value(answers.get(f"contradicts::{passage_id}"), "noul", 0.0) or 0.0
        )
        injection = float(
            _value(answers.get(f"injection::{passage_id}"), "noul", 0.0) or 0.0
        )
        keep = relevant >= rel_min and evidence >= evid_min and injection < inj_max
        rank = (0.50 * relevant) + (0.35 * evidence) - (0.15 * injection)
        conflict = contradicts >= contra_min

        enriched = {
            **passage,
            "relevance": relevant,
            "evidence": evidence,
            "contradicts": contradicts,
            "injection": injection,
            "rank": rank,
            "conflict": conflict,
            "verdict": "include" if keep else "drop",
        }

        if keep:
            included.append(enriched)
            if conflict:
                flagged.append(enriched)
        else:
            dropped.append(enriched)
            if injection >= inj_max:
                injection_dropped += 1

    included.sort(key=lambda passage: (-passage["rank"], _distance(passage)))

    answerable = float(_value(answers.get("answerable"), "noul", 0.0) or 0.0)
    coverage_score = round(float(_value(answers.get("coverage"), "score", 0.0) or 0.0))
    coverage_score = max(0, min(3, coverage_score))
    # Kept passages are the real gate: low whole-set answerability downgrades the
    # status to "partial" instead of suppressing an answer Judge #2 kept.
    no_answer = not included
    answerability_low = answerable < answerability_min
    security_note = ""
    if injection_dropped:
        security_note = (
            f"{injection_dropped} retrieved passage(s) attempted prompt injection and were dropped"
        )

    return {
        "included": included,
        "dropped": dropped,
        "flagged": flagged,
        "conflicts": len(flagged),
        "injection_dropped": injection_dropped,
        "security_note": security_note,
        "no_answer": no_answer,
        "status": "partial" if (flagged or answerability_low) else "supported",
        "coverage": f"{coverage_score}/3",
        "reason": "no_kept_passages" if not included else (
            "answerability_below_threshold" if answerability_low else "supported"
        ),
    }