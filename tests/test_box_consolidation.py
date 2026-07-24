"""箱体震荡识别的关键边界测试。"""
import unittest

from services.indicators import _analyze_box_window, _is_valid_box


def _rows(closes: list[float]) -> list[tuple]:
    """构造无关字段最小化的 OHLC 数据。"""
    return [
        (f"2026-01-{i + 1:02d}", close, close + 5, close - 5, close, 1000)
        for i, close in enumerate(closes)
    ]


class BoxConsolidationTests(unittest.TestCase):
    def test_repeated_range_is_valid_box(self):
        # 30 日内反复测试 105 / 95，收盘均在区间内。
        closes = [100 + (2 if i % 2 else -2) for i in range(30)]
        item = _analyze_box_window(_rows(closes))

        self.assertIsNotNone(item)
        self.assertGreaterEqual(item["in_box_ratio"], 0.75)
        self.assertFalse(item["is_directional_trend"])
        self.assertTrue(_is_valid_box(item))

    def test_directional_trend_cannot_pass_as_box(self):
        closes = [100 + i * 0.5 for i in range(30)]
        item = _analyze_box_window(_rows(closes))

        self.assertIsNotNone(item)
        self.assertTrue(item["is_directional_trend"])
        self.assertFalse(_is_valid_box(item))

    def test_low_close_containment_cannot_pass_as_box(self):
        item = _analyze_box_window(_rows([100 + (2 if i % 2 else -2) for i in range(30)]))

        self.assertIsNotNone(item)
        item["in_box_ratio"] = 0.70
        self.assertFalse(_is_valid_box(item))

    def test_wide_range_cannot_pass_as_box(self):
        item = _analyze_box_window(_rows([100 + (2 if i % 2 else -2) for i in range(30)]))

        self.assertIsNotNone(item)
        item["box_height_pct"] = 26
        self.assertFalse(_is_valid_box(item))


if __name__ == "__main__":
    unittest.main()
