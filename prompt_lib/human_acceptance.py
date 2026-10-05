"""Irreversible, per-output human decisions for a bound independent exam."""
import json
import secrets

from .core import BizError, canonical_hash, new_id, now_iso, sha256_text


def output_is_blinded(db, output_id):
    row = db.one('SELECT o.item_id,j.manifest_json,r.snapshot_json,j.state FROM outputs o '
                 'JOIN acceptance_jobs j ON j.run_id=o.run_id JOIN runs r ON r.id=o.run_id WHERE o.id=?', (output_id,))
    return bool(row and row['state'] != 'completed' and
                json.loads(row['snapshot_json']).get('acceptance', {}).get('evaluation_source') == 'human' and
                row['item_id'] in {art['item_id'] for art in json.loads(row['manifest_json'])})


class HumanAcceptance:
    def __init__(self, db):
        self.db = db

    def register(self, job_id, item_id, baseline, candidate):
        with self.db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM acceptance_jobs WHERE id=?", (job_id,)).fetchone()
            if job['state'] != 'running':
                raise BizError('EXAM_STATE_INVALID', '考试已停止或状态已变化', status=409)
            store = json.loads(job['review_json'])
            existing = store.get(item_id)
            records = []
            for side, output in (('baseline', baseline), ('candidate', candidate)):
                row = conn.execute('SELECT text,status FROM outputs WHERE id=?', (output['id'],)).fetchone()
                records.append({'side': side, 'output_id': output['id'],
                                'output_hash': sha256_text(row['text']), 'status': row['status']})
            if existing:
                if sorted(existing['outputs'], key=lambda r: r['side']) != sorted(records, key=lambda r: r['side']):
                    raise BizError('HUMAN_OUTPUT_CHANGED', '已绑定人工评审的输出发生变化', status=409)
                if any(r['rating']['context_hash'] != self.context_hash(job) for r in existing['ratings'].values()):
                    raise BizError('HUMAN_EVIDENCE_CHANGED', '人工评审后考试标准发生变化', status=409)
                return existing
            if secrets.randbits(1):
                records.reverse()
            entry = {'public_id': new_id('review'), 'outputs': records, 'ratings': {}}
            store[item_id] = entry
            conn.execute('UPDATE acceptance_jobs SET review_json=? WHERE id=?',
                         (json.dumps(store, ensure_ascii=False), job_id))
            return entry

    @staticmethod
    def context_hash(job):
        if canonical_hash(json.loads(job['manifest_json'])) != job['manifest_hash']:
            raise BizError('EXAM_MANIFEST_INVALID', '考试清单内容与指纹不一致', status=409)
        return canonical_hash({'binding': json.loads(job['binding_json']),
                               'manifest_hash': job['manifest_hash'],
                               'rubric': json.loads(job['rubric_json']),
                               'contract': json.loads(job['contract_json']), 'method': 'human-per-side-v2'})

    @staticmethod
    def rules(job):
        from prompt_core.evaluation import severity_rules
        try:
            return severity_rules(json.loads(job['rubric_json']))
        except ValueError as exc:
            raise BizError('SEVERITY_SCHEMA_INVALID', str(exc), status=422) from exc

    def view(self, rid):
        job = self.db.one('SELECT * FROM acceptance_jobs WHERE run_id=?', (rid,))
        if not job or job['state'] not in ('waiting_human', 'completed'):
            raise BizError('HUMAN_REVIEW_NOT_READY', '整批输出尚未准备完毕或考试已停止', status=409)
        store = json.loads(job['review_json'])
        if not store:
            raise BizError('HUMAN_REVIEW_NOT_READY', '此考试未配置人工最终评价', status=409)
        manifest = json.loads(job['manifest_json'])
        pairs = []
        for art in manifest:
            entry = store[art['item_id']]
            sides = {}
            for label, record in zip(('A', 'B'), entry['outputs']):
                output = self.db.one('SELECT text FROM outputs WHERE id=?', (record['output_id'],))
                if sha256_text(output['text']) != record['output_hash']:
                    raise BizError('HUMAN_OUTPUT_CHANGED', '人工评审输出校验失败', status=409)
                sides[label] = {'text': output['text'], 'output_hash': record['output_hash'],
                                'generation_status': record['status'],
                                'submitted': label in entry['ratings']}
            pairs.append({'public_id': entry['public_id'], 'task_input': art['runtime_input'],
                          'reference': art.get('evaluation_only', {}), 'sides': sides})
        return {'context_hash': self.context_hash(job), 'rubric': json.loads(job['rubric_json']),
                'severity_rules': self.rules(job),
                'contract': json.loads(job['contract_json']), 'pairs': pairs,
                'complete': all(len(entry['ratings']) == 2 for entry in store.values())}

    def submit(self, rid, public_id, label, payload):
        if label not in ('A', 'B') or not isinstance(payload, dict):
            raise BizError('HUMAN_RATING_INVALID', '必须指定A或B及完整评审', status=422)
        with self.db.tx() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT * FROM acceptance_jobs WHERE run_id=?', (rid,)).fetchone()
            if not job or job['state'] != 'waiting_human':
                raise BizError('EXAM_STATE_INVALID', '仅待人工评审的考试允许提交', status=409)
            store = json.loads(job['review_json'])
            entry = next((r for r in store.values() if r['public_id'] == public_id), None)
            if entry is None:
                raise BizError('NOT_FOUND', '评审配对不属于此考试', status=404)
            record = entry['outputs'][0 if label == 'A' else 1]
            output = conn.execute('SELECT text FROM outputs WHERE id=?', (record['output_id'],)).fetchone()
            if (payload.get('context_hash') != self.context_hash(job) or
                    payload.get('output_hash') != record['output_hash'] or
                    sha256_text(output['text']) != record['output_hash']):
                raise BizError('HUMAN_EVIDENCE_CHANGED', '标准或输出指纹不匹配，请重新加载', status=409)
            for field in ('usable', 'severe'):
                if field not in payload or payload[field] is not None and type(payload[field]) is not bool:
                    raise BizError('HUMAN_RATING_INVALID', '可用性和严重错误须为布尔值或unknown(null)', status=422)
            for field in ('reviewer', 'reason'):
                if not isinstance(payload.get(field), str) or not payload[field].strip() or len(payload[field]) > 4000:
                    raise BizError('HUMAN_RATING_INVALID', '须填写评审人及判断依据', status=422)
            if record['status'] != 'ok' and payload['usable'] is not False:
                raise BizError('HUMAN_RATING_INVALID', '生成失败或截断不能标为可用', status=422)
            categories = payload.get('categories', [])
            if not isinstance(categories, list) or any(not isinstance(c, str) or not c.strip() or len(c) > 200 for c in categories):
                raise BizError('HUMAN_RATING_INVALID', '问题类别须为非空字符串列表', status=422)
            evidence = payload.get('evidence', [])
            if not isinstance(evidence, list) or payload['severe'] is True and not evidence:
                raise BizError('HUMAN_EVIDENCE_REQUIRED', '严重错误须提供规则及输出原文证据', status=422)
            review_elapsed = payload.get('review_elapsed_seconds')
            if (review_elapsed is not None and
                    (type(review_elapsed) is not int or not 0 <= review_elapsed <= 6 * 60 * 60)):
                raise BizError('HUMAN_REVIEW_TIME_INVALID', '页面前台时长须为0到6小时内的整数秒', status=422)
            for ev in evidence:
                if not isinstance(ev, dict):
                    raise BizError('HUMAN_EVIDENCE_INVALID', '证据格式错误', status=422)
                start, end, quote = ev.get('start'), ev.get('end'), ev.get('quote')
                if (type(start) is not int or type(end) is not int or
                        not 0 <= start < end <= len(output['text']) or
                        not isinstance(quote, str) or output['text'][start:end] != quote or
                        not isinstance(ev.get('rule_id'), str) or not ev['rule_id'].strip()):
                    raise BizError('HUMAN_EVIDENCE_INVALID', '规则或原文码点位置不一致', status=422)
                if ev['rule_id'] not in self.rules(job):
                    raise BizError('HUMAN_RULE_INVALID', '严重问题须对应本场冻结标准中的规则编号', status=422)
            if payload['severe'] is not None and not self.rules(job):
                raise BizError('HUMAN_RULE_INVALID', '本场未定义严重问题标准，只能记录为无法判断', status=422)
            rating = {k: payload[k] for k in ('context_hash', 'output_hash', 'usable', 'severe', 'reviewer', 'reason')}
            rating.update(categories=categories, evidence=evidence,
                          review_elapsed_seconds=review_elapsed)
            old = entry['ratings'].get(label)
            if old:
                previous = dict(old['rating'])
                current = dict(rating)
                # Timer telemetry may advance between a lost response and an
                # identical retry; it does not change the human decision.
                previous.pop('review_elapsed_seconds', None)
                current.pop('review_elapsed_seconds', None)
                if previous != current:
                    raise BizError('HUMAN_RATING_LOCKED', '人工评审已锁定，不能看结果后修改', status=409)
                return {'submitted': True, 'idempotent': True}
            other_label = 'B' if label == 'A' else 'A'
            other = entry['ratings'].get(other_label)
            other_record = entry['outputs'][1 if label == 'A' else 0]
            if other and record['output_hash'] == other_record['output_hash'] and any(
                    rating[k] != other['rating'][k] for k in ('usable', 'severe')):
                raise BizError('HUMAN_IDENTICAL_CONFLICT', '同一输入下完全相同的输出不能得到互相矛盾的可用性或严重错误结论', status=409)
            entry['ratings'][label] = {'rating': rating, 'submitted_at': now_iso()}
            conn.execute('UPDATE acceptance_jobs SET review_json=?,updated_at=? WHERE id=?',
                         (json.dumps(store, ensure_ascii=False), now_iso(), job['id']))
            conn.execute('INSERT INTO audit_log(id,actor,action,target,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
                         (new_id('aud'), rating['reviewer'], 'acceptance.human_rating',
                          job['id'] + ':' + public_id + ':' + label, canonical_hash(rating), now_iso()))
            return {'submitted': True, 'idempotent': False}

    def decisions(self, entry):
        if len(entry['ratings']) != 2:
            return None
        return {record['side']: entry['ratings'][label]['rating']
                for label, record in zip(('A', 'B'), entry['outputs'])}

    def evidence(self, rid):
        job = self.db.one('SELECT * FROM acceptance_jobs WHERE run_id=?', (rid,))
        if not job:
            raise BizError('HUMAN_EVIDENCE_CHANGED', '人工验收任务不存在', status=409)
        store = json.loads(job['review_json'])
        manifest = json.loads(job['manifest_json'])
        context = self.context_hash(job)
        if set(store) != {art['item_id'] for art in manifest}:
            raise BizError('HUMAN_EVIDENCE_CHANGED', '人工评审未覆盖完整考试清单', status=409)
        for entry in store.values():
            if set(entry['ratings']) != {'A', 'B'}:
                raise BizError('HUMAN_EVIDENCE_CHANGED', '人工评审尚未全部提交', status=409)
            for label, record in zip(('A', 'B'), entry['outputs']):
                rating = entry['ratings'][label]['rating']
                output = self.db.one('SELECT text FROM outputs WHERE id=?', (record['output_id'],))
                target = job['id'] + ':' + entry['public_id'] + ':' + label
                audit = self.db.one("SELECT payload_hash FROM audit_log WHERE action='acceptance.human_rating' AND target=?", (target,))
                if (not output or sha256_text(output['text']) != record['output_hash'] or
                        rating['output_hash'] != record['output_hash'] or rating['context_hash'] != context or
                        (rating.get('review_elapsed_seconds') is not None and
                         (type(rating['review_elapsed_seconds']) is not int or
                          not 0 <= rating['review_elapsed_seconds'] <= 6 * 60 * 60)) or
                        not audit or audit['payload_hash'] != canonical_hash(rating)):
                    raise BizError('HUMAN_EVIDENCE_CHANGED', '人工评审原文、标准或审计记录不一致', status=409)
        side_count = 2 * len(manifest)
        ratings = [record['rating'] for entry in store.values() for record in entry['ratings'].values()]
        result = {'context_hash': context, 'review_hash': canonical_hash(store),
                  'side_count': side_count,
                  'reviewers': sorted({rating['reviewer'] for rating in ratings})}
        # Do not change the shape of evidence for immutable pre-telemetry reports.
        if any('review_elapsed_seconds' in rating for rating in ratings):
            elapsed_values = [rating.get('review_elapsed_seconds') for rating in ratings]
            timed_count = sum(value is not None for value in elapsed_values)
            result['review_duration'] = {
                'measurement': 'browser_foreground_page_seconds',
                'seconds': max(elapsed_values) if timed_count == side_count else None,
                'timed_side_count': timed_count,
                'side_count': side_count,
                'note': '浏览器页面可见且获焦时长估算；可能包含阅读停顿，不代表可核验的净工时。',
            }
        return result
