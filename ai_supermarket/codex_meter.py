"""来源：本项目原创。连接既有CodexModel、任务预算和计量；不估算账单或自动重试。"""

import hashlib
import json
import threading
import time

from .common import MarketError, compact, identifier, integer
from steward.codex_model import build_prompt, schema_for


def codex_usage(receipt, model_name):
    """Consume trusted adapter telemetry, never numbers in model-generated content."""
    if (not isinstance(receipt, dict) or receipt.get('provider') != 'codex'
            or receipt.get('completed') is not True or receipt.get('model') != model_name
            or type(receipt.get('completed_turns')) is not int or receipt['completed_turns'] != 1):
        raise MarketError('invalid_provider_receipt', '缺少匹配模型的受信完成记录；费用与用量需核对。')
    usage = receipt.get('usage')
    if not isinstance(usage, dict):
        raise MarketError('invalid_provider_receipt', '模型用量记录格式无效。')
    values = {}
    for name in ('input_tokens', 'output_tokens', 'cached_input_tokens'):
        value = usage.get(name)
        if value is not None:
            integer(value, 0, 10**15, name)
        values[name] = value
    if values['cached_input_tokens'] is not None and (
            values['input_tokens'] is None or values['cached_input_tokens'] > values['input_tokens']):
        raise MarketError('invalid_provider_receipt', '缓存用量不能超过总输入或在总输入未知时填写。')
    # The inspected CLI example exposes a separate reasoning count. Until a concrete
    # version establishes whether it overlaps output_tokens, do not invent a total.
    reasoning = usage.get('reasoning_output_tokens')
    if reasoning is not None:
        integer(reasoning, 0, 10**15, 'reasoning_output_tokens')
        if reasoning:
            values['output_tokens'] = None
    return values


class MeasuredCodexCalls:
    """Trusted host API. Own the supplied model exclusively; no model tools are exposed.

    The model is an existing steward.codex_model.CodexModel, not a product script.
    Tests may supply a deterministic double. This class does not grant account usage.
    """
    def __init__(self, model, meter, budget):
        if meter.principal != budget.principal:
            raise MarketError('measurement_scope_mismatch', '预算与计量主体必须一致。')
        self.model, self.meter, self.budget = model, meter, budget
        self._model_lock = threading.Lock()

    def _linked_call(self, reservation_id):
        with self.meter.store.connect() as db:
            rows = db.execute('''SELECT * FROM calls WHERE run_id=? AND task_id=? AND reference=? LIMIT 2''',
                (self.meter.run_id, self.budget.task_id, 'codex-budget:' + reservation_id)).fetchall()
        if len(rows) > 1:
            raise MarketError('measurement_conflict', '一个预留对应多个模型调用，须核对原始记录。')
        return dict(rows[0]) if rows else None

    def _unknown(self, reservation_id):
        try:
            self.budget.mark_unknown(reservation_id)
        except Exception:
            # Preserve the primary failure; durable in_flight still holds exposure.
            pass

    def run(self, request_id, role, context, max_micro_cny):
        # Queue before budget admission, so a waiting call cannot bypass a pause.
        with self._model_lock:
            return self._run(request_id, role, context, max_micro_cny)

    def _run(self, request_id, role, context, max_micro_cny):
        identifier(request_id, 'request_id')
        if role not in ('chat', 'project_manager'):
            raise MarketError('invalid_params', '此适配仅处理既有纯文本chat或project_manager调用。')
        if not isinstance(context, dict):
            raise MarketError('invalid_params', '模型上下文必须为对象。')
        # Freeze the exact request and bind it to the existing budget idempotency key.
        try:
            frozen = compact(context)
            frozen.encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise MarketError('invalid_params', '模型上下文需要有效JSON。') from exc
        if len(frozen.encode('utf-8')) > 80000:
            raise MarketError('invalid_params', '上下文过长，请缩小本次任务。')
        if self.model.effort != 'medium':
            raise MarketError('invalid_params', '现有模型适配固定medium，不能记录其他推理配置。')
        prompt = build_prompt(role, json.loads(frozen))
        spec = {'adapter': 'codex-meter-v1', 'role': role, 'context': json.loads(frozen), 'model': self.model.name,
                'effort': self.model.effort, 'run': self.meter.run_id,
                'measurement_path': str(self.meter.store.path),
                'prompt_hash': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                'schema': schema_for(role)}
        reference = 'codex:' + hashlib.sha256(compact(spec).encode('utf-8')).hexdigest()
        reserved = self.budget.reserve_micro(request_id, 'model', reference, max_micro_cny)
        rid = reserved['reservation_id']
        claim = self.budget.begin(rid)
        if not claim['execute']:
            call = self._linked_call(rid)
            return {'status': 'not_executed', 'budget': claim, 'call_id': call['id'] if call else None,
                    'next_action': 'reconcile_original_request', 'result_replayed': False}
        call_id, started = None, time.monotonic()
        try:
            call_id = self.meter.begin('model', 'codex-budget:' + rid, self.budget.task_id)
            self.model.last_receipt = None
            result = self.model.respond(role, spec['context'])
            usage = codex_usage(self.model.last_receipt, spec['model'])
            elapsed = max(0, round((time.monotonic() - started) * 1000))
            self.meter.end(call_id, 'succeeded', elapsed)
            self.meter.receipt(call_id, 'codex-host:' + call_id, **usage)
            self.budget.mark_unknown(rid)
        except BaseException:
            if call_id is not None:
                try:
                    self.meter.end(call_id, 'unknown', max(0, round((time.monotonic() - started) * 1000)))
                except Exception:
                    pass
            self._unknown(rid)
            raise
        return {'status': 'completed_cost_unknown', 'result': result, 'call_id': call_id,
                'budget': self.budget.status(rid), 'cost_micro_cny': None,
                'provider_bill_verified': False, 'next_action': 'reconcile_cost_without_reexecution'}

    def record_cost(self, reservation_id, call_id, actual_micro_cny, evidence_ref):
        """Trusted operator only. Reconcile supplied real evidence; never call the model."""
        identifier(reservation_id, 'reservation_id')
        identifier(call_id, 'call_id')
        identifier(evidence_ref, 'evidence_ref')
        integer(actual_micro_cny, 0, 10**15, 'actual_micro_cny')
        self.budget.status(reservation_id)  # Validate task and principal before either write.
        call = self._linked_call(reservation_id)
        if not call or call['id'] != call_id or call['kind'] != 'model':
            raise MarketError('measurement_scope_mismatch', '用量记录不属于此任务的模型预留。')
        if call['receipt_ref'] != 'codex-host:' + call_id or call['state'] != 'succeeded':
            raise MarketError('measurement_conflict', '缺少完成与遥测记录；先核对原调用，不自动补零。')
        if call['cost_micro_cny'] not in (None, actual_micro_cny):
            raise MarketError('measurement_conflict', '已知计量费用不同，不能覆盖。')
        # Two ledgers are not one transaction. A repeated reconciliation may repair
        # the second write, but a settled budget never dispatches another model call.
        settled = self.budget.settle_micro(reservation_id, actual_micro_cny, evidence_ref)
        self.meter.receipt(call_id, call['receipt_ref'], cost_micro_cny=actual_micro_cny)
        return {'status': 'reconciled', 'budget': settled, 'call_id': call_id,
                'provider_bill_verified': False, 'evidence_ref': evidence_ref}
