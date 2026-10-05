"""GEPA boundary: mutable prompt body, explicit data views and owned calls.

The supplied evaluator must perform physical requests through the application's
budget ledger. This adapter never constructs a provider or reads credentials.
"""
from copy import deepcopy
from importlib import metadata
import math


SUPPORTED_VERSION = "0.1.4"


def load_sdk():
    try:
        installed = metadata.version("gepa")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("GEPA扩展未安装；人工比较仍可使用。安装可选requirements-gepa.txt。") from exc
    if installed != SUPPORTED_VERSION:
        raise RuntimeError(f"GEPA版本不兼容：需要{SUPPORTED_VERSION}，当前{installed}。")
    import gepa
    return gepa


class PromptAdapter:
    propose_new_texts = None

    def __init__(self, train_view, selection_view, evaluator, stop_signal, batch_factory=None):
        self.train = self._view(train_view, "dev")
        self.selection = self._view(selection_view, "select")
        train_groups = {item["source_group"] for item in self.train.values()}
        if train_groups & {item["source_group"] for item in self.selection.values()}:
            raise ValueError("开发和选择数据不能共享来源组。")
        if self.train.keys() & self.selection.keys():
            raise ValueError("开发和选择案例ID不能重叠。")
        self.evaluator = evaluator
        self.stop_signal = stop_signal
        self.batch_factory = batch_factory
        self.records = []

    @staticmethod
    def _view(items, purpose):
        view = {}
        for item in items:
            if (item.get("purpose") != purpose or not item.get("source_group")
                    or not item.get("id") or item["id"] in view):
                raise ValueError("数据用途、来源组或案例ID无效。")
            view[item["id"]] = deepcopy(item)
        if not view:
            raise ValueError("开发和选择数据均不能为空。")
        return view

    @staticmethod
    def _body(candidate):
        if set(candidate) != {"body"} or not isinstance(candidate["body"], str) or not candidate["body"].strip():
            raise ValueError("GEPA只允许修改提示词正文body。")
        return candidate["body"]

    def evaluate(self, batch, candidate, capture_traces=False):
        body = self._body(candidate)
        traces, outputs, scores = [], [], []
        for item in batch:
            if self.stop_signal():
                raise InterruptedError("优化已取消；未执行后续调用。")
            stored = self.train.get(item.get("id")) or self.selection.get(item.get("id"))
            if stored is None or stored != item:
                raise ValueError("评价案例不属于冻结开发/选择视图。")
            result = deepcopy(self.evaluator(body, deepcopy(stored)))
            score = result.get("score")
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1):
                raise ValueError("评价器必须返回0到1的有限分数。")
            trace = {"case": deepcopy(stored), "result": result}
            traces.append(trace)
            outputs.append(result.get("output", ""))
            scores.append(float(score))
            self.records.append({"body": body, **deepcopy(trace)})
        factory = self.batch_factory or load_sdk().EvaluationBatch
        return factory(outputs=outputs, scores=scores,
                       trajectories=traces if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        self._body(candidate)
        if components_to_update != ["body"] or eval_batch.trajectories is None:
            raise ValueError("反思需要正文组件和完整开发轨迹。")
        records = []
        for trace in eval_batch.trajectories:
            item = trace["case"]
            if self.train.get(item.get("id")) != item:
                raise ValueError("选择或封存数据不能进入反思。")
            result = trace["result"]
            records.append({"Inputs": item["input"],
                            "Generated Outputs": result.get("output", ""),
                            "Feedback": {"score": result["score"],
                                         "status": result.get("status", "unknown"),
                                         "details": result.get("feedback", "")}})
        return {"body": records}
