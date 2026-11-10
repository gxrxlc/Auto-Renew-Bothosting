#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token

BILLINGS_URL = "https://bot-hosting.net/a/billings"

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "SESSION_TOKEN"

# 获取cookie到期时间
def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            return value, expiry_dt
    return None, None

# 检查是否需要更新cookie
def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

# 更新cookie到secrets
def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

# 发送tg通知（有截图时用 sendPhoto，失败自动退回纯文字）
def send_telegram_message(message: str, photo: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    try:
        if photo and os.path.exists(photo):
            with open(photo, "rb") as f:
                r = requests.post(f"{base}/sendPhoto",
                                  data={"chat_id": TG_CHAT_ID, "caption": message[:1024]},
                                  files={"photo": f}, timeout=30)
            if r.ok:
                print("✅ Telegram 通知(含截图)已发送")
                return
            print(f"⚠️ 截图发送失败，改发文字: {r.text[:200]}")
        requests.post(f"{base}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 安全截图
def shot(sb, name: str) -> str:
    try:
        sb.save_screenshot(name)
        return name
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")
        return ""

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****'

    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False

# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except Exception:
        return countdown_str

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",   # Expires 2026/07/07
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",   # Expires 07/07/2026 (MM/DD/YYYY)
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",        # 2026/07/07 - renew
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",        # 07/07/2026 - renew
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            parts = date_str.split('/')
            if len(parts[-1]) == 4 and len(parts[0]) == 2:  # MM/DD/YYYY -> YYYY/MM/DD
                return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None

# 提取冷却倒计时（两种文案都认）
def extract_cooldown(page_source: str):
    m = (re.search(r"Renewal cooldown\s*(\d{2}:\d{2}:\d{2})", page_source)
         or re.search(r"Renew in\s*(\d{2}:\d{2}:\d{2})", page_source))
    return m.group(1) if m else None

#   Discord OAuth 登录（SESSION_TOKEN 失效时的备用方案）
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """用 DC_TOKEN 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    params = {
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    }
    authorize_url = f"{DISCORD_API}?{urllib.parse.urlencode(params)}"
    referer = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params)

    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    proxies = None
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb) -> bool:
    """通过 Discord Token 走完整 OAuth 流程登录 bot-hosting.net"""
    print("\n🔑 通过 Discord Token 登录...")

    state = capture_discord_state(sb)
    if not state:
        shot(sb, "login_no_state.png")
        return False

    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        shot(sb, "login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        shot(sb, "login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        shot(sb, "login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    shot(sb, "login_timeout.png")
    return False


def open_billings(sb):
    print(f"🌐 访问 {BILLINGS_URL} ...")
    sb.open(BILLINGS_URL)
    sb.wait_for_ready_state_complete()
    sb.sleep(3)
    current_url = sb.get_current_url()
    print(f"📝 当前URL: {current_url}, Title: {sb.get_title()}")
    return current_url


# 点击弹窗里的续期按钮（等待 Turnstile token + 按钮可用；JS 扫描所有可见按钮兜底；失败时打印按钮列表）
_MODAL_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const tok = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]')).map(i => i.value || '').join('');
  const btns = Array.from(document.querySelectorAll('button, [role="button"], a.btn, input[type="submit"]')).filter(vis);
  const info = btns.map((b, i) => ({i, text: (b.innerText || b.value || '').trim().slice(0, 40),
      disabled: !!(b.disabled || b.getAttribute('aria-disabled') === 'true' || b.classList.contains('disabled')),
      inModal: !!b.closest('[role="dialog"], .modal, [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="popup"], [data-state="open"]')}));
  return {token_len: tok.length, buttons: info};
}
"""

_MODAL_WORDS = ("renew", "confirm", "claim", "continue", "submit", "extend", "yes")

def click_modal_renew(sb, wait_seconds=40) -> bool:
    outer_texts = ("renew free plan",)
    last = None
    for sec in range(wait_seconds):
        try:
            last = sb.execute_script("return (" + _MODAL_JS + ")()")
        except Exception as e:
            print(f"⚠️ 读取弹窗按钮失败: {e}")
            sb.sleep(1)
            continue
        btns = last.get("buttons", [])
        cands = [b for b in btns if any(w in b["text"].lower() for w in _MODAL_WORDS)]
        # 优先弹窗内、非外部按钮、DOM 中靠后的
        cands.sort(key=lambda b: (not b["inModal"], b["text"].lower() in outer_texts, -b["i"]))
        enabled = [b for b in cands if not b["disabled"]]
        if enabled and (enabled[0]["inModal"] or enabled[0]["text"].lower() not in outer_texts):
            b = enabled[0]
            print(f"👉 弹窗按钮: '{b['text']}' (inModal={b['inModal']}, token_len={last.get('token_len')})")
            try:
                sb.execute_script("""
                  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
                  const btns = Array.from(document.querySelectorAll('button, [role="button"], a.btn, input[type="submit"]')).filter(vis);
                  btns[arguments[0]].scrollIntoView({block: 'center'});
                  btns[arguments[0]].click();
                """, b["i"])
                print("✅ 已点击弹窗续期按钮")
                return True
            except Exception as e:
                print(f"⚠️ 点击失败: {e}")
        if sec % 10 == 0:
            print(f"⏳ 等待弹窗按钮可用... token_len={last.get('token_len')} 候选={[(b['text'], b['disabled'], b['inModal']) for b in cands]}")
        sb.sleep(1)
    print(f"🔍 页面可见按钮: {[(b['text'], 'disabled' if b['disabled'] else 'ok', 'modal' if b['inModal'] else '') for b in (last or {}).get('buttons', [])]}")
    print(f"🔍 Turnstile token 长度: {(last or {}).get('token_len')}")
    return False


# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 方式1: SESSION_TOKEN Cookie 登录（默认）
        if SESSION_TOKEN:
            print("🚀 启动浏览器...")
            sb.open("https://bot-hosting.net/")
            sb.wait_for_ready_state_complete()
            sb.sleep(2)

            print("📝 注入 Cookie...")
            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

            current_url = open_billings(sb)
            if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
            else:
                print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}")

        # 方式2: Discord OAuth 登录（备用）
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                current_url = open_billings(sb)
                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg),
                                  photo=shot(sb, "login_failed.png"))
            return

        if _LOGIN_METHOD == "Discord Token":
            print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新到 Secrets")

        # 提取当前到期日期
        sb.sleep(2)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if not sb.is_element_visible(selector):
                    continue
                button_text = sb.get_text(selector).strip()
                low = button_text.lower()
                # 冷却中：Renew in hh:mm:ss / Renewal cooldown hh:mm:ss / 按钮被禁用
                m = re.search(r"(\d{1,2}:\d{2}:\d{2})", button_text)
                disabled = False
                try:
                    disabled = (sb.get_attribute(selector, "disabled") is not None) or not sb.is_element_enabled(selector)
                except Exception:
                    pass
                if "cooldown" in low or "renew in" in low or m or disabled:
                    countdown_text = m.group(1) if m else None
                    print(f"⏳ 续期按钮处于冷却: '{button_text}'")
                    break
                if "renew" in low:
                    outer_renew_selector = selector
                    print(f"✅ 续期按钮可用: '{button_text}'")
                    break
            except Exception:
                pass

        # 页面其他位置的冷却文案兜底
        if not outer_renew_selector and not countdown_text:
            countdown_text = extract_cooldown(page_source)

        if outer_renew_selector:
            # 点击外部续期按钮等待弹窗
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=shot(sb, "outer_click_failed.png"))
                return

            # 处理弹窗中的 Turnstile
            print("🔒 检测弹窗中的 Turnstile 验证...")
            turnstile_passed = False
            for attempt in range(1, 4):
                try:
                    sb.uc_gui_click_captcha()
                    time.sleep(12)
                except Exception as e:
                    print(f"⚠️ 点击 Turnstile 出错: {e}")

                if wait_for_turnstile_pass(sb, timeout=20):
                    turnstile_passed = True
                    break
                else:
                    print(f"⏳ 第 {attempt} 次未通过，重试点击...")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=shot(sb, "turnstile_failed.png"))
                return

            # 点击弹窗里的续期按钮
            print("⏳ 等待弹窗续期按钮可用并点击...")
            time.sleep(3)
            shot(sb, "modal_before_click.png")
            if not click_modal_renew(sb):
                print("❌ 弹窗里没找到可点击的续期按钮")
                send_telegram_message(format_notification("❌ 续期失败", error="弹窗里没找到可点击的续期按钮",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=shot(sb, "modal_button_not_clicked.png"))
                return

            # 等待生效后刷新账单页再判断
            print("⏳ 等待续期生效并刷新账单页...")
            sb.sleep(8)
            shot(sb, "after_renew_click.png")
            open_billings(sb)

            new_page_text = sb.get_page_source()
            new_expiry = extract_expiry_date(new_page_text)
            new_countdown = extract_cooldown(new_page_text)
            print(f"📅 续期前: {current_expiry} | 续期后: {new_expiry} | 冷却: {new_countdown or '无'}")
            result_png = shot(sb, "renew_result.png")

            if (new_expiry and new_expiry != current_expiry) or new_countdown:
                extra = (f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
                         if new_countdown else "到期日期已更新")
                print("✅ 续期成功")
                send_telegram_message(format_notification("✅ 续期成功", extra=extra,
                                                          expiry_date=new_expiry or "（未获取到）"),
                                      photo=result_png)
            else:
                print("⚠️ 已点击并刷新，到期日期仍未变化，请手动检查")
                send_telegram_message(format_notification("⚠️ 续期可能未成功",
                                                          extra="已点击弹窗按钮并刷新，到期日未变，请看截图",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=result_png)

        else:
            skip_png = shot(sb, "renew_skipped.png")
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(format_notification("⏳ 未到续期时间",
                                                          extra=f"⏱️ 可续期时间: {friendly}后",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=skip_png)
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(format_notification("ℹ️ 无需续期",
                                                          extra="当前状态未知，请手动检查",
                                                          expiry_date=current_expiry or "（未获取到）"),
                                      photo=skip_png)

        # 更新SESSION_TOKEN
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功")
                else:
                    print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
            else:
                print("⚠️ 未设置 GH_TOKEN，无法自动更新")
                print(f"📋 请手动设置 SESSION_TOKEN = {new_token[:4]}...{new_token[-4:]}")
        else:
            print("✅ SESSION_TOKEN 无需更新")

        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
