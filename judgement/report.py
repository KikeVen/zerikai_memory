"""Plain-text Jev report rendering helpers.

Builds the additive report block attached to grounded answers and the fixed
no-answer line used when Jev decides retrieval should stop. Pure formatting;
no I/O and no imports from main.py.
"""

# Exact user-facing text returned when Jev decides no passage warrants an answer.
# Emitted verbatim by render_no_answer and used as the fixed stop message when
# retrieval is suppressed; changing it changes the visible no-answer output.
NO_ANSWER_LINE = "I couldn't find anything relevant in memory for that question."

# Static explanation header prepended to every composed report by render_report.
# Documents the 0-1 probability fields (relevance, evidence, contradicts,
# injection), the verdict values (include, conflict, drop), status, coverage,
# conflicts, and the separate L2 distance scale. User-facing prose only.
_LEGEND = (
    "Legend: relevance, evidence, contradicts, and injection are probabilities from 0 to 1.\n"
    "High relevance/evidence = the passage is on-topic and usable as evidence. High contradicts =\n"
    "the passage conflicts with a factual claim in my question (not the answer) — check it. High\n"
    "injection = retrieved text tried to instruct the AI; treat it as untrusted. verdict: include =\n"
    "used as evidence, conflict = kept but flagged, drop = excluded. status = overall verdict.\n"
    "coverage = how much of my question the evidence covers (e.g. 2/3). conflicts = the number of\n"
    "passages that disagree with a factual claim in my question. Sources = L2 vector distance,\n"
    "lower is closer (a separate scale from the 0-1 probabilities above)."
)


def _score_tuple(evidence: dict) -> tuple[float | None, str | None]:
    """Return the preferred score and label for a source item.
    Picks the first numeric score in priority order: "rerank_score" labeled
    "rerank", then "l2_distance" labeled "L2", then "distance" labeled "L2".
    Returns (None, None) when none of the three keys holds a number.
    """
    if isinstance(evidence.get("rerank_score"), (int, float)):
        return float(evidence["rerank_score"]), "rerank"
    if isinstance(evidence.get("l2_distance"), (int, float)):
        return float(evidence["l2_distance"]), "L2"
    if isinstance(evidence.get("distance"), (int, float)):
        return float(evidence["distance"]), "L2"
    return None, None


def _format_location(evidence: dict) -> str:
    """Return file:line location text for an evidence item.
    Reads "source_file", defaulting to "unknown" when absent, and appends
    ":lineno" only when "lineno" is present and non-empty. Guarantees a
    non-empty string so downstream rows always have a location label.
    """
    location = evidence.get("source_file", "unknown")
    if evidence.get("lineno") not in (None, ""):
        location += f":{evidence['lineno']}"
    return location


def _format_source_block(evidence_list: list[dict]) -> str:
    """Build the trailing Sources block using the existing file:line contract.
    Emits a "Sources:" header then one "* location — score (label)" bullet per
    item, falling back to "* location (no score)" when no numeric score exists,
    and appending " — note" when the item has a "note". Returns "" for an empty
    list and joins rows with newlines; no trailing newline.
    """
    if not evidence_list:
        return ""
    lines = ["Sources:"]
    for evidence in evidence_list:
        score, label = _score_tuple(evidence)
        location = _format_location(evidence)
        line = (
            f"* {location} — {score:.2f} ({label})"
            if isinstance(score, (int, float))
            else f"* {location} (no score)"
        )
        note = evidence.get("note")
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)


def _format_verdict(evidence: dict) -> str:
    """Return the row verdict text for an evidence item.
    Returns "conflict" when "conflict" is truthy and "verdict" is "include";
    otherwise returns the item's "verdict" value, defaulting to "include".
    """
    if evidence.get("conflict") and evidence.get("verdict") == "include":
        return "conflict"
    return evidence.get("verdict", "include")


def _format_evidence_row(index: int, evidence: dict) -> str:
    """Build one plain-text Evidence row.
    Formats "  {index}. {location}[ {name}] relevance=.. evidence=..
    contradicts=.. injection=.. verdict=..", coercing the four probability
    fields to float (default 0.0) with 2 decimals and omitting the bracketed
    name when absent. Verdict text comes from _format_verdict.
    """
    location = _format_location(evidence)
    name = evidence.get("name")
    name_part = f" [{name}]" if name else ""
    return (
        f"  {index}. {location}{name_part} "
        f"relevance={float(evidence.get('relevance', 0.0)):.2f} "
        f"evidence={float(evidence.get('evidence', 0.0)):.2f} "
        f"contradicts={float(evidence.get('contradicts', 0.0)):.2f} "
        f"injection={float(evidence.get('injection', 0.0)):.2f} "
        f"verdict={_format_verdict(evidence)}"
    )


def _passage_summary(composed: dict) -> str:
    """Summarize included and dropped evidence counts.
    Splits dropped passages into "irrelevant" and "injection" counts using
    composed["injection_dropped"]. Returns "N included, 0 dropped" when nothing
    was dropped, else "N included, M dropped (X irrelevant, Y injection)".
    """
    included_count = len(composed.get("included", []))
    dropped = composed.get("dropped", [])
    dropped_count = len(dropped)
    injection_dropped = int(composed.get("injection_dropped", 0) or 0)
    irrelevant_dropped = max(0, dropped_count - injection_dropped)
    if dropped_count:
        return (
            f"{included_count} included, {dropped_count} dropped "
            f"({irrelevant_dropped} irrelevant, {injection_dropped} injection)"
        )
    return f"{included_count} included, 0 dropped"


def _guidance_lines(composed: dict) -> list[str]:
    """Build the Guidance subsection lines.
    Derives trust (low with no included passages, high when status is
    "supported", else medium) and an action string that varies with status and
    conflicts. Always emits "trust:" and "action:" lines, then conditionally
    appends "security:", "gap:", and "suggested_query:" when those keys are set.
    """
    included_count = len(composed.get("included", []))
    status = composed.get("status", "supported")
    conflicts = int(composed.get("conflicts", 0) or 0)
    if not included_count:
        trust = "low"
    elif status == "supported":
        trust = "high"
    else:
        trust = "medium"
    passage_label = "passage" if included_count == 1 else "passages"
    if status == "supported":
        action = (
            f"answer is supported by {included_count} included {passage_label}; cite it directly"
        )
    elif conflicts > 0:
        action = (
            "answer is partly grounded; cite evidence directly and note conflicting support"
        )
    else:
        action = (
            "answer is partly grounded; cite evidence directly and note the coverage gap"
        )
    lines = [f"  trust: {trust}", f"  action: {action}"]

    security_note = composed.get("security_note")
    if security_note:
        lines.append(f"  security: {security_note}")

    gap = composed.get("gap")
    if gap:
        lines.append(f"  gap: {gap}")

    suggested_query = composed.get("suggested_query")
    if suggested_query:
        lines.append(f"  suggested_query: {suggested_query}")

    return lines


def render_report(composed: dict) -> str:
    """Render the plain-text Jev report block for an answer response.
    Assembles the _LEGEND header, optional "answer", an "Assessment:" section
    (status, coverage, passages, conflicts), an "Evidence:" section listing
    included then dropped rows via _format_evidence_row, and a "Guidance:"
    section from _guidance_lines. Pure formatting; returns newline-joined text.
    """
    answer = composed.get("answer", "")
    included = list(composed.get("included", []))
    dropped = list(composed.get("dropped", []))
    evidence_rows = included + dropped

    lines = [_LEGEND, ""]
    if answer:
        lines.extend([answer, ""])

    lines.extend(
        [
            "Assessment:",
            f"  status: {composed.get('status', 'supported')}",
        ]
    )

    coverage = composed.get("coverage")
    if coverage not in (None, ""):
        lines.append(f"  coverage: {coverage}")

    lines.append(f"  passages: {_passage_summary(composed)}")
    lines.append(f"  conflicts: {int(composed.get('conflicts', 0) or 0)}")

    lines.extend(["", "Evidence:"])
    for index, evidence in enumerate(evidence_rows, start=1):
        lines.append(_format_evidence_row(index, evidence))

    lines.extend(["", "Guidance:"])
    lines.extend(_guidance_lines(composed))

    return "\n".join(lines)


def render_no_answer(suggested: list[str] | None = None) -> str:
    """Render the fixed no-answer line with optional suggested search words.
    Returns NO_ANSWER_LINE unchanged when suggested is empty or all falsy;
    otherwise appends a newline and "Suggested search words: " followed by the
    comma-joined truthy words. No I/O or side effects.
    Args:
        suggested: Optional search words to append; falsy entries are skipped.
    Returns:
        str: Either the bare NO_ANSWER_LINE or that line plus the suggestions.
    """
    if not suggested:
        return NO_ANSWER_LINE
    words = ", ".join(word for word in suggested if word)
    if not words:
        return NO_ANSWER_LINE
    return f"{NO_ANSWER_LINE}\nSuggested search words: {words}"
