"""来源：本项目原创。内部佣金规则；不签约、不授权、不执行结算。

调用方必须从平台认可的供应商协议取得档位和日期，不能使用商品或模型
自行声明的优惠。协议与报价绑定由commission_agreements负责，本模块只计算。
"""

import calendar
from datetime import date, timedelta

from .common import MarketError, choice, integer


def _date(value, name):
    if type(value) is not date:
        raise MarketError('invalid_params', f'{name}需要平台业务日期。')
    return value


def _intro_end(starts_on, *, terms_version=1):
    _date(starts_on, 'starts_on')
    integer(terms_version, 1, 2, 'terms_version')
    if terms_version == 2:
        try:
            return starts_on + timedelta(days=90)
        except OverflowError:
            raise MarketError('invalid_params', '优惠结束日期超出范围。') from None
    index = starts_on.year * 12 + starts_on.month - 1 + 6
    year, month = divmod(index, 12)
    if year > 9999:
        raise MarketError('invalid_params', '优惠结束日期超出范围。')
    month += 1
    return date(year, month, min(starts_on.day, calendar.monthrange(year, month)[1]))


def _rate_for_terms(tier, on_date, *, starts_on=None, ends_on=None, negotiated_bps=None,
                    terms_version=1):
    """Select a rate for NEW orders; refunds must use the original frozen rate.

    Dates use the platform's Asia/Shanghai business calendar. Windows are
    start-inclusive/end-exclusive. A negotiated agreement takes one explicit
    tier; discounts never stack. This calculation does not prove agreement rights.
    """
    choice(tier, ('standard', 'introductory', 'negotiated'), 'tier')
    integer(terms_version, 1, 2, 'terms_version')
    standard = 800 if terms_version == 2 else 1000
    _date(on_date, 'on_date')
    if tier == 'standard':
        if any(v is not None for v in (starts_on, ends_on, negotiated_bps)):
            raise MarketError('invalid_params', '标准档不接受优惠条款。')
        return standard
    _date(starts_on, 'starts_on')
    if tier == 'introductory':
        if ends_on is not None or negotiated_bps is not None:
            raise MarketError('invalid_params', '首批优惠的期限和5%费率由条款版本固定。')
        ends_on, rate = _intro_end(starts_on, terms_version=terms_version), 500
    else:
        _date(ends_on, 'ends_on')
        integer(negotiated_bps, 300, 500, 'negotiated_bps')
        rate = negotiated_bps
    if ends_on <= starts_on:
        raise MarketError('invalid_params', '协议结束日期必须晚于开始日期。')
    return rate if starts_on <= on_date < ends_on else standard


def _fee_amounts(paid_cents, refunded_cents, locked_bps):
    """Pure cumulative calculation, not a refund ledger or a payment receipt."""
    integer(paid_cents, 0, 100_000_000, 'paid_cents')
    integer(refunded_cents, 0, paid_cents, 'refunded_cents')
    integer(locked_bps, 300, 1000, 'locked_bps')
    if locked_bps not in (800, 1000) and locked_bps > 500:
        raise MarketError('invalid_params', '仅支持标准8%、历史10%或已约定的3%～5%。')
    net = paid_cents - refunded_cents
    # Round the cumulative remaining amount, never each refund independently.
    platform = (net * locked_bps + 5000) // 10000
    return net, platform, net - platform
