"""Bind calibration evidence to the evaluator that actually executed it."""
import inspect
import json

from .core import BizError, canonical_hash


def freeze_evaluator(config):
    from .engine import get_connection
    connection = get_connection(config)
    frozen = dict(config)
    frozen["model"] = config.get("model") or connection.get("model") or "default"
    frozen["connection_snapshot"] = {k: v for k, v in connection.items()
                                     if k in ("id", "provider", "base_url", "model",
                                              "supports_temperature", "supports_seed",
                                              "unsupported_params", "mock_inject")}
    return frozen


def evaluator_binding(db, rubric_id, config):
    from .engine import evaluate_once, evaluation_messages, request_fingerprint
    from prompt_core.evaluation import severity_rules, validate_rubric_result
    frozen = freeze_evaluator(config)
    row = db.one("SELECT schema_json FROM rubrics WHERE id=?", (rubric_id,))
    if row is None:
        return None
    params = dict(frozen.get("params") or {})
    params.setdefault("max_tokens", 2048)
    identity = {"rubric_id": rubric_id, "rubric": json.loads(row["schema_json"]),
                "model": frozen["model"], "params": params,
                "metric": frozen.get("metric"),
                "connection": frozen["connection_snapshot"],
                "implementation": canonical_hash({
                    "evaluate": inspect.getsource(evaluate_once),
                    "messages": inspect.getsource(evaluation_messages),
                    "request": inspect.getsource(request_fingerprint),
                    "severity_rules": inspect.getsource(severity_rules),
                    "validation": inspect.getsource(validate_rubric_result)})}
    return {"version": 1, "hash": canonical_hash(identity)}


def admitted_for(db, judge, rubric_id, config):
    if not judge or judge["status"] != "audited" or judge["rubric_id"] != rubric_id:
        return False
    metrics = json.loads(judge["metrics_json"] or "{}")
    if metrics.get("admission", {}).get("eligible") is not True:
        return False
    try:
        return bool(metrics.get("evaluator_binding") and
                    metrics["evaluator_binding"] == evaluator_binding(db, rubric_id, config))
    except (BizError, ValueError, TypeError, KeyError):
        return False


def severe_admitted_for(db, judge, rubric_id, config):
    """A saved flag cannot replace live independent severity audit evidence."""
    try:
        if not admitted_for(db,judge,rubric_id,config):
            return False
        metrics=json.loads(judge['metrics_json'] or '{}')
        if metrics.get('admission',{}).get('severe_admission') is not True:
            return False
        reference=metrics.get('severity_audit') or {}
        from .severity_audits import SeverityAudits
        service=SeverityAudits(db)
        task=service.get(reference.get('audit_id'))
        binding=evaluator_binding(db,rubric_id,config)
        if (task['judge_id']!=judge['id'] or task['project_id']!=judge['project_id']
                or reference.get('snapshot_hash')!=task['snapshot_hash']
                or reference.get('evaluator_binding')!=binding
                or task['evaluator_binding']!=binding):
            return False
        evidence=service.verify_evidence(task['id'])
        return evidence['evidence_valid'] is True and evidence['statistically_eligible'] is True
    except (BizError,ValueError,TypeError,KeyError):
        return False
