"""Map Ollaya native answers onto the noul-shaped fields compose_evidence reads.
Ollaya returns Score for contradicts and Choice for injection; compose_evidence expects
noul floats for both. This module is the only place that knows Ollaya's answer types.
Never imports from sibling packages; pure mapping, no I/O, never raises.
"""


def _level_probability(probabilities, level: int) -> float:
    """Return P(level) from a Score answer's probabilities dict or list.
    Handles both dict (keyed by str(level)) and list (indexed by level).
    Returns 0.0 when probabilities is absent or the level is out of range.
    """
    if not probabilities:
        return 0.0
    if isinstance(probabilities, dict):
        if str(level) in probabilities:
            return float(probabilities[str(level)])
        values = list(probabilities.values())
    else:
        values = list(probabilities)
    return float(values[level]) if level < len(values) else 0.0


def to_compose(answers: dict) -> dict:
    """Map native answers onto the noul-shaped fields compose_evidence reads.
    contradicts := probability of the 'incompatible with the premise' level (level 0).
    injection   := probabilities['instruction_override'].
    noul/score/choice pass through unchanged as {"noul": value}.
    Never raises; unknown keys are dropped.
    Args:
        answers: Raw answer dicts from ollaya.client.call keyed by question name,
                 each carrying at least "type" and "probabilities".
    Returns:
        dict: Same keys, each value {"noul": float} compatible with compose_evidence.
    """
    result: dict = {}
    for name, answer in answers.items():
        if not isinstance(answer, dict):
            continue
        if name.startswith("contradicts::"):
            # Score level 0 = most conflicting ("states the opposite of the query").
            result[name] = {"noul": _level_probability(answer.get("probabilities"), 0)}
        elif name.startswith("injection::"):
            probabilities = answer.get("probabilities") or {}
            result[name] = {
                "noul": float(probabilities.get("instruction_override", 0.0))
            }
        elif answer.get("noul") is not None:
            result[name] = {"noul": float(answer["noul"])}
        elif answer.get("score") is not None:
            result[name] = {"score": answer["score"]}
        elif answer.get("choice") is not None:
            result[name] = {"choice": answer["choice"]}
    return result
