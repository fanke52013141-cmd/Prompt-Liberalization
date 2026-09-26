"""统一错误协议、ID/哈希工具与四种任务模板。

错误体统一 code/message/field_errors/retryable/correlation_id（开发方案第7节）。
四种任务模板来自 PRD 第6.1节，仅作为可编辑初始化草案。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


# ---------------------------------------------------------------- 错误协议

class BizError(Exception):
    """业务错误：code 面向程序，message 面向用户的中文可行动解释。"""

    def __init__(self, code: str, message: str, field_errors: dict | None = None,
                 retryable: bool = False, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_errors = field_errors or {}
        self.retryable = retryable
        self.status = status

    def body(self, correlation_id: str) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": self.field_errors,
            "retryable": self.retryable,
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------- ID / 哈希

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj) -> str:
    """稳定序列化哈希：同内容必同哈希（快照/幂等/去重的基础）。"""
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------- 任务模板 (PRD 6.1)

# runtime_fields: 生成模型可见的白名单输入（BR01）。
# evaluation_fields: 仅评价使用，绝不进入生成请求。
# dimensions: 0-3 分锚点维度草案；severity_examples: 严重问题举例。
TASK_TEMPLATES = {
    "student_feedback": {
        "label": "学员作答点评",
        "runtime_fields": [
            {"name": "question", "type": "text", "label": "题目"},
            {"name": "student_answer", "type": "text", "label": "学员答案"},
            {"name": "grade_level", "type": "enum", "label": "学段",
             "options": ["小学", "初中", "高中"]},
            {"name": "reference_material", "type": "text", "label": "运行时允许提供的资料",
             "required": False},
        ],
        "evaluation_fields": [
            {"name": "expert_answer", "type": "text", "label": "参考答案"},
            {"name": "expert_comment", "type": "text", "label": "教研批注"},
        ],
        "evaluation_unit": "一份完整点评",
        "dimensions": [
            {"name": "判断正确", "anchors": {"0": "判断学员答案对错时结论错误", "1": "判断含混，无法据此使用",
                                    "2": "判断基本正确但有遗漏", "3": "判断正确且指出了关键依据"}},
            {"name": "具体定位", "anchors": {"0": "未定位学员问题或定位错误", "1": "泛泛指出有问题",
                                    "2": "定位到具体步骤但缺细节", "3": "精确定位到出错步骤与原因"}},
            {"name": "建议可执行", "anchors": {"0": "无建议或建议错误", "1": "泛泛鼓励，无法据此修改",
                                    "2": "指出具体修改动作但有遗漏", "3": "动作对应错误且学生能按步骤完成"}},
            {"name": "表达友好", "anchors": {"0": "语气生硬或有贬损", "1": "平淡机械",
                                    "2": "语气恰当", "3": "语气恰当且有激励性"}},
        ],
        "severity_examples": ["虚构学员错误", "纠正为错误结论"],
        "primary_metric": "一份完整点评可直接使用的比例",
    },
    "question_explain": {
        "label": "题目解析",
        "runtime_fields": [
            {"name": "question", "type": "text", "label": "题目"},
            {"name": "grade_level", "type": "enum", "label": "适用学段",
             "options": ["小学", "初中", "高中"]},
            {"name": "reference_material", "type": "text", "label": "允许使用的参考",
             "required": False},
        ],
        "evaluation_fields": [
            {"name": "standard_answer", "type": "text", "label": "标准答案"},
        ],
        "evaluation_unit": "一份完整解析",
        "dimensions": [
            {"name": "结论正确", "anchors": {"0": "给出错误答案", "1": "答案含混不清",
                                    "2": "答案正确但依据不足", "3": "答案正确且依据明确"}},
            {"name": "关键步骤完整", "anchors": {"0": "缺少关键推导步骤", "1": "步骤跳跃难以跟随",
                                    "2": "步骤完整但有跳步", "3": "关键步骤完整可跟随"}},
            {"name": "易理解", "anchors": {"0": "表述混乱无法理解", "1": "表述晦涩",
                                  "2": "表述清楚", "3": "表述清楚且有直观解释"}},
        ],
        "severity_examples": ["错答案", "推导关键错误"],
        "primary_metric": "完整解析可直接使用的比例",
    },
    "article_title": {
        "label": "公众号标题",
        "runtime_fields": [
            {"name": "source_text", "type": "text", "label": "原文/事实摘要"},
            {"name": "audience", "type": "text", "label": "受众"},
            {"name": "count", "type": "integer", "label": "数量"},
            {"name": "length_limit", "type": "integer", "label": "长度限制(字)"},
        ],
        "evaluation_fields": [
            {"name": "fact_check_notes", "type": "text", "label": "事实核查备注"},
        ],
        "evaluation_unit": "一次生成的一组标题（不把同组标题当独立来源）",
        "dimensions": [
            {"name": "忠实", "anchors": {"0": "标题与事实矛盾", "1": "标题有夸大成分",
                                 "2": "基本忠实", "3": "忠实且准确概括"}},
            {"name": "吸引力", "anchors": {"0": "平淡无点击意愿", "1": "吸引力弱",
                                  "2": "有一定吸引力", "3": "吸引力强且不标题党"}},
            {"name": "差异性", "anchors": {"0": "组内标题高度雷同", "1": "差异小",
                                  "2": "角度有区分", "3": "多角度且各有侧重"}},
            {"name": "风格符合", "anchors": {"0": "与账号风格冲突", "1": "风格不符",
                                    "2": "风格基本符合", "3": "风格精准匹配"}},
        ],
        "severity_examples": ["标题与事实矛盾", "编造承诺"],
        "primary_metric": "一组标题中至少有一个可采用的来源比例",
    },
    "article_framework": {
        "label": "公众号文章框架",
        "runtime_fields": [
            {"name": "topic", "type": "text", "label": "主题"},
            {"name": "audience", "type": "text", "label": "受众"},
            {"name": "material", "type": "text", "label": "材料"},
            {"name": "goal", "type": "text", "label": "目标"},
            {"name": "length", "type": "integer", "label": "篇幅(字)"},
        ],
        "evaluation_fields": [
            {"name": "material_manifest", "type": "text", "label": "可用材料清单"},
        ],
        "evaluation_unit": "一份完整框架",
        "dimensions": [
            {"name": "逻辑", "anchors": {"0": "结构与任务冲突", "1": "逻辑断裂",
                                 "2": "逻辑通顺", "3": "逻辑严密且有推进"}},
            {"name": "覆盖", "anchors": {"0": "遗漏主题关键面", "1": "覆盖不足",
                                 "2": "覆盖主要方面", "3": "覆盖全面且有取舍理由"}},
            {"name": "论据可获得", "anchors": {"0": "编造材料", "1": "论据来源不明",
                                     "2": "论据基本可获得", "3": "论据全部来自给定材料"}},
            {"name": "可展开", "anchors": {"0": "无法按框架展开", "1": "展开需大幅重构",
                                  "2": "小调整可展开", "3": "无须实质重构即可展开"}},
        ],
        "severity_examples": ["编造材料", "结构与任务冲突"],
        "primary_metric": "框架无须实质重构即可展开的比例",
    },
}


def template_draft(task_type: str) -> dict:
    """返回任务的契约与评价标准草案（可编辑初始化建议）。"""
    if task_type not in TASK_TEMPLATES:
        raise BizError("TASK_TYPE_UNKNOWN", f"未知任务类型：{task_type}，可选：{list(TASK_TEMPLATES)}")
    t = TASK_TEMPLATES[task_type]
    return {
        "task_type": task_type,
        "label": t["label"],
        "runtime_fields": t["runtime_fields"],
        "evaluation_fields": t["evaluation_fields"],
        "evaluation_unit": t["evaluation_unit"],
        "rubric_draft": {"dimensions": t["dimensions"], "severity_examples": t["severity_examples"]},
        "primary_metric": t["primary_metric"],
        "is_demo": True,  # PRD P01：模板中示例明确为演示数据
    }
