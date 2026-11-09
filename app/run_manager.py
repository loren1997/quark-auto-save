"""Bounded, authenticated-by-caller background runs and redacted log history."""

import atexit
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid


STAGES = {"start", "signin", "task", "search", "verify", "gap", "transfer", "rename", "download", "finish"}
ACTIVE = {"queued", "running"}
ID_PATTERN = re.compile(r"[a-f0-9]{32}")
MAX_RUNS, MAX_LINES, MAX_BYTES = 20, 1000, 512 * 1024
SUMMARY_FIELDS = {"id", "status", "trigger", "task", "stage", "source", "query", "discovery_id",
                  "created_at", "started_at", "finished_at", "error", "exit_code", "warning", "cursor"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_payload(payload):
    allowed = {"tasklist", "quark_test", "cookie", "push_config", "discovery_id", "shareurl"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("运行参数必须是 JSON 对象，且只包含允许的任务参数")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 256 * 1024:
        raise ValueError("运行参数过大")
    if "discovery_id" in payload:
        if (set(payload) - {"discovery_id", "shareurl"}
                or not isinstance(payload["discovery_id"], str)
                or not re.fullmatch(r"[\w:-]{1,120}", payload["discovery_id"])):
            raise ValueError("热门订阅参数不正确")
        if "shareurl" in payload and (not isinstance(payload["shareurl"], str) or len(payload["shareurl"]) > 2000):
            raise ValueError("分享链接不正确")
        return deepcopy(payload)
    if "shareurl" in payload or not isinstance(payload.get("quark_test", False), bool):
        raise ValueError("运行参数不正确")
    tasks = payload.get("tasklist", [])
    if (not isinstance(tasks, list) or len(tasks) > 200
            or any(not isinstance(task, dict) or not isinstance(task.get("taskname"), str)
                   or len(task["taskname"]) > 300 for task in tasks)):
        raise ValueError("任务列表格式不正确")
    if "cookie" in payload and (not isinstance(payload["cookie"], list)
            or any(not isinstance(item, str) or len(item) > 20000 for item in payload["cookie"])):
        raise ValueError("Cookie 参数格式不正确")
    if "push_config" in payload and not isinstance(payload["push_config"], dict):
        raise ValueError("通知参数格式不正确")
    return deepcopy(payload)


def secret_values(value):
    values = set()
    def visit(item, sensitive=False):
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, sensitive or bool(re.search(r"cookie|password|secret|token|authorization|credential|(?:^|_)key$", str(key), re.I)))
        elif isinstance(item, list):
            for child in item:
                visit(child, sensitive)
        elif sensitive and isinstance(item, str) and len(item) >= 3:
            values.add(item)
            values.update(part for part in item.splitlines() if len(part) >= 3)
            for token in re.findall(r"(?:^|[;\s])[^=;\s]+\s*=\s*([^;\s]+)", item):
                if len(token) >= 3:
                    values.add(token)
    visit(value)
    return sorted(values, key=len, reverse=True)


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        text = text.replace(secret, "[已隐藏]")
    def safe_url(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or ""
            if "drive.quark.cn" in host or host.startswith("dl-"):
                return "[下载链接已隐藏]"
            return f"{parsed.scheme}://{host}/[链接已脱敏]"
        except ValueError:
            return "[链接已隐藏]"
    text = re.sub(r"https?://[^\s<>\"']+", safe_url, text, flags=re.I)
    text = re.sub(r"(?i)(cookie|authorization|password|secret|stoken|access_token|refresh_token|token|sign|signature|__puus|__pus)\s*[\"']?\s*[:=]\s*[^\r\n]+", r"\1=[已隐藏]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[:4000]


class RunManager:
    def __init__(self, config_path, python_path, script_path, timeout):
        self.config_path = os.path.abspath(config_path)
        self.python_path, self.script_path, self.timeout = python_path, os.path.abspath(script_path), timeout
        self.directory = Path(self.config_path).parent / "runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.records, self.processes, self.owned = {}, {}, set()
        gate = self._take_gate()
        try:
            for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                if not ID_PATTERN.fullmatch(path.stem) or path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > MAX_BYTES * 3:
                        continue
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if (record.get("id") != path.stem or not isinstance(record.get("logs"), list)
                            or record.get("status") not in ACTIVE | {"succeeded", "failed", "interrupted"}):
                        continue
                    if record.get("status") in ACTIVE and gate is not None:
                        record.update(status="interrupted", finished_at=now(), error="服务已重启，上次运行已中断")
                        self._persist(record)
                    self.records[record["id"]] = record
                except (OSError, ValueError, TypeError, AttributeError):
                    logging.warning("跳过损坏的运行日志")
            self._prune()
        finally:
            self._release_gate(gate)
        atexit.register(self.close)

    def _take_gate(self):
        stream = open(self.directory / ".active.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError:
            stream.close()
            return None

    @staticmethod
    def _release_gate(stream):
        if stream is not None:
            stream.close()

    def _persist(self, record):
        path = self.directory / (record["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # Windows 的短暂读句柄可能阻止替换；日志读取不应中断正在转存的任务。
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.02)

    def _prune(self):
        records = sorted(self.records.values(), key=lambda item: item["created_at"], reverse=True)
        for record in records[MAX_RUNS:]:
            if record["status"] not in ACTIVE:
                (self.directory / (record["id"] + ".json")).unlink(missing_ok=True)
                self.records.pop(record["id"], None)

    @staticmethod
    def _summary(record):
        return {key: deepcopy(value) for key, value in record.items() if key in SUMMARY_FIELDS}

    def _refresh_other_runs(self):
        paths = {path.stem: path for path in self.directory.glob("*.json")
                 if ID_PATTERN.fullmatch(path.stem) and not path.is_symlink()}
        for run_id in set(self.records) - set(paths) - self.owned:
            self.records.pop(run_id, None)
        for run_id, path in paths.items():
            record = self.records.get(run_id)
            if run_id in self.owned or (record and record["status"] not in ACTIVE):
                continue
            try:
                if path.stat().st_size > MAX_BYTES * 3:
                    continue
                current = json.loads(path.read_text(encoding="utf-8"))
                if (current.get("id") == run_id and isinstance(current.get("logs"), list)
                        and current.get("status") in ACTIVE | {"succeeded", "failed", "interrupted"}):
                    self.records[run_id] = current
            except (OSError, ValueError, TypeError):
                logging.warning("无法刷新另一进程的运行记录")

    def list(self):
        with self.condition:
            self._refresh_other_runs()
            return [self._summary(record) for record in sorted(self.records.values(), key=lambda item: item["created_at"], reverse=True)]

    def get(self, run_id, after=0):
        if not ID_PATTERN.fullmatch(run_id):
            return None
        with self.condition:
            self._refresh_other_runs()
            record = self.records.get(run_id)
            if record is None:
                return None
            first = record["logs"][0]["cursor"] if record["logs"] else record["cursor"] + 1
            return {**self._summary(record), "logs": deepcopy([line for line in record["logs"] if line["cursor"] > after]),
                    "first_cursor": first, "truncated": after < first - 1}

    def start(self, payload, trigger="manual"):
        payload = validate_payload(payload)
        with self.condition:
            self._refresh_other_runs()
            active = next((record for record in self.records.values() if record["status"] in ACTIVE), None)
            if active:
                return False, self._summary(active)
            gate = self._take_gate()
            if gate is None:
                return False, None
            try:
                with open(self.config_path, encoding="utf-8") as stream:
                    config = json.load(stream)
                secrets = secret_values([config, payload])
                tasks = payload.get("tasklist") or []
                title = payload.get("discovery_id") or (tasks[0]["taskname"] if tasks else "全部任务")
                record = {"id": uuid.uuid4().hex, "status": "queued", "trigger": trigger,
                          "task": redact(title, secrets), "stage": "start", "source": "", "query": "",
                          "discovery_id": payload.get("discovery_id", ""), "created_at": now(),
                          "started_at": None, "finished_at": None, "error": "", "exit_code": None,
                          "warning": False, "cursor": 0, "logs": []}
                self.records[record["id"]] = record
                self.owned.add(record["id"])
                self._persist(record)
                self._prune()
                threading.Thread(target=self._execute, args=(record, payload, secrets, gate), daemon=True,
                                 name="qas-run-" + record["id"][:8]).start()
                return True, self._summary(record)
            except Exception as exc:
                try:
                    if "record" in locals():
                        self.owned.discard(record["id"])
                        record.update(status="failed", finished_at=now(),
                                      error=f"无法启动后台运行（{type(exc).__name__}）")
                        try:
                            self._persist(record)
                        except OSError:
                            # 启动失败后不能留下 queued，否则后续运行会一直被占用。
                            (self.directory / (record["id"] + ".json")).unlink(missing_ok=True)
                            logging.error("无法保存后台启动失败记录")
                finally:
                    self._release_gate(gate)
                raise

    def _line(self, record, line, secrets):
        with self.condition:
            if line.startswith("QAS_PROGRESS: "):
                try:
                    progress = json.loads(line[len("QAS_PROGRESS: "):])
                    if isinstance(progress, dict) and progress.get("stage") in STAGES:
                        record["stage"] = progress["stage"]
                        for key in ("task", "source", "query"):
                            if key in progress and isinstance(progress[key], str):
                                record[key] = redact(progress[key], secrets)[:300]
                except (ValueError, TypeError):
                    pass
            else:
                line = redact(line.rstrip("\r\n"), secrets)
                record["cursor"] += 1
                record["logs"].append({"cursor": record["cursor"], "text": line})
                while len(record["logs"]) > MAX_LINES or sum(len(item["text"].encode()) for item in record["logs"]) > MAX_BYTES:
                    record["logs"].pop(0)
                if any(word in line for word in ("仍有中间缺集", "未完成", "失败", "❌")):
                    record["warning"] = True
                logging.info("%s", line)
            self.condition.notify_all()

    def _execute(self, record, payload, secrets, gate):
        process, timer = None, None
        timed_out = threading.Event()
        try:
            environment = os.environ.copy()
            for key in ("TASKLIST", "QUARK_TEST", "COOKIE", "PUSH_CONFIG", "QAS_RUN_PAYLOAD"):
                environment.pop(key, None)
            environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", QAS_RUN_PROGRESS="1")
            script = self.script_path
            if "discovery_id" in payload:
                script = str(Path(__file__).resolve().parents[1] / "scripts" / "subscribe_and_run.py")
                environment["QAS_RUN_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)
            elif payload.get("quark_test"):
                environment.update(QUARK_TEST="true", COOKIE=json.dumps(payload.get("cookie", [])),
                                   PUSH_CONFIG=json.dumps(payload.get("push_config", {})))
            elif payload.get("tasklist"):
                environment["TASKLIST"] = json.dumps(payload["tasklist"], ensure_ascii=False)
            with self.condition:
                record.update(status="running", started_at=now())
                self._persist(record)
            process = subprocess.Popen([self.python_path, "-u", script, self.config_path], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1, env=environment,
                                       creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                                       if os.name == "nt" else 0)
            with self.condition:
                self.processes[record["id"]] = process
            def expire():
                if process.poll() is None:
                    timed_out.set()
                    process.kill()
            timer = threading.Timer(self.timeout, expire)
            timer.daemon = True
            timer.start()
            last_write = 0
            while True:
                line = process.stdout.readline(65536)
                if not line:
                    break
                if len(line) == 65536 and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = process.stdout.readline(65536)
                    line = "[过长日志行已隐藏]"
                self._line(record, line, secrets)
                if line.startswith("QAS_PROGRESS: ") or time.monotonic() - last_write >= 1:
                    with self.condition:
                        self._persist(record)
                    last_write = time.monotonic()
            code = process.wait()
            with self.condition:
                record.update(exit_code=code, status="succeeded" if code == 0 and not timed_out.is_set() else "failed")
                if timed_out.is_set():
                    record["error"] = f"运行超过 {self.timeout} 秒，已终止；下次运行可重试"
                elif code != 0:
                    record["error"] = f"运行失败，退出码 {code}；请查看日志"
        except Exception as exc:
            with self.condition:
                record.update(status="failed", error=f"运行异常（{type(exc).__name__}）")
            self._line(record, record["error"], secrets)
        finally:
            if timer:
                timer.cancel()
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
            with self.condition:
                record["finished_at"] = now()
                self.processes.pop(record["id"], None)
                self.owned.discard(record["id"])
                try:
                    self._persist(record)
                except OSError:
                    logging.error("无法保存运行日志")
                self._release_gate(gate)
                self.condition.notify_all()

    def stream(self, run_id):
        cursor = 0
        while True:
            result = self.get(run_id, cursor)
            if result is None:
                return
            for line in result["logs"]:
                yield f"data: {line['text']}\n\n"
            cursor = result["cursor"]
            if result["status"] not in ACTIVE:
                yield "data: [DONE]\n\n"
                return
            with self.condition:
                self.condition.wait(timeout=0.5)

    def close(self):
        with self.condition:
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()
