"""Send bounded progress events to the background run collector."""
import json
import os


def report_progress(stage, *, task=None, source="", query=""):
    if os.environ.get("QAS_RUN_PROGRESS") != "1":
        return
    event = {"stage": stage, "source": str(source)[:80], "query": str(query)[:200]}
    if task is not None:
        event["task"] = str(task)[:200]
    print("QAS_PROGRESS: " + json.dumps(event, ensure_ascii=False), flush=True)
