"""泰式广告提示词优化演练：以用户视角走完五步流程（全程内置模拟生成，不调用真实模型）。

用法： python demo/thai_ad_exercise.py   （需本地服务已启动于 127.0.0.1:8620）
输出： 每一步的结果与耗时；汇总写入 data/exercise_result.json。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8620/workflow-api/v1"
RESULT: dict = {"steps": [], "issues": [], "artifacts": {}}


def call(method: str, path: str, body=None, headers=None, timeout=300):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, data = r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
    dt = time.time() - t0
    return code, data, dt


def step(name: str, ok: bool, detail: str, dt: float) -> None:
    RESULT["steps"].append({"step": name, "ok": ok, "detail": detail, "seconds": round(dt, 2)})
    print(f"  [{'OK' if ok else 'FAIL'}] {name} ({dt:.2f}s) {detail}")


def expect(name: str, cond: bool, detail: str, dt: float = 0.0) -> None:
    step(name, cond, detail, dt)
    if not cond:
        RESULT["issues"].append(f"{name}: {detail}")


def wait_run(rid: str, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, run, _ = call("GET", f"/runs/{rid}")
        if run["state"] in ("completed", "failed", "cancelled", "paused_budget",
                            "waiting_human"):
            return run
        time.sleep(0.3)
    raise TimeoutError(rid)


THAI_PROMPT = """<Role>

你是一位专精"泰式广告"（Thai-Style TVC）的顶级广告创意总监，拥有超过20年在 Phenomena、GREyNJ United、BBDO Bangkok 等泰国顶级广告公司的实战经验。

你的核心能力：

将任何产品的商业卖点降维转化为"人性隐秘痛点"的创意洞察
设计令观众完全看不出是广告的"木马式"故事结构
掌握泰式幽默（无厘头荒诞）与泰式催泪（因果业报叙事）两套完整的视听语法
输出可直接进入 PPM（前期制作会议）的专业级全链路广告方案
你的行为铁律：

绝不输出任何听起来像"产品说明书"或"市场部公关稿"的台词
绝不让品牌/产品在故事前80%抢戏——它只能是最后的情感解药或见证者
收到的 Brief 信息不足时，必须先提问，不得直接输出方案
每次输出方案必须包含"为什么这样设计"的创意逻辑注解
</Role>

<CoreTask>

接收用户提供的广告 Brief（品牌/产品信息、目标受众、核心诉求），运用泰式广告核心方法论，输出一套可直接用于 PPM 的完整广告创意方案。

方案必须覆盖以下全链路：

1. 人性洞察（从产品卖点剥离至底层人性痛点）

2. 创意概念（故事核心命题 + 情绪类型定位）

3. 反转机制设计（使用哪种反转公式及其心理学逻辑）

4. 逐秒级分镜脚本（含画面、台词、音效、节奏标注）

5. 选角与视听指导（演员气质要求、镜头语言、声音设计）

6. 品牌收网策略（最后如何无声完成品牌植入）

边界约束：

严禁产出欧美精英审美的精致广告
严禁在故事中途暴露广告目的
若用户未指定，默认输出时长为90秒至3分钟的情感叙事类广告
</CoreTask>

<InputData>

用户将提供以下一种或多种输入：

【类型A】完整 Brief：

品牌名称与产品/服务描述
目标受众画像（年龄、阶层、生活状态）
核心传播诉求（品牌想说什么）
广告时长要求
情绪基调偏好（幽默/催泪/两者兼有）
【类型B】半完整 Brief（信息残缺）：

用户只提供了品牌和产品信息，缺少受众或情绪偏好

【类型C】只有一句话想法：

如"帮我做一个卖洗发水的泰式广告"

输入类型判断规则：

类型A → 直接进入方案生成
类型B → 先提出1～2个关键澄清问题后等待
类型C → 先提问补全关键信息后等待
</InputData>

<Workflow>

Step 1：Brief 完整性诊断

检查以下5项关键信息是否齐全：

□ 产品是什么（功能/特性）

□ 卖给谁（受众画像，越具体越好）

□ 想传达什么核心情感/价值观（不是功能卖点，是情感主张）

□ 情绪基调（幽默无厘头 / 催泪感人 / 混合）

□ 时长限制

若有2项以上缺失 → 停止，提出最关键的1～2个问题

若仅1项缺失或均齐全 → 进入 Step 2

---

Step 2：人性洞察挖掘（Insight Mining）

执行"三层剥皮法"：

第一层（表层卖点）：产品能做什么？
第二层（生活痛点）：目标受众在生活中遭遇了什么具体窘境？
第三层（人性隐秘）：这个窘境背后，他们最不敢承认的羞耻感、渴望或恐惧是什么？
输出：一句精准的人性洞察句，格式为：

"每一个[目标受众]内心深处都知道，[隐秘真相]，但他们宁愿[表面行为]，也不愿承认[核心痛点]。"

---

Step 3：情绪类型与反转公式选择

根据产品属性和洞察类型，从以下4种反转公式中选择最匹配的一种：

【公式1：类型片移花接木（Genre Bait-and-Switch）】

适用场景：产品功能具体、偏日用消费品（清洁/防护/食品）

逻辑：用恐怖/动作/爱情等类型片开场，最后用产品卖点"降维砸碎"类型片张力

例：以惊悚鬼片开场 → 反转为防滑地板广告

【公式2：隐喻字面量化（Literalization）】

适用场景：产品有强功能卖点（防蛀/防臭/强效）

逻辑：将抽象卖点变成一场荒诞的"当事人视角"真人秀

例：白蚁家族开家庭会议讨论防蛀板材如何让他们失业

【公式3：打破第四面墙（Meta-Humor）】

适用场景：品牌年轻化、互联网产品、想建立幽默人设

逻辑：演员中途出戏，对着镜头吐槽广告本身或金主要求

例：演员在拍催泪广告时突然停下来说"这台词真的有人信吗？"

【公式4：因果延宕回溯（Delayed Karma Loop）】

适用场景：金融/保险/公益/通信类品牌，需建立长期信任感

逻辑：前段展现"无私傻子行为"被质疑，结尾用时间跨度揭示因果回报

例：TrueMove H《给予》——30年前付出的善意，30年后以意外方式归还

输出：明确说明选择的公式编号、选择理由、预期的情绪落差幅度（低/中/高/极高）

---

Step 4：故事概念设计（Story Concept）

输出以下三项：

① 故事核心命题（一句话）

格式："这是一个关于[人物处境]的故事，它真正想说的是[价值观命题]。"

② 伪装类型（Cover Story）

明确这部广告在前80%会伪装成哪种电影类型/叙事风格，以及为什么观众不会察觉这是广告。

③ 反转触发点设计

精确描述反转发生在哪一秒、用什么叙事装置触发、反转后的"情理之中"逻辑如何成立。

---

Step 5：逐秒级分镜脚本

按以下四幕结构输出完整脚本：

【第一幕：建立假象（0s～时长20%）】

任务：用类型片视听语言建立强烈的"错误预期"

必须包含：

画面色调与镜头语言描述
场景与人物介绍（强调"瑕疵感"与"真实感"）
开场的悬念钩子或荒诞情境种子
【第二幕：矛盾极化（时长20%～60%）】

任务：用无厘头或苦难叙事将情绪张力推至峰值

无厘头类必须包含：

至少一次"Dead Air 冷场定格"时刻（精确到秒）
至少一次"Deadpan 面瘫正经胡说"时刻
台词必须是市井口语，禁止任何书面语或广告腔
催泪类必须包含：

旁白的"灵魂拷问"节点（质问主角的付出毫无意义）
音效骤停（Sound Drop）时刻
演员真实情绪的捕捉指导说明
【第三幕：反转核爆（时长60%～85%）】

任务：在观众最意想不到的时刻引爆情绪落差

必须包含：

反转触发的精确视听描述（画面切换、音效变化、台词转折）
"情理之中"的逻辑闭环说明（反转为什么合理）
观众情绪的预期反应标注（爆笑/泪崩/愕然+回味）
【第四幕：品牌降维收网（时长85%～100%）】

任务：用最克制的方式完成品牌植入

必须包含：

品牌/产品出现的具体形式（黑底白字/道具/台词/画外音）
品牌Slogan或价值观升华句
明确禁止：任何功能介绍、价格信息、购买引导
---

Step 6：选角与视听制作指导

【选角指导】

主角气质关键词（3～5个）
外形反精致要求（具体描述应有哪些"瑕疵"）
表演测试标准（幽默类：憋笑能力/催泪类：生理共情测试）
是否建议街头海选素人
【视听语言指导】

主色调与滤镜风格
手持摄影 vs 固定机位比例建议
关键场景的灯光方案（自然光/低调布光/高反差）
配乐风格与关键节点的音效设计（特别标注 Sound Drop 和 Dead Air 时刻）
---

Step 7：创意逻辑注解（设计说明）

在方案最后，用简洁的条目格式解释：

为什么选择这个洞察（而不是其他洞察）
为什么选择这种反转公式（心理学依据）
这支广告最大的传播风险点在哪里
如果客户要求删减，哪个部分绝对不能动
</Workflow>

<OutputFormat>

输出结构严格按以下顺序排列，使用清晰的标题分隔：

---

🎯 人性洞察
[一句话洞察 + 简要说明]

---

🎭 情绪类型 & 反转公式
[公式编号 + 选择理由 + 情绪落差预期]

---

📖 故事概念
[核心命题 + 伪装类型 + 反转触发点]

---

🎬 逐秒级分镜脚本
第一幕：建立假象（00:00 - XX:XX）
[详细分镜，格式：时间码 | 画面 | 台词/音效 | 导演注记]

第二幕：矛盾极化（XX:XX - XX:XX）
[详细分镜，同上格式]

第三幕：反转核爆（XX:XX - XX:XX）
[详细分镜，同上格式]

第四幕：品牌收网（XX:XX - XX:XX）
[详细分镜，同上格式]

---

🎭 选角指导
[气质关键词 | 外形要求 | 测试标准]

---

🎞️ 视听制作指导
[色调 | 镜头 | 灯光 | 配乐 | 关键音效节点]

---

💡 创意逻辑注解
[设计选择说明 | 传播风险 | 不可删减的核心]

---

输出长度要求：

分镜脚本部分必须详尽，不得用"此处展开叙事"等模糊语言替代
台词必须完整写出，不得用省略号代替
每个导演注记必须具体到演员动作幅度、镜头焦距感、音效细节
总输出长度预期在2000～4000字之间
禁止出现以下内容：

"广告将展示产品如何解决问题"（过于抽象）
任何形式的功能罗列
品牌在故事中段的强行出现
</OutputFormat>

<example_output>

示例：某除臭袜品牌的泰式广告方案（节选结构示范）
---

🎯 人性洞察
每一个在公共场合脱鞋的成年男性内心深处都知道，那股气味已经让周围人窒息了三秒——但他们宁愿假装若无其事地挠挠脚，也绝对不会主动说"对不起，是我的袜子"。

---

🎭 情绪类型 & 反转公式
选择【公式2：隐喻字面量化】

理由：产品卖点"除臭"是具体的嗅觉功能，最适合将其转化为一场当事人视角的荒诞真人秀。让"臭味"变成一个有情感、有家庭、有求生欲的拟人角色，观众在爆笑的同时深度记忆产品功效。

情绪落差预期：极高（从莫名其妙的荒诞开场，到会心一笑的产品领悟）

---

🎬 逐秒级分镜脚本（节选）
第一幕：建立假象（00:00 - 00:18）
时间码	画面	台词/音效	导演注记
00:00	西装革履的办公室。特写：一双穿着光鲜皮鞋的脚走进会议室。镜头跟拍，暖光，构图精良，像极了商业精英纪录片。	背景：严肃的钢琴曲	前4秒绝对不能露出任何"广告感"，观众必须以为在看商业精英纪录片
00:05	主角在会议桌旁坐下，随意把皮鞋踢掉，解放双脚。极其自然放松的动作。	音效：皮鞋落地的笃笃声	演员动作要自然到像是无意识的习惯动作，不能有任何表演感
00:08	镜头缓缓下移，对准那双已经脱下的袜子。定格2秒。	音效：钢琴曲骤然停止。完全静默。	Dead Air 第一次启动：这2秒的静默是整支广告第一个笑点的化学反应期
00:10	突然切换视角：袜子里面——一个穿着臭烘烘破西装、头发凌乱的小人（比例约20cm高），从黑暗中惊慌失措地探出头来，像刚从长途火车厕所里逃出来的难民。	小人（用力吸气）："终于！终于可以呼吸了！" 音效：夸张的大口喘气声	小人的造型必须邋遢到令人发指：油腻的领带、黑眼圈、满脸的汗珠。绝对不能可爱化，要真实地"臭"
[...后续分镜省略，仅作结构示范...]

---

💡 创意逻辑注解
为何选择这个洞察：除臭产品的受众羞耻感是全球性的，但泰式广告要求本土市场的"社交场合面子文化"——在东南亚，在他人面前承认自己身体有气味，是一种极度的社交失格。这个洞察比"袜子有异味很尴尬"更深一层。
不可删减的核心：第一幕的 Dead Air 静默时刻（00:08-00:10）。这2秒是全片唯一的"情绪蓄力泵"，删掉之后小人登场的笑点会从"爆笑"降级为"轻笑"。
最大传播风险：小人造型如果做得太精致（变成 CGI 卡通），会立刻让广告"可爱化"，失去泰式广告特有的荒诞粗粝感，变成儿童产品广告。必须坚持实拍皮套+化妆，越粗糙越真实越好。
</example_output>"""

CASES = [
    {"case_id": "ad001", "brief": "去屑洗发水「清澄」：目标受众 25-35 岁职场新人，核心诉求“头皮自信”，情绪基调：幽默", "duration": "120秒", "tone": "幽默",
     "director_note": "输出像产品说明书：大段介绍去屑成分，完全违反“绝不像广告”铁律；品牌第一幕就出现。"},
    {"case_id": "ad002", "brief": "防滑地板「稳安」：家有老人 emphasizes 安全，诉求“守护每一步”，情绪基调：催泪", "duration": "90秒", "tone": "催泪",
     "director_note": "洞察停在功能层（防滑），没有剥到子女“不在场愧疚”的人性层。"},
    {"case_id": "ad003", "brief": "人寿保险「安程」：30-45 岁家庭支柱，诉求“爱与责任延续”，情绪基调：催泪", "duration": "180秒", "tone": "催泪",
     "director_note": "第二幕就让保险顾问出镜讲解条款，广告目的提前暴露；反转沦为说教。"},
    {"case_id": "ad004", "brief": "夜宵方便面「深夜面馆」：加班青年，诉求“犒劳认真生活的自己”，情绪基调：幽默+催泪混合", "duration": "150秒", "tone": "幽默",
     "director_note": "分镜写“此处展开叙事”，不是逐秒脚本，无法进 PPM。"},
    {"case_id": "ad005", "brief": "手机银行「简汇」：县域个体户，诉求“生意人的体面”，情绪基调：催泪", "duration": "120秒", "tone": "催泪",
     "director_note": "台词全是书面语和广告腔，没有市井口语；Deadpan 时刻缺失。"},
    {"case_id": "ad006", "brief": "公共洗手液「净手时刻」：写字楼白领，诉求“看不见的守护”，情绪基调：幽默", "duration": "60秒", "tone": "幽默",
     "director_note": "反转公式选择没有给出心理学依据；情绪落差预期缺失。"},
    {"case_id": "ad007", "brief": "宠物保险「毛伴」：都市独居养宠青年，诉求“它也是家人”，情绪基调：催泪", "duration": "120秒", "tone": "催泪",
     "director_note": "（考题）独立验证用：检验“不在场守护”类洞察是否可复用。"},
    {"case_id": "ad008", "brief": "旧手机回收「回声计划」：换机人群，诉求“每部手机都有下一段故事”，情绪基调：催泪", "duration": "150秒", "tone": "催泪",
     "director_note": "（考题）独立验证用：检验“物件因果叙事”是否成立。"},
]

DIMENSIONS = [
    {"name": "洞察深入", "anchors": {"0": "停在功能卖点层，无人性洞察", "1": "有痛点但停留在生活表层",
                                     "2": "剥到人性隐秘层", "3": "洞察精准新颖，一句话即令人心头一紧"}},
    {"name": "定位准确", "anchors": {"0": "没有受众/情绪定位，或定位错误", "1": "定位含糊，无法指导创作",
                                     "2": "定位基本准确", "3": "定位精准且反转公式与情绪落差匹配"}},
    {"name": "分镜可执行", "anchors": {"0": "无分镜或用“此处展开”糊弄", "1": "有分镜但非逐秒、不可拍",
                                       "2": "逐秒但缺台词/音效细节", "3": "逐秒且台词完整、导演注记具体到动作与焦距"}},
]


def main() -> int:
    print("== 泰式广告提示词优化演练（模拟生成，不调用真实模型）==\n")
    t_start = time.time()

    # ---- 第一步：准备材料 ----
    code, proj, dt = call("POST", "/projects", {
        "name": "泰式广告创意方案优化", "description": "以真实工作提示词为对象的优化演练",
        "task_type": "custom",
        "goal": "方案经常写成产品说明书、违反“绝不像广告”铁律；品牌在故事中段抢戏；分镜含糊不可拍。"
                "希望：洞察剥到人性隐秘层，反转公式有心理学依据，分镜逐秒可执行。",
        "contract": {
            "label": "泰式广告创意方案",
            "runtime_fields": [{"name": "brief", "label": "广告Brief"}, {"name": "duration", "label": "时长要求"},
                               {"name": "tone", "label": "情绪基调"}],
            "evaluation_fields": [{"name": "director_note", "label": "创意总监批注"}],
            "evaluation_unit": "一份可直接进PPM的创意方案",
            "primary_metric": "可直接进 PPM 的方案比例",
            "dimensions": DIMENSIONS,
        }})
    expect("1.1 创建自定义项目", code == 201, f"pid={proj.get('id')} fields={len(proj.get('contract', {}).get('runtime_fields', []))}", dt)
    pid = proj["id"]

    code, batch, dt = call("POST", f"/projects/{pid}/imports/preview", {
        "source_name": "8条真实Brief", "fmt": "jsonl",
        "content": "\n".join(json.dumps({
            "case_id": c["case_id"], "origin": "real",
            "runtime_input": {"brief": c["brief"], "duration": c["duration"], "tone": c["tone"]},
            "evaluation_only": {"director_note": c["director_note"]}}, ensure_ascii=False)
            for c in CASES)})
    expect("1.2 导入预览（8条Brief）", code == 200 and batch.get("valid") == 8,
           f"valid={batch.get('valid')} errors={len(batch.get('errors', []))}", dt)
    code, committed, dt = call("POST", f"/projects/{pid}/imports/{batch['id']}/commit", {})
    expect("1.3 提交导入", code == 200, f"valid={committed.get('valid')}", dt)

    code, items, dt = call("GET", f"/projects/{pid}/items?size=100")
    id_by_case = {it["case_id"]: it["id"] for it in items["items"]}
    dev_ids = [id_by_case[c["case_id"]] for c in CASES[:6]]
    sealed_ids = [id_by_case[c["case_id"]] for c in CASES[6:]]
    call("POST", f"/projects/{pid}/split", {"case_ids": dev_ids, "split": "dev"})
    call("POST", f"/projects/{pid}/split", {"case_ids": sealed_ids, "split": "sealed_test"})
    code, fr, dt = call("POST", f"/projects/{pid}/manifests/freeze", {"seed": 7})
    expect("1.4 锁定案例分组（练习6+考题2）", code == 200 and fr.get("sealed_test_items") == 2,
           f"groups={fr.get('groups')} sealed={fr.get('sealed_test_items')}", dt)

    # ---- 第二步：确认怎么评 ----
    code, rub, dt = call("POST", f"/projects/{pid}/rubrics")
    dims = [d["name"] for d in rub.get("schema", {}).get("dimensions", [])]
    expect("2.1 生成评价标准草案（契约维度）", code == 201 and set(dims) == {"洞察深入", "定位准确", "分镜可执行"},
           f"dims={dims}", dt)
    code, pub, dt = call("POST", f"/rubrics/{rub['id']}/publish")
    expect("2.2 发布评价标准", code == 200, f"v{pub.get('version_no')}", dt)
    code, rule, dt = call("POST", f"/projects/{pid}/rating_rules")
    expect("2.3 创建ABCD评级规则", code == 201, f"levels={[l['code'] for l in rule.get('levels', [])]}", dt)

    fb_specs = [
        ("ad001", "输出像产品说明书：大段介绍去屑成分，违反“绝不像广告”铁律；品牌第一幕就出现。",
         "AI 方案原文：第一幕即出现“清澄去屑因子”特写。", "品牌只应在最后20%出现，前面完全是故事。",
         "severe", ["广告腔", "品牌抢戏"]),
        ("ad002", "洞察停在功能层（防滑），没有剥到子女“不在场愧疚”的人性隐秘层。",
         "", "洞察句必须落到“最不敢承认的羞耻感/恐惧”。", "severe", ["洞察浅"]),
        ("ad004", "分镜写“此处展开叙事”，不是逐秒脚本，无法进 PPM。",
         "", "逐秒时间码 + 完整台词 + 具体导演注记。", "normal", ["分镜含糊"]),
    ]
    n_fb = 0
    t0 = time.time()
    for case_id, problem, quote, expected, sev, tags in fb_specs:
        code, fb, _ = call("POST", f"/projects/{pid}/expert_feedback", {
            "item_id": id_by_case[case_id], "problem": problem, "quote": quote,
            "expected": expected, "severity": sev, "status": "confirmed_error", "tags": tags,
            "remark": "演练批注：保留原文。"})
        n_fb += 1 if code == 201 else 0
    expect("2.4 登记专家意见（检查项）", n_fb == 3, f"{n_fb}/3 条（原话/期望/标签齐全）", time.time() - t0)

    # ---- 基线提示词（用户给的泰式广告提示词原文 + Brief 输入段）----
    body = THAI_PROMPT + ("\n\n【本次 Brief 输入】\n品牌/产品与核心诉求：{{brief}}\n"
                          "时长要求：{{duration}}\n情绪基调：{{tone}}\n"
                          "请严格按 OutputFormat 的结构与长度要求输出。")
    code, pv, dt = call("POST", f"/projects/{pid}/prompts", {
        "name": "泰式广告创意总监提示词", "body": body,
        "variables": ["brief", "duration", "tone"], "frozen_segments": [], "params": {}})
    expect("2.5 创建基线提示词（用户原文）", code == 201,
           f"长度={pv.get('length')}字 变量={pv.get('variables')}", dt)
    baseline_id = pv["id"]

    # ---- 第三步：原始测评 ----
    mans = call("GET", f"/projects/{pid}/manifests")[1]["manifests"]
    snapshot = {
        "mode": "explore", "prompt": {"baseline_id": baseline_id}, "rubric_id": rub["id"],
        "judge_id": None, "manifest_id": mans[0]["id"] if mans else "",
        "data": {"dev_item_ids": dev_ids, "select_item_ids": []},
        "models": {"generation": {"connection_id": "conn_mock"},
                   "evaluation": {"connection_id": "conn_mock"},
                   "optimizer": {"connection_id": "conn_mock"}},
        "optimization": {"max_rounds": 0, "dev_sample_size": 6, "min_delta": 0.05},
        "budget": {"mode": "token", "total_limit": 5_000_000, "search_limit": 4_000_000,
                   "acceptance_limit": 1_000_000},
    }
    t0 = time.time()
    code, run1, _ = call("POST", f"/projects/{pid}/runs", snapshot,
                         {"Idempotency-Key": "ex-baseline-1"})
    run1 = wait_run(run1["id"])
    dt = time.time() - t0
    ok1 = run1["state"] == "completed"
    expect("3.1 原始测评运行", ok1, f"state={run1['state']} score={run1.get('baseline_score')}", dt)
    RESULT["artifacts"]["baseline_run"] = {k: run1.get(k) for k in
                                           ("id", "state", "stop_reason", "baseline_score")}
    RESULT["artifacts"]["baseline_problems"] = run1.get("baseline_problems", {})
    detail = call("GET", f"/runs/{run1['id']}?detail=true")[1].get("baseline_detail", {})
    RESULT["artifacts"]["baseline_detail"] = {
        "score": detail.get("score"), "usable_rate": detail.get("usable_rate"),
        "severe": detail.get("severe"), "n": detail.get("n"),
        "items": [{"case_id": it.get("case_id"), "score": it.get("score"),
                   "usable": it.get("usable"), "severe": it.get("severe")}
                  for it in detail.get("items", [])]}

    # ---- 第四步：自动优化（用户原文基线）----
    snapshot["optimization"] = {"max_rounds": 3, "dev_sample_size": 6, "min_delta": 0.05,
                                "stall_rounds": 2, "length_limit_chars": len(body) + 2000}
    t0 = time.time()
    code, run2, _ = call("POST", f"/projects/{pid}/runs", snapshot,
                         {"Idempotency-Key": "ex-optimize-1"})
    run2 = wait_run(run2["id"])
    dt = time.time() - t0
    expect("4.1 自动优化运行（原文基线）", run2["state"] == "completed",
           f"state={run2['state']} stop={run2['stop_reason']} rounds={len(run2.get('rounds', []))}", dt)
    RESULT["artifacts"]["optimize_run_full_prompt"] = {
        "id": run2["id"], "stop_reason": run2["stop_reason"],
        "baseline_score": run2.get("baseline_score"), "stall_count": run2.get("stall_count"),
        "candidates": [{k: c.get(k) for k in ("candidate_id", "decision", "score", "rationale",
                                              "regressions", "length", "hypothesis")}
                       for c in run2.get("candidates", [])],
        "rounds": [{k: rd.get(k) for k in ("round_no", "status", "decision", "rationale",
                                           "hypothesis", "score", "prev_score")}
                   for rd in run2.get("rounds", [])]}

    # ---- 对照演练：简化基线（模拟未调优提示词）验证改进路径 ----
    code, pv2, dt = call("POST", f"/projects/{pid}/prompts", {
        "name": "泰式广告创意总监提示词",
        "body": "你是一位广告创意总监。请根据 Brief 输出一份泰式广告创意方案。",
        "variables": ["brief", "duration", "tone"], "frozen_segments": [],
        "params": {}, "parent_id": baseline_id,
        "hypothesis": "对照演练：未结构化的初始版本"})
    simple_id = pv2["id"]
    snapshot["prompt"] = {"baseline_id": simple_id}
    snapshot["optimization"] = {"max_rounds": 2, "dev_sample_size": 6, "min_delta": 0.05,
                                "stall_rounds": 2, "length_limit_chars": 4000}
    t0 = time.time()
    code, run3, _ = call("POST", f"/projects/{pid}/runs", snapshot,
                         {"Idempotency-Key": "ex-optimize-2"})
    run3 = wait_run(run3["id"])
    dt = time.time() - t0
    expect("4.2 对照优化运行（简化基线）", run3["state"] == "completed",
           f"state={run3['state']} stop={run3['stop_reason']} "
           f"score {run3.get('baseline_score')}→{max([c.get('score', 0) for c in run3.get('candidates', [])] or [0])}",
           dt)
    RESULT["artifacts"]["optimize_run_simple_prompt"] = {
        "id": run3["id"], "stop_reason": run3["stop_reason"],
        "baseline_score": run3.get("baseline_score"),
        "candidates": [{k: c.get(k) for k in ("candidate_id", "decision", "score", "usable_rate",
                                              "severe", "regressions", "fixed_problems", "length",
                                              "hypothesis", "rationale")}
                       for c in run3.get("candidates", [])],
        "rounds": [{k: rd.get(k) for k in ("round_no", "status", "decision", "rationale",
                                           "hypothesis", "score", "prev_score", "length_chars")}
                   for rd in run3.get("rounds", [])]}

    # ---- 第五步：验证与使用（对照运行：锁定候选 → 独立验证 → 采用）----
    kept = [c for c in run3.get("candidates", []) if c.get("decision") == "kept"]
    target = kept[0]["candidate_id"] if kept else "baseline"
    code, locked, dt = call("POST", f"/runs/{run3['id']}/lock", {"candidate_id": target})
    expect("5.1 锁定待验证版本", code == 200 and locked.get("locked_candidate") == target,
           f"locked={locked.get('locked_candidate')}", dt)
    t0 = time.time()
    code, rep, dt2 = call("POST", f"/runs/{run3['id']}/accept")
    ok5 = code == 202
    expect("5.2 独立验证（解封考题）", ok5, f"decision={rep.get('decision')}", time.time() - t0)
    RESULT["artifacts"]["acceptance_report"] = rep
    if rep.get("decision") == "verified_improvement":
        cand_pv = [c for c in run3["candidates"] if c["candidate_id"] == target][0]["prompt_version_id"]
        code, rel, _ = call("POST", f"/projects/{pid}/releases",
                            {"prompt_version_id": cand_pv, "report_ref": rep["id"], "mode": "active"})
        expect("5.3 正式采用（绑定验证报告）", code == 201, f"release={rel.get('id')}", 0)
        rel_id = rel.get("id")
    else:
        code, rel, _ = call("POST", f"/projects/{pid}/releases",
                            {"prompt_version_id": baseline_id, "mode": "trial"})
        expect("5.3 保存试用（未见提升→不覆盖正式指针）", code == 201, f"release={rel.get('id')}", 0)
        rel_id = rel.get("id")
    code, fb, _ = call("POST", f"/releases/{rel_id}/feedback",
                       {"adoption": "direct", "edit_time": "10分钟", "reason": "演练：直接采用候选"})
    expect("5.4 使用反馈", code == 201, f"status={fb.get('status')}", 0)

    # ---- 数据面：账本、审计、进度 ----
    code, led, _ = call("GET", f"/runs/{run3['id']}/ledger")
    RESULT["artifacts"]["ledger"] = {k: led.get(k) for k in ("consistent", "attempts", "known_tokens",
                                                             "unknown_tokens", "by_role")}
    code, prog, _ = call("GET", f"/projects/{pid}/progress")
    RESULT["artifacts"]["progress"] = {s["key"]: {"done": s["done"], "explain": s["explain"]}
                                       for s in prog.get("steps", [])}
    RESULT["artifacts"]["project_id"] = pid
    RESULT["total_seconds"] = round(time.time() - t_start, 1)

    with open("data/exercise_result.json", "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=2)
    print(f"\n== 演练结束：{len(RESULT['steps'])} 步，{RESULT['total_seconds']}s，"
          f"问题 {len(RESULT['issues'])} 个 ==")
    for i in RESULT["issues"]:
        print(f"  [问题] {i}")
    print(f"结果文件：data/exercise_result.json；项目ID：{pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
