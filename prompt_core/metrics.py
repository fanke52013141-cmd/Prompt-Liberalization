"""Deterministic task metrics with explicit missing and format failures."""
def validate_metric_config(config):
    if not isinstance(config, dict) or config.get("type") not in ("exact_match", "classification"):
        raise ValueError("unsupported metric")
    if "strip" in config and type(config["strip"]) is not bool:
        raise ValueError("strip must be an explicit boolean")
    if config["type"] == "classification":
        labels = config.get("labels")
        if not isinstance(labels, list) or len(labels) < 2 or any(not isinstance(x, str) or not x for x in labels) or len(set(labels)) != len(labels):
            raise ValueError("classification requires unique explicit labels")
        average = config.get("average")
        if average not in ("macro", "micro", "binary"):
            raise ValueError("classification requires explicit averaging")
        if average == "binary" and config.get("positive_class") not in labels:
            raise ValueError("binary metrics require positive_class")
    return dict(config)


def evaluate_metric(output, reference, config):
    validate_metric_config(config)
    kind = config.get("type")
    if kind not in ("exact_match", "classification"):
        raise ValueError("unsupported metric")
    if reference is None:
        return {"status": "unknown", "usable": None, "error": "missing_reference"}
    if not isinstance(output, str) or not isinstance(reference, str):
        raise ValueError("output and reference must be strings")
    if config.get("strip", False):
        output, reference = output.strip(), reference.strip()
    if kind == "classification":
        labels = config.get("labels")
        if not isinstance(labels, list) or len(labels) < 2 or any(not isinstance(x, str) for x in labels) or len(set(labels)) != len(labels):
            raise ValueError("classification requires unique explicit labels")
        if reference not in labels:
            return {"status": "unknown", "usable": None, "error": "invalid_reference"}
        if output not in labels:
            return {"status": "format_failure", "usable": False, "prediction": None, "reference": reference}
    return {"status": "ok", "usable": output == reference, "prediction": output, "reference": reference}


def classification_summary(results, labels, *, average, positive_class=None):
    if average not in ("macro", "micro", "binary") or len(set(labels)) != len(labels):
        raise ValueError("explicit valid averaging and labels required")
    if average == "binary" and positive_class not in labels:
        raise ValueError("binary metrics require positive_class")
    known = [r for r in results if r["status"] != "unknown"]
    counts = {}
    for label in labels:
        tp = sum(r.get("prediction") == label and r.get("reference") == label for r in known)
        fp = sum(r.get("prediction") == label and r.get("reference") != label for r in known)
        fn = sum(r.get("prediction") != label and r.get("reference") == label for r in known)
        counts[label] = {"tp": tp, "fp": fp, "fn": fn, "support": tp+fn,
                         "f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}
    if average == "binary":
        f1 = counts[positive_class]["f1"]
    elif average == "macro":
        f1 = sum(m["f1"] for m in counts.values()) / len(labels) if all(m["f1"] is not None for m in counts.values()) else None
    else:
        tp = sum(m["tp"] for m in counts.values())
        denom = sum(2*m["tp"]+m["fp"]+m["fn"] for m in counts.values())
        f1 = 2*tp/denom if denom else None
    return {"total": len(results), "known": len(known), "unknown": len(results)-len(known),
            "format_failures": sum(r["status"] == "format_failure" for r in known),
            "average": average, "positive_class": positive_class, "f1": f1, "per_class": counts}
