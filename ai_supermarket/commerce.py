"""来源：本项目原创。版本化佣金草案；精确整数计算，不执行支付或结算。"""

from ._commission import _fee_amounts
from .common import choice


LEGACY_POLICY_VERSION = 'commission-10-net-half-up-draft-v1'
POLICY_VERSION = 'commission-8-net-half-up-draft-v2'
MAX_PRICE_CENTS = 100_000_000


def fee_breakdown(paid_cents, refunded_cents=0, *, policy_version=POLICY_VERSION):
    """Cumulative net rounding makes a split refund equal to one aggregate refund."""
    choice(policy_version, (LEGACY_POLICY_VERSION, POLICY_VERSION), 'policy_version')
    rate = 1000 if policy_version == LEGACY_POLICY_VERSION else 800
    net, commission, seller = _fee_amounts(paid_cents, refunded_cents, rate)
    return {'currency': 'CNY', 'net_cents': net, 'commission_bps': rate,
            'platform_cents': commission, 'seller_before_other_fees_cents': seller,
            'stall_fee_cents': 0, 'channel_fee_cents': None, 'policy_version': policy_version,
            'settlement_policy': 'draft', 'real_payment': False}
