import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from run_progress import report_progress


class RunProgressTests(unittest.TestCase):
    def test_disabled_by_default_and_bounded_when_enabled(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            report_progress("search", task="作品")
        self.assertEqual(output.getvalue(), "")
        with patch.dict(os.environ, {"QAS_RUN_PROGRESS": "1"}), contextlib.redirect_stdout(output):
            report_progress("search", task="作品", source="搜狗", query="字" * 250)
        event = json.loads(output.getvalue().removeprefix("QAS_PROGRESS: "))
        self.assertEqual(event, {"stage": "search", "task": "作品", "source": "搜狗", "query": "字" * 200})

    def test_source_updates_do_not_clear_task_but_clear_old_query(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"QAS_RUN_PROGRESS": "1"}), contextlib.redirect_stdout(output):
            report_progress("verify", source="夸克分享")
        event = json.loads(output.getvalue().removeprefix("QAS_PROGRESS: "))
        self.assertNotIn("task", event)
        self.assertEqual(event["query"], "")


if __name__ == "__main__":
    unittest.main()
