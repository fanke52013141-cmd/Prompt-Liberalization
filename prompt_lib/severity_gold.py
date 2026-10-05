"""Explicit human severity gold, bound to complete task and output evidence."""
import json

from .core import BizError, canonical_hash, new_id, now_iso, sha256_text
from .human_acceptance import output_is_blinded
from .judge_binding import evaluator_binding, freeze_evaluator
from prompt_core.evaluation import severity_rules


class SeverityGold:
    def __init__(self, db):
        self.db = db

    def context(self, pid, output_id, rubric_id):
        output = self.db.one('SELECT * FROM outputs WHERE id=? AND project_id=?', (output_id, pid))
        rubric = self.db.one("SELECT * FROM rubrics WHERE id=? AND project_id=? AND status='published'", (rubric_id, pid))
        if not output or not rubric:
            raise BizError('GOLD_CONTEXT_MISMATCH', '输出及已发布标准必须属于当前项目', status=422)
        if output_is_blinded(self.db, output_id):
            raise BizError('HUMAN_IDENTITY_BLINDED', '考试盲评尚未完成，不能用于金标标注', status=409)
        sealed = self.db.one("SELECT 1 FROM sealed_artifacts WHERE item_id=? AND access_state='sealed'", (output['item_id'],))
        if sealed:
            raise BizError('GOLD_SEALED_PROTECTED', '未使用的封存案例不能用于评价器构建或校准', status=409)
        if output['status'] != 'ok':
            raise BizError('GOLD_OUTPUT_INCOMPLETE', '严重检测校准金标须绑定完整输出', status=422)
        item = self.db.one('SELECT * FROM dataset_items WHERE id=? AND project_id=?', (output['item_id'], pid))
        if not item or not item['source_group_id']:
            raise BizError('GOLD_SOURCE_MISSING', '金标必须绑定明确来源的实际任务', status=422)
        task = {'input': json.loads(item['runtime_input_json']), 'reference': json.loads(item['evaluation_only_json']),
                'source_group': item['source_group_id'], 'split': item['split']}
        if item['split'] == 'sealed_test':
            job = self.db.one("SELECT manifest_json,manifest_hash,state FROM acceptance_jobs WHERE run_id=?", (output['run_id'],))
            if not job or job['state'] != 'completed' or canonical_hash(json.loads(job['manifest_json'])) != job['manifest_hash']:
                raise BizError('GOLD_SEALED_PROTECTED', '封存输出须有完整、已完成且未篡改的考试任务', status=409)
            artifact = next((a for a in json.loads(job['manifest_json']) if a['item_id'] == output['item_id']), None)
            if not artifact or artifact.get('source_group_id') != item['source_group_id']:
                raise BizError('GOLD_EVIDENCE_CHANGED', '原考试任务或来源组不一致', status=409)
            task['input'], task['reference'] = artifact['runtime_input'], artifact.get('evaluation_only', {})
        schema = json.loads(rubric['schema_json'])
        try:
            rules = severity_rules(schema)
        except ValueError as exc:
            raise BizError('SEVERITY_SCHEMA_INVALID', str(exc), status=422) from exc
        if not rules:
            raise BizError('SEVERITY_SCHEMA_INVALID', '须先定义严重问题规则', status=422)
        return {'output_id': output_id, 'rubric_id': rubric_id, 'text': output['text'],
                'rules': rules, 'task': task, 'output_hash': sha256_text(output['text']),
                'rubric_hash': canonical_hash(schema), 'case_hash': canonical_hash(task)}

    @staticmethod
    def _validated_gold(context, payload, adjudication=None):
        if not isinstance(payload, dict) or payload.get('confirmed_human_review') is not True:
            raise BizError('GOLD_HUMAN_REQUIRED', '须明确确认已逐条人工核验，模型预标注不能直接作金标', status=422)
        labels = payload.get('labels')
        if not isinstance(labels, dict) or set(labels) != set(context['rules']) or any(v is not None and type(v) is not bool for v in labels.values()):
            raise BizError('GOLD_LABEL_INVALID', '必须逐规则填写true/false/unknown(null)，不能用普通评分代替', status=422)
        for field in ('reviewer','reason'):
            if not isinstance(payload.get(field), str) or not payload[field].strip() or len(payload[field]) > 4000:
                raise BizError('GOLD_HUMAN_REQUIRED', '须填写人工评审身份及判断依据', status=422)
        for field in ('output_hash','rubric_hash','case_hash'):
            if payload.get(field) != context[field]:
                raise BizError('GOLD_EVIDENCE_CHANGED', '输出、标准或任务指纹不匹配，请重新加载', status=409)
        evidence = payload.get('evidence', [])
        if not isinstance(evidence, list):
            raise BizError('GOLD_EVIDENCE_INVALID', '证据须为列表', status=422)
        supported = set()
        for ev in evidence:
            if not isinstance(ev, dict) or not isinstance(ev.get('rule_id'), str) or ev['rule_id'] not in context['rules'] or labels[ev['rule_id']] is not True:
                raise BizError('GOLD_EVIDENCE_INVALID', '证据必须对应已标为严重的冻结规则', status=422)
            start, end, quote = ev.get('start'), ev.get('end'), ev.get('quote')
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(context['text']) or context['text'][start:end] != quote:
                raise BizError('GOLD_EVIDENCE_INVALID', '原文码点位置与引用不一致', status=422)
            supported.add(ev['rule_id'])
        if {k for k,v in labels.items() if v is True} != supported:
            raise BizError('GOLD_EVIDENCE_REQUIRED', '每个严重正例都须有可定位原文证据', status=422)
        gold = {k: context[k] for k in ('output_hash','rubric_hash','case_hash')}
        gold.update(labels=labels, reviewer=payload['reviewer'], reason=payload['reason'], evidence=evidence,
                    source_group=context['task']['source_group'])
        if adjudication:
            gold['adjudication'] = adjudication
        return gold

    def submit(self, pid, payload):
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            context = self.context(pid, payload.get('output_id'), payload.get('rubric_id'))
            gold = self._validated_gold(context, payload)
            existing = conn.execute("SELECT id,scores_json FROM annotations WHERE project_id=? AND output_id=? AND rubric_id=? AND source='human_severity' ORDER BY created_at DESC LIMIT 1",
                                    (pid, context['output_id'], context['rubric_id'])).fetchone()
            if existing:
                if json.loads(existing['scores_json']).get('severity_gold') != gold:
                    raise BizError('GOLD_LOCKED', '已确认的严重金标不可覆盖；更正须另行仲裁', status=409)
                return {'id':existing['id'], 'idempotent':True}
            aid = new_id('sgold')
            conn.execute("INSERT INTO annotations(id,project_id,output_id,rubric_id,source,gold_status,scores_json,evidence_json,purpose,annotator,submitted,created_at) VALUES(?,?,?,?,'human_severity','human_verified',?,?,'severity_calibration',?,1,?)",
                         (aid, pid, context['output_id'], context['rubric_id'], json.dumps({'severity_gold':gold}, ensure_ascii=False),
                          json.dumps(gold['evidence'], ensure_ascii=False), gold['reviewer'], now_iso()))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'), gold['reviewer'], 'severity_gold.confirmed', aid, canonical_hash(gold), now_iso()))
            return {'id':aid, 'idempotent':False}

    def adjudication_context(self, pid, aid):
        row = self.db.one("SELECT * FROM annotations WHERE id=? AND project_id=? AND source='human_severity'",
                          (aid, pid))
        if not row or row['gold_status'] not in ('human_verified','adjudicated'):
            raise BizError('GOLD_NOT_VERIFIED','只能仲裁已确认的人工严重金标',status=422)
        if self.db.one('SELECT id FROM annotations WHERE supersedes_id=? LIMIT 1',(aid,)):
            raise BizError('GOLD_SUPERSEDED','该金标已被仲裁结果取代，请载入最新记录',status=409)
        gold = json.loads(row['scores_json']).get('severity_gold')
        return self.context(pid,row['output_id'],row['rubric_id']) | {'gold_id':aid,'current_gold':gold}

    def adjudicate(self, pid, aid, payload):
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute("SELECT * FROM annotations WHERE id=? AND project_id=? AND source='human_severity'",
                                 (aid,pid)).fetchone()
            if not prior or prior['gold_status'] not in ('human_verified','adjudicated'):
                raise BizError('GOLD_NOT_VERIFIED','只能仲裁已确认的人工严重金标',status=422)
            if conn.execute('SELECT 1 FROM annotations WHERE supersedes_id=? LIMIT 1',(aid,)).fetchone():
                raise BizError('GOLD_SUPERSEDED','该金标已被仲裁结果取代，请载入最新记录',status=409)
            previous = json.loads(prior['scores_json']).get('severity_gold')
            action = 'severity_gold.adjudicated' if prior['gold_status']=='adjudicated' else 'severity_gold.confirmed'
            event = conn.execute('SELECT payload_hash FROM audit_log WHERE target=? AND action=?',(aid,action)).fetchone()
            if not previous or not event or event['payload_hash']!=canonical_hash(previous):
                raise BizError('GOLD_EVIDENCE_CHANGED','原金标或确认记录已变化，不能以损坏证据仲裁',status=409)
            context = self.context(pid,prior['output_id'],prior['rubric_id'])
            adjudication = {'supersedes_id':aid,'previous_hash':canonical_hash(previous)}
            gold = self._validated_gold(context,payload,adjudication)
            new_annotation_id = new_id('sgold')
            stamp=now_iso()
            conn.execute("INSERT INTO annotations(id,project_id,output_id,rubric_id,source,gold_status,scores_json,evidence_json,purpose,annotator,submitted,created_at,supersedes_id) VALUES(?,?,?,?,'human_severity','adjudicated',?,?,'severity_calibration',?,1,?,?)",
                         (new_annotation_id,pid,prior['output_id'],prior['rubric_id'],
                          json.dumps({'severity_gold':gold},ensure_ascii=False),
                          json.dumps(gold['evidence'],ensure_ascii=False),gold['reviewer'],stamp,aid))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),gold['reviewer'],'severity_gold.adjudicated',new_annotation_id,canonical_hash(gold),stamp))
            return {'id':new_annotation_id,'supersedes_id':aid,'idempotent':False}

    def audit_manifest(self, jid, build_ids, audit_ids):
        judge = self.db.one('SELECT * FROM judges WHERE id=?', (jid,))
        if not judge:
            raise BizError('NOT_FOUND', '评价器不存在', status=404)
        if not all(isinstance(refs, list) and refs and all(isinstance(i,str) for i in refs) and len(set(refs)) == len(refs) for refs in (build_ids,audit_ids)):
            raise BizError('GOLD_REFS_INVALID', '须明确提供非空且不重复的构建/审计金标ID', status=422)
        def collect(refs):
            records = []
            for aid in refs:
                ann = self.db.one("SELECT * FROM annotations WHERE id=? AND project_id=? AND rubric_id=? AND source='human_severity' AND gold_status IN ('human_verified','adjudicated')",
                                  (aid, judge['project_id'], judge['rubric_id']))
                if not ann:
                    raise BizError('GOLD_NOT_VERIFIED', '只能使用当前项目及标准下已确认的人工严重金标', status=422)
                if self.db.one('SELECT id FROM annotations WHERE supersedes_id=? LIMIT 1',(aid,)):
                    raise BizError('GOLD_SUPERSEDED','此金标已被仲裁结果取代，须使用最新金标',status=409)
                gold = json.loads(ann['scores_json'])['severity_gold']
                context = self.context(judge['project_id'], ann['output_id'], judge['rubric_id'])
                action = 'severity_gold.adjudicated' if ann['gold_status']=='adjudicated' else 'severity_gold.confirmed'
                event = self.db.one("SELECT payload_hash FROM audit_log WHERE target=? AND action=?", (aid,action))
                lineage_ok = True
                if ann['gold_status']=='adjudicated':
                    lineage=gold.get('adjudication') or {}
                    parent=self.db.one("SELECT * FROM annotations WHERE id=? AND project_id=? AND output_id=? AND rubric_id=? AND source='human_severity'",
                        (ann['supersedes_id'],judge['project_id'],ann['output_id'],judge['rubric_id']))
                    if parent:
                        parent_gold=json.loads(parent['scores_json']).get('severity_gold')
                        parent_action='severity_gold.adjudicated' if parent['gold_status']=='adjudicated' else 'severity_gold.confirmed'
                        parent_event=self.db.one('SELECT payload_hash FROM audit_log WHERE target=? AND action=?',(parent['id'],parent_action))
                        lineage_ok=bool(lineage.get('supersedes_id')==parent['id'] and parent_gold and
                            lineage.get('previous_hash')==canonical_hash(parent_gold) and parent_event and
                            parent_event['payload_hash']==canonical_hash(parent_gold))
                    else:
                        lineage_ok=False
                if any(gold[k] != context[k] for k in ('output_hash','rubric_hash','case_hash')) or not event or event['payload_hash'] != canonical_hash(gold):
                    raise BizError('GOLD_EVIDENCE_CHANGED', '金标原文、任务、标准或确认记录已变化', status=409)
                if not lineage_ok:
                    raise BizError('GOLD_EVIDENCE_CHANGED','仲裁链与前一金标记录不一致',status=409)
                records.append({'gold_id':aid, 'gold_hash':canonical_hash(gold), 'output_id':ann['output_id'],
                                'source_group':gold['source_group'], 'labels':gold['labels'], 'context':context})
            return records
        build, audit = collect(build_ids), collect(audit_ids)
        groups_b, groups_a = [r['source_group'] for r in build], [r['source_group'] for r in audit]
        if len(set(groups_b)) != len(groups_b) or len(set(groups_a)) != len(groups_a):
            raise BizError('AUDIT_DUPLICATE_SOURCE', '每个来源组只能贡献一条构建或审计金标', status=422)
        if set(groups_b) & set(groups_a):
            raise BizError('SOURCE_OVERLAP', '严重金标构建与审计来源必须隔离', status=422)
        config = freeze_evaluator(json.loads(judge['model_config_json']))
        from prompt_core.severity_calibration import audit_severity
        policy = config.get('severity_calibration_policy')
        rules = list(audit[0]['context']['rules'])
        preflight = audit_severity([{'source_group':r['source_group'], 'human_gold':r['labels'],
                                   'prediction':{key:None for key in rules}} for r in audit],
                                  rules, policy, build_sources=groups_b)
        if not preflight['per_rule']:
            raise BizError('SEVERITY_POLICY_INVALID', '严重检测审计门槛缺失或无效：' + ','.join(preflight['reasons']), status=422)
        return {'judge_id':jid, 'project_id':judge['project_id'], 'rubric_id':judge['rubric_id'],
                'model_config':config, 'evaluator_binding':evaluator_binding(self.db, judge['rubric_id'], config),
                'policy':policy, 'build':build, 'audit':audit}
