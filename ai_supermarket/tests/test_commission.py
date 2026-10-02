"""来源：本项目原创。费率期限、退款守恒和现有报价兼容性。"""

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import unittest

from ai_supermarket._commission import _fee_amounts, _intro_end, _rate_for_terms
from ai_supermarket.commerce import fee_breakdown, POLICY_VERSION, LEGACY_POLICY_VERSION
from ai_supermarket.common import MarketError


class CommissionTests(unittest.TestCase):
    def test_standard_and_intro_boundaries(self):
        start = date(2026, 9, 23)
        self.assertEqual(1000, _rate_for_terms('standard', start))
        for on, expected in [(date(2026, 9, 22), 1000), (start, 500),
                             (date(2027, 3, 22), 500), (date(2027, 3, 23), 1000)]:
            self.assertEqual(expected, _rate_for_terms('introductory', on, starts_on=start))

    def test_six_calendar_months_clamp_month_end_and_leap_year(self):
        self.assertEqual(date(2027, 2, 28), _intro_end(date(2026, 8, 31)))
        self.assertEqual(date(2028, 2, 29), _intro_end(date(2027, 8, 31)))
        self.assertEqual(date(2027, 3, 23), _intro_end(date(2026, 9, 23)))
        with self.assertRaises(MarketError):
            _intro_end(date(9999, 12, 31))

    def test_negotiated_rate_and_expiry(self):
        for rate in (300, 375, 500):
            for on, expected in [(date(2026, 1, 1), rate), (date(2027, 1, 1), 1000)]:
                self.assertEqual(expected, _rate_for_terms('negotiated', on,
                    starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1), negotiated_bps=rate))

    def test_no_inferred_or_stacked_discounts(self):
        on = date(2026, 1, 1)
        cases = [('unknown', {}), ('introductory', {}),
                 ('introductory', {'starts_on': on, 'negotiated_bps': 300}),
                 ('standard', {'starts_on': on}),
                 ('negotiated', {'starts_on': on, 'ends_on': on, 'negotiated_bps': 300}),
                 ('negotiated', {'starts_on': on, 'ends_on': date(2027, 1, 1)})]
        for tier, args in cases:
            with self.subTest(tier=tier, args=args), self.assertRaises(MarketError):
                _rate_for_terms(tier, on, **args)
        for bad in ('2026-01-01', datetime(2026, 1, 1), None, True):
            with self.assertRaises(MarketError):
                _rate_for_terms('standard', bad)

    def test_free_and_standard_sale_examples(self):
        for rate, expected in [(300, 300), (500, 500), (1000, 1000)]:
            self.assertEqual((0, 0, 0), _fee_amounts(0, 0, rate))
            self.assertEqual((10000, expected, 10000 - expected), _fee_amounts(10000, 0, rate))

    def test_rounding_against_decimal_oracle_and_conservation(self):
        for rate in (300, 375, 500, 800, 1000):
            for paid in list(range(201)) + [9999, 100_000_000]:
                net, fee, seller = _fee_amounts(paid, 0, rate)
                oracle = int((Decimal(paid) * rate / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                self.assertEqual(oracle, fee)
                self.assertEqual(paid, fee + seller)
                self.assertGreaterEqual(seller, 0)

    def test_split_refunds_replay_full_refund_and_original_rate(self):
        locked = _rate_for_terms('introductory', date(2026, 1, 1), starts_on=date(2026, 1, 1))
        self.assertEqual(1000, _rate_for_terms('introductory', date(2027, 1, 1), starts_on=date(2026, 1, 1)))
        fees = [_fee_amounts(10001, refund, locked)[1] for refund in (0, 1, 1, 5001, 10001)]
        self.assertEqual(500, fees[0])
        self.assertEqual(fees[1], fees[2])
        self.assertEqual(500, sum(a - b for a, b in zip(fees, fees[1:])))
        self.assertEqual(0, fees[-1])

    def test_invalid_amounts_and_rates(self):
        for args in [(True, 0, 500), (-1, 0, 500), (1, 2, 500), (1, -1, 500),
                     (100_000_001, 0, 500), (1, 0, True), (1, 0, 0),
                     (1, 0, 299), (1, 0, 501), (1, 0, 999), (1, 0, 1001)]:
            with self.subTest(args=args), self.assertRaises(MarketError):
                _fee_amounts(*args)

    def test_new_standard_and_legacy_policy_are_distinct(self):
        self.assertEqual({'currency': 'CNY', 'net_cents': 10000, 'commission_bps': 800,
            'platform_cents': 800, 'seller_before_other_fees_cents': 9200,
            'stall_fee_cents': 0, 'channel_fee_cents': None, 'policy_version': POLICY_VERSION,
            'settlement_policy': 'draft', 'real_payment': False}, fee_breakdown(10000))
        self.assertEqual(1000, fee_breakdown(10000, policy_version=LEGACY_POLICY_VERSION)['platform_cents'])
        with self.assertRaises(MarketError):
            fee_breakdown(10000, policy_version='unknown')

    def test_ninety_days_are_not_three_calendar_months(self):
        start = date(2026, 9, 30)
        self.assertEqual(date(2026, 12, 29), _intro_end(start, terms_version=2))
        for on, expected in [(date(2026, 9, 29), 800), (start, 500),
                             (date(2026, 12, 28), 500), (date(2026, 12, 29), 800)]:
            self.assertEqual(expected, _rate_for_terms('introductory', on,
                             starts_on=start, terms_version=2))
        with self.assertRaises(MarketError):
            _intro_end(date(9999, 12, 31), terms_version=2)
        for bad in (True, 0, 3, '2'):
            with self.assertRaises(MarketError):
                _rate_for_terms('standard', start, terms_version=bad)


if __name__ == '__main__':
    unittest.main()
