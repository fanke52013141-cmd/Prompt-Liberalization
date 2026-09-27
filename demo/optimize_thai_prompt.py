"""泰式广告提示词优化·操作脚本（一次性操作记录）。

重复执行会产生新的提示词版本（v8/v9…），请勿直接重跑；
如需复现优化，参考 docs/泰式广告提示词优化记录.md 的增补内容全文。
依赖 demo/thai_ad_exercise.py 的 BASE/call/wait_run/THAI_PROMPT，需本地服务已启动。
"""
import sys, json, time
sys.path.insert(0, "demo")
from thai_ad_exercise import BASE, call, wait_run, THAI_PROMPT

PID = "prj_33d02d5a258c48b9a655"
V1 = "pv_643965dc400d400ab"

# ---- 第 1 轮：把"禁令"变成"关卡"，把"逐秒"变成"硬格式" ----
ADD_R1 = """

<SelfCheck>
提交前的五道输出关卡（与正文冲突时以本节为准；任何一条不过，必须重写后再输出）：

□ L1 广告感检测：把方案前 80% 的每一句台词单独抽出来读——只要有一句"换成任何品牌都成立、且一听就是广告"，该句重写。台词标准：像偷拍到的真实对话，不像朗诵稿。
□ L2 品牌出场审计：品牌名、Logo、包装、产品名在总时长的 85% 之前累计出现不得超过 2 秒，且只能以道具/环境静物形式出现；故事中段出现品牌名或产品展示 = 直接不合格，整段重写。品牌只能出现在第四幕收网。
□ L3 说明书检测：全文不得出现成分、参数、功能对比、价格、渠道、促销信息。产品的功能只允许通过"故事的因果结果"被观众自己推断出来。
□ L4 逐秒完整性：分镜表禁止出现"此处展开""后续省略""等"等任何占位写法；每个镜头≤5 秒且四列齐全；台词必须完整写出，禁止以省略号结尾。
□ L5 洞察层级：人性洞察句必须落在"最不敢承认的羞耻/恐惧/渴望"层；停在"生活不方便""功能烦恼"层 = 不合格，退回 Step 2 重剥。

方案末尾附【自检记录】：L1—L5 逐条写"通过"或"重写 N 次后通过"，不许全写通过来应付。
</SelfCheck>

<StoryboardRules>
分镜硬性格式（OutputFormat 第 4 节的强制细则）：
- 表格固定四列：时间码（精确到秒）｜画面（含景别、机位、色调）｜台词/音效（台词逐字写出＋音效标注）｜导演注记（表演动作幅度、镜头焦距感、音效细节，三选三必填）。
- 单镜头时长≤5 秒；总镜头数≥总时长÷5。
- 必须显式标注：至少 1 处 Dead Air 冷场定格（精确到秒）、至少 1 处 Sound Drop 音效骤停（精确到秒）、反转触发的精确秒位。
- 每一幕开头用一行写明该幕任务与本幕"观众预期管理"目标。
</StoryboardRules>"""

# ---- 第 2 轮：洞察给"锚点+对照例"，公式选择给"排除法" ----
ADD_R2 = """

<InsightBar>
Step 2 三层剥皮法的合格判据（修订增补，优先于正文）：
- 检验标准："把洞察句里的产品删掉，剩下的仍然是一句成立的人性真相"——删掉产品就不成立的，是卖点，不是洞察。
- 三层对照例（以去屑洗发水为例）：
  ✗ 功能层："头皮屑让人尴尬"——这是产品卖点复述，0 分。
  ✗ 痛苦层："职场新人社交压力大"——这是处境描述，还没挖到人，1 分档。
  ✓ 人性隐秘层："每一个常穿深色衣服的年轻人内心深处都知道，肩上的白点同事早就看见了——但他宁愿换掉三件浅色外套，也不愿承认这份工作已经连按时洗头都保证不了。"——羞耻+自我掩饰+深层恐惧，3 分档。
- 输出的洞察句之后必须自标一层（功能/痛苦/隐秘），自标为前两层 = 不合格。
</InsightBar>

<FormulaDiscipline>
Step 3 反转公式选择改为排除法（防止默认套用单一公式）：
- 必须对四个公式逐一输出一行裁决："公式 N：不选——因为（产品属性/洞察类型不匹配的具体理由）"或"公式 N：选定——因为…"，四个都必须写，不许跳过。
- 选定的公式必须给出：情绪落差幅度（低/中/高/极高）、落差点（精确到秒）、以及"如果观众在第 10 秒就看穿这是广告，问题出在哪"的预防说明。
</FormulaDiscipline>"""

BRIEF_INPUT = ("\n\n【本次 Brief 输入】\n品牌/产品与核心诉求：{{brief}}\n"
               "时长要求：{{duration}}\n情绪基调：{{tone}}\n"
               "请严格按 OutputFormat 的结构与长度要求输出，并完成 SelfCheck 五道关卡后再提交。")

v2_body = THAI_PROMPT + ADD_R1 + BRIEF_INPUT
v3_body = THAI_PROMPT + ADD_R1 + ADD_R2 + BRIEF_INPUT

print("== 第 1 轮改写：禁令 → 五关自检 + 分镜硬格式 ==")
code, v2, dt = call("POST", f"/projects/{PID}/prompts", {
    "name": "泰式广告创意总监提示词", "body": v2_body,
    "variables": ["brief", "duration", "tone"], "frozen_segments": [], "params": {},
    "parent_id": V1, "origin": "manual",
    "hypothesis": "三个专家问题同源于'禁令只有口号没有执行机制'：新增提交前五关自检"
                  "（L1广告感/L2品牌出场审计/L3说明书检测/L4逐秒完整性/L5洞察层级）"
                  "与分镜硬性格式，把'绝不像广告'变成可核对的关卡。预期修复：广告腔、品牌抢戏、分镜含糊。"})
assert code == 201, v2
print(f"  v{v2['version_no']} 已登记 len={v2['length']} parent=v1  假设：把禁令变成可核对的关卡")

print("== 自评 v2：L5 只是'输出时拒收'，没教'怎么写出合格洞察'；洞察浅未根治 ==")
print("== 第 2 轮改写：洞察合格判据 + 三层对照例 + 公式排除法 ==")
code, v3, dt = call("POST", f"/projects/{PID}/prompts", {
    "name": "泰式广告创意总监提示词", "body": v3_body,
    "variables": ["brief", "duration", "tone"], "frozen_segments": [], "params": {},
    "parent_id": v2["id"], "origin": "manual",
    "hypothesis": "洞察浅的根因是三层剥皮法缺质量锚点：加'删掉产品仍成立才算洞察'判据与"
                  "功能/痛苦/隐秘三层对照例，并要求输出时自标层级；反转公式改排除法论证防偷懒。"
                  "预期修复：洞察停在功能层。"})
assert code == 201, v3
print(f"  v{v3['version_no']} 已登记 len={v3['length']} parent=v{v2['version_no']}  假设：洞察给锚点+对照例，公式给排除法")

print("== 模拟回归验证：以 v3 为基线跑原始测评（不调真实模型）==")
mans = call("GET", f"/projects/{PID}/manifests")[1]["manifests"]
dev_ids = [it["id"] for it in call("GET", f"/projects/{PID}/items?split=dev&size=100")[1]["items"]]
code, run, _ = call("POST", f"/projects/{PID}/runs", {
    "mode": "explore", "prompt": {"baseline_id": v3["id"]}, "rubric_id": call("GET", f"/projects/{PID}/rubrics")[1]["rubrics"][0]["id"],
    "judge_id": None, "manifest_id": mans[0]["id"],
    "data": {"dev_item_ids": dev_ids, "select_item_ids": []},
    "models": {"generation": {"connection_id": "conn_mock"}, "evaluation": {"connection_id": "conn_mock"},
               "optimizer": {"connection_id": "conn_mock"}},
    "optimization": {"max_rounds": 0, "dev_sample_size": 6, "min_delta": 0.05},
    "budget": {"mode": "token", "total_limit": 5_000_000, "search_limit": 4_000_000,
               "acceptance_limit": 1_000_000}}, {"Idempotency-Key": "opt-v3-smoke-1"})
run = wait_run(run["id"])
led = call("GET", f"/runs/{run['id']}/ledger")[1]
ok = run["state"] == "completed" and run["baseline_score"] >= 2.66 and led["consistent"]
print(f"  回归：state={run['state']} 模拟分={run['baseline_score']:.3f}（无回退，阈值2.667）"
      f" severe={run.get('baseline_problems', {}) and '见事件流' or '0'} 账本一致={led['consistent']} → {'通过' if ok else '失败'}")

print("== 试用发布 v3（试用不改正式指针）==")
code, rel, _ = call("POST", f"/projects/{PID}/releases",
                    {"prompt_version_id": v3["id"], "mode": "trial"})
print(f"  release={rel['id']} status={rel['status']}（正式采用需真实模型验证后绑定 verified 报告）")

print("\n版本链：v1 原文(4984) → v2 五关自检+分镜硬格式 → v3 洞察锚点+公式排除法"
      f"（len={v3['length']}）")
print(f"模拟回归：模拟分 {run['baseline_score']:.3f} 保持满分档、0 严重错误、账本一致——结构不变量无回退；"
      "语义效果需接真实模型验证（系统内一切生成为模拟）")
