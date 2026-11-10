"""Discover and recheck bounded public Telegram channels without credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import logging
import re
import sqlite3
import time

from .channel_search import DEFAULT_TELEGRAM_CHANNELS, channel_results
from .common import SearchError
from .source_health import SourceHealth
from .websearch import WebSearch


DIRECTORY_URL = "https://api.github.com/repos/fish2018/pansou/issues/4"
DIRECTORY_PAGE = "https://github.com/fish2018/pansou/issues/4"
PRIORITY_CHANNELS = ("ucquark", "Quark_Movies", "Netdisk_Movies", "Aliyun_4K_Movies", "leoziyuan")


def _valid_name(value):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value))
            and not value.lower().endswith("bot")
            and value.lower() not in {"joinchat", "addstickers", "addemoji", "share", "login", "proxy", "socks", "confirmphone", "setlanguage"})


def _reason(error):
    message = str(error)
    status = re.search(r"HTTP\s+([45]\d{2})", message)
    if status:
        return "HTTP " + status[1]
    for pattern, label in (
        (r"人工验证|captcha|challenge", "需要人工验证"),
        (r"超时|timeout", "请求超时"),
        (r"时间预算|时间上限", "达到本轮时间预算"),
        (r"未发现影视夸克链接", "未发现影视夸克链接"),
        (r"未返回公开消息|不公开", "频道未公开"),
        (r"格式|json|decode", "响应格式异常"),
        (r"非公网|地址不符合|重定向", "频道地址不符合访问规则"),
        (r"网络|连接|connect|代理|SSL|TLS", "网络连接失败"),
    ):
        if re.search(pattern, message, re.I):
            return label
    return "查询失败"


class ChannelRegistry:
    def __init__(self, path=None):
        self.path = path
        self.health = SourceHealth(path)

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS discovered_channels ("
                       "name TEXT PRIMARY KEY COLLATE NOCASE, status TEXT NOT NULL, reason TEXT NOT NULL, "
                       "last_checked REAL NOT NULL, next_check REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS channel_discovery ("
                       "id INTEGER PRIMARY KEY CHECK(id=1), last_checked REAL NOT NULL, next_check REAL NOT NULL, "
                       "reason TEXT NOT NULL, lease REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO channel_discovery VALUES (1, 0, 0, '', 0)")
            yield db

    @staticmethod
    def _directory(body):
        data = json.loads(body)
        if (not isinstance(data, dict) or data.get("number") != 4 or data.get("html_url") != DIRECTORY_PAGE
                or not isinstance(data.get("user"), dict) or data["user"].get("login") != "fish2018"
                or not isinstance(data.get("body"), str)):
            raise SearchError("频道目录响应格式异常")
        match = re.search(r"\b(?:export\s+)?CHANNELS=([A-Za-z0-9_,@]+)(?=\s|`|$)", data["body"])
        if not match:
            raise SearchError("频道目录响应格式异常")
        names = {}
        for name in match[1].split(",")[:160]:
            if _valid_name(name):
                names.setdefault(name.casefold(), name)
        if not names:
            raise SearchError("频道目录响应格式异常")
        preferred = [names[name.casefold()] for name in PRIORITY_CHANNELS if name.casefold() in names]
        return list(dict.fromkeys(preferred + list(names.values())))

    def _retry_at(self, name, reason, now):
        delay = 6 * 3600 if reason in ("未发现影视夸克链接", "频道未公开") else 300
        try:
            self.health.check("https://t.me/s/" + name)
        except SearchError as error:
            match = re.search(r"剩余 (\d{1,8}) 秒", str(error))
            if match:
                delay = max(delay, min(int(match[1]), 7 * 86400))
        return now + delay

    def record_failure(self, channel, reason):
        if not self.path or not _valid_name(channel):
            return
        now, reason = time.time(), _reason(reason)
        if reason == "需要人工验证":
            self.health.record("https://t.me/s/" + channel, reason)
        retry = self._retry_at(channel, reason, now)
        try:
            with self._connect() as db:
                db.execute("UPDATE discovered_channels SET status='cooldown', reason=?, last_checked=?, next_check=? WHERE name=?",
                           (reason, now, retry, channel))
        except (sqlite3.Error, OSError) as error:
            logging.warning("频道停用状态写入失败 (%s)", type(error).__name__)

    def status(self):
        result = {"last_discovery": 0, "next_discovery": 0, "discovery_reason": "", "channels": []}
        if not self.path:
            return result
        try:
            with self._connect() as db:
                row = db.execute("SELECT last_checked, next_check, reason FROM channel_discovery WHERE id=1").fetchone()
                result.update(last_discovery=row[0], next_discovery=row[1], discovery_reason=row[2])
                rows = db.execute("SELECT name, status, reason, last_checked, next_check FROM discovered_channels ORDER BY rowid LIMIT 40").fetchall()
                result["channels"] = [dict(zip(("name", "status", "reason", "last_checked", "next_check"), row))
                                      for row in rows if _valid_name(row[0])]
        except (sqlite3.Error, OSError) as error:
            logging.warning("频道状态读取失败 (%s)", type(error).__name__)
            result["discovery_reason"] = "频道状态读取失败"
        return result

    def active_channels(self, configured=None):
        if not self.path:
            return []
        if isinstance(configured, str):
            configured = re.split(r"[,，\s]+", configured)
        existing = {name.lstrip("@").casefold() for name in (configured or []) if isinstance(name, str)}
        try:
            self.health.check("https://t.me/")
        except SearchError:
            return []
        return [row["name"] for row in self.status()["channels"]
                if row["status"] == "active" and row["name"].casefold() not in existing][:6]

    @staticmethod
    def _probe(client, name, deadline):
        started = time.time()
        body, final = client._fetch("https://t.me/s/" + name, deadline)
        if final.rstrip("/").casefold() != ("https://t.me/s/" + name).casefold():
            raise SearchError("频道重定向地址不符合访问规则")
        rows = channel_results("Telegram @" + name, body, final, "quark")
        for row in rows:
            title = row.get("title", "")
            if re.search(r"软件|APP\b|APK\b|游戏|课程|教程|电子书|小说|漫画", title, re.I):
                continue
            if re.search(r"动画|动漫|国漫|电影|影视|电视剧|剧集|番剧|日漫|国剧|日剧|韩剧|美剧|纪录片|综艺|"
                         r"\bS\d{1,2}E\d{1,4}\b|(?:更新|更至)[^\n]{0,8}\d+\s*[集话]", row.get("snippet", ""), re.I):
                return started
        raise SearchError("未发现影视夸克链接")

    def maintain(self, config):
        sources = config.get("source") or {} if isinstance(config, dict) else {}
        channels = (sources.get("channelsearch") or {}) if isinstance(sources, dict) else {}
        if (not self.path or not isinstance(channels, dict)
                or str(channels.get("enable", True)).lower() != "true"
                or str(channels.get("auto_discover", True)).lower() != "true"):
            return self.status()
        configured = channels.get("telegram_channels", DEFAULT_TELEGRAM_CHANNELS)
        if isinstance(configured, str):
            configured = re.split(r"[,，\s]+", configured)
        configured = {name.lstrip("@").casefold() for name in configured if isinstance(name, str)} if isinstance(configured, list) else set()
        now, deadline = time.time(), time.monotonic() + 30
        lease = now + 60
        client = None
        claimed = False
        try:
            with self._connect() as db:
                claimed = bool(db.execute("UPDATE channel_discovery SET lease=? WHERE id=1 AND lease<=?", (lease, now)).rowcount)
                if claimed:
                    discover = db.execute("SELECT next_check FROM channel_discovery WHERE id=1").fetchone()[0] <= now
                    if discover:
                        db.execute("UPDATE channel_discovery SET last_checked=?, next_check=? WHERE id=1", (now, now + 86400))
            if not claimed:
                return self.status()
            client = WebSearch(health_path=self.path)
            if discover:
                try:
                    body, final = client._fetch(DIRECTORY_URL, deadline, allow_json=True)
                    if final != DIRECTORY_URL:
                        raise SearchError("频道目录重定向地址不符合访问规则")
                    names = self._directory(body)
                    with self._connect() as db:
                        known = [row[0] for row in db.execute("SELECT name FROM discovered_channels WHERE status IN ('active','standby')")]
                        current = {name.casefold() for name in names}
                        names = list(dict.fromkeys([name for name in known if name.casefold() in current] + names))[:40]
                        db.execute("DELETE FROM discovered_channels WHERE name NOT IN (" + ",".join("?" for _ in names) + ")", names)
                        db.executemany("INSERT OR IGNORE INTO discovered_channels VALUES (?, 'pending', '', 0, 0)", [(name,) for name in names])
                        db.execute("UPDATE channel_discovery SET reason='' WHERE id=1")
                except (SearchError, ValueError, TypeError) as error:
                    with self._connect() as db:
                        db.execute("UPDATE channel_discovery SET reason=? WHERE id=1", (_reason(error),))
            with self._connect() as db:
                active = db.execute("SELECT COUNT(*) FROM discovered_channels WHERE status='active'").fetchone()[0]
                ready = db.execute("SELECT name FROM discovered_channels WHERE status='standby' AND next_check>? ORDER BY last_checked DESC LIMIT ?",
                                   (now, max(0, 6 - active))).fetchall()
                db.executemany("UPDATE discovered_channels SET status='active' WHERE name=?", ready)
                active += len(ready)
                rows = db.execute("SELECT name FROM discovered_channels WHERE next_check<=? "
                                  "AND (status IN ('active','cooldown') OR ?<6) ORDER BY last_checked, rowid", (now, active)).fetchall()
                names = [row[0] for row in rows if _valid_name(row[0]) and row[0].casefold() not in configured][:3]
            if time.monotonic() < deadline:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    jobs = [(name, executor.submit(self._probe, client, name, deadline)) for name in names]
                    for name, future in jobs:
                        try:
                            started = future.result()
                            checked = time.time()
                            with self._connect() as db:
                                active = db.execute("SELECT COUNT(*) FROM discovered_channels WHERE status='active' AND name<>?", (name,)).fetchone()[0]
                                state = "active" if active < 6 else "standby"
                                restored = db.execute("UPDATE discovered_channels SET status=?, reason='', last_checked=?, next_check=? "
                                                      "WHERE name=? AND last_checked<=?", (state, checked, checked + 3600, name, started)).rowcount
                            if restored:
                                self.health.clear("https://t.me/s/" + name, checked_after=started)
                        except (SearchError, ValueError, TypeError) as error:
                            self.record_failure(name, error)
        except (sqlite3.Error, OSError) as error:
            logging.warning("频道自动维护失败 (%s)", type(error).__name__)
        finally:
            if client:
                client.session.close()
            if claimed:
                try:
                    with self._connect() as db:
                        db.execute("UPDATE channel_discovery SET lease=0 WHERE id=1 AND lease=?", (lease,))
                except (sqlite3.Error, OSError):
                    logging.warning("频道维护租约释放失败，下轮将按到期时间重试")
        return self.status()
