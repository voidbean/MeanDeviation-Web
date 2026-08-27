import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import watch_notify


class WatchNotifyTest(unittest.TestCase):
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
