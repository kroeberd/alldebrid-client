import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *a, **kw: None,
        ClientSession=object,
        TCPConnector=lambda **kw: None,
        FormData=object,
        ClientError=Exception,
        ServerDisconnectedError=Exception,
        ClientConnectorError=Exception,
        ClientOSError=Exception,
    )

from services import flexget


class FlexGetScheduleTests(unittest.TestCase):
    def test_task_schedule_json_is_parsed(self):
        cfg = types.SimpleNamespace(
            flexget_task_schedules_json='[{"task":"movies","interval_minutes":60,"jitter_seconds":120,"enabled":true}]',
            flexget_schedule_minutes=0,
            flexget_jitter_seconds=0,
            flexget_tasks_raw="",
        )
        with patch("services.flexget._cfg", return_value=cfg):
            schedules = flexget.get_task_schedules()
        self.assertEqual(
            schedules,
            [{"task": "movies", "interval_minutes": 60, "jitter_seconds": 120, "enabled": True}],
        )

    def test_legacy_schedule_falls_back_to_configured_tasks(self):
        cfg = types.SimpleNamespace(
            flexget_task_schedules_json="[]",
            flexget_schedule_minutes=30,
            flexget_jitter_seconds=90,
            flexget_tasks_raw="movies, tv",
        )
        with patch("services.flexget._cfg", return_value=cfg):
            schedules = flexget.get_task_schedules()
        self.assertEqual(
            schedules,
            [
                {"task": "movies", "interval_minutes": 30, "jitter_seconds": 90, "enabled": True},
                {"task": "tv", "interval_minutes": 30, "jitter_seconds": 90, "enabled": True},
            ],
        )

    def test_legacy_schedule_without_named_tasks_runs_all(self):
        cfg = types.SimpleNamespace(
            flexget_task_schedules_json="[]",
            flexget_schedule_minutes=45,
            flexget_jitter_seconds=15,
            flexget_tasks_raw="",
        )
        with patch("services.flexget._cfg", return_value=cfg):
            schedules = flexget.get_task_schedules()
        self.assertEqual(
            schedules,
            [{"task": "*", "interval_minutes": 45, "jitter_seconds": 15, "enabled": True}],
        )


if __name__ == "__main__":
    unittest.main()
