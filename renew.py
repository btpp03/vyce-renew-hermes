#!/usr/bin/env python3
"""vyce.cloud 免费机每日续期
1) 有缓存的 session cookie 就直接用（cookie 有效期 ~14 天）
2) cookie 失效 → 用 GitHub OAuth 全自动重登（设备验证码从 Gmail IMAP 自读）
3) 拿到有效 session 后调 POST /api/servers/<id>/renew；24h 冷却期内返回 429 属正常
"""
import base64
import email
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header

BASE = "https://vyce.cloud"
REPO = os.environ.get("GITHUB_REPOSITORY", "btpp03/vyce-renew-hermes")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SITEKEY = os.environ.get("BESTCAP_SITEKEY", "bc_site_b05a3b42a31232929d6194f3")


def log(*a):
    print(*a, flush=True)


def bj(ts_ms):
    """epoch ms → 北京时间字符串"""
    import datetime
    return (datetime.datetime.utcfromtimestamp(ts_ms / 1000)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def tg(text):
    """TG 通知（@btppnode_bot → 用户 DM，无话题）。未配置就静默跳过。"""
    tok = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not tok or not chat:
        log("[tg] 未配置 TG_BOT_TOKEN/TG_CHAT_ID，跳过通知")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true"
    }).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            log("[tg] 发送 ->", json.loads(r.read().decode()).get("ok"))
    except Exception as e:
        log("[tg] 发送失败:", e)


def fail(msg):
    log("FAIL:", msg)
    tg("❌ vyce.cloud 续期失败\n" + msg)
    sys.exit(1)


def api(path, method="GET", cookie=None, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"__Host-vyce_sid={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def put_secret(name, value, pat):
    """把新 cookie 写回 repo secret（下次就不用再登 GitHub）"""
    if not pat:
        log("!! 无 GH_PAT，跳过 secret 回写")
        return
    from nacl import encoding, public

    h = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json", "User-Agent": "vyce-renew"}
    try:
        rq = urllib.request.Request(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=h)
        pk = json.load(urllib.request.urlopen(rq, timeout=30))
        sealed = public.SealedBox(
            public.PublicKey(pk["key"].encode(), encoding.Base64Encoder)
        ).encrypt(value.encode())
        body = json.dumps(
            {"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]}
        ).encode()
        rq = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/secrets/{name}",
            data=body, method="PUT",
            headers={**h, "Content-Type": "application/json"})
        urllib.request.urlopen(rq, timeout=30)
        log(f"secret {name} 已更新")
    except Exception as e:
        log("!! 回写 secret 失败:", e)


def mail_code(after_ts):
    user = os.environ.get("GMAIL_USER", "btpp03@gmail.com")
    pw = os.environ["GMAIL_APP_PW"]
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)
    st, d = M.search(None, 'FROM', '"github"')
    for i in reversed(d[0].split()[-10:]):
        st, msg = M.fetch(i, "(RFC822)")
        m = email.message_from_bytes(msg[0][1])
        subj = str(email.header.make_header(decode_header(m.get("Subject") or "")))
        if "verify your device" not in subj.lower():
            continue
        try:
            ts = email.utils.parsedate_to_datetime(m.get("Date")).timestamp()
        except Exception:
            continue
        if ts < after_ts:
            continue
        body = ""
        for part in m.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception:
                    pass
        mm = re.search(r"Verification code:\s*(\d{6})", body)
        if mm:
            M.logout()
            return mm.group(1)
    M.logout()
    return None


def github_login():
    """完整 GitHub OAuth → 返回 vyce session cookie"""
    from playwright.sync_api import sync_playwright

    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        page = ctx.new_page()
        page.goto(BASE + "/api/auth/github/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        log("login A:", page.url[:70])
        if page.query_selector("#login_field"):
            page.fill("#login_field", os.environ["GH_USER"])
            page.fill("#password", os.environ["GH_PW"])
            page.click("input[type=submit][name=commit]")
            page.wait_for_load_state("domcontentloaded", timeout=90000)
            time.sleep(3)
            log("login B:", page.url[:70])
        if "verified-device" in page.url:
            code = None
            for _ in range(10):
                code = mail_code(t0 - 120)
                if code:
                    break
                time.sleep(6)
            log("device code:", code)
            if not code:
                b.close()
                raise RuntimeError("拿不到 GitHub 设备验证码")
            page.fill("#otp", code)
            # GitHub 输满 6 位常会自动提交，此时再点按钮会因已跳转而超时
            try:
                page.click("button[type=submit]:has-text('Verify')", timeout=8000)
            except Exception as e:
                log("otp 提交按钮跳过:", type(e).__name__)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=90000)
            except Exception:
                pass
            time.sleep(3)
            if "verified-device" in page.url:
                page.fill("#otp", code)
                page.click("button[type=submit]:has-text('Verify')", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=90000)
                time.sleep(3)
            log("login C:", page.url[:70])
        if "/login/oauth/authorize" in page.url:
            try:
                page.evaluate("""() => { const b = Array.from(document.querySelectorAll('button'))
                    .find(e => (e.innerText||'').trim().toLowerCase().startsWith('authorize')); if (b) b.click(); }""")
            except Exception as e:
                log("authorize 点击异常:", type(e).__name__)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=90000)
            except Exception:
                pass
            time.sleep(4)
            log("login D:", page.url[:70])
        # 用页面内 fetch 确认 session 真的生效
        try:
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(2)
            chk = page.evaluate("""async () => { const r = await fetch('/api/me',{credentials:'same-origin'}); return r.status; }""")
            log("post-login /api/me ->", chk)
        except Exception as e:
            log("post-login 校验异常:", type(e).__name__)
        sid = None
        for c in ctx.cookies():
            if c["name"] == "__Host-vyce_sid":
                sid = c["value"]
        b.close()
    if not sid:
        raise RuntimeError("登录后没拿到 vyce session cookie")
    log("got fresh sid, len", len(sid))
    return sid


def browser_renew(sid, server_id, attempts=None):
    """2026-10-24 起 renew 需要 Bestcap 人机验证：
    用无头 Chromium 打开站点 → 渲染 bestcap 挂件 → 点复选框拿 token
    → 在页面内 fetch 提交 renew（同源，带 cookie/Origin）。返回 (status, body)。

    attempts 默认 1：站方校验坏的时候重试再多也没用，别把账号刷成机器人。
    """
    if attempts is None:
        attempts = int(os.environ.get("CAPTCHA_ATTEMPTS", "1"))
    from playwright.sync_api import sync_playwright

    js_render = """async (sk) => {
      const old = document.getElementById('hermes-bc');
      if (old) old.remove();
      const el = document.createElement('div');
      el.id = 'hermes-bc'; el.className = 'bestcap';
      el.style.cssText = 'position:fixed;left:10px;top:10px;z-index:99999;background:#fff;';
      document.body.appendChild(el);
      window.__tok = null; window.__err = null;
      window.bestcap.render(el, {sitekey: sk,
        callback: (t) => { window.__tok = t; },
        'expired-callback': () => { window.__err = 'expired'; },
        'error-callback': (e) => { window.__err = 'error:' + String(e); }});
      return true;
    }"""

    last = (0, "no attempt")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1100, "height": 800}, user_agent=UA)
        ctx.add_cookies([{"name": "__Host-vyce_sid", "value": sid,
                          "url": BASE, "secure": True, "httpOnly": True, "sameSite": "Lax"}])
        page = ctx.new_page()
        page.goto(BASE + "/#/servers", wait_until="domcontentloaded", timeout=90000)
        ok = False
        for _ in range(60):
            try:
                if page.evaluate("() => !!(window.bestcap && window.bestcap.render)"):
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ok:
            b.close()
            raise RuntimeError("bestcap 脚本没加载出来")
        sk = SITEKEY
        for i in range(attempts):
            page.evaluate(js_render, sk)
            time.sleep(2.5)
            try:
                page.frame_locator("iframe[title='Bestcap human verification']").locator("#check").click(timeout=20000)
            except Exception as e:
                log(f"captcha 点击失败({i+1}):", type(e).__name__)
            tok = None
            for _ in range(45):
                tok = page.evaluate("() => window.__tok")
                if tok:
                    break
                time.sleep(1)
            if not tok:
                log(f"captcha 未出 token({i+1}), err=", page.evaluate("() => window.__err"))
                continue
            log(f"captcha token #{i+1} len", len(tok))
            last = page.evaluate("""async ([id, tk]) => {
                const r = await fetch('/api/servers/' + id + '/renew', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({captchaResponse: tk})});
                return [r.status, await r.text()];
            }""", [server_id, tok])
            log("renew(browser) ->", last[0], str(last[1])[:200])
            if last[0] in (200, 401, 429):
                break
            if "captcha" not in str(last[1]):
                break
            time.sleep(3)
        try:
            for c in ctx.cookies():
                if c["name"] == "__Host-vyce_sid":
                    globals()["_NEW_SID"] = c["value"]
        except Exception:
            pass
        b.close()
    return last


def main():
    sid = os.environ.get("VYCE_SID", "").strip()
    pat = os.environ.get("GH_PAT", "").strip()
    server_id = os.environ.get("VYCE_SERVER_ID", "").strip()
    if not server_id:
        fail("缺 VYCE_SERVER_ID secret")

    fresh = False
    st = 0
    if sid:
        st, body = api("/api/me", cookie=sid)
        log("cookie check /api/me ->", st)
    if not sid or st == 401:
        sid = github_login()
        fresh = True

    # 确认服务器还在自己名下
    st, body = api("/api/servers", cookie=sid)
    if st != 200:
        fail(f"/api/servers 返回 {st}: {body[:150]}")
    ids = [s["id"] for s in json.loads(body).get("servers", [])]
    if server_id not in ids:
        fail(f"服务器不在账号名下（被删了？）现有: {ids}")

    # 先看剩余时间：>25h 直接跳过，避免每跳都去点 captcha（站方会当机器人）
    st0, b0 = api(f"/api/servers/{server_id}", cookie=sid)
    left0 = None
    status0 = None
    try:
        sv0 = json.loads(b0)["server"]
        status0 = sv0["status"]
        left0 = (sv0["expiresAt"] / 1000.0 - time.time()) / 3600.0
        log(f"现状：{status0}，剩余 {left0:.1f}h（到期 {bj(sv0['expiresAt'])}）")
    except Exception as e:
        log("读服务器状态失败:", e)

    # 已过期且已关机 → 优先走 start 兜底（平台按 Renew and start 处理，不校验 captcha）
    # 只在 status 真是 stopped 时调，避免对运行中的机器误触发
    if left0 is not None and left0 < 0 and status0 == "stopped":
        st3, b3 = api(f"/api/servers/{server_id}/start",
                      method="POST", cookie=sid, body={})
        log("start 兜底 ->", st3, b3[:200])
        if st3 == 200:
            try:
                sv3 = json.loads(b3)["server"]
            except Exception:
                sv3 = {}
            if sv3.get("status") == "running" and sv3.get("expiresAt"):
                exp3 = bj(sv3["expiresAt"])
                log(f"start 兜底成功：running，新到期 {exp3}（北京时间）")
                tg(f"🔁 vyce.cloud 续期成功（走 start 兜底，captcha 仍未修）\n"
                   f"{sv3.get('name', '')} · running\n新到期 {exp3}（北京时间）")
                if fresh:
                    put_secret("VYCE_SID", sid, pat)
                return
        log("start 兜底没成功，继续走 captcha 续期")
    elif left0 is not None and left0 > 6:
        log(f"剩余 {left0:.1f}h > 6h，本次跳续期（进 6 小时窗口才动手）")
        if fresh:
            put_secret("VYCE_SID", sid, pat)
        return

    st, body = browser_renew(sid, server_id)
    body = str(body)
    log("renew ->", st, body[:300])
    if st == 401 and not fresh:
        sid = github_login()
        fresh = True
        st, body = browser_renew(sid, server_id)
        body = str(body)
        log("renew(retry) ->", st, body[:300])

    if st == 200:
        st2, b2 = api(f"/api/servers/{server_id}", cookie=sid)
        try:
            sv = json.loads(b2)["server"]
            exp = bj(sv["expiresAt"])
            log(f"续期成功，新到期 {exp}（北京时间），状态 {sv['status']}")
            tg(f"🔁 vyce.cloud 续期成功\n"
               f"{sv['name']} · {sv['status']}\n"
               f"新到期 {exp}（北京时间）")
        except Exception:
            log("续期成功:", b2[:300])
            tg("🔁 vyce.cloud 续期成功（取新到期时间失败，详情见 Actions 日志）")
    elif st == 429:
        log("冷却期内（24h 只能续一次），本次跳过")
    elif st == 400 and "captcha" in body:
        # 2026-10-24：站长新加 Bestcap 人机验证，无头浏览器能拿到 token 但服务端
        # 一律返回 invalid_captcha（本机/住宅出口/官方 UI 三种方式都一样）→ 平台侧校验坏了。
        # 重试腿每小时一跳，为避免刷屏只在这几个带里各推一条 TG。
        left = None
        exp_ms = None
        try:
            _, b2 = api(f"/api/servers/{server_id}", cookie=sid)
            sv = json.loads(b2)["server"]
            exp_ms = sv["expiresAt"]
            left = (exp_ms / 1000.0 - time.time()) / 3600.0
            log(f"captcha 校验被服务端拒绝，剩余 {left:.1f}h（到期 {bj(sv['expiresAt'])}）")
        except Exception as e:
            log("查剩余时间失败:", e)
        if fresh:
            put_secret("VYCE_SID", sid, pat)
        # 2026-10-25 实测兜底：机器因未续期被关机后，POST /start 不校验 captcha，
        # 平台按「Renew and start」处理 —— 返回 running 且 expiresAt = now + 48h。
        # captcha 坏掉期间用它续命。
        if left is not None and left < 0:
            st3, b3 = api(f"/api/servers/{server_id}/start",
                          method="POST", cookie=sid, body={})
            log("兜底 start ->", st3, b3[:200])
            if st3 == 200:
                try:
                    sv3 = json.loads(b3)["server"]
                except Exception:
                    sv3 = {}
                if sv3.get("status") == "running" and sv3.get("expiresAt"):
                    exp3 = bj(sv3["expiresAt"])
                    log(f"start 兜底成功：running，新到期 {exp3}（北京时间）")
                    tg(f"🔁 vyce.cloud 续期成功（走 start 兜底，captcha 仍未修）\n"
                       f"{sv3.get('name', '')} · running\n新到期 {exp3}（北京时间）")
                    return
        if left is None or left >= 6:
            log("剩余 >6h，静默跳过（等平台修 captcha，或等重试腿命中）")
            return
        band = None
        if 5.5 <= left < 6:
            band = "⏳ vyce 剩余不足 6 小时"
        elif 0.5 <= left < 1:
            band = "⏳ vyce 剩余不足 1 小时"
        elif 0 <= left < 0.5:
            band = "🚨 vyce 即将到期"
        elif -0.5 <= left < 0:
            # 刚过期那一次（每小时一跳，最多推 1~2 次，之后静默防刷屏）
            band = "🚨 vyce 已过期（机器已关机），数据还能留 3 天，修好即续上"
        if band:
            extra = ""
            if left < 0:
                extra = f"数据保留到 {bj((exp_ms or 0) + 3 * 86400000)} 北京时间。"
            tg(f"{band}（{left:.1f}h，到期 {bj(int((time.time() + left * 3600) * 1000))} 北京时间）\n"
               f"站方 captcha 校验仍然失败，续期腿每小时自动重试（过期关机后会自动用 /start 兜底救回）；{extra}"
               f"若一直不修就手动去 vyce.cloud 点 Renew。")
        else:
            log("窗口内静默（每小时一跳，只在 6h/1h/最后半小时/刚过期各推一次）")
    else:
        fail(f"renew 返回 {st}: {body[:150]}")

    if fresh:
        put_secret("VYCE_SID", sid, pat)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"异常 {type(e).__name__}: {e}")
