"""来源：本项目原创。受信宿主接入助手；不是任意AI客户端的自动挂钩。"""

from .common import MarketError, identifier


class TaskResources:
    """Construct at the host's task boundary; one initial check, no model polling."""

    def __init__(self, market, task_id, goal, kind=None, purpose=None, budget=None, meter=None):
        self.market = market
        self.task_id = identifier(task_id, 'task_id')
        if budget is not None and (budget.task_id != self.task_id or budget.principal != market.principal):
            raise MarketError('budget_scope_mismatch', '宿主预算必须绑定当前任务及主体。')
        self.budget = budget
        if meter is not None and meter.principal != market.principal:
            raise MarketError('measurement_scope_mismatch', '计量主体须与宿主市场主体一致。')
        self.meter = meter
        self.purpose = market.default_purpose if purpose is None else purpose
        self.candidates = self._observe('search', 'market.begin_task',
                                       lambda: market.begin_task(self.task_id, goal, kind, self.purpose))

    def _observe(self, kind, reference, operation):
        if self.meter is None:
            return operation()
        result, _ = self.meter.observe(kind, reference, self.task_id, operation)
        return result

    def use(self, resource_id, version, request_id):
        """The caller/AI chooses the resource. The host verifies and loads only that item."""
        identifier(resource_id, 'resource_id')
        identifier(version, 'version')
        import hashlib
        reference = hashlib.sha256((resource_id + '@' + version).encode()).hexdigest()
        return self._observe('resource', reference, lambda: self._use(resource_id, version, request_id))

    def _use(self, resource_id, version, request_id):
        identifier(request_id, 'request_id')
        # Task namespace prevents one task reusing another task's logical action key.
        import hashlib
        from .common import compact
        key = hashlib.sha256(compact([self.task_id, request_id]).encode()).hexdigest()
        reservation, extra = None, 0
        acquisition_options = {}
        if hasattr(self.market, 'host_policy'):
            if hasattr(self.market, '_host_preflight'):
                compatibility, digest = self.market._host_preflight(resource_id, version, self.purpose)
                acquisition_options['expected_release_digest'] = digest
            else:
                compatibility = self.market.check_compatibility(resource_id, version, purpose=self.purpose)
            if compatibility['status'] != 'ready':
                return {'status': 'unavailable', 'error_code': compatibility['status'],
                        'compatibility': compatibility, 'next_action': 'stop_or_choose_other_resource'}
            extra = compatibility['extra_max_cents']
            if extra:
                if self.budget is None:
                    return {'status': 'unavailable', 'error_code': 'shared_budget_required',
                            'next_action': 'host_must_bind_approved_task_budget'}
        # Exchange binds the exact conditions used to compute this dependency
        # reservation. Existing logical requests must never silently drop binding.
        acquired = self.market.acquire(resource_id, version, key, purpose=self.purpose, **acquisition_options)
        if acquired['status'] != 'acquired':
            return acquired
        if extra:
            # Acquiring here grants only free/existing static rights; the market cannot perform paid purchases.
            # Reserve dependency cost after availability is proven, before returning content for host use.
            reference = hashlib.sha256(compact([resource_id, version, self.purpose]).encode()).hexdigest()
            reservation = self.budget.reserve('dependency:' + reference, 'dependency', reference, extra)
            if reservation['state'] in ('cancelled', 'in_flight', 'unknown'):
                return {'status': 'unavailable', 'error_code': 'budget_operation_' + reservation['state'],
                        'budget': reservation, 'next_action': 'host_must_check_original_operation'}
        result = self.market.read(acquired['acquisition_id'], self.task_id, key)
        if reservation:
            result['budget'] = reservation
        return result

    def checked(self, usage_id, validator, passed):
        return self._observe('feedback', 'market.record_check',
                             lambda: self.market.record_check(usage_id, validator, passed))
