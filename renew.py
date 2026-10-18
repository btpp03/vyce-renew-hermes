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

    st, body = api(f"/api/servers/{server_id}/renew", "POST", cookie=sid, body={})
    log("renew ->", st, body[:300])
    if st == 401 and not fresh:
        sid = github_login()
        fresh = True
        st, body = api(f"/api/servers/{server_id}/renew", "POST", cookie=sid, body={})
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
