import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from telegram_web.worker import AUTH_STATE, TelegramWebWorker, WEB_URL


class FakeLocator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    def filter(self, **kwargs):
        return self

    def count(self):
        return int(self.kind != "button")

    def is_visible(self):
        return True

    def is_enabled(self):
        return not getattr(self.page, "password_disabled", False)

    def element_handle(self, **kwargs):
        return self

    def evaluate(self, source):
        self.page.fills.append("")

    def dispose(self):
        pass

    def screenshot(self, **kwargs):
        return b"\x89PNG\r\n\x1a\nfixture"

    def fill(self, value, **kwargs):
        self.page.fills.append(value)

    def press(self, key, **kwargs):
        self.page.presses.append(key)
        self.page.state = "authorized"

    def get_attribute(self, name):
        return ""


class FakePage:
    def __init__(self):
        self.state = "waiting_qr"
        self.urls, self.fills, self.presses, self.threads = [], [], [], []
        self.search_result = {"success": True, "results": [], "archived_count": 9,
                              "partial": False, "progress": {"phase": "complete", "current": 3, "total": 3}}
        self.logout_error = False

    def goto(self, url, **kwargs):
        self.urls.append(url)
        self.threads.append(threading.get_ident())

    def evaluate(self, source, arg=None):
        self.threads.append(threading.get_ident())
        if source == AUTH_STATE:
            return self.state
        if "auth.logOut" in source:
            if self.logout_error:
                raise RuntimeError("private credential must not leak")
            return True
        self.last_search = arg
        return self.search_result

    def locator(self, selector):
        return FakeLocator(self, "password" if "password" in selector else "qr")

    def get_by_role(self, *args, **kwargs):
        return FakeLocator(self, "button")

    def wait_for_function(self, *args, **kwargs):
        return None


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def set_default_timeout(self, value):
        pass

    def close(self):
        self.closed = True


class TelegramWebWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.now = 100.0
        self.launches = []

        def factory(path):
            self.launches.append(path)
            return self.context

        self.worker = TelegramWebWorker(self.temp.name, clock=lambda: self.now, browser_factory=factory)

    def tearDown(self):
        self.worker.close()
        self.temp.cleanup()

    def start(self):
        return self.worker.submit("start")

    def authorize(self):
        result = self.start()
        self.page.state = "authorized"
        self.worker.submit("poll", {"id": result["attempt_id"]})

    def test_status_does_not_start_browser_or_expose_qr(self):
        result = self.worker.submit("status")
        self.assertEqual(result["data"]["state"], "disconnected")
        self.assertEqual(self.launches, [])
        self.assertNotIn("qr", result)
        self.assertNotIn("attempt_id", result)

    def test_qr_is_only_returned_by_explicit_attempt(self):
        result = self.start()
        self.assertRegex(result["attempt_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(result["qr"]["id"], result["attempt_id"])
        self.assertTrue(result["qr"]["image"].startswith("data:image/png;base64,"))
        self.assertNotIn("qr", self.worker.submit("status"))
        self.assertEqual(self.page.urls, [WEB_URL])
        self.assertEqual(len(set(self.page.threads)), 1)
        self.assertNotEqual(self.page.threads[0], threading.get_ident())

    def test_replaced_qr_refuses_old_poll_and_password(self):
        old = self.start()["attempt_id"]
        new = self.start()["attempt_id"]
        self.assertNotEqual(old, new)
        self.page.state = "waiting_password"
        self.assertEqual(self.worker.submit("poll", {"id": old})["code"], "STALE_ATTEMPT")
        self.assertEqual(self.worker.submit("password", {"id": old, "password": "secret"})["code"], "STALE_ATTEMPT")
        self.assertEqual(self.page.fills, [])

    def test_password_uses_official_field_only_and_clears_it(self):
        attempt = self.start()["attempt_id"]
        self.page.state = "waiting_password"
        result = self.worker.submit("password", {"id": attempt, "password": "fixture secret"})
        self.assertTrue(result["data"]["authorized"])
        self.assertEqual(self.page.presses, ["Enter"])
        self.assertEqual(self.page.fills, ["fixture secret", ""])
        self.assertNotIn("fixture secret", json.dumps(result))

    def test_password_still_being_verified_is_not_submitted_twice(self):
        attempt = self.start()["attempt_id"]
        self.page.state = "waiting_password"
        self.page.password_disabled = True
        result = self.worker.submit("password", {"id": attempt, "password": "fixture secret"})
        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "PASSWORD_PENDING")
        self.assertEqual(self.page.fills, [])
        self.assertEqual(self.page.presses, [])

    def test_login_expires_and_old_attempt_cannot_revive(self):
        attempt = self.start()["attempt_id"]
        self.now += 301
        result = self.worker.submit("poll", {"id": attempt})
        self.assertEqual(result["data"]["state"], "disconnected")
        self.assertTrue(self.context.closed)
        self.assertNotIn("qr", result)
        self.assertFalse(self.worker.submit("poll", {"id": attempt})["success"])

    def test_search_without_login_never_opens_browser(self):
        result = self.worker.submit("search", {"query": "电视剧"})
        self.assertEqual(result["code"], "AUTH_REQUIRED")
        self.assertEqual(self.launches, [])

    def test_search_budget_and_progress(self):
        self.authorize()
        self.page.search_result["bot"] = {"requests": 1, "links": 2, "pending": 0, "failed": 0}
        result = self.worker.submit("search", {"query": " 测试作品 ", "limit": 300, "timeout": 90})
        self.assertTrue(result["success"])
        self.assertEqual(self.page.last_search, {"query": "测试作品", "limit": 100, "timeout": 45.0})
        self.assertEqual(result["data"]["archived_count"], 9)
        self.assertEqual(result["progress"]["phase"], "complete")
        self.assertEqual(result["bot"]["links"], 2)

    def test_flood_wait_skips_following_browser_request(self):
        self.authorize()
        self.page.search_result = {"success": False, "code": "FLOOD_WAIT", "retry_after": 60}
        self.worker.submit("search", {"query": "测试作品"})
        calls = len(self.page.threads)
        result = self.worker.submit("search", {"query": "另一作品"})
        self.assertEqual(result["code"], "FLOOD_WAIT")
        self.assertEqual(len(self.page.threads), calls)

    def test_logout_clears_profile_even_when_remote_fails(self):
        self.authorize()
        (Path(self.temp.name) / "browser" / "fixture-session").write_text("private")
        self.page.logout_error = True
        result = self.worker.submit("logout")
        self.assertFalse(result["data"]["authorized"])
        self.assertEqual(result["data"]["state"], "disconnected")
        self.assertEqual(result["code"], "LOGOUT_UNCONFIRMED")
        self.assertFalse((Path(self.temp.name) / "browser").exists())
        self.assertFalse((Path(self.temp.name) / "authorized").exists())
        self.assertNotIn("private credential", json.dumps(result))

    def test_restart_restores_only_previously_authorized_profile(self):
        self.authorize()
        self.worker.close()
        self.context = FakeContext(self.page)
        self.worker = TelegramWebWorker(self.temp.name, browser_factory=lambda path: self.context)
        self.assertFalse(self.worker.submit("status")["data"]["authorized"])
        self.assertIsNone(self.worker._context)
        self.assertTrue(self.worker.submit("search", {"query": "作品"})["data"]["authorized"])

    def test_logout_after_restart_reports_unconfirmed_remote_revocation(self):
        self.authorize()
        self.worker.close()
        self.worker = TelegramWebWorker(self.temp.name, browser_factory=lambda path: self.context)
        result = self.worker.submit("logout")
        self.assertEqual(result["code"], "LOGOUT_UNCONFIRMED")
        self.assertFalse((Path(self.temp.name) / "browser").exists())

    def test_restored_session_waits_for_official_page_initialization(self):
        self.authorize()
        self.worker.close()
        self.page.state = "starting"
        self.page.wait_for_function = lambda *args, **kwargs: setattr(self.page, "state", "authorized")
        self.worker = TelegramWebWorker(self.temp.name, browser_factory=lambda path: FakeContext(self.page))
        result = self.worker.submit("search", {"query": "作品"})
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["authorized"])

    def test_restored_session_initialization_timeout_keeps_profile(self):
        self.authorize()
        self.worker.close()
        self.page.state = "starting"

        def unavailable(*args, **kwargs):
            raise TimeoutError("fixture only")

        self.page.wait_for_function = unavailable
        self.worker = TelegramWebWorker(self.temp.name, browser_factory=lambda path: FakeContext(self.page))
        result = self.worker.submit("search", {"query": "作品"})
        self.assertEqual(result["code"], "TIMEOUT")
        self.assertTrue((Path(self.temp.name) / "authorized").exists())

    def test_logout_barrier_cancels_queued_start_and_stale_running_result(self):
        entered, release = threading.Event(), threading.Event()
        original = self.worker._start

        def delayed(payload):
            entered.set()
            release.wait(3)
            return original(payload)

        self.worker._start = delayed
        outcomes = []
        running = threading.Thread(target=lambda: outcomes.append(self.worker.submit("start")))
        running.start()
        self.assertTrue(entered.wait(2))
        queued = threading.Thread(target=lambda: outcomes.append(self.worker.submit("start")))
        queued.start()
        # Wait only on the local queue, never real browser or network operations.
        for _ in range(100):
            if self.worker._queue.qsize():
                break
            threading.Event().wait(.005)
        logout = threading.Thread(target=lambda: outcomes.append(self.worker.submit("logout")))
        logout.start()
        for _ in range(100):
            if self.worker._generation:
                break
            threading.Event().wait(.005)
        release.set()
        for thread in (running, queued, logout):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(sum(r.get("code") == "STALE_ATTEMPT" for r in outcomes), 2)
        self.assertEqual(self.worker.submit("status")["data"]["state"], "disconnected")
        self.assertIsNone(self.worker._attempt)

    def test_invalid_actions_and_payload_are_rejected(self):
        self.assertFalse(self.worker.submit("eval", {"script": "bad"})["success"])
        self.assertFalse(self.worker.submit("start", ["bad"])["success"])
        self.assertFalse(self.worker.submit("search", {"query": ""})["success"])
        self.assertEqual(self.launches, [])


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(NODE, "Node is needed to execute the isolated browser bridge fixtures")
class TelegramArchiveBridgeTests(unittest.TestCase):
    def run_bridge(self, scenario):
        setup = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const bridge = (new Function('return (' + fs.readFileSync(process.argv[1], 'utf8') + '\n)'))();
global.window = global;
global.location = {origin: 'https://web.telegram.org', pathname: '/k/'};
let calls = [];
global.rootScope = {myId: 777, managers: {apiManager: {invokeApi: async (method, params, options) => {
  calls.push({method, params, options});
  if (global.validateBotCall && ['contacts.resolveUsername','messages.startBot','messages.getHistory','messages.search'].includes(method)) {
    global.validateBotCall(method, params);
  } else {
    assert.equal(params.folder_id, 1);
    assert.ok(['messages.getDialogs', 'messages.getPinnedDialogs', 'messages.searchGlobal'].includes(method));
  }
  assert.equal(options.floodMaxTimeout, 0);
  return await response(method, params);
}}}};
const user = {_: 'user', id: 1, access_hash: 'PRIVATE_ACCESS_HASH', first_name: 'Friend', username: 'frienduser'};
const chat = {_: 'chat', id: 2, title: 'Archived group'};
const channel = {_: 'channel', id: 3, access_hash: 'PRIVATE_ACCESS_HASH', title: 'Archived channel', username: 'publicchannel'};
const peers = [{_: 'peerUser', user_id: 1}, {_: 'peerChat', chat_id: 2}, {_: 'peerChannel', channel_id: 3}];
const dialog = (peer, folder = 1, id = 9) => ({_: 'dialog', folder_id: folder, peer, top_message: id});
const message = (peer, id, body) => ({_: 'message', peer_id: peer, id, date: 1000-id, message: body});
const allDialogs = () => ({_: 'messages.dialogs', dialogs: peers.map(p=>dialog(p)), messages: [], users:[user], chats:[chat,channel]});
const emptyPinned = () => ({_: 'messages.peerDialogs', dialogs: [], users: [], chats: [], messages: []});
const run = (query='作品', limit=30) => bridge({query,limit,timeout:2});
"""
        script = setup + "\n(async()=>{\n" + scenario + "\nconsole.log('OK');})().catch(e=>{console.error(e);process.exitCode=1;});"
        result = subprocess.run([NODE, "-e", script, str(ROOT / "telegram_web/archive_bridge.js")],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_all_archive_types_and_strict_unarchived_exclusion(self):
        self.run_bridge(r"""
global.response = async method => method==='messages.getPinnedDialogs' ? emptyPinned()
 : method==='messages.getDialogs' ? {...allDialogs(), dialogs:[...peers.map(p=>dialog(p)),dialog({_: 'peerUser',user_id:99},0)]}
 : {_: 'messages.messages', messages:[...peers.map((p,i)=>message(p,i+1,`作品 https://pan.quark.cn/s/abcdef00000${i}`)),
     message({_: 'peerUser',user_id:99},40,'UNARCHIVED_SECRET https://pan.quark.cn/s/abcdef999999')],users:[user],chats:[chat,channel]};
const result=await run();assert.equal(result.archived_count,3);assert.equal(result.results.length,3);
assert.equal(result.results[0].source_url,undefined);assert.equal(result.results[1].source_url,undefined);
assert.equal(result.results[2].source_url,'https://t.me/publicchannel/3');
assert.ok(!JSON.stringify(result).includes('UNARCHIVED_SECRET'));
assert.ok(!JSON.stringify(result).includes('PRIVATE_ACCESS_HASH'));
assert.ok(calls.find(c=>c.method==='messages.getDialogs').params.exclude_pinned);
assert.ok(calls.every(c=>!c.method.includes('readHistory')&&!c.method.includes('join')&&!c.method.includes('send')));
""")

    def test_archive_and_message_pagination_use_all_offsets(self):
        self.run_bridge(r"""
let archivePage=0,searchPage=0;
global.response=async (method,params)=>{
 if(method==='messages.getPinnedDialogs')return emptyPinned();
 if(method==='messages.getDialogs'){
  if(archivePage++===0)return {_: 'messages.dialogsSlice',count:3,dialogs:[dialog(peers[0])],messages:[message(peers[0],9,'metadata')],users:[user],chats:[]};
  assert.equal(params.offset_id,9);assert.equal(params.offset_date,991);assert.equal(params.offset_peer._,'inputPeerUser');return allDialogs();
 }
 if(searchPage++===0)return {_: 'messages.messagesSlice',count:2,next_rate:321,messages:[message(peers[1],20,'作品 https://pan.quark.cn/s/abcdef111111')],users:[],chats:[chat]};
 assert.equal(params.offset_rate,321);assert.equal(params.offset_id,20);assert.equal(params.offset_peer._,'inputPeerChat');
 return {_: 'messages.messages',messages:[message(peers[2],19,'作品 https://pan.quark.cn/s/abcdef222222')],users:[],chats:[channel]};
};
const result=await run();assert.equal(archivePage,2);assert.equal(searchPage,2);assert.equal(result.results.length,2);assert.equal(result.partial,false);
""")

    def test_resource_url_and_copy_buttons_are_indexed_without_clicking(self):
        self.run_bridge(r"""
const post=message(peers[2],12,'作品名 提取码: a1b2');
post.reply_markup={_: 'replyInlineMarkup',rows:[{buttons:[
 {_: 'keyboardButtonUrl',text:'获取资源',url:'https://pan.quark.cn/s/abcdef111111'},
 {_: 'keyboardButtonCopy',text:'复制链接',copy_text:'https://pan.quark.cn/s/abcdef111111'},
 {_: 'keyboardButtonUrl',text:'广告',url:'https://example.com/tracker'},
 {_: 'keyboardButtonCallback',text:'领取',data:'NO_CALLBACK'}]}]};
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'
 ? allDialogs() : {_: 'messages.messages',messages:[post],users:[],chats:[channel]};
const result=await run();assert.equal(result.results.length,1);
assert.equal(result.results[0].url,'https://pan.quark.cn/s/abcdef111111?pwd=a1b2');
assert.equal(calls.length,3);
""")

    def run_bot_bridge(self, scenario):
        self.run_bridge(r"""
const oscar={_: 'user',id:42,access_hash:'BOT_PRIVATE_HASH',username:'Oscar4KBot',pFlags:{bot:true}};
const botPeer={_: 'peerUser',user_id:42};
const post=message(peers[2],12,'作品 第4季');
post.reply_markup={rows:[{buttons:[{_: 'keyboardButtonUrl',text:'📥 获取资源',url:'tg://resolve?domain=Oscar4KBot&start=get59171'}]}]};
let posts=[post], starts=0, historyReads=0, botResolved=oscar, replyOverride, archived=true;
const reply=(id,body,extra={})=>({...message(botPeer,id,body),from_id:botPeer,...extra});
global.validateBotCall=(method,params)=>{
  if(method==='contacts.resolveUsername')assert.equal(params.username,'Oscar4KBot');
  else if(method==='messages.search'){assert.equal(params.peer.channel_id,3);assert.equal(params.limit,20);}
  else {assert.equal(params.peer.user_id,42);assert.equal(params.peer.access_hash,'BOT_PRIVATE_HASH');}
  if(method==='messages.startBot'){
    assert.equal(params.bot.user_id,42);assert.equal(params.start_param,'get59171');
    assert.match(params.random_id,/^-?\d+$/);
  }
};
global.response=async(method,params)=>{
  if(method==='messages.getPinnedDialogs')return emptyPinned();
  if(method==='messages.getDialogs')return {...allDialogs(),dialogs:archived?allDialogs().dialogs:[]};
  if(method==='messages.searchGlobal')return {_: 'messages.messages',messages:posts,users:[],chats:[channel]};
  if(method==='contacts.resolveUsername')return {peer:botPeer,users:[botResolved]};
  if(method==='messages.startBot'){
    starts++;return {updates:[{_: 'updateNewMessage',message:reply(101,'/start get59171',{pFlags:{out:true}})}]};
  }
  if(method==='messages.getHistory'){
    historyReads++;
    if(params.limit===1)return {messages:[reply(100,'作品 https://pan.quark.cn/s/OLD000111111')]};
    assert.equal(params.min_id,100);
    return {messages:replyOverride?replyOverride():[reply(102,'#作品 https://pan.quark.cn/s/NEW000111111')]};
  }
  throw Error('unexpected method');
};
""" + scenario)

    def test_oscar_start_reply_is_prioritized_and_keeps_archived_provenance(self):
        self.run_bot_bridge(r"""
posts.unshift(message(peers[2],15,'作品 https://pan.quark.cn/s/STALE0111111'));
const result=await run('作品',1);
assert.equal(starts,1);assert.equal(result.results[0].url,'https://pan.quark.cn/s/NEW000111111');
assert.equal(result.results[0].title,'作品 第4季');assert.equal(result.results[0].source_url,'https://t.me/publicchannel/12');
assert.equal(result.bot.requests,1);assert.equal(result.bot.links,1);assert.equal(result.partial,true);
assert.ok(!JSON.stringify(result).includes('BOT_PRIVATE_HASH'));assert.ok(!JSON.stringify(result).includes('get59171'));
const second=await run('作品',1);assert.equal(starts,1);assert.equal(second.results[0].url,'https://pan.quark.cn/s/STALE0111111');
""")

    def test_archived_oscar_channel_is_searched_before_global_stale_results(self):
        self.run_bot_bridge(r"""
channel.username='Oscar_4Kmovies';
const original=response;
global.response=async(method,params)=>{
 if(method==='messages.search')return {messages:[post]};
 if(method==='messages.searchGlobal')return {_: 'messages.messages',messages:[message(peers[2],20,'作品 https://pan.quark.cn/s/STALE0111111')]};
 return original(method,params);
};
const result=await run('作品',1);
assert.equal(result.results[0].url,'https://pan.quark.cn/s/NEW000111111');
assert.equal(result.bot.channel_posts,1);assert.equal(result.bot.buttons,1);assert.equal(starts,1);
assert.ok(!calls.some(c=>c.method==='messages.searchGlobal'));
await run('作品',1);assert.equal(calls.filter(c=>c.method==='messages.search').length,1);
""")

    def test_unarchived_oscar_channel_is_never_searched_directly(self):
        self.run_bot_bridge(r"""
channel.username='Oscar_4Kmovies';archived=false;
await run();assert.ok(!calls.some(c=>c.method==='messages.search'));assert.equal(starts,0);
""")

    def test_archived_oscar_title_and_active_secondary_username_are_recognized(self):
        for identity in ("channel.title='奥斯卡4K蓝光(精品)影视磁力站🍿';delete channel.username;",
                         "delete channel.username;channel.usernames=[{username:'Oscar_4Kmovies',pFlags:{active:true}}];"):
            self.run_bot_bridge(identity + r"""
const original=response;
global.response=async(method,params)=>method==='messages.search'?{messages:[post]}:original(method,params);
const result=await run('作品',1);
assert.equal(result.bot.channel_archived,1);assert.equal(starts,1);
assert.equal(result.results[0].url,'https://pan.quark.cn/s/NEW000111111');
assert.ok(!calls.some(c=>c.method==='messages.searchGlobal'));
""")

    def test_unarchived_oscar_title_and_inactive_username_do_not_authorize_search(self):
        for identity in ("channel.title='奥斯卡4K蓝光(精品)影视磁力站';archived=false;",
                         "channel.usernames=[{username:'Oscar_4Kmovies',pFlags:{}}];posts=[];"):
            self.run_bot_bridge(identity + r"""
const result=await run();assert.equal(result.bot.channel_archived,0);
assert.ok(!calls.some(c=>c.method==='messages.search'));assert.equal(starts,0);
""")

    def test_oscar_recognition_is_retried_for_unfinished_query_after_metadata_changes(self):
        self.run_bot_bridge(r"""
posts=[message(peers[2],11,'作品 https://pan.quark.cn/s/STALE0111111'),
       message(peers[2],10,'作品 https://pan.quark.cn/s/STALE0222222')];
assert.equal((await run('作品',1)).partial,true);assert.equal(starts,0);
channel.title='奥斯卡4K蓝光(精品)影视磁力站';
const original=response;
global.response=async(method,params)=>method==='messages.search'?{messages:[post]}:original(method,params);
const result=await run('作品',1);assert.equal(result.bot.channel_archived,1);assert.equal(starts,1);
assert.equal(result.results[0].url,'https://pan.quark.cn/s/NEW000111111');
""")

    def test_oscar_https_buttons_and_delayed_reply_copy_buttons(self):
        self.run_bot_bridge(r"""
post.reply_markup.rows[0].buttons[0].url='https://t.me/Oscar4KBot?start=get59171';
let polls=0;
replyOverride=()=>++polls===1?[reply(102,'正在获取')]:[reply(102,'#作品',{
 reply_markup:{rows:[{buttons:[{_: 'keyboardButtonCopy',copy_text:'https://pan.quark.cn/s/NEW000111111'}]}]}})];
const result=await run();assert.equal(polls,2);assert.equal(starts,1);assert.equal(result.results.length,1);
""")

    def test_oscar_ignores_old_unrelated_and_forged_replies(self):
        self.run_bot_bridge(r"""
replyOverride=()=>[
 reply(100,'作品 https://pan.quark.cn/s/OLD000111111'),
 reply(102,'其他作品 https://pan.quark.cn/s/OTHER0111111'),
 reply(103,'一念永恒 https://pan.quark.cn/s/FORGED111111',{from_id:{_: 'peerUser',user_id:43}}),
 reply(104,'一念永恒 https://pan.quark.cn/s/OUT000111111',{pFlags:{out:true}}),
 reply(105,'资源 https://pan.quark.cn/s/RIGHT0111111',{reply_to:{reply_to_msg_id:101}})];
const result=await run('一念永恒');assert.equal(result.results.length,1);assert.ok(result.results[0].url.includes('RIGHT0'));
""")

    def test_unsupported_or_unarchived_resource_buttons_never_start_a_bot(self):
        self.run_bot_bridge(r"""
const urls=[
 'tg://resolve?domain=OtherBot&start=get59171','tg://resolve?domain=Oscar4KBot&start=pay59171',
 'tg://resolve?domain=Oscar4KBot&start=get59171&start=other',
 'tg://resolve?domain=Oscar4KBot&start=get59171&admin=1',
 'https://t.me@evil.example/Oscar4KBot?start=get59171',
 'https://t.me/Oscar4KBot?start=get59171#extra'];
posts=urls.map((url,i)=>({...post,id:i+1,reply_markup:{rows:[{buttons:[{_: 'keyboardButtonUrl',text:'获取资源',url}]}]}}));
posts.push({...post,peer_id:{_: 'peerChannel',channel_id:99}});
posts.push({...post,id:88,reply_markup:{rows:[{buttons:[{_: 'keyboardButtonCallback',text:'获取资源',data:'NO'}]}]}});
const result=await run();assert.equal(starts,0);assert.equal(result.results.length,0);
assert.ok(!calls.some(c=>c.method==='contacts.resolveUsername'));
""")

    def test_non_bot_resolution_preserves_direct_candidates_and_reports_failure(self):
        self.run_bot_bridge(r"""
botResolved={...oscar,pFlags:{}};
posts.push(message(peers[2],14,'作品 https://pan.quark.cn/s/DIRECT111111'));
const result=await run();assert.equal(starts,0);assert.equal(result.results.length,1);assert.equal(result.bot.failed,1);
""")

    def test_ambiguous_start_timeout_is_not_repeated_and_resumes_reply_read(self):
        self.run_bot_bridge(r"""
const original=response;
global.response=async(method,params)=>{
 if(method==='messages.startBot'){starts++;throw {type:'BUDGET_EXHAUSTED'};}
 return original(method,params);
};
const first=await run();assert.equal(first.code,'BUDGET_EXHAUSTED');assert.equal(first.bot.pending,1);
const second=await run();assert.equal(starts,1);assert.equal(second.results.length,1);
""")

    def test_pending_bot_button_removed_from_archive_is_not_read(self):
        self.run_bot_bridge(r"""
const original=response;
global.response=async(method,params)=>{
 if(method==='messages.startBot'){starts++;throw {type:'BUDGET_EXHAUSTED'};}
 return original(method,params);
};
await run();archived=false;const previous=historyReads;
const result=await run();assert.equal(starts,1);assert.equal(historyReads,previous);assert.equal(result.results.length,0);
""")

    def test_oscar_cache_reuses_reply_then_expires_and_honors_account_change(self):
        self.run_bot_bridge(r"""
await run();await run();assert.equal(starts,1);
const job=window.__qasArchiveReadState.botJobs.get('get59171');job.created-=16*60*1000;
await run();assert.equal(starts,2);
rootScope.myId=778;await run();assert.equal(starts,3);
""")

    def test_oscar_flood_wait_stops_requests_and_account_switch_prevents_start(self):
        self.run_bot_bridge(r"""
const original=response;
global.response=async(method,params)=>{
 if(method==='contacts.resolveUsername')throw {type:'FLOOD_WAIT_30'};
 return original(method,params);
};
const first=await run();assert.equal(first.code,'FLOOD_WAIT');assert.equal(first.retry_after,30);assert.equal(starts,0);
global.response=async(method,params)=>{
 const value=await original(method,params);
 if(method==='contacts.resolveUsername')rootScope.myId=888;
 return value;
};
const second=await run();assert.equal(second.code,'AUTH_REQUIRED');assert.equal(starts,0);
""")

    def test_oscar_pending_replies_resume_without_resending_then_report_exhaustion(self):
        self.run_bot_bridge(r"""
replyOverride=()=>[reply(102,'请稍候')];
const first=await run();assert.equal(first.bot.pending,1);assert.equal(first.partial,true);assert.equal(starts,1);
window.__qasArchiveReadState.botJobs.get('get59171').polls=2;
const second=await run();assert.equal(second.bot.failed,1);assert.equal(second.bot.pending,0);assert.equal(starts,1);
assert.equal(second.partial,false);assert.equal(second.results.length,0);
""")

    def test_forbidden_last_dialog_can_page_to_readable_archive(self):
        self.run_bridge(r"""
let archivePage=0;
const forbidden={...channel, _: 'channelForbidden'};
global.response=async(method,params)=>{
 if(method==='messages.getPinnedDialogs')return emptyPinned();
 if(method==='messages.getDialogs'){
  if(archivePage++===0)return {_: 'messages.dialogsSlice',count:2,dialogs:[dialog(peers[2])],messages:[message(peers[2],9,'metadata')],users:[],chats:[forbidden]};
  assert.equal(params.offset_id,9);assert.equal(params.offset_peer.channel_id,3);
  return {...allDialogs(),dialogs:[dialog(peers[0])],chats:[]};
 }
 return {_: 'messages.messages',messages:[message(peers[2],8,'BLOCKED https://pan.quark.cn/s/abcdef111111'),message(peers[0],7,'作品 https://pan.quark.cn/s/abcdef222222')],users:[user],chats:[forbidden]};
};
const result=await run();assert.equal(result.success,true);assert.equal(archivePage,2);
assert.equal(result.archived_count,1);assert.equal(result.results.length,1);
assert.ok(!JSON.stringify(result).includes('BLOCKED'));
""")

    def test_search_pagination_falls_back_to_last_message_date_without_next_rate(self):
        self.run_bridge(r"""
let page=0;
global.response=async(method,params)=>{
 if(method==='messages.getPinnedDialogs')return emptyPinned();
 if(method==='messages.getDialogs')return allDialogs();
 if(page++===0)return {_: 'messages.messagesSlice',count:2,
   messages:[message(peers[1],20,'作品 https://pan.quark.cn/s/abcdef111111')],users:[],chats:[chat]};
 assert.equal(params.offset_rate,980);assert.equal(params.offset_id,20);
 return {_: 'messages.messages',messages:[message(peers[1],19,'作品 https://pan.quark.cn/s/abcdef222222')],users:[],chats:[chat]};
};
const result=await run();assert.equal(result.results.length,2);assert.equal(result.partial,false);
""")

    def test_small_result_limit_keeps_buffer_and_rechecks_archive(self):
        self.run_bridge(r"""
let removed=false, searches=0;
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'
 ? {...allDialogs(),dialogs:removed?[dialog(peers[0])]:peers.map(p=>dialog(p))}
 : (searches++, {_: 'messages.messages',messages:peers.map((p,i)=>message(p,i+1,`作品 https://pan.quark.cn/s/abcdef00000${i}`)),users:[user],chats:[chat,channel]});
const first=await run('作品',1);assert.equal(first.results.length,1);assert.equal(first.partial,true);
removed=true;const second=await run('作品',1);assert.equal(second.results.length,0);assert.equal(searches,1);assert.equal(second.partial,false);
""")

    def test_query_switch_clears_old_cursor_even_with_incomplete_archive(self):
        self.run_bridge(r"""
window.__qasArchiveReadState={account:'777',archives:{peers:new Map(),pinned:true,complete:false,date:0,id:0,peer:{_:'inputPeerEmpty'},total:0,cursor:''},
 searches:new Map([['OLD',{query:'OLD',id:987,peer:{_: 'inputPeerUser',user_id:1},rate:55,pending:[{peerKey:'u:1',candidate:{title:'OLD_SECRET',url:'bad'}}],urls:new Set(),done:false,used:Date.now()}]])};
global.response=async(method,params)=>{
 if(method==='messages.getDialogs')return allDialogs();
 assert.equal(method,'messages.searchGlobal');assert.equal(params.q,'NEW');assert.equal(params.offset_id,0);assert.equal(params.offset_rate,0);
 return {_: 'messages.messages',messages:[],users:[],chats:[]};
};
const result=await run('NEW');assert.ok(!JSON.stringify(result).includes('OLD_SECRET'));assert.equal(result.partial,false);
""")

    def test_archive_failure_never_falls_back_to_unscoped_search(self):
        self.run_bridge(r"""
global.response=async method=>{if(method==='messages.getPinnedDialogs')return emptyPinned();throw {type:'FLOOD_WAIT_25'};};
const result=await run();assert.equal(result.code,'FLOOD_WAIT');assert.equal(result.retry_after,25);assert.equal(result.results.length,0);
assert.equal(calls.some(c=>c.method==='messages.searchGlobal'),false);
""")

    def test_hidden_links_are_extracted_without_query_as_title(self):
        self.run_bridge(r"""
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'?allDialogs()
 :{_: 'messages.messages',messages:[{...message(peers[0],1,'Actual release title'),entities:[{_: 'messageEntityTextUrl',url:'https://pan.quark.cn/s/abcdef123456'}]}],users:[user],chats:[]};
const result=await run('SEARCH TERM');assert.equal(result.results[0].title,'Actual release title');assert.equal(result.results[0].url,'https://pan.quark.cn/s/abcdef123456');
""")

    def test_multiple_queries_keep_separate_resume_buffers(self):
        self.run_bridge(r"""
let searches=0;
global.response=async(method,params)=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'?allDialogs()
 : (searches++, {_: 'messages.messages',messages:[message(peers[0],1,`${params.q} https://pan.quark.cn/s/${params.q}000001`),message(peers[0],2,`${params.q} https://pan.quark.cn/s/${params.q}000002`)],users:[user],chats:[]});
const a=await run('AAAAAA',1);const b=await run('BBBBBB',1);const resumed=await run('AAAAAA',1);
assert.equal(searches,2);assert.equal(resumed.results[0].url,'https://pan.quark.cn/s/AAAAAA000002');
assert.ok(!JSON.stringify(resumed.results).includes('BBBBBB'));assert.equal(resumed.partial,false);
""")

    def test_access_code_and_selected_subfolder_are_preserved(self):
        self.run_bridge(r"""
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'?allDialogs()
 :{_: 'messages.messages',messages:[message(peers[0],1,'作品 https://pan.quark.cn/s/abcdef123456?pwd=ABCD#/list/share/subfolder'),message(peers[0],2,'作品 https://pan.quark.cn/s/abcdef222222 提取码：X9Y8')],users:[user],chats:[]};
const result=await run();assert.equal(result.results[0].url,'https://pan.quark.cn/s/abcdef123456?pwd=ABCD#/list/share/subfolder');
assert.equal(result.results[1].url,'https://pan.quark.cn/s/abcdef222222?pwd=X9Y8');
""")

    def test_multi_share_message_never_assigns_first_access_code_to_every_share(self):
        self.run_bridge(r"""
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'?allDialogs()
 :{_: 'messages.messages',messages:[message(peers[0],1,'A https://pan.quark.cn/s/abcdef111111 提取码AAAA\nB https://pan.quark.cn/s/abcdef222222 提取码BBBB')],users:[user],chats:[]};
const result=await run();assert.equal(result.results.length,2);assert.ok(result.results.every(r=>!r.url.includes('pwd=')));
""")

    def test_restricted_kicked_and_deleted_archived_peers_are_excluded(self):
        self.run_bridge(r"""
user.pFlags={deleted:true};chat.pFlags={kicked:true};channel.pFlags={restricted:true};
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():allDialogs();
const result=await run();assert.equal(result.archived_count,0);assert.equal(result.results.length,0);
assert.equal(calls.some(c=>c.method==='messages.searchGlobal'),false);
""")

    def test_resume_cache_is_bounded_and_expired_entries_restart(self):
        self.run_bridge(r"""
let searches=0;
global.response=async(method,params)=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'?allDialogs()
 : (searches++,{_: 'messages.messages',messages:[message(peers[0],1,'作品 https://pan.quark.cn/s/abcdef111111'),message(peers[0],2,'作品 https://pan.quark.cn/s/abcdef222222')],users:[user],chats:[]});
for(let i=0;i<22;i++)await run('title '+i,1);
assert.equal(window.__qasArchiveReadState.searches.size,20);
assert.equal(window.__qasArchiveReadState.searches.has('title 0'),false);
window.__qasArchiveReadState.searches.get('title 21').used=0;
await run('title 21',1);assert.equal(searches,23);
""")

    def test_unfinished_archive_search_resumes_at_next_weekly_run(self):
        self.run_bridge(r"""
let searches=0;
global.response=async method=>method==='messages.getPinnedDialogs'?emptyPinned():method==='messages.getDialogs'
 ? allDialogs() : (searches++,{_: 'messages.messages',messages:[
 message(peers[0],1,'作品 https://pan.quark.cn/s/abcdef111111'),
 message(peers[0],2,'作品 https://pan.quark.cn/s/abcdef222222')],users:[user],chats:[]});
await run('作品',1);
window.__qasArchiveReadState.searches.get('作品').used-=7*24*60*60*1000;
const result=await run('作品',1);
assert.equal(searches,1);assert.equal(result.results[0].url,'https://pan.quark.cn/s/abcdef222222');
""")

    def test_account_switch_during_call_drops_payload(self):
        self.run_bridge(r"""
global.response=async()=>{rootScope.myId=888;return allDialogs();};
const result=await run();assert.equal(result.code,'AUTH_REQUIRED');assert.equal(result.results.length,0);
""")

    def test_wrong_origin_never_calls_bridge(self):
        self.run_bridge(r"""
location.origin='https://example.test';global.response=async()=>{throw Error('must not call');};
const result=await run();assert.equal(result.code,'BRIDGE_UNAVAILABLE');assert.equal(calls.length,0);
""")


if __name__ == "__main__":
    unittest.main()
