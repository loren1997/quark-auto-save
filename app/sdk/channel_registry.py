"""Discover and recheck bounded public Telegram channels from local source choices."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from itertools import zip_longest
import json
import hashlib
import logging
import re
import sqlite3
import time
from urllib.parse import urlsplit

from .channel_search import DEFAULT_TELEGRAM_CHANNELS, channel_results
from .common import SearchError
from .source_health import SourceHealth
from .websearch import WebSearch


DIRECTORY_URL = "https://api.github.com/repos/fish2018/pansou/issues/4"
DIRECTORY_PAGE = "https://github.com/fish2018/pansou/issues/4"
PRIORITY_CHANNELS = ("ucquark", "Quark_Movies", "Netdisk_Movies", "Aliyun_4K_Movies", "leoziyuan")
MAX_CHANNELS = 80


def _valid_name(value):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value))
            and not value.lower().endswith("bot")
            and value.lower() not in {"joinchat", "addlist", "addstickers", "addemoji", "share", "login", "proxy", "socks", "confirmphone", "setlanguage"})


def folder_urls(value):
    if isinstance(value, str):
        value = [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list) or len(value) > 3 or any(
            not isinstance(item, str) or not re.fullmatch(r"https://t\.me/addlist/[A-Za-z0-9_-]{1,128}", item)
            for item in value):
        raise SearchError("Telegram 文件夹最多 3 项，请填写有效的 t.me/addlist 链接")
    return list(dict.fromkeys(value))


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
        (r"频道不可访问|频道已封禁|频道受限", "频道不可访问"),
        (r"未启用.*账号", "未启用 Telegram 账号"),
        (r"未登录|未授权|需要登录", "Telegram 账号未登录"),
        (r"账号繁忙", "Telegram 账号繁忙"),
        (r"账号请求过于频繁", "Telegram 账号请求过于频繁"),
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
            db.execute("CREATE TABLE IF NOT EXISTS channel_origins ("
                       "origin TEXT PRIMARY KEY, names TEXT NOT NULL, last_checked REAL NOT NULL, "
                       "next_check REAL NOT NULL, reason TEXT NOT NULL)")
            if not db.execute("SELECT 1 FROM channel_origins WHERE origin='directory'").fetchone():
                names = [row[0] for row in db.execute("SELECT name FROM discovered_channels ORDER BY rowid LIMIT 40")]
                state = db.execute("SELECT last_checked, next_check, reason FROM channel_discovery WHERE id=1").fetchone()
                db.execute("INSERT INTO channel_origins VALUES ('directory', ?, ?, ?, ?)",
                           (json.dumps(names), *state))
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

    def _retry_at(self, name, reason, now, check_health=True):
        delay = 6 * 3600 if reason in ("未发现影视夸克链接", "频道未公开", "频道不可访问") else 300
        if not check_health:
            return now + delay
        try:
            self.health.check("https://t.me/s/" + name)
        except SearchError as error:
            match = re.search(r"剩余 (\d{1,8}) 秒", str(error))
            if match:
                delay = max(delay, min(int(match[1]), 7 * 86400))
        return now + delay

    def record_failure(self, channel, reason, check_health=True, register=False, retry_after=None):
        if not self.path or not _valid_name(channel):
            return
        now, reason = time.time(), _reason(reason)
        if reason == "需要人工验证" and check_health:
            self.health.record("https://t.me/s/" + channel, reason)
        retry = self._retry_at(channel, reason, now, check_health)
        if isinstance(retry_after, (int, float)) and 0 < retry_after <= 7 * 86400:
            retry = now + max(300, retry_after)
        try:
            with self._connect() as db:
                if register:
                    db.execute("INSERT OR IGNORE INTO discovered_channels VALUES (?, 'pending', '', 0, 0)", (channel,))
                db.execute("UPDATE discovered_channels SET status='cooldown', reason=?, last_checked=?, "
                           "next_check=CASE WHEN reason='频道不可访问' AND ?='频道不可访问' THEN MAX(next_check,?) ELSE ? END "
                           "WHERE name=? AND (reason<>'频道不可访问' OR next_check<=? OR ?='频道不可访问')",
                           (reason, now, reason, retry, retry, channel, now, reason))
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
                rows = db.execute("SELECT name, status, reason, last_checked, next_check FROM discovered_channels ORDER BY rowid LIMIT ?", (MAX_CHANNELS,)).fetchall()
                result["channels"] = [dict(zip(("name", "status", "reason", "last_checked", "next_check"), row))
                                      for row in rows if _valid_name(row[0])]
                result["origins"] = [{"kind": "directory" if row[0] == "directory" else "folder",
                                      "last_checked": row[1], "next_check": row[2], "reason": row[3]}
                                     for row in db.execute("SELECT origin, last_checked, next_check, reason FROM channel_origins ORDER BY origin")]
        except (sqlite3.Error, OSError) as error:
            logging.warning("频道状态读取失败 (%s)", type(error).__name__)
            result["discovery_reason"] = "频道状态读取失败"
        return result

    def active_channels(self, configured=None, check_health=True):
        if not self.path:
            return []
        if isinstance(configured, str):
            configured = re.split(r"[,，\s]+", configured)
        existing = {name.lstrip("@").casefold() for name in (configured or []) if isinstance(name, str)}
        if check_health:
            try:
                self.health.check("https://t.me/")
            except SearchError:
                return []
        names = [row["name"] for row in self.status()["channels"]
                 if row["status"] in ("active", "standby") and row["name"].casefold() not in existing]
        if len(names) > 6:
            offset = int(time.time() // 300) % len(names)
            names = names[offset:] + names[:offset]
        return names[:6]

    def available_channels(self, names):
        if not self.path:
            return list(names)
        try:
            with self._connect() as db:
                blocked = {row[0].casefold() for row in db.execute("SELECT name FROM discovered_channels "
                           "WHERE status='cooldown' AND reason='频道不可访问' AND next_check>?", (time.time(),))}
        except (sqlite3.Error, OSError) as error:
            logging.warning("频道停用状态读取失败 (%s)，保留原查询名单", type(error).__name__)
            return list(names)
        return [name for name in names if name.casefold() not in blocked]

    def record_account_results(self, channels, candidates, started):
        if not self.path:
            return
        known = {name.casefold() for name in channels}
        verified = set()
        for item in candidates:
            try:
                self._validate_media([item])
                page = urlsplit(item.get("source_url") or item.get("page_url") or "")
                match = re.fullmatch(r"/([A-Za-z][A-Za-z0-9_]{3,31})/\d+", page.path)
                if page.scheme == "https" and page.hostname == "t.me" and match and match[1].casefold() in known:
                    verified.add(match[1])
            except (SearchError, ValueError):
                continue
        try:
            with self._connect() as db:
                for name in verified:
                    active = db.execute("SELECT COUNT(*) FROM discovered_channels WHERE status='active' AND name<>?", (name,)).fetchone()[0]
                    checked = time.time()
                    db.execute("UPDATE discovered_channels SET status=?, reason='', last_checked=?, next_check=? "
                               "WHERE name=? AND (last_checked<=? OR reason<>'频道不可访问')",
                               ("active" if active < 6 else "standby", checked, checked + 3600, name, started))
        except (sqlite3.Error, OSError) as error:
            logging.warning("Telegram 频道验证状态写入失败 (%s)", type(error).__name__)

    @staticmethod
    def _probe(client, name, deadline):
        started = time.time()
        body, final = client._fetch("https://t.me/s/" + name, deadline)
        if final.rstrip("/").casefold() != ("https://t.me/s/" + name).casefold():
            raise SearchError("频道重定向地址不符合访问规则")
        rows = channel_results("Telegram @" + name, body, final, "quark")
        ChannelRegistry._validate_media(rows)
        return started

    @staticmethod
    def _validate_media(rows):
        for row in rows:
            if not isinstance(row, dict) or not re.match(r"^https://pan\.quark\.cn/s/[A-Za-z0-9]+(?:[?#]|$)",
                                                        str(row.get("shareurl") or row.get("url") or "")):
                continue
            title = row.get("taskname") or row.get("title", "")
            if re.search(r"软件|APP\b|APK\b|游戏|课程|教程|电子书|小说|漫画", title, re.I):
                continue
            if re.search(r"动画|动漫|国漫|电影|影视|电视剧|剧集|番剧|日漫|国剧|日剧|韩剧|美剧|纪录片|综艺|"
                         r"\bS\d{1,2}E\d{1,4}\b|(?:更新|更至)[^\n]{0,8}\d+\s*[集话]", row.get("content") or row.get("snippet", ""), re.I):
                return
        raise SearchError("未发现影视夸克链接")

    def _refresh_origins(self, client, folders, account, deadline, now, configured):
        origins = {"directory": DIRECTORY_URL}
        origins.update({"folder:" + hashlib.sha256(url.encode()).hexdigest(): url for url in (folders or [])})
        with self._connect() as db:
            if folders is not None:
                db.execute("DELETE FROM channel_origins WHERE origin NOT IN (" + ",".join("?" for _ in origins) + ")", list(origins))
            db.executemany("INSERT OR IGNORE INTO channel_origins VALUES (?, '[]', 0, 0, '')", [(key,) for key in origins])
            due = [row[0] for row in db.execute("SELECT origin FROM channel_origins WHERE next_check<=? ORDER BY origin", (now,))
                   if row[0] in origins]
        for origin in due:
            if time.monotonic() >= deadline:
                break
            names, reason, retry = None, "", 86400
            try:
                if origin == "directory":
                    body, final = client._fetch(DIRECTORY_URL, deadline, allow_json=True)
                    if final != DIRECTORY_URL:
                        raise SearchError("频道目录重定向地址不符合访问规则")
                    names = self._directory(body)[:40]
                elif account is None:
                    reason, retry = "未启用 Telegram 账号", 300
                else:
                    result = account.read_folder(origins[origin], deadline=deadline)
                    if result.get("ok"):
                        names = [item.get("name") for item in result.get("channels", [])
                                 if isinstance(item, dict) and _valid_name(item.get("name"))][:40]
                    else:
                        reason = _reason(result.get("error") or "Telegram 账号查询失败")
                        retry = max(300, min(int(result.get("retry_after") or 0), 7 * 86400))
            except (SearchError, ValueError, TypeError) as error:
                reason = _reason(error)
                if origin != "directory":
                    retry = 300
            with self._connect() as db:
                if names is not None:
                    unique = {name.casefold(): name for name in names}
                    db.execute("UPDATE channel_origins SET names=? WHERE origin=?", (json.dumps(list(unique.values())), origin))
                db.execute("UPDATE channel_origins SET last_checked=?, next_check=?, reason=? WHERE origin=?",
                           (now, now + retry, reason, origin))
        with self._connect() as db:
            entries = db.execute("SELECT origin, names, last_checked, next_check, reason FROM channel_origins "
                                 "ORDER BY CASE WHEN origin='directory' THEN 1 ELSE 0 END, origin").fetchall()
            pool, groups = {}, []
            for _origin, encoded, *_state in entries:
                names = json.loads(encoded)
                names = [name for name in names if _valid_name(name)] if isinstance(names, list) else []
                groups.append(names)
                for name in names:
                    pool.setdefault(name.casefold(), name)
            known = [row[0] for row in db.execute("SELECT name FROM discovered_channels WHERE status='active' LIMIT 6")
                     if row[0].casefold() in pool]
            manual = [row[0] for row in db.execute("SELECT name FROM discovered_channels") if row[0].casefold() in configured]
            ordered = {}
            # 各来源轮流给候选名额，旧备用池不能占满容量而饿死新文件夹。
            interleaved = [name for row in zip_longest(*groups) for name in row if name is not None]
            for name in manual + known + interleaved:
                ordered.setdefault(name.casefold(), name)
            names = list(ordered.values())[:MAX_CHANNELS]
            if names:
                db.execute("DELETE FROM discovered_channels WHERE name NOT IN (" + ",".join("?" for _ in names) + ")", names)
                db.executemany("INSERT OR IGNORE INTO discovered_channels VALUES (?, 'pending', '', 0, 0)", [(name,) for name in names])
            else:
                db.execute("DELETE FROM discovered_channels")
            db.execute("UPDATE channel_discovery SET last_checked=?, next_check=?, reason=? WHERE id=1",
                       (max(row[2] for row in entries), min(row[3] for row in entries),
                        next((row[4] for row in entries if row[4]), "")))

    @staticmethod
    def _probe_account(account, name, deadline):
        started = time.time()
        result = account.search("pan.quark.cn", [name], limit=5, deadline=deadline)
        for item in result.get("unavailable_channels") or []:
            if item.get("name", "").casefold() == name.casefold():
                error = SearchError("频道不可访问")
                error.retry_after = item.get("retry_after")
                raise error
        if not result.get("ok"):
            raise SearchError(result.get("error") or "Telegram 账号查询失败")
        if not result.get("candidates") and result.get("errors"):
            raise SearchError(result["errors"][0].get("error") or "Telegram 账号查询失败")
        ChannelRegistry._validate_media(result.get("candidates") or [])
        return started

    def maintain(self, config):
        sources = config.get("source") or {} if isinstance(config, dict) else {}
        channels = (sources.get("channelsearch") or {}) if isinstance(sources, dict) else {}
        if (not self.path or not isinstance(channels, dict)
                or str(channels.get("enable", True)).lower() != "true"):
            return self.status()
        discover_enabled = str(channels.get("auto_discover", True)).lower() == "true"
        configured = channels.get("telegram_channels", DEFAULT_TELEGRAM_CHANNELS)
        if isinstance(configured, str):
            configured = re.split(r"[,，\s]+", configured)
        configured = {name.lstrip("@").casefold() for name in configured if isinstance(name, str)} if isinstance(configured, list) else set()
        if not discover_enabled and not any(row["name"].casefold() in configured and row["status"] == "cooldown"
                                           and row["reason"] == "频道不可访问" and row["next_check"] <= time.time()
                                           for row in self.status()["channels"]):
            return self.status()
        try:
            folders = folder_urls(channels.get("telegram_folders", []))
        except SearchError:
            logging.warning("Telegram 文件夹配置无效，保留已有候选并跳过目录变更")
            folders = None
        account = None
        if str((sources.get("telegram") or {}).get("enable", False)).lower() == "true":
            from .telegram_account import TelegramAccount
            account = TelegramAccount(self.path)
        account_probe = account if account is not None and account.status().get("authorized") else None
        now, deadline = time.time(), time.monotonic() + 30
        lease = now + 60
        client = None
        claimed = False
        try:
            with self._connect() as db:
                claimed = bool(db.execute("UPDATE channel_discovery SET lease=? WHERE id=1 AND lease<=?", (lease, now)).rowcount)
            if not claimed:
                return self.status()
            client = WebSearch(health_path=self.path)
            if discover_enabled:
                self._refresh_origins(client, folders, account, deadline, now, configured)
            with self._connect() as db:
                active = db.execute("SELECT COUNT(*) FROM discovered_channels WHERE status='active'").fetchone()[0]
                ready = db.execute("SELECT name FROM discovered_channels WHERE status='standby' AND next_check>? ORDER BY last_checked DESC LIMIT ?",
                                   (now, max(0, 6 - active))).fetchall()
                db.executemany("UPDATE discovered_channels SET status='active' WHERE name=?", ready)
                active += len(ready)
                rows = db.execute("SELECT name, status, reason FROM discovered_channels WHERE next_check<=? "
                                  "ORDER BY last_checked, rowid", (now,)).fetchall()
                eligible = [row for row in rows if _valid_name(row[0]) and (
                    (discover_enabled and row[0].casefold() not in configured)
                    or (row[0].casefold() in configured and row[1] == "cooldown" and row[2] == "频道不可访问"))]
                pending = [row[0] for row in eligible if row[1] == "pending"]
                retry = [row[0] for row in eligible if row[1] != "pending"]
                names = (pending[:1] + retry[:2] + pending[1:])[:3]
            if time.monotonic() < deadline:
                with ThreadPoolExecutor(max_workers=1 if account_probe else 2) as executor:
                    jobs = [(name, executor.submit(self._probe_account, account_probe, name, deadline) if account_probe
                             else executor.submit(self._probe, client, name, deadline)) for name in names]
                    for name, future in jobs:
                        try:
                            started = future.result()
                            checked = time.time()
                            with self._connect() as db:
                                active = db.execute("SELECT COUNT(*) FROM discovered_channels WHERE status='active' AND name<>?", (name,)).fetchone()[0]
                                state = "active" if active < 6 else "standby"
                                restored = db.execute("UPDATE discovered_channels SET status=?, reason='', last_checked=?, next_check=? "
                                                      "WHERE name=? AND last_checked<=?", (state, checked, checked + 3600, name, started)).rowcount
                            if restored and account_probe is None:
                                self.health.clear("https://t.me/s/" + name, checked_after=started)
                        except (SearchError, ValueError, TypeError) as error:
                            self.record_failure(name, error, check_health=account_probe is None,
                                                retry_after=getattr(error, "retry_after", None))
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
