"""来源：本项目原创。持久化本地问题记录；不会向客服或外部 AI 发送信息。"""
import hashlib
import json
import uuid

from .engine import WalletError, _text


class SupportDesk:
    def __init__(self, wallet):
        self.wallet = wallet

    @staticmethod
    def _load(db):
        row = db.execute('SELECT value FROM settings WHERE id=3').fetchone()
        return json.loads(row[0]) if row else []

    @staticmethod
    def _save(db, tickets):
        db.execute('INSERT INTO settings(id,value) VALUES(3,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                   (json.dumps(tickets, ensure_ascii=False),))

    @staticmethod
    def _public(ticket):
        return {key: value for key, value in ticket.items() if not key.startswith('_')}

    def list(self):
        with self.wallet._connection() as db:
            return [self._public(ticket) for ticket in reversed(self._load(db))]

    def create(self, body):
        if set(body) != {'order_id', 'category', 'description', 'idempotency_key'}:
            raise WalletError('问题记录需要订单、类别、说明和防重复编号')
        order_id = _text(body['order_id'], '订单编号', 100)
        key = _text(body['idempotency_key'], '防重复编号', 100)
        description = _text(body['description'], '问题说明', 1000, multiline=True)
        category = body['category']
        if category not in ('payment', 'delivery', 'refund', 'other'):
            raise WalletError('不支持的问题类别')
        digest = hashlib.sha256(json.dumps([order_id, category, description], ensure_ascii=False).encode()).hexdigest()
        with self.wallet._connection() as db:
            tickets = self._load(db)
            existing = next((ticket for ticket in tickets if ticket['_key'] == key), None)
            if existing:
                if existing['_hash'] != digest:
                    raise WalletError('相同编号不能用于不同问题', 'idempotency_conflict', 409)
                return self._public(existing)
            if len(tickets) >= 1000:
                raise WalletError('本地试点问题记录已达上限，请先导出记录并联系维护者', 'capacity_reached', 409)
            order = self.wallet._get(db, order_id)
            now = self.wallet._now().isoformat()
            ticket = {'id': uuid.uuid4().hex[:20], 'order_id': order_id, 'category': category,
                      'description': description, 'status': 'open', 'revision': 1,
                      'created_at': now, 'updated_at': now, 'resolution': '',
                      'order_snapshot': {key: order[key] for key in ('id', 'status', 'task_id', 'product_title', 'amount_cents')},
                      '_key': key, '_hash': digest}
            tickets.append(ticket)
            self._save(db, tickets)
            self.wallet._audit(db, 'support_opened', '创建本地问题记录 ' + ticket['id'], order_id, 'owner')
            return self._public(ticket)

    def update(self, ticket_id, body):
        if set(body) != {'status', 'resolution', 'expected_revision'}:
            raise WalletError('更新需要处理状态、说明和记录版本')
        if body['status'] not in ('open', 'in_progress', 'resolved'):
            raise WalletError('无效的问题处理状态')
        if type(body['expected_revision']) is not int:
            raise WalletError('记录版本应是整数')
        resolution = _text(body['resolution'], '处理说明', 1000, multiline=True)
        with self.wallet._connection() as db:
            tickets = self._load(db)
            ticket = next((item for item in tickets if item['id'] == ticket_id), None)
            if not ticket:
                raise WalletError('问题记录不存在', 'not_found', 404)
            if ticket['revision'] != body['expected_revision']:
                raise WalletError('问题记录已变化，请刷新后再处理', 'revision_conflict', 409)
            ticket.update(status=body['status'], resolution=resolution,
                          revision=ticket['revision'] + 1, updated_at=self.wallet._now().isoformat())
            self._save(db, tickets)
            self.wallet._audit(db, 'support_updated', '更新本地问题记录 ' + ticket_id + '：' + body['status'], ticket['order_id'], 'owner')
            return self._public(ticket)
