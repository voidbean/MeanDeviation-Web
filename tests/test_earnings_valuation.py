"""盈利校正估值的边界测试。"""
import unittest

from core.strategy import _calc_earnings_valuation


class EarningsValuationTests(unittest.TestCase):
    def test_uses_deducted_profit_growth_first(self):
        result = _calc_earnings_valuation(40, [{
            "end_date": "2026-06-30", "ann_date": "2026-08-01",
            "dt_netprofit_yoy": 50, "netprofit_yoy": 80,
            "q_sales_yoy": 30,
        }])
        self.assertEqual(result["growth_label"], "扣非净利润同比")
        self.assertEqual(result["peg"], 0.8)
        self.assertEqual(result["status"], "盈利支撑较强")

    def test_falls_back_to_parent_profit_growth(self):
        result = _calc_earnings_valuation(30, [{
            "end_date": "2026-06-30", "dt_netprofit_yoy": None,
            "netprofit_yoy": 20,
        }])
        self.assertEqual(result["growth_label"], "归母净利润同比")
        self.assertEqual(result["peg"], 1.5)

    def test_negative_growth_disables_peg(self):
        result = _calc_earnings_valuation(20, [{"dt_netprofit_yoy": -5}])
        self.assertNotIn("peg", result)
        self.assertEqual(result["status"], "盈利收缩")

    def test_extreme_growth_is_marked_as_low_base_risk(self):
        result = _calc_earnings_valuation(80, [{"dt_netprofit_yoy": 250}])
        self.assertNotIn("peg", result)
        self.assertEqual(result["status"], "高增长待核验")

    def test_acceleration_uses_previous_report(self):
        result = _calc_earnings_valuation(40, [
            {"dt_netprofit_yoy": 50},
            {"dt_netprofit_yoy": 35},
        ])
        self.assertEqual(result["acceleration"], 15)


if __name__ == "__main__":
    unittest.main()
