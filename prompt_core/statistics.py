"""Paired binary measurements with separate unknown and failure semantics."""
import math


def exact_mcnemar(improved, regressed):
    if type(improved) is not int or type(regressed) is not int or min(improved, regressed) < 0:
        raise ValueError("Discordant counts must be nonnegative integers")
    n = improved + regressed
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(improved, regressed) + 1)) / (2 ** n))


def missing_bounds(usable, unusable, unknown):
    if any(type(n) is not int or n < 0 for n in (usable, unusable, unknown)):
        raise ValueError("Counts must be nonnegative integers")
    total = usable + unusable + unknown
    return {"low": usable / total if total else 0.0,
            "high": (usable + unknown) / total if total else 0.0,
            "total": total, "unknown_n": unknown}


def binary_rating(value):
    if value is True or value == "usable":
        return True
    if value is False or value in ("minor", "unusable"):
        return False
    return None


def summarize_pairs(pairs):
    """Input: baseline/candidate True, False, None; each row remains in N."""
    for row in pairs:
        for side in ("baseline", "candidate"):
            if row.get(side) is not None and type(row[side]) is not bool:
                raise ValueError("Pair outcomes must be boolean or unknown")
    n = len(pairs)
    counts = {side: {"usable": 0, "unusable": 0, "unknown": 0}
              for side in ("baseline", "candidate")}
    known = []
    worst, best = [], []
    for row in pairs:
        b, c = row.get("baseline"), row.get("candidate")
        for side, value in (("baseline", b), ("candidate", c)):
            counts[side]["unknown" if value is None else "usable" if value else "unusable"] += 1
        if b is not None and c is not None:
            known.append((b, c))
        worst.append((True if b is None else b, False if c is None else c))
        best.append((False if b is None else b, True if c is None else c))

    def effect(outcomes):
        fix = sum(not b and c for b, c in outcomes)
        regress = sum(b and not c for b, c in outcomes)
        return {"fix": fix, "regress": regress,
                "gain": (fix - regress) / len(outcomes) if outcomes else 0.0,
                "p": exact_mcnemar(fix, regress)}

    return {"total": n, "known_n": len(known), "unknown_pairs": n - len(known),
            "counts": counts,
            "bounds": {side: missing_bounds(**values) for side, values in counts.items()},
            "known": effect(known), "worst_case": effect(worst), "best_case": effect(best)}
