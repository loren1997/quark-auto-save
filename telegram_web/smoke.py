"""Offline container check: start Chromium, execute JavaScript, persist a profile."""
import tempfile
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright
from telegram_web.server import make_handler
from telegram_web.worker import TelegramWebWorker, WEB_URL


def check_password_login(page, folder):
    # Mirrors WebK PasswordInputHelpers' two opacity-zero autofill decoys.
    fixture = """<!doctype html><style>
      .stealthy{opacity:0;pointer-events:none;z-index:-1;outline:none;max-width:1px;
      max-height:1px;margin:0;position:absolute;top:0;left:0;overflow:hidden}
      </style><div class="_pagePassword_fixture_1"><div class="input-field">
      <input type="password" class="stealthy" tabindex="-1" aria-hidden="true">
      <input id="real-password" type="password" name="notsearch_password" class="input-field-input">
      <input type="password" class="stealthy" tabindex="-1" aria-hidden="true">
      </div></div><textarea id="chat-composer"></textarea><script>
      window.rootScope = {managers:{apiManager:{invokeApi(){throw Error('No network in fixture');}}}};
      window.submitted = 0;
      document.querySelector('#real-password').addEventListener('keypress', event => {
        if(event.key !== 'Enter') return;
        window.submitted++;
        const input = event.target;
        input.disabled = true;
        setTimeout(() => {
          if(window.outcome === 'error') {
            input.disabled = false; input.classList.add('error');
          } else {
            window.rootScope.myId = 123;
            document.querySelector('._pagePassword_fixture_1').style.display = 'none';
          }
        }, window.delay || 0);
      });</script>"""
    page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=fixture)
               if route.request.url == WEB_URL else route.abort())
    base = Path(folder) / "login-fixture"
    base.mkdir()
    worker = TelegramWebWorker(base)
    worker._page = page

    def reset(outcome="success", delay=0):
        page.goto(WEB_URL)
        page.evaluate("([outcome,delay]) => {window.outcome=outcome;window.delay=delay;}", [outcome, delay])
        worker._attempt = "a" * 32
        worker._expires = time.monotonic() + 60

    def submit():
        return worker._password({"id": "a" * 32, "password": "test-only-2fa"})

    reset()
    legacy = page.locator('[class*="pagePassword"] input[type="password"]').filter(visible=True)
    assert legacy.count() == 3, "Fixture must reproduce the three-input bug"
    result = submit()
    assert result["success"] and result["data"]["authorized"], result
    assert page.locator("#real-password").input_value() == ""
    assert page.locator("#chat-composer").input_value() == ""
    assert page.locator(".stealthy").evaluate_all("items => items.every(input => input.value === '')")

    reset("error")
    result = submit()
    assert result.get("code") == "PASSWORD_FAILED", result
    assert page.locator("#real-password").input_value() == ""

    reset()
    page.locator("#real-password").evaluate("input => input.type = 'text'")
    assert submit()["data"]["authorized"]

    reset()
    page.locator("#real-password").evaluate("input => input.after(input.cloneNode())")
    assert not submit()["success"]
    assert page.evaluate("window.submitted") == 0
    assert page.locator("input").evaluate_all("items => items.every(input => input.value === '')")

    reset(delay=8000)
    pending = submit()
    assert pending.get("code") == "PASSWORD_PENDING", pending
    assert page.locator("#real-password").input_value() == ""
    assert submit().get("code") == "PASSWORD_PENDING"
    assert page.evaluate("window.submitted") == 1
    page.wait_for_function("() => !!window.rootScope.myId", timeout=5000)
    assert worker._poll({"id": "a" * 32})["data"]["authorized"]
    page.unroute("**/*")
    page.goto("about:blank")
    print("Official-style 2FA DOM: decoys, wrong password, success, revealed field, ambiguous input and delayed verification passed", flush=True)


def check_resource_button(page):
    page.unroute_all()
    page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<!doctype html>")
               if route.request.url == WEB_URL else route.abort())
    page.goto(WEB_URL)
    page.evaluate("""() => {
      const peer={_: 'peerChannel',channel_id:3};
      const channel={_: 'channel',id:3,access_hash:'fixture',username:'archivefixture',title:'归档影视'};
      const bot={_: 'user',id:42,access_hash:'fixture-bot',username:'Oscar4KBot',pFlags:{bot:true}};
      const botPeer={_: 'peerUser',user_id:42};
      const post={_: 'message',id:12,peer_id:peer,date:1,message:'一念永恒 第4季',reply_markup:{rows:[{buttons:[
        {_: 'keyboardButtonUrl',text:'获取资源',url:'tg://resolve?domain=Oscar4KBot&start=get59171'}]}]}};
      window.calls=[];
      window.rootScope={myId:777,managers:{apiManager:{invokeApi:async(method,params)=>{
        window.calls.push(method);
        if(method==='messages.getPinnedDialogs')return {dialogs:[],messages:[],chats:[],users:[]};
        if(method==='messages.getDialogs')return {_: 'messages.dialogs',dialogs:[{_: 'dialog',folder_id:1,peer}],messages:[],chats:[channel]};
        if(method==='messages.searchGlobal')return {_: 'messages.messages',messages:[post],chats:[channel]};
        if(method==='contacts.resolveUsername')return {peer:botPeer,users:[bot]};
        if(method==='messages.startBot'){
          if(params.start_param!=='get59171'||params.bot.user_id!==42)throw Error('Wrong bot request');
          return {updates:[{message:{_: 'message',id:101,peer_id:botPeer,pFlags:{out:true},message:'/start get59171'}}]};
        }
        if(method==='messages.getHistory')return {messages:params.limit===1?[]:[{
          _: 'message',id:102,peer_id:botPeer,from_id:botPeer,reply_to:{reply_to_msg_id:101},message:'资源',
          reply_markup:{rows:[{buttons:[{_: 'keyboardButtonCopy',copy_text:'https://pan.quark.cn/s/fixture123456'}]}]}}]};
        throw Error('Unexpected API call');
      }}}};
    }""")
    bridge = Path(__file__).with_name("archive_bridge.js").read_text(encoding="utf-8")
    result = page.evaluate(bridge, {"query": "一念永恒", "limit": 6, "timeout": 5})
    assert result["success"] and len(result["results"]) == 1, result
    assert result["bot"]["requests"] == 1 and result["bot"]["links"] == 1, result
    assert result["results"][0]["source_url"] == "https://t.me/archivefixture/12", result
    assert page.evaluate("calls.filter(m=>m==='messages.startBot').length") == 1
    print("Archived Oscar resource button and reply extraction: passed (offline fixture)", flush=True)


def main():
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as driver:
        profile = Path(folder) / "profile"
        for index in range(2):
            context = driver.chromium.launch_persistent_context(str(profile), headless=True)
            page = context.pages[0]
            page.goto("about:blank")
            assert page.evaluate("1 + 1") == 2
            if index == 0:
                context.add_cookies([{"name": "profile-check", "value": "ok",
                                     "domain": "example.invalid", "path": "/", "expires": 2147483647}])
                check_password_login(page, folder)
                check_resource_button(page)
            else:
                assert any(item["name"] == "profile-check" for item in context.cookies())
            context.close()
        token = Path(folder) / "token"
        token.write_text("a" * 64)
        worker = TelegramWebWorker(Path(folder) / "worker-profile")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/status",
                              headers={"Authorization": "Bearer " + "a" * 64})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
            assert result["data"]["scope"] == "archived"
            assert result["data"]["available"] is True
            assert result["data"]["authorized"] is False
        finally:
            server.shutdown()
            server.server_close()
            worker.close()
    print("Headless Chromium, persistent profile and authenticated service: passed", flush=True)


if __name__ == "__main__":
    main()
