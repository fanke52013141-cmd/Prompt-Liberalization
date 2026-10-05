"""Single-pass substitutions; inserted input is never treated as a template."""
import re

RENDERER_VERSION = "frozen-runtime-v2"


def substitute_once(body, values):
    def replace(match):
        name = match.group(1)
        if name not in values:
            raise ValueError("Missing template variable: " + name)
        return str(values[name])
    return re.sub(r"\{\{([^{}]+)\}\}", replace, body)


def compile_messages(body, variables, frozen_segments, runtime, task_label, case_id=""):
    missing = [name for name in variables if name not in runtime]
    if missing:
        raise ValueError("Missing variables: " + ", ".join(missing))
    values = {name: runtime[name] for name in variables}
    rendered = substitute_once(body, values)
    constraints = "\n".join(segment["text"] for segment in frozen_segments if segment.get("text"))
    system = ("固定规则：\n" + constraints + "\n\n任务说明：\n" + rendered) if constraints else rendered
    lines = ["任务类型：" + task_label, "[案例 " + case_id + "]"]
    lines.extend(str(name) + "：" + str(value) for name, value in values.items())
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def render_text(body, input_text):
    if "{输入}" in body:
        return body.replace("{输入}", input_text)
    return body + "\n\n" + input_text
