"""Content bindings for development/selection experiments; never include raw references."""
import json

from .core import BizError, canonical_hash


def bound_item(db, project_id, item_id, expected_split):
    row = db.one("SELECT * FROM dataset_items WHERE project_id=? AND id=?", (project_id, item_id))
    if row is None or row["split"] != expected_split or expected_split not in ("dev", "select"):
        raise BizError("DATA_SNAPSHOT_CHANGED", "案例不存在或用途已改变，请新建实验", status=409)
    return {"id": row["id"], "case_id": row["case_id"], "split": row["split"],
            "source_group_id": row["source_group_id"],
            "runtime_input": json.loads(row["runtime_input_json"]),
            "evaluation_only": json.loads(row["evaluation_only_json"])}


def freeze_cases(db, project_id, data):
    bindings = {}
    for split in ("dev", "select"):
        for item_id in data.get(f"{split}_item_ids", []):
            item = bound_item(db, project_id, item_id, split)
            bindings[item_id] = {"split": split, "content_hash": canonical_hash(item)}
    return bindings


def verified_item(db, project_id, item_id, bindings):
    if not isinstance(bindings, dict) or item_id not in bindings:
        raise BizError("DATA_SNAPSHOT_MISSING", "实验缺少案例绑定，请新建实验", status=409)
    binding = bindings[item_id]
    item = bound_item(db, project_id, item_id, binding["split"])
    if canonical_hash(item) != binding["content_hash"]:
        raise BizError("DATA_SNAPSHOT_CHANGED", "案例输入、参考或来源已改变，请新建实验", status=409)
    return item


def verify_cases(db, project_id, data):
    bindings = data.get("case_bindings")
    for split in ("dev", "select"):
        for item_id in data.get(f"{split}_item_ids", []):
            verified_item(db, project_id, item_id, bindings)
