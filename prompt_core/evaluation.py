"""Strict parsing for rubric evaluations; missing information stays unknown."""


def severity_rules(schema):
    """Give legacy textual business rules stable identifiers within this rubric."""
    rules = {}
    entries = schema.get('severity_examples', [])
    if not isinstance(entries, list):
        raise ValueError('严重规则必须为列表')
    for index, entry in enumerate(entries, 1):
        if isinstance(entry, str) and entry.strip():
            key, description = f'severity_{index}', entry
        elif isinstance(entry, dict):
            key = entry.get('rule_id') or entry.get('id')
            description = entry.get('description') or entry.get('text')
        else:
            raise ValueError('严重规则必须有编号及明确描述')
        if not isinstance(key, str) or not key.strip() or not isinstance(description, str) or not description.strip():
            raise ValueError('严重规则必须有编号及明确描述')
        if key in rules:
            raise ValueError('严重规则编号重复')
        rules[key] = description
    return rules


def validate_rubric_result(data, dimensions, *, output_text=None, rules=None):
    unknown = {"abstain": True, "severe": None, "error": "invalid_evaluation"}
    if not isinstance(data, dict) or type(data.get("abstain")) is not bool:
        return unknown
    if data["abstain"]:
        return {"abstain": True, "severe": None}
    scores = data.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(dimensions):
        return unknown
    if any(type(score) is not int or not 0 <= score <= 3 for score in scores.values()):
        return unknown
    severe = data.get("severe")
    if severe is not None and type(severe) is not bool:
        return unknown
    result = {"scores": scores, "abstain": False, "severe": severe,
              "violations": []}
    violations = data.get('violations', [])
    error = None
    if not isinstance(output_text, str) or not isinstance(rules, dict) or not rules:
        error = 'severity_context_missing'
    elif not isinstance(violations, list):
        error = 'severity_evidence_invalid'
    elif severe is True and not violations or severe is False and violations:
        error = 'severity_evidence_inconsistent'
    else:
        for ev in violations:
            if not isinstance(ev, dict):
                error = 'severity_evidence_invalid'
                break
            start, end, quote = ev.get('start'), ev.get('end'), ev.get('quote')
            if (not isinstance(ev.get('rule_id'), str) or ev['rule_id'] not in rules or
                    type(start) is not int or type(end) is not int or
                    not 0 <= start < end <= len(output_text) or
                    not isinstance(quote, str) or output_text[start:end] != quote):
                error = 'severity_evidence_invalid'
                break
            result['violations'].append({k: ev[k] for k in ('rule_id', 'start', 'end', 'quote')})
    if error:
        result.update(severe=None, violations=[], severity_error=error)
    return result
