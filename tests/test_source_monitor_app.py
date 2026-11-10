import contextlib
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from apscheduler.schedulers.background import BackgroundScheduler


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web


class SourceMonitorAppTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = str(Path(directory.name) / "config.json")
        self.config = {
            "webui": {"username": "test", "password": "private-password"},
            "cookie": ["private-cookie"], "tasklist": [], "crontab": "0 8 * * *",
            "source": {"pansou": {"token": "private-pansou-token", "enable": False}},
        }
        self.patch("CONFIG_PATH", self.config_path)
        self.patch("config_data", copy.deepcopy(self.config))
        self.maintain = self.patch("maintain_sources", return_value={"checked": 1, "recovered": []})
        self.run_manager = self.patch("get_run_manager")
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        web.Config.write_json(self.config_path, self.config)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(web, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def scheduler(self):
        scheduler = BackgroundScheduler()
        self.patch("scheduler", scheduler)
        # Exercise APScheduler's real job definitions without starting its worker.
        patcher = patch.object(scheduler, "start")
        patcher.start()
        self.addCleanup(patcher.stop)
        return scheduler

    def test_status_requires_login_and_never_runs_probe(self):
        registry = self.patch("ChannelRegistry")
        response = web.app.test_client().get("/source_status")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json["success"])
        registry.assert_not_called()
        self.maintain.assert_not_called()

    def test_status_exposes_only_runtime_fields_and_saved_flags(self):
        self.config["source"].update({
            "monitor": {"enable": False},
            "channelsearch": {"enable": False, "auto_discover": False, "telegram_channels": ["custom_channel"]},
        })
        web.Config.write_json(self.config_path, self.config)
        registry = self.patch("ChannelRegistry")
        registry.return_value.status.return_value = {
            "last_discovery": 10, "next_discovery": 20, "discovery_reason": "请求超时",
            "token": "private-registry-token",
            "channels": [{"name": "example_channel", "status": "active", "reason": "",
                          "last_checked": 10, "next_check": 20, "cookie": "private-registry-cookie"}],
        }
        health = self.patch("SourceHealth")
        health.return_value.status.return_value = [
            {"host": "example.com", "reason": "请求超时", "next_check": 30,
             "status": "cooldown", "token": "private-health-token"}]
        response = self.client.get("/source_status")
        self.assertEqual(response.status_code, 200)
        data = response.json["data"]
        self.assertFalse(data["monitor_enabled"])
        self.assertFalse(data["auto_discover_enabled"])
        self.assertFalse(data["channel_search_enabled"])
        self.assertEqual(data["registry"]["channels"][0]["name"], "example_channel")
        self.assertEqual(set(data["sources"][0]), {"host", "reason", "next_check", "status"})
        self.assertEqual(set(data["registry"]["channels"][0]), {"name", "status", "reason", "last_checked", "next_check"})
        self.assertNotIn("private-", response.get_data(as_text=True))
        registry.assert_called_once_with(web.Config.search_health_path(self.config_path))
        self.maintain.assert_not_called()
        self.run_manager.assert_not_called()
        self.assertEqual(web.Config.read_json(self.config_path), self.config)

    def test_status_missing_database_and_options_are_safe_defaults(self):
        response = self.client.get("/source_status")
        self.assertEqual(response.status_code, 200)
        data = response.json["data"]
        self.assertTrue(data["monitor_enabled"])
        self.assertTrue(data["auto_discover_enabled"])
        self.assertEqual(data["sources"], [])
        self.assertEqual(data["registry"]["channels"], [])
        self.maintain.assert_not_called()

    def test_status_config_error_does_not_leak_credentials(self):
        with patch.object(web.Config, "read_json", side_effect=ValueError("private-cookie")):
            response = self.client.get("/source_status")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-cookie", response.get_data(as_text=True))

    def test_monitor_reads_latest_settings_and_does_not_create_run_history(self):
        self.config["source"]["channelsearch"] = {
            "enable": True, "auto_discover": False, "telegram_channels": ["custom_channel"]}
        web.Config.write_json(self.config_path, self.config)
        web.run_source_monitor()
        self.maintain.assert_called_once_with(self.config, web.Config.search_health_path(self.config_path))
        self.run_manager.assert_not_called()
        self.assertEqual(web.Config.read_json(self.config_path), self.config)
        for disabled in (False, "false"):
            self.config["source"]["monitor"] = {"enable": disabled}
            web.Config.write_json(self.config_path, self.config)
            web.run_source_monitor()
        self.assertEqual(self.maintain.call_count, 1)

    def test_monitor_failure_is_logged_without_secrets_and_next_tick_can_retry(self):
        self.maintain.side_effect = [RuntimeError("private-cookie"), {"checked": 0, "recovered": []}]
        with self.assertLogs(level="WARNING") as messages:
            web.run_source_monitor()
        self.assertIn("RuntimeError", messages.output[0])
        self.assertNotIn("private-cookie", messages.output[0])
        web.run_source_monitor()
        self.assertEqual(self.maintain.call_count, 2)

    def test_scheduler_keeps_first_probe_and_interval_when_cron_is_reloaded(self):
        scheduler = self.scheduler()
        before = datetime.now()
        self.assertTrue(web.reload_tasks())
        job = scheduler.get_job(web.SOURCE_MONITOR_JOB)
        self.assertEqual(job.func, web.run_source_monitor)
        self.assertEqual(job.trigger.interval.total_seconds(), 300)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)
        self.assertGreaterEqual((job.next_run_time.replace(tzinfo=None) - before).total_seconds(), 24)
        self.assertLessEqual((job.next_run_time.replace(tzinfo=None) - before).total_seconds(), 30)
        next_run = job.next_run_time
        web.config_data["crontab"] = "0 20 * * *"
        web.reload_tasks()
        self.assertIs(scheduler.get_job(web.SOURCE_MONITOR_JOB), job)
        self.assertEqual(job.next_run_time, next_run)
        self.assertEqual(sum(item.id == web.SOURCE_MONITOR_JOB for item in scheduler.get_jobs()), 1)
        self.maintain.assert_not_called()

    def test_scheduler_respects_monitor_disable_and_can_run_without_transfer_cron(self):
        scheduler = self.scheduler()
        web.reload_tasks()
        web.config_data["source"]["monitor"] = {"enable": False}
        web.reload_tasks()
        self.assertIsNone(scheduler.get_job(web.SOURCE_MONITOR_JOB))
        web.config_data["source"]["monitor"]["enable"] = True
        web.config_data["crontab"] = ""
        self.assertFalse(web.reload_tasks())
        self.assertIsNotNone(scheduler.get_job(web.SOURCE_MONITOR_JOB))
        self.maintain.assert_not_called()

    def test_update_saves_both_switches_and_keeps_custom_channels(self):
        self.patch("reload_tasks", return_value=True)
        source = copy.deepcopy(self.config["source"])
        source.update({"monitor": {"enable": False}, "channelsearch": {
            "enable": False, "auto_discover": False, "telegram_channels": ["custom_channel"], "qq_channels": []}})
        response = self.client.post("/update", json={"source": source})
        self.assertTrue(response.json["success"])
        self.assertEqual(web.Config.read_json(self.config_path)["source"], source)


if __name__ == "__main__":
    unittest.main()
