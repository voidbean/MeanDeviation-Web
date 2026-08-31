import unittest

from services.watch_plan_context import is_buy_action, render_live_rule_message


class WatchPlanContextTests(unittest.TestCase):
    def test_replaces_stale_insufficient_cash_with_live_lots(self):
        message = "回踩支撑可观察买入，当前资金不足一手，只允许观察"
        rendered = render_live_rule_message(message, 50_000, 10)
        self.assertIn("观察买入", rendered)
        self.assertIn("最多可买50手", rendered)
        self.assertNotIn("不足一手", rendered)
        self.assertNotIn("只允许观察", rendered)

    def test_live_cash_can_turn_old_buy_plan_into_observe_only(self):
        message = "站稳后买入，最多可买20手（预留手续费）"
        rendered = render_live_rule_message(message, 999, 10)
        self.assertIn("不足1手", rendered)
        self.assertNotIn("最多可买20手", rendered)
        self.assertFalse(is_buy_action(rendered))

    def test_sell_message_is_not_decorated(self):
        message = "跌破风险线减仓"
        self.assertEqual(render_live_rule_message(message, 50_000, 10), message)

    def test_cancel_buy_is_not_reenabled_by_cash(self):
        message = "结构破坏，放弃买入"
        self.assertEqual(render_live_rule_message(message, 50_000, 10), message)


if __name__ == "__main__":
    unittest.main()
