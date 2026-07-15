import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.service_lifecycle_notify import notify_failure, notify_start


class ServiceLifecycleNotifyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "service-failure.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normal_stop_does_not_notify_or_create_state(self):
        notifier = Mock(return_value=True)

        notified = notify_failure(
            service_result="success",
            exit_code="exited",
            exit_status="0",
            state_file=self.state_file,
            notifier=notifier,
        )

        self.assertFalse(notified)
        self.assertFalse(self.state_file.exists())
        notifier.assert_not_called()

    def test_failure_persists_context_and_sends_notification(self):
        notifier = Mock(return_value=True)

        notified = notify_failure(
            service_result="signal",
            exit_code="killed",
            exit_status="9",
            state_file=self.state_file,
            notifier=notifier,
            hostname="target-host",
            now="2026-07-15 14:00:00 CST",
        )

        self.assertTrue(notified)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual("signal", state["service_result"])
        self.assertEqual("killed", state["exit_code"])
        self.assertEqual("9", state["exit_status"])
        title, content = notifier.call_args.args[:2]
        self.assertIn("服务异常", title)
        self.assertIn("target-host", content)
        self.assertIn("signal", content)

    def test_start_after_failure_notifies_recovery_and_removes_state(self):
        self.state_file.write_text(
            json.dumps(
                {
                    "service_result": "signal",
                    "exit_code": "killed",
                    "exit_status": "9",
                    "failed_at": "2026-07-15 14:00:00 CST",
                    "hostname": "target-host",
                }
            ),
            encoding="utf-8",
        )
        notifier = Mock(return_value=True)

        notified = notify_start(
            state_file=self.state_file,
            notifier=notifier,
            hostname="target-host",
            now="2026-07-15 14:00:07 CST",
        )

        self.assertTrue(notified)
        self.assertFalse(self.state_file.exists())
        title, content = notifier.call_args.args[:2]
        self.assertIn("服务已恢复", title)
        self.assertIn("2026-07-15 14:00:00 CST", content)
        self.assertIn("2026-07-15 14:00:07 CST", content)

    def test_recovery_keeps_state_when_notification_fails(self):
        self.state_file.write_text(
            json.dumps({"service_result": "exit-code"}),
            encoding="utf-8",
        )
        notifier = Mock(return_value=False)

        notified = notify_start(
            state_file=self.state_file,
            notifier=notifier,
        )

        self.assertFalse(notified)
        self.assertTrue(self.state_file.exists())

    def test_start_without_failure_state_sends_started_notification(self):
        notifier = Mock(return_value=True)

        notified = notify_start(
            state_file=self.state_file,
            notifier=notifier,
            hostname="target-host",
            now="2026-07-15 14:00:07 CST",
        )

        self.assertTrue(notified)
        title, content = notifier.call_args.args[:2]
        self.assertIn("服务已启动", title)
        self.assertIn("target-host", content)


if __name__ == "__main__":
    unittest.main()
