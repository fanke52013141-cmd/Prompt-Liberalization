"""Atomically bind sealed artifacts before any acceptance model request."""
import json
import random

from .core import BizError, canonical_hash, new_id, now_iso


def bind_exam(db, run, baseline, candidate, contract):
    binding = {"snapshot_hash": run["snapshot_hash"], "baseline_hash": baseline["hash"],
               "candidate_hash": candidate["hash"]}
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM acceptance_jobs WHERE run_id=? AND candidate_ref=?",
                                (run["id"], candidate["id"])).fetchone()
        if existing:
            if json.loads(existing["binding_json"]) != binding:
                raise BizError("EXAM_BINDING_CHANGED", "考试版本或配置改变，不能恢复", status=409)
            manifest = json.loads(existing["manifest_json"])
            if canonical_hash(manifest) != existing["manifest_hash"]:
                raise BizError("EXAM_MANIFEST_INVALID", "考试清单校验失败", status=409)
            return dict(existing)
        other = conn.execute("SELECT id FROM acceptance_jobs WHERE project_id=? LIMIT 1",
                             (run["project_id"],)).fetchone()
        if other:
            raise BizError("TEST_ALREADY_CONSUMED", "考题已绑定其他考试：" + other["id"], status=409)
        rows = conn.execute("SELECT * FROM sealed_artifacts WHERE project_id=? AND access_state='sealed'",
                            (run["project_id"],)).fetchall()
        if not rows:
            raise BizError("TEST_ALREADY_CONSUMED", "封存考题不存在或已消耗", status=409)
        groups = {}
        for row in rows:
            payload = json.loads(row["sealed_json"])
            if not payload.get("source_group_id"):
                source = conn.execute("SELECT source_group_id FROM dataset_items WHERE id=?", (row["item_id"],)).fetchone()
                payload["source_group_id"] = source[0] if source else None
            artifact = {**payload, "item_id": row["item_id"], "sha256": row["sha256"]}
            groups.setdefault(payload.get("source_group_id") or "unknown_source", []).append(artifact)
        rng = random.Random(run["snapshot"].get("seed", 20260927))
        manifest = [rng.choice(sorted(group, key=lambda item: item["item_id"]))
                    for _, group in sorted(groups.items())]
        job_id = new_id("acc")
        rubric = conn.execute("SELECT schema_json FROM rubrics WHERE id=? AND project_id=?",
                              (run["snapshot"]["rubric_id"], run["project_id"])).fetchone()
        if rubric is None:
            raise BizError("EXAM_RUBRIC_INVALID", "考试标准不属于当前项目", status=409)
        conn.execute("INSERT INTO acceptance_jobs(id,run_id,project_id,candidate_ref,binding_json,"
                     "manifest_json,manifest_hash,contract_json,artifact_total,created_at,rubric_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, run["id"], run["project_id"], candidate["id"], json.dumps(binding),
                      json.dumps(manifest, ensure_ascii=False), canonical_hash(manifest),
                      json.dumps(contract, ensure_ascii=False), len(rows), now_iso(), rubric[0]))
        conn.execute("UPDATE sealed_artifacts SET access_state='consumed' WHERE project_id=? AND access_state='sealed'",
                     (run["project_id"],))
        conn.execute("INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                     (new_id("aud"), "system", "acceptance.bound", job_id, canonical_hash(binding), now_iso()))
        return dict(conn.execute("SELECT * FROM acceptance_jobs WHERE id=?", (job_id,)).fetchone())
