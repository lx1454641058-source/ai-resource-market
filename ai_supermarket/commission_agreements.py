"""来源：本项目原创。可信宿主协议登记和报价费率快照，不支付或签约。

登记入口仅供已有账本管理权限的本地宿主调用，不暴露给卖家/买方工具。
evidence_ref是宿主核验记录的引用，不因填入字符串就证明实际签约。
协议修订和报价快照利用已有meta表，在原报价事务内提交，无结构迁移。
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math

from ._commission import _fee_amounts, _intro_end, _rate_for_terms
from .commerce import LEGACY_POLICY_VERSION, POLICY_VERSION, fee_breakdown
from .common import MarketError, compact, fields, identifier, integer, parse_json, text


AGREEMENT_POLICY = 'commission-agreement-net-half-up-draft-v1'
BUSINESS_ZONE = timezone(timedelta(hours=8))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _prefix(seller):
    identifier(seller, 'seller')
    return 'commission_agreement:' + _hash(seller) + ':'


def _read(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return parse_json(row[0]) if row else None


def _business_date(timestamp):
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        raise MarketError('invalid_clock', '计费需要有效宿主时间。')
    try:
        return datetime.fromtimestamp(timestamp, BUSINESS_ZONE).date()
    except (ValueError, OverflowError, OSError):
        raise MarketError('invalid_clock', '计费时间超出范围。') from None


def _terms(record):
    fields(record, ('seller', 'agreement_id', 'revision', 'tier', 'starts_on', 'ends_on',
                    'negotiated_bps', 'evidence_ref'), ('terms_version',))
    identifier(record['seller'], 'seller')
    identifier(record['agreement_id'], 'agreement_id')
    integer(record['revision'], 1, 1_000_000, 'revision')
    text(record['evidence_ref'], 'evidence_ref', 400)
    dates = {}
    for name in ('starts_on', 'ends_on'):
        value = record[name]
        try:
            dates[name] = None if value is None else date.fromisoformat(value)
            if value is not None and dates[name].isoformat() != value:
                raise ValueError
        except (TypeError, ValueError):
            raise MarketError('invalid_params', '协议日期需要YYYY-MM-DD。') from None
    # Unversioned saved agreements retain their original six-month / 10% terms.
    args = {**dates, 'negotiated_bps': record['negotiated_bps'],
            'terms_version': record.get('terms_version', 1)}
    _rate_for_terms(record['tier'], date(2000, 1, 1), **args)
    return args


def record_agreement(store, seller, agreement_id, tier, evidence_ref, *, expected_revision=0,
                     starts_on=None, ends_on=None, negotiated_bps=None, terms_version=2):
    """Trusted local admin only. New immutable revision with compare-and-set.

    A replay of exactly the same revision is harmless and never moves the current
    pointer backwards. It does not authenticate or accept a supplier's contract.
    """
    integer(expected_revision, 0, 999_999, 'expected_revision')
    integer(terms_version, 1, 2, 'terms_version')
    record = dict(seller=seller, agreement_id=agreement_id, revision=expected_revision + 1,
                  tier=tier, starts_on=starts_on, ends_on=ends_on,
                  negotiated_bps=negotiated_bps, evidence_ref=evidence_ref)
    # Keep legacy replay byte-for-byte compatible; all new agreements default to v2.
    if terms_version == 2:
        record['terms_version'] = terms_version
    _terms(record)
    prefix = _prefix(seller)
    key = prefix + str(record['revision'])
    with store.connect(write=True) as db:
        current = _current(db, seller)
        old = _read(db, key)
        if old is not None:
            if _canonical(old) != _canonical(record):
                raise MarketError('agreement_conflict', '该协议修订已存在且内容不同。')
            return record
        if (current['revision'] if current else 0) != expected_revision:
            raise MarketError('revision_conflict', '供应商协议已变化。')
        db.execute('INSERT INTO meta VALUES (?,?)', (key, compact(record)))
        pointer = compact({'revision': record['revision'], 'sha256': _hash(record)})
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (prefix + 'current', pointer))
    return record


def _current(db, seller):
    prefix = _prefix(seller)
    pointer = _read(db, prefix + 'current')
    if pointer is None:
        # Missing pointers must not silently erase an existing discount/history.
        if db.execute('SELECT 1 FROM meta WHERE key LIKE ? LIMIT 1', (prefix + '%',)).fetchone():
            raise MarketError('agreement_corrupt', '供应商协议指针缺失。')
        return None
    fields(pointer, ('revision', 'sha256'))
    integer(pointer['revision'], 1, 1_000_000, 'revision')
    record = _read(db, prefix + str(pointer['revision']))
    if record is None or _hash(record) != pointer['sha256']:
        raise MarketError('agreement_corrupt', '供应商协议记录不完整。')
    _terms(record)
    if record['seller'] != seller or record['revision'] != pointer['revision']:
        raise MarketError('agreement_corrupt', '供应商协议归属不符。')
    return record


def fee_snapshot(db, seller, price_cents, timestamp):
    """Return compact fees, never expose the private evidence reference to buyers."""
    day = _business_date(timestamp)
    record = _current(db, seller)
    if record is None:
        return fee_breakdown(price_cents)
    args = _terms(record)
    rate = _rate_for_terms(record['tier'], day, **args)
    net, platform, supplier = _fee_amounts(price_cents, 0, rate)
    end = (_intro_end(args['starts_on'], terms_version=args['terms_version'])
           if record['tier'] == 'introductory' else args['ends_on'])
    boundaries = [datetime.combine(d, time(), BUSINESS_ZONE).timestamp()
                  for d in (args['starts_on'], end) if d is not None and d > day]
    return {'currency': 'CNY', 'net_cents': net, 'commission_bps': rate,
            'platform_cents': platform, 'seller_before_other_fees_cents': supplier,
            'stall_fee_cents': 0, 'channel_fee_cents': None, 'policy_version': AGREEMENT_POLICY,
            'settlement_policy': 'draft', 'real_payment': False,
            'agreement': {'seller': seller, 'id': record['agreement_id'], 'revision': record['revision'],
                          'sha256': _hash(record)},
            'valid_until': min(boundaries) if boundaries else None}


def freeze_quote_fee(db, quote_id, fee):
    if fee['policy_version'] == AGREEMENT_POLICY:
        db.execute('INSERT INTO meta VALUES (?,?)', ('quote_fee:' + quote_id, compact(fee)))


def quote_fee(db, row, seller, *, timestamp=None):
    """Read authoritative frozen fees; optionally validate an UNCONFIRMED quote.

    Omit timestamp for historical accounting. This does not prove an order was
    accepted/paid or a refund succeeded; those need their own channel ledger.
    """
    stored = _read(db, 'quote_fee:' + row['id'])
    if row['policy_version'] in (LEGACY_POLICY_VERSION, POLICY_VERSION):
        if stored is not None:
            raise MarketError('agreement_corrupt', '旧报价不应带新协议快照。')
        fee = fee_breakdown(row['price_cents'], policy_version=row['policy_version'])
    elif row['policy_version'] == AGREEMENT_POLICY:
        if stored is None or not isinstance(stored, dict):
            raise MarketError('agreement_corrupt', '报价费率快照缺失。')
        agreement = stored.get('agreement')
        fields(agreement, ('seller', 'id', 'revision', 'sha256'))
        integer(agreement['revision'], 1, 1_000_000, 'revision')
        record = _read(db, _prefix(seller) + str(agreement['revision']))
        _terms(record)
        if (record['seller'] != seller or agreement['seller'] != seller
                or _hash(record) != agreement['sha256'] or record['agreement_id'] != agreement['id']):
            raise MarketError('agreement_corrupt', '报价协议绑定不符。')
        # Reconstruct under the historical record, not the current supplier terms.
        args = _terms(record)
        rate = _rate_for_terms(record['tier'], _business_date(row['created_at']), **args)
        net, platform, supplier = _fee_amounts(row['price_cents'], 0, rate)
        base = {**fee_breakdown(row['price_cents']), 'commission_bps': rate,
                'platform_cents': platform, 'seller_before_other_fees_cents': supplier,
                'policy_version': AGREEMENT_POLICY, 'agreement': agreement,
                'valid_until': stored.get('valid_until')}
        end = (_intro_end(args['starts_on'], terms_version=args['terms_version'])
               if record['tier']=='introductory' else args['ends_on'])
        bounds = [datetime.combine(d,time(),BUSINESS_ZONE).timestamp() for d in (args['starts_on'],end)
                  if d is not None and d > _business_date(row['created_at'])]
        base['valid_until'] = min(bounds) if bounds else None
        if _canonical(base) != _canonical(stored):
            raise MarketError('agreement_corrupt', '报价佣金金额或有效期被改变。')
        fee = stored
    else:
        raise MarketError('policy_changed', '不支持的原报价费用规则。')
    if timestamp is not None:
        _business_date(timestamp)
        if not row['created_at'] <= timestamp < row['expires_at']:
            raise MarketError('quote_expired', '报价已过期或时间无效。')
        if _canonical(fee_snapshot(db, seller, row['price_cents'], timestamp)) != _canonical(fee):
            raise MarketError('policy_changed', '供应商协议或费率已变化，请重新核价。')
    return fee
