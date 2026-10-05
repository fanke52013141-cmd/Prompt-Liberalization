"""Durable severity calibration jobs, created before any provider dispatch."""
import json
import inspect
import threading
import time
from contextlib import nullcontext

from .core import BizError, canonical_hash, new_id, now_iso
from .ledger import BudgetState, Ledger, validate_money_budget_needs_prices
from .severity_gold import SeverityGold
from prompt_core.severity_calibration import audit_severity, wilson_one_sided

_locks = {}
_workers = {}
_workers_lock = threading.Lock()
_MAX_AUDIT_WORKERS = 2
_PROVIDER_TIMEOUT_SECONDS = 120
_active_executions = set()


def statistics_binding():
    return canonical_hash({'audit':inspect.getsource(audit_severity), 'interval':inspect.getsource(wilson_one_sided)})


class SeverityAudits:
    def __init__(self, db):
        self.db = db

    def dispatch(self, aid):
        key=(str(self.db.path),aid)
        with _workers_lock:
            task=self.get(aid)
            worker=_workers.get(key)
            if worker and worker.is_alive():
                return task | {'worker_active':True,'idempotent':True}
            if task['state']=='completed':
                return task | {'worker_active':False,'idempotent':True}
            if task['state'] not in ('bound','paused_budget','paused_interrupted'):
                raise BizError('AUDIT_STATE_INVALID','当前审计状态不能派发',status=409)
            for old_key in list(_workers):
                if not _workers[old_key].is_alive():
                    del _workers[old_key]
            occupied=_active_executions | {k for k,w in _workers.items() if w.is_alive()}
            if len(occupied)>=_MAX_AUDIT_WORKERS:
                raise BizError('AUDIT_CAPACITY_BUSY','后台审计并发已满，请等待现有任务完成后再派发',status=409)
            def work():
                try:
                    self.execute(aid)
                except Exception:
                    # execute persists a safe paused state; never expose provider
                    # response or credentials through a worker exception log.
                    pass
            worker=threading.Thread(target=work,name=f'severity-{aid}',daemon=True)
            _workers[key]=worker
            worker.start()
            return task | {'worker_active':True,'idempotent':False}

    @staticmethod
    def drain_workers(timeout=_PROVIDER_TIMEOUT_SECONDS+5):
        """Let bounded provider calls settle before the service closes SQLite."""
        deadline=time.monotonic()+timeout
        with _workers_lock:
            workers=list(_workers.values())
        for worker in workers:
            worker.join(max(0,deadline-time.monotonic()))

    def create(self, jid, body):
        submitted = body.get('budget')
        if not isinstance(submitted, dict):
            raise BizError('BUDGET_REQUIRED', '创建严重检测审计任务须提供明确预算', status=422)
        # Fresh jobs start at zero. Never trust client-spent amounts or prices.
        budget = BudgetState({k:submitted[k] for k in ('mode','total_limit','search_limit','acceptance_limit') if k in submitted})
        budget.validate()
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            manifest = SeverityGold(self.db).audit_manifest(jid, body.get('build_gold_ids'), body.get('audit_gold_ids'))
            if manifest['model_config'].get('metric'):
                raise BizError('SEVERITY_EVALUATOR_INVALID','确定性质量指标不提供逐规则严重检测，不能执行模型严重审计',status=422)
            setting = conn.execute("SELECT value_json FROM settings WHERE key='prices'").fetchone()
            prices = json.loads(setting[0]) if setting else {}
            model = manifest['model_config']['model']
            validate_money_budget_needs_prices(budget.to_json(), {'evaluation':{'model':model}}, prices)
            if budget.mode == 'money':
                budget.prices = {model:dict(prices[model])}
            snapshot = {'manifest':manifest, 'initial_budget':budget.to_json(), 'statistics_binding':statistics_binding()}
            fingerprint = canonical_hash(snapshot)
            existing = conn.execute('SELECT id FROM severity_audits WHERE judge_id=? AND snapshot_hash=?', (jid,fingerprint)).fetchone()
            if existing:
                return self.get(existing[0]) | {'idempotent':True}
            audit_sources = {r['source_group'] for r in manifest['audit']}
            for prior in conn.execute('SELECT snapshot_json,snapshot_hash FROM severity_audits WHERE project_id=?', (manifest['project_id'],)):
                if canonical_hash(json.loads(prior['snapshot_json'])) != prior['snapshot_hash']:
                    raise BizError('SNAPSHOT_INTEGRITY_INVALID', '已占用审计来源的历史快照损坏，不能绕过历史重新申请', status=409)
                previous = json.loads(prior[0])['manifest']
                exposed = {r['source_group'] for partition in ('build','audit') for r in previous[partition]}
                if audit_sources & exposed:
                    raise BizError('AUDIT_SOURCE_ALREADY_USED', '新审计来源曾被其他已绑定任务用于构建或审计；请准备新来源，恢复须继续原任务', status=409)
            aid = new_id('saudit')
            stamp = now_iso()
            conn.execute('INSERT INTO severity_audits(id,judge_id,project_id,snapshot_json,snapshot_hash,budget_state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (aid,jid,manifest['project_id'],json.dumps(snapshot,ensure_ascii=False),fingerprint,
                          json.dumps(budget.to_json()),stamp,stamp))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),'system','severity_audit.bound',aid,fingerprint,stamp))
            return self.get(aid) | {'idempotent':False}

    def get(self, aid):
        row = self.db.one('SELECT * FROM severity_audits WHERE id=?', (aid,))
        if not row:
            raise BizError('NOT_FOUND','严重检测审计任务不存在',status=404)
        snapshot = json.loads(row['snapshot_json'])
        if canonical_hash(snapshot) != row['snapshot_hash']:
            raise BizError('SNAPSHOT_INTEGRITY_INVALID','审计快照校验失败',status=409)
        manifest = snapshot['manifest']
        return {k:row[k] for k in ('id','judge_id','project_id','snapshot_hash','state','error','revision','created_at','updated_at')} | {
            'budget':json.loads(row['budget_state_json']), 'metrics':json.loads(row['metrics_json']),
            'build_count':len(manifest['build']), 'audit_count':len(manifest['audit']),
            'rule_count':len(manifest['audit'][0]['context']['rules']),
            'policy':manifest['policy'], 'evaluator_binding':manifest['evaluator_binding']}

    def list(self, jid):
        if not self.db.one('SELECT id FROM judges WHERE id=?',(jid,)):
            raise BizError('NOT_FOUND','评价器不存在',status=404)
        return [self.get(row['id']) for row in self.db.query('SELECT id FROM severity_audits WHERE judge_id=? ORDER BY created_at DESC',(jid,))]

    def _verify(self, snapshot):
        if snapshot.get('statistics_binding') != statistics_binding():
            raise BizError('AUDIT_PROGRAM_CHANGED','审计统计程序变化或旧任务缺少绑定，不能重新解释旧协议',status=409)
        manifest=snapshot['manifest']
        current=SeverityGold(self.db).audit_manifest(manifest['judge_id'],
            [r['gold_id'] for r in manifest['build']], [r['gold_id'] for r in manifest['audit']])
        if current!=manifest:
            raise BizError('AUDIT_CONTEXT_CHANGED','金标、任务或模型配置已变化，不能继续原审计',status=409)

    def update_budget(self, aid, body):
        fields={'total_limit','search_limit','acceptance_limit'}
        if (not isinstance(body,dict) or set(body)!={'limits','revision'}
                or not isinstance(body['limits'],dict) or set(body['limits'])!=fields
                or type(body['revision']) is not int):
            raise BizError('BUDGET_UPDATE_INVALID','须提供三项新额度及当前revision',status=422)
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task=self.get(aid)
            if task['state']!='paused_budget':
                raise BizError('AUDIT_STATE_INVALID','只有预算暂停时可增加额度',status=409)
            if task['revision']!=body['revision']:
                raise BizError('REVISION_CONFLICT','任务已变化，请刷新后重试',status=409)
            current=BudgetState(task['budget'])
            Ledger._refresh(conn,aid,current)
            updated=BudgetState({**current.to_json(),**body['limits']})
            updated.validate()
            if any(getattr(updated,k)<getattr(current,k) for k in fields):
                raise BizError('BUDGET_CANNOT_DECREASE','不能减少已有额度',status=422)
            if all(getattr(updated,k)==getattr(current,k) for k in fields):
                raise BizError('BUDGET_NOT_INCREASED','须明确增加额度',status=422)
            # This task dispatches only evaluation calls in the search phase.
            if updated.acceptance_limit!=current.acceptance_limit:
                raise BizError('BUDGET_UPDATE_INVALID','审计任务不能修改独立验收预留',status=422)
            stamp=now_iso()
            conn.execute('UPDATE severity_audits SET budget_state_json=?,revision=revision+1,updated_at=? WHERE id=?',
                         (json.dumps(updated.to_json()),stamp,aid))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),'system','severity_audit.budget_increased',aid,
                          canonical_hash({'before':current.to_json(),'after':updated.to_json(),'revision':body['revision']}),stamp))
        return self.get(aid)

    def cancel(self, aid, body):
        if (not isinstance(body,dict) or set(body)!={'revision','reason'}
                or type(body['revision']) is not int or not isinstance(body['reason'],str)
                or not body['reason'].strip()):
            raise BizError('AUDIT_CANCEL_INVALID','停止须提供当前revision和原因',status=422)
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task=self.get(aid)
            if task['state']=='cancelled':
                return task | {'idempotent':True}
            if task['state']=='completed':
                raise BizError('AUDIT_STATE_INVALID','已完成任务不能停止',status=409)
            if task['revision']!=body['revision']:
                raise BizError('REVISION_CONFLICT','任务已变化，请刷新后重试',status=409)
            stamp=now_iso()
            conn.execute("UPDATE severity_audits SET state='cancelled',error='',revision=revision+1,updated_at=? WHERE id=?",(stamp,aid))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),'system','severity_audit.cancelled',aid,canonical_hash(body),stamp))
        return self.get(aid)

    def receipt_binding(self, aid):
        rows=self.db.query('SELECT logical_id,attempt_id,request_hash,status,response_json,actual_in,actual_out FROM ledger WHERE run_id=? ORDER BY logical_id,attempt_index',(aid,))
        return canonical_hash([dict(row) for row in rows])

    def activate(self, aid, body):
        if (not isinstance(body,dict) or set(body)!={'revision','reviewer','reason'}
                or type(body['revision']) is not int
                or any(not isinstance(body[k],str) or not body[k].strip() for k in ('reviewer','reason'))):
            raise BizError('AUDIT_ACTIVATION_INVALID','激活须提供当前revision、审核人及原因',status=422)
        from .judge_binding import admitted_for
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task=self.get(aid)
            if task['revision']!=body['revision']:
                raise BizError('REVISION_CONFLICT','任务已变化，请刷新后重试',status=409)
            evidence=self.verify_evidence(aid)
            if evidence['statistically_eligible'] is not True:
                raise BizError('SEVERITY_AUDIT_NOT_ELIGIBLE','独立严重审计未达到冻结准入门槛',status=422)
            snapshot=json.loads(conn.execute('SELECT snapshot_json FROM severity_audits WHERE id=?',(aid,)).fetchone()[0])
            manifest=snapshot['manifest']
            judge=conn.execute('SELECT * FROM judges WHERE id=?',(task['judge_id'],)).fetchone()
            if not admitted_for(self.db,judge,manifest['rubric_id'],manifest['model_config']):
                raise BizError('SCORE_CALIBRATION_REQUIRED','评分评价器须先取得对应配置的有效准入',status=422)
            reference={'audit_id':aid,'snapshot_hash':task['snapshot_hash'],'evaluator_binding':task['evaluator_binding'],
                       'receipt_binding':evidence['receipt_binding'],'eligible':True}
            metrics=json.loads(judge['metrics_json'] or '{}')
            if metrics.get('admission',{}).get('severe_admission') is True and metrics.get('severity_audit')==reference:
                return task | {'activated':True,'idempotent':True}
            metrics['severity_audit']=reference
            metrics['admission']['severe_admission']=True
            metrics.pop('severity_reactivation_required',None)
            conn.execute('UPDATE judges SET metrics_json=? WHERE id=?',(json.dumps(metrics,ensure_ascii=False),judge['id']))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),body['reviewer'],'severity_audit.activated',aid,
                          canonical_hash({'reference':reference,'reviewer':body['reviewer'],'reason':body['reason']}),now_iso()))
        return self.get(aid) | {'activated':True,'idempotent':False}

    def verify_evidence(self, aid):
        """Recheck live evidence; this diagnostic never grants admission."""
        with self.db._lock:
            return self._verify_evidence(aid)

    def _verify_evidence(self, aid):
        nested=self.db._conn.in_transaction
        with (nullcontext(self.db._conn) if nested else self.db.tx()) as conn:
            if not nested:
                conn.execute('BEGIN IMMEDIATE')
            task=self.get(aid)
            if task['state']!='completed':
                raise BizError('AUDIT_STATE_INVALID','只有已完成审计可核验准入证据',status=409)
            snapshot=json.loads(conn.execute('SELECT snapshot_json FROM severity_audits WHERE id=?',(aid,)).fetchone()[0])
            self._verify(snapshot)
            metrics=task['metrics']
            completion=conn.execute("SELECT payload_hash FROM audit_log WHERE action='severity_audit.completed' AND target=?",(aid,)).fetchall()
            if len(completion)!=1 or completion[0][0]!=canonical_hash(metrics) or metrics.get('receipt_binding')!=self.receipt_binding(aid):
                raise BizError('AUDIT_EVIDENCE_CHANGED','审计结果、完成记录或调用回执变化',status=409)
            manifest=snapshot['manifest']
            rules=list(manifest['audit'][0]['context']['rules'])
            schema=json.loads(conn.execute('SELECT schema_json FROM rubrics WHERE id=?',(manifest['rubric_id'],)).fetchone()[0])
            dimensions=[d['name'] for d in schema.get('dimensions',[])]
            for partition in ('build','audit'):
                records=metrics.get('records',{}).get(partition,[])
                expected=manifest[partition]
                if len(records)!=len(expected) or any(
                    record.get('gold_id')!=gold['gold_id'] or record.get('source_group')!=gold['source_group']
                    or record.get('human_gold')!=gold['labels'] for record,gold in zip(records,expected)):
                    raise BizError('AUDIT_EVIDENCE_CHANGED','审计统计记录与冻结人工金标不一致',status=409)
                from prompt_core.evaluation import validate_rubric_result
                from .engine import evaluation_messages, request_fingerprint, get_connection
                for record,gold in zip(records,expected):
                    config=manifest['model_config']
                    fingerprint=request_fingerprint('evaluation',config['model'],
                        evaluation_messages(schema,gold['context']['text'],gold['context']['task']['input'],gold['context']['task']['reference']),
                        config.get('params') or {},get_connection(config))
                    calls=conn.execute('SELECT status,response_json,request_hash FROM ledger WHERE run_id=? AND logical_id=? ORDER BY attempt_index DESC',
                        (aid,f"{aid}:{partition}:{gold['gold_id']}")).fetchall()
                    if any(r['request_hash']!=fingerprint for r in calls):
                        raise BizError('AUDIT_EVIDENCE_CHANGED','调用指纹与冻结请求正文不一致',status=409)
                    prediction={key:None for key in rules}
                    receipt=next((r for r in calls if r['status'] in ('ok','usage_unknown') and r['response_json']),None)
                    if receipt:
                        try:
                            response=json.loads(receipt['response_json'])
                            parsed=validate_rubric_result(json.loads(response['text']),dimensions,
                                output_text=gold['context']['text'],rules=gold['context']['rules'])
                            if response['finish']=='stop' and not parsed.get('abstain') and type(parsed.get('severe')) is bool:
                                found={v['rule_id'] for v in parsed.get('violations',[])}
                                prediction={key:key in found for key in rules}
                        except (ValueError,KeyError,TypeError):
                            pass  # Invalid/truncated model output remains unknown.
                    elif not calls or any(r['status']!='failed' for r in calls):
                        raise BizError('AUDIT_EVIDENCE_CHANGED','逐金标调用缺失或仍有未确认结果',status=409)
                    if record.get('prediction')!=prediction:
                        raise BizError('AUDIT_EVIDENCE_CHANGED','逐规则预测与原始模型响应不一致',status=409)
                computed=audit_severity(records,rules,manifest['policy'],build_sources=[] if partition=='build' else
                    [r['source_group'] for r in manifest['build']])
                if computed!=metrics.get(partition):
                    raise BizError('AUDIT_EVIDENCE_CHANGED','审计统计无法按冻结协议复算',status=409)
            return {'audit_id':aid,'evidence_valid':True,'statistically_eligible':metrics['audit']['eligible'],
                    'snapshot_hash':task['snapshot_hash'],'receipt_binding':metrics['receipt_binding'],
                    'note':'证据核验不等同正式准入，仍须评价器评分准入及正式采用流程'}

    def execute(self, aid):
        key=(str(self.db.path),aid)
        lock=_locks.setdefault((str(self.db.path),aid),threading.Lock())
        if not lock.acquire(blocking=False):
            raise BizError('AUDIT_ALREADY_RUNNING','此审计任务正在执行',status=409)
        with _workers_lock:
            if len(_active_executions)>=_MAX_AUDIT_WORKERS:
                lock.release()
                raise BizError('AUDIT_CAPACITY_BUSY','审计执行并发已满，请等待现有任务完成',status=409)
            _active_executions.add(key)
        try:
            return self._execute(aid)
        except Exception as exc:
            state='paused_budget' if isinstance(exc,BizError) and exc.code=='BUDGET_EXHAUSTED' else 'paused_interrupted'
            message=exc.message if isinstance(exc,BizError) else '审计中断，已保存响应和费用保留'
            self.db.execute("UPDATE severity_audits SET state=?,error=?,updated_at=?,revision=revision+1 WHERE id=? AND state='running'",
                            (state,message,now_iso(),aid))
            raise
        finally:
            with _workers_lock:
                _active_executions.discard(key)
            lock.release()

    def _execute(self, aid):
        from .engine import evaluate_once
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task=self.get(aid)
            if task['state']=='completed':
                return task | {'idempotent':True}
            if task['state'] not in ('bound','paused_budget','paused_interrupted'):
                raise BizError('AUDIT_STATE_INVALID','审计状态不允许执行',status=409)
            row=conn.execute('SELECT snapshot_json FROM severity_audits WHERE id=?',(aid,)).fetchone()
            snapshot=json.loads(row[0])
            self._verify(snapshot)
            conn.execute("UPDATE severity_audits SET state='running',error='',revision=revision+1,updated_at=? WHERE id=?",(now_iso(),aid))
        manifest=snapshot['manifest']
        budget=BudgetState(task['budget'])
        ledger=Ledger(self.db)
        rule_ids=list(manifest['audit'][0]['context']['rules'])
        records={}
        for partition in ('build','audit'):
            records[partition]=[]
            for gold in manifest[partition]:
                if self.get(aid)['state']=='cancelled':
                    return self.get(aid)
                self._verify(snapshot)
                result=evaluate_once(gold['context']['text'],manifest['rubric_id'],manifest['model_config'],aid,'search',
                    budget=budget,ledger=ledger,task_input=gold['context']['task']['input'],
                    evaluation_reference=gold['context']['task']['reference'],logical_id=f"{partition}:{gold['gold_id']}",
                    pause_on_unconfirmed=True,physical_attempts=1)
                prediction={key:None for key in rule_ids}
                if not result.get('abstain') and type(result.get('severe')) is bool:
                    found={v['rule_id'] for v in result.get('violations',[])}
                    prediction={key:key in found for key in rule_ids}
                records[partition].append({'gold_id':gold['gold_id'],'source_group':gold['source_group'],
                                           'human_gold':gold['labels'],'prediction':prediction})
        metrics={'build':audit_severity(records['build'],rule_ids,manifest['policy'],build_sources=[]),
                 'audit':audit_severity(records['audit'],rule_ids,manifest['policy'],
                                       build_sources=[r['source_group'] for r in manifest['build']]),
                 'records':records,'evaluator_binding':manifest['evaluator_binding'],
                 'statistics_binding':snapshot['statistics_binding'],
                 'receipt_binding':self.receipt_binding(aid),
                 'prediction_semantics':'validated violation IDs; unreported rules are negative detections; unknown remains unknown'}
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._verify(snapshot)
            current=conn.execute('SELECT state FROM severity_audits WHERE id=?',(aid,)).fetchone()
            if not current or current[0]!='running':
                raise BizError('AUDIT_STATE_INVALID','审计状态变化，不写入完成结果',status=409)
            conn.execute("UPDATE severity_audits SET state='completed',metrics_json=?,error='',revision=revision+1,updated_at=? WHERE id=?",
                         (json.dumps(metrics,ensure_ascii=False),now_iso(),aid))
            judge=conn.execute('SELECT metrics_json FROM judges WHERE id=?',(manifest['judge_id'],)).fetchone()
            judge_metrics=json.loads(judge[0] or '{}')
            judge_metrics['severity_audit']={'audit_id':aid,'snapshot_hash':task['snapshot_hash'],
                                            'evaluator_binding':manifest['evaluator_binding'],
                                            'eligible':metrics['audit']['eligible']}
            # Activation additionally needs verified ordinal admission and live
            # audit/gold evidence checks; never mint that from one stored boolean.
            judge_metrics.setdefault('admission',{})['severe_admission']=False
            conn.execute('UPDATE judges SET metrics_json=? WHERE id=?',(json.dumps(judge_metrics,ensure_ascii=False),manifest['judge_id']))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'),'system','severity_audit.completed',aid,canonical_hash(metrics),now_iso()))
        return self.get(aid)
