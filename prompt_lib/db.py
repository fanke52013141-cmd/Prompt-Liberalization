"""SQLite 存储层。

本地单机使用；所有时间存 UTC（开发方案第6节）。JSON 字段直接存 TEXT。
单写连接 + RLock，适配 uvicorn 默认线程池。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "prompt_lab.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  task_type TEXT NOT NULL, contract_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',           -- active / archived
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  note TEXT DEFAULT '', snapshot_json TEXT NOT NULL, hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS dataset_items (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, case_id TEXT NOT NULL,
  source_group_id TEXT NOT NULL, origin TEXT NOT NULL,             -- real / synthetic
  runtime_input_json TEXT NOT NULL, evaluation_only_json TEXT NOT NULL DEFAULT '{}',
  split TEXT NOT NULL DEFAULT 'unassigned',                        -- dev/select/sealed_test/unassigned
  scene_tags TEXT DEFAULT '', eval_status TEXT DEFAULT 'none',     -- none/model_pre/human_verified/adjudicated
  content_hash TEXT NOT NULL, version_id TEXT, created_at TEXT NOT NULL,
  UNIQUE(project_id, case_id)
);
CREATE TABLE IF NOT EXISTS sealed_artifacts (
  item_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, sealed_json TEXT NOT NULL,
  sha256 TEXT NOT NULL, access_state TEXT NOT NULL DEFAULT 'sealed',  -- sealed / unsealed / consumed
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, preview_hash TEXT NOT NULL,
  source_name TEXT DEFAULT '', total INTEGER NOT NULL, valid INTEGER NOT NULL,
  errors_json TEXT NOT NULL DEFAULT '[]', excluded_json TEXT NOT NULL DEFAULT '[]',
  committed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(project_id, preview_hash)
);
CREATE TABLE IF NOT EXISTS split_manifests (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_version_id TEXT NOT NULL,
  group_map_hash TEXT NOT NULL, seed INTEGER NOT NULL, weights_json TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'frozen', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rubrics (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',            -- draft/published/retired
  schema_json TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS outputs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, item_id TEXT NOT NULL,
  prompt_version_id TEXT NOT NULL, run_id TEXT DEFAULT '',
  role TEXT NOT NULL DEFAULT 'generation',         -- generation / evaluation
  text TEXT DEFAULT '', status TEXT NOT NULL,      -- ok / failed / incomplete / eval_ok / eval_failed
  score_json TEXT DEFAULT '', usage_json TEXT DEFAULT '{}',
  request_hash TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, output_id TEXT NOT NULL,
  rubric_id TEXT NOT NULL, source TEXT NOT NULL,   -- human / model_pre
  gold_status TEXT NOT NULL DEFAULT 'none',        -- none / model_pre / human_verified / adjudicated
  scores_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '[]',
  pair_public_id TEXT DEFAULT '', side TEXT DEFAULT '', purpose TEXT DEFAULT '',
  annotator TEXT DEFAULT '', submitted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blind_pairs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, public_id TEXT NOT NULL,
  left_output_id TEXT NOT NULL, right_output_id TEXT NOT NULL,
  order_seed INTEGER NOT NULL, purpose TEXT NOT NULL, revealed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(public_id)
);
CREATE TABLE IF NOT EXISTS judges (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  rubric_id TEXT NOT NULL, model_config_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',            -- draft/calibrating/audited/stale/failed
  build_refs_json TEXT NOT NULL DEFAULT '[]', audit_refs_json TEXT NOT NULL DEFAULT '[]',
  metrics_json TEXT NOT NULL DEFAULT '{}', config_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS prompt_versions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
  version_no INTEGER NOT NULL, parent_id TEXT DEFAULT '',
  frozen_segments_json TEXT NOT NULL DEFAULT '[]', -- [{name, text}] 服务端冻结 (BR02)
  body TEXT NOT NULL,                              -- 模板正文，含 {{变量}} 与可变组件标记
  variables_json TEXT NOT NULL DEFAULT '[]',       -- 运行时白名单变量名
  params_json TEXT NOT NULL DEFAULT '{}',
  hash TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'manual',  -- manual/optimizer/trial
  hypothesis TEXT DEFAULT '', notes_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, name, version_no)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, idempotency_key TEXT DEFAULT '',
  payload_hash TEXT DEFAULT '', snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  -- queued/running/waiting_human/paused_budget/stopping/completed/failed/cancelled
  stage TEXT DEFAULT '', stop_reason TEXT DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
  baseline_prompt_id TEXT DEFAULT '', baseline_score REAL DEFAULT 0,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  locked_candidate TEXT DEFAULT '', budget_state_json TEXT NOT NULL DEFAULT '{}',
  error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, logical_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL, attempt_index INTEGER NOT NULL,
  role TEXT NOT NULL, model TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'search',
  reserved_tokens INTEGER NOT NULL DEFAULT 0,
  actual_in INTEGER, actual_out INTEGER, actual_cost TEXT,
  currency TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'reserved',
  -- reserved / ok / sent_unknown / failed
  request_hash TEXT DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(attempt_id), UNIQUE(logical_id, attempt_index)
);
CREATE TABLE IF NOT EXISTS acceptance_reports (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
  candidate_ref TEXT NOT NULL, baseline_ref TEXT NOT NULL,
  test_manifest_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  stats_json TEXT NOT NULL, decision TEXT NOT NULL,
  -- verified_improvement/no_improvement/regression/inconclusive/evaluation_invalid
  consumed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, prompt_version_id TEXT NOT NULL,
  model_config_json TEXT NOT NULL, report_ref TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'trial',            -- trial / active / rolled_back
  revision INTEGER NOT NULL DEFAULT 1, adoption_note TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, release_id TEXT NOT NULL, project_id TEXT NOT NULL,
  adoption TEXT NOT NULL,                          -- direct/minor_edit/major_edit/abandoned/missing
  edit_time TEXT DEFAULT '', reason TEXT DEFAULT '', status TEXT DEFAULT 'pending_review',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
  payload_hash TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(run_id, seq)
);
CREATE TABLE IF NOT EXISTS tags (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
  definition TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
  merged_into TEXT DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS expert_feedback (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, item_id TEXT NOT NULL DEFAULT '',
  quote TEXT DEFAULT '',                           -- 专家原话/引用（保留原文）
  problem TEXT NOT NULL,                           -- 问题描述及适用条件
  expected TEXT DEFAULT '',                        -- 期望表现
  check_method TEXT DEFAULT '',                    -- 检查方式
  severity TEXT NOT NULL DEFAULT 'normal',         -- severe/normal/preference
  status TEXT NOT NULL DEFAULT 'pending',          -- pending/confirmed_error/preference/unverified/resolved/retired
  tags_json TEXT NOT NULL DEFAULT '[]',
  remark TEXT DEFAULT '',                          -- 专家备注原文（系统不得摘要覆盖）
  source TEXT NOT NULL DEFAULT 'manual',           -- manual/import
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rating_rules (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  levels_json TEXT NOT NULL,                       -- [{code,name,trend,meaning,display_basis}]
  status TEXT NOT NULL DEFAULT 'draft',            -- draft/published
  note TEXT DEFAULT '', hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS case_reviews (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, item_id TEXT NOT NULL,
  rule_id TEXT NOT NULL, rating TEXT NOT NULL,     -- 等级码 / cannot_judge / not_rated
  resolutions_json TEXT NOT NULL DEFAULT '[]',     -- [{feedback_id,status,note}] fixed/partial/open/unknown
  new_problems_json TEXT NOT NULL DEFAULT '[]',    -- [{description,severity}]
  regress_note TEXT DEFAULT '', remark TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'human',            -- human/auto_suggested/human_confirmed/human_corrected
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_rounds (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, round_no INTEGER NOT NULL,
  hypothesis TEXT DEFAULT '', problem_evidence_json TEXT NOT NULL DEFAULT '[]',
  prompt_version_id TEXT DEFAULT '', score REAL, prev_score REAL,
  usable_rate REAL, severe INTEGER, regressions INTEGER DEFAULT 0,
  fixed_problems_json TEXT NOT NULL DEFAULT '[]',
  decision TEXT DEFAULT '', rationale TEXT DEFAULT '', next_direction TEXT DEFAULT '',
  length_chars INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'scored',           -- scored/rewrite_failed/no_change
  detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(run_id, round_no)
);
CREATE INDEX IF NOT EXISTS idx_items_proj ON dataset_items(project_id, split);
CREATE INDEX IF NOT EXISTS idx_outputs_item ON outputs(item_id);
CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger(run_id);
"""


# 旧库平滑迁移：缺列补列（历史数据与新配置共存，不覆盖旧结果——优化1.0 §13 历史兼容）
_MIGRATIONS = (
    ("runs", "baseline_detail_json",
     "ALTER TABLE runs ADD COLUMN baseline_detail_json TEXT NOT NULL DEFAULT '{}'"),
    ("runs", "round_no", "ALTER TABLE runs ADD COLUMN round_no INTEGER NOT NULL DEFAULT 0"),
    ("runs", "current_best_pv", "ALTER TABLE runs ADD COLUMN current_best_pv TEXT DEFAULT ''"),
    ("runs", "best_detail_json",
     "ALTER TABLE runs ADD COLUMN best_detail_json TEXT NOT NULL DEFAULT '{}'"),
    ("runs", "stall_count", "ALTER TABLE runs ADD COLUMN stall_count INTEGER NOT NULL DEFAULT 0"),
)


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_DB
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, col, ddl in _MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    self._conn.execute(ddl)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.tx() as conn:
            conn.execute(sql, params)

    @staticmethod
    def j(row, key: str, default=None):
        """读取行内 JSON 列。"""
        raw = row[key] if row is not None else None
        if not raw:
            return default
        return json.loads(raw)


# 全局单例（uvicorn 单进程模式下安全）
_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


def set_db(db: DB) -> None:
    global _db
    _db = db
