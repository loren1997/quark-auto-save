"""Discover installed PanSou adapters; record search evidence, never install remote code."""

from contextlib import closing, contextmanager
import hashlib
import logging
import re
import sqlite3
import time

from .common import SearchError
from .source_health import SourceHealth


class IndexCatalog:
    def __init__(self, path, server):
        self.path = path
        self.server = server
        self.key = hashlib.sha256(server.rstrip('/').encode()).hexdigest()

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS index_inventory ('
                       'server TEXT PRIMARY KEY, checked REAL, next_check REAL, reason TEXT, lease REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS index_plugins ('
                       'server TEXT, name TEXT, state TEXT, reason TEXT, seen REAL, checked REAL, '
                       'next_check REAL, hits INTEGER, PRIMARY KEY(server,name))')
            yield db

    def status(self):
        result = {'last_discovery': 0, 'next_discovery': 0, 'reason': '', 'plugins': []}
        if not self.path:
            return result
        try:
            with self._connect() as db:
                row = db.execute('SELECT checked,next_check,reason FROM index_inventory WHERE server=?', (self.key,)).fetchone()
                if row:
                    result.update(zip(('last_discovery', 'next_discovery', 'reason'), row))
                rows = db.execute('SELECT name,state,reason,checked,next_check,hits FROM index_plugins '
                                  "WHERE server=? ORDER BY state='removed',name LIMIT 128", (self.key,)).fetchall()
                result['plugins'] = [dict(zip(('name', 'state', 'reason', 'last_checked', 'next_check', 'hits'), row))
                                     for row in rows]
        except (sqlite3.Error, OSError):
            logging.warning('索引插件状态读取失败，保留原搜索配置')
        return result

    def discover(self, client):
        if not self.path:
            return
        now = time.time()
        try:
            with self._connect() as db:
                db.execute("INSERT OR IGNORE INTO index_inventory VALUES (?,0,0,'',0)", (self.key,))
                claim = db.execute('UPDATE index_inventory SET lease=? WHERE server=? AND lease<=? AND next_check<=?',
                                   (now + 60, self.key, now, now))
                if not claim.rowcount:
                    return
            try:
                names = client.plugins()
            except SearchError:
                with self._connect() as db:
                    db.execute('UPDATE index_inventory SET next_check=?,reason=?,lease=0 WHERE server=?',
                               (now + 300, '插件目录暂不可读取；保留上次名单并继续其他搜索', self.key))
                return
            with self._connect() as db:
                for name in names:
                    db.execute("INSERT INTO index_plugins VALUES (?,?,'pending','',?,0,0,0) "
                               "ON CONFLICT(server,name) DO UPDATE SET seen=excluded.seen, "
                               "state=CASE WHEN state='removed' THEN 'pending' ELSE state END",
                               (self.key, name, now))
                db.execute("UPDATE index_plugins SET state='removed',reason='服务已移除此插件' WHERE server=? AND seen<?",
                           (self.key, now))
                db.execute('DELETE FROM index_plugins WHERE server=? AND name NOT IN '
                           '(SELECT name FROM index_plugins WHERE server=? ORDER BY seen DESC LIMIT 128)', (self.key, self.key))
                db.execute("UPDATE index_inventory SET checked=?,next_check=?,reason='',lease=0 WHERE server=?",
                           (now, now + 3600, self.key))
        except (sqlite3.Error, OSError):
            logging.warning('索引插件发现记录失败，下轮巡检重试')

    def select(self, requested):
        status = self.status()
        if not status['last_discovery']:
            return requested
        rows = status['plugins']
        if requested:
            unknown = set(requested) - {row['name'] for row in rows if row['state'] != 'removed'}
            if unknown:
                raise SearchError('所选搜索插件未在 PanSou 服务启用')
            rows = [row for row in rows if row['name'] in requested]
        available = [row['name'] for row in rows if row['state'] != 'removed' and
                     not (row['state'] == 'cooldown' and row['next_check'] > time.time())]
        if not available:
            raise SearchError('PanSou 没有已启用且可查询的插件；后台巡检将继续检查')
        return available

    def record(self, name, hits=0, reason='', started=None):
        if not self.path or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
            return
        now = time.time()
        state = 'cooldown' if reason else ('active' if hits else 'empty')
        try:
            with self._connect() as db:
                db.execute('UPDATE index_plugins SET state=?,reason=?,checked=?,next_check=?,hits=? '
                           "WHERE server=? AND name=? AND state<>'removed' AND checked<=?",
                           (state, reason, now, now + (300 if reason else 3600), hits,
                            self.key, name, now if started is None else started))
        except (sqlite3.Error, OSError):
            logging.warning('索引插件查询结果记录失败')

    def record_results(self, results, started):
        counts = {}
        for item in results:
            for name in re.findall(r'(?:^| / )plugin:([a-z0-9_-]+)', str(item.get('channel') or '')):
                counts[name] = counts.get(name, 0) + 1
        for name, count in counts.items():
            self.record(name, hits=count, started=started)

    def probe(self, client, requested=()):
        # Two real queries per monitor round; an empty result is not proof of failure.
        due = [row for row in self.status()['plugins'] if row['state'] != 'removed' and
               row['next_check'] <= time.time() and (not requested or row['name'] in requested)]
        due.sort(key=lambda row: (row['next_check'], row['name']))
        for row in due[:2]:
            health = SourceHealth(self.path)
            try:
                health.check(self.server)
            except SearchError:
                break
            started = time.time()
            try:
                results = client.search('庆余年', plugins=[row['name']], refresh=True, timeout=(3, 10), wait_for_index=False)
                self.record(row['name'], hits=len(results), started=started)
            except SearchError as error:
                from resource_search import search_error_reason
                reason = search_error_reason(error)
                self.record(row['name'], reason=reason, started=started)
                response = getattr(error.__cause__, 'response', None)
                health.record(self.server, reason, response.headers.get('Retry-After') if response is not None else None)
