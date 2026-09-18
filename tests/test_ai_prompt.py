"""个股分析提示词的持仓上下文与交易约束回归测试。"""
import unittest
from unittest.mock import patch

from core.strategy import build_ai_prompt, build_ai_system_prompt


class AiPromptTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "code": "600000", "name": "测试股票", "current_price": 114,
            "high": 115, "low": 112, "avg_price": 113,
            "upper_line": 117, "lower_line": 110,
            "cost_price": 116.5, "quantity": 100,
            "stage_high": 0, "stage_low": 0,
            "n20_high": 120, "n20_low": 100,
            "n60_high": 125, "n60_low": 95, "signal": "观察",
        }
        market = patch("services.market_context.load_market_context", return_value={})
        market.start()
        self.addCleanup(market.stop)

    def test_cost_and_quantity_reach_intraday_and_eod_prompts(self):
        for mode in ("intraday", "eod"):
            with self.subTest(mode=mode):
                prompt = build_ai_prompt(self.result, [], mode=mode)
                self.assertIn("持仓中，成本价 116.5", prompt)
                self.assertIn("持仓股数：100 股", prompt)
                self.assertIn("当日可卖数量：未提供", prompt)
                self.assertIn("总持仓不等于可卖数量", prompt)

    def test_missing_quantity_is_unknown_not_zero_or_inferred(self):
        del self.result["quantity"]
        for quantity in ("missing", None):
            if quantity is None:
                self.result["quantity"] = None
            with self.subTest(quantity=quantity):
                prompt = build_ai_prompt(self.result, [])
                self.assertIn("持仓股数：未知（未提供，不得推算股数）", prompt)

    def test_zero_and_odd_lot_quantities_are_preserved(self):
        for quantity in (0, 50, 150, 200):
            with self.subTest(quantity=quantity):
                self.result.update(quantity=quantity, cost_price=116.5 if quantity else 0)
                prompt = build_ai_prompt(self.result, [])
                self.assertIn(f"持仓股数：{quantity} 股", prompt)
                if quantity == 0:
                    self.assertIn("持仓状态：未持仓", prompt)

    def test_system_rules_apply_to_all_analysis_modes(self):
        with patch("core.strategy.load_skills", return_value="技能库内容"):
            for mode in ("intraday", "eod"):
                for holding in (True, False):
                    with self.subTest(mode=mode, holding=holding):
                        prompt = build_ai_system_prompt(mode, holding)
                        for rule in (
                            "下一交易日", "100股或其整数倍",
                            "零股余额须一次性卖出", "不得建议卖50股留50股",
                            "科创板、北交所", "不得一律套用100股规则",
                            "不得把总持仓当作可卖数量", "各笔卖出合计不得超过可卖数量",
                            "硬约束优先于下文技能库和分析模板",
                        ):
                            self.assertIn(rule, prompt)
                        self.assertTrue(prompt.endswith("技能库内容"))

    def test_eod_half_position_example_is_conditional(self):
        prompt = build_ai_prompt(self.result, [], mode="eod")
        self.assertIn("仅在持仓及可卖数量允许合法拆分时考虑减仓约50%", prompt)
        self.assertIn("否则明确无法分批", prompt)
        self.assertNotIn("回落___%触发，减仓约50%", prompt)


if __name__ == "__main__":
    unittest.main()
