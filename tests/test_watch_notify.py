import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import watch_notify


class WatchNotifyTest(unittest.TestCase):
    def test_followup_is_labelled_observation_not_rule_trigger(self):
        with patch.object(watch_notify, "_save_cursor"), patch.object(watch_notify, "system_notification") as notify:
            consumer = watch_notify.EventConsumer(0)
            item = {"id": 1, "name": "利通电子", "event_type": "breakout_followup", "priority": "observe",
                    "price": 116, "message": "持续走强，非买卖信号"}
            consumer.handle(item)
            consumer.handle(item)
            notify.assert_called_once()
            self.assertIn("持续走强·状态观察", notify.call_args.args[0])
            self.assertNotIn("规则触发", notify.call_args.args[0])

    def test_consumer_persists_cursor_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            cursor = Path(folder) / "cursor"
            notifications = []
            with patch.object(watch_notify, "STATE_DIR", Path(folder)), \
                 patch.object(watch_notify, "CURSOR_FILE", cursor), \
                 patch.object(watch_notify, "system_notification",
                              side_effect=lambda title, message, priority: notifications.append((title, message, priority))):
                consumer = watch_notify.EventConsumer(3)
                event = {"id": 4, "code": "000001", "name": "测试股", "event_type": "calibration",
                         "priority": "risk", "price": 10.2, "message": "减仓风控"}
                consumer.handle(event)
                consumer.handle(event)

            self.assertEqual(cursor.read_text(encoding="utf-8"), "4")
            self.assertEqual(len(notifications), 1)
            self.assertIn("校准", notifications[0][0])
            self.assertEqual(notifications[0][2], "risk")


if __name__ == "__main__":
    unittest.main()
