# -*- coding: utf-8 -*-
import os
import re
import time
import json
import base64
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

# ── 环境变量 ─────────────────────────────────────────────────────
JIJYUN_WEBHOOK_URL = os.getenv("JIJYUN_WEBHOOK_URL", "")
SF_API_KEY         = os.getenv("SF_API_KEY", "")
KIMI_API_KEY       = os.getenv("KIMI_API_KEY", "")
GROK_COOKIES_JSON  = os.getenv("Super_GROK_COOKIES", "")
PAT_FOR_SECRETS    = os.getenv("PAT_FOR_SECRETS", "")
GITHUB_REPOSITORY  = os.getenv("GITHUB_REPOSITORY", "")


# ── 飞书多 Webhook ────────────────────────────────────────────────
def get_feishu_webhooks() -> list:
    urls = []
    for suffix in ["", "_1", "_2", "_3"]:
        url = os.getenv(f"FEISHU_WEBHOOK_URL{suffix}", "")
        if url:
            urls.append(url)
    return urls


# ── 日期工具 ─────────────────────────────────────────────────────
def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════════════
# Session 管理：加载 + 自动续期
# ════════════════════════════════════════════════════════════════
def prepare_session_file() -> bool:
    """
    把 Super_GROK_COOKIES 写到本地 session_state.json。
    返回 True  = Playwright storage state 格式（续期后）
    返回 False = 原始 Cookie-Editor 数组格式（首次手动导入）
    """
    if not GROK_COOKIES_JSON:
        print("[Session] ⚠️ Super_GROK_COOKIES 未配置", flush=True)
        return False
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if isinstance(data, dict) and "cookies" in data:
            with open("session_state.json", "w", encoding="utf-8") as f:
                json.dump(data, f)
            print("[Session] ✅ 检测到 Playwright storage state 格式（续期后）", flush=True)
            return True
        else:
            print(f"[Session] ✅ 检测到 Cookie-Editor 数组格式（{len(data)} 条）", flush=True)
            return False
    except Exception as e:
        print(f"[Session] ❌ 解析失败：{e}", flush=True)
        return False


def load_raw_cookies(context):
    """Cookie-Editor 数组 → 注入 Playwright context（首次使用）"""
    try:
        cookies = json.loads(GROK_COOKIES_JSON)
        formatted = []
        for c in cookies:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".grok.com"),
                "path":   c.get("path", "/"),
            }
            if "httpOnly" in c: cookie["httpOnly"] = c["httpOnly"]
            if "secure"   in c: cookie["secure"]   = c["secure"]
            ss = c.get("sameSite", "")
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
            formatted.append(cookie)
        context.add_cookies(formatted)
        print(f"[Session] ✅ 注入 {len(formatted)} 条 Cookie", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Cookie 注入失败：{e}", flush=True)


def save_and_renew_session(context):
    """
    终极续期方案：
    1. 保存当前 Playwright storage state 到本地
    2. 通过 GitHub API 加密后写回 Super_GROK_COOKIES Secret
    下次运行时自动加载最新 session，永不过期
    """
    try:
        context.storage_state(path="session_state.json")
        print("[Session] ✅ storage state 已保存到本地", flush=True)
    except Exception as e:
        print(f"[Session] ❌ 保存 storage state 失败：{e}", flush=True)
        return

    if not PAT_FOR_SECRETS or not GITHUB_REPOSITORY:
        print("[Session] ⚠️ PAT_FOR_SECRETS 或 GITHUB_REPOSITORY 未配置，跳过自动续期", flush=True)
        return

    try:
        from nacl import encoding, public as nacl_public

        with open("session_state.json", "r", encoding="utf-8") as f:
            state_str = f.read()

        headers = {
            "Authorization": f"token {PAT_FOR_SECRETS}",
            "Accept": "application/vnd.github.v3+json",
        }

        # 获取仓库公钥
        key_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_resp.raise_for_status()
        key_data = key_resp.json()

        # libsodium 加密
        pub_key = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed  = nacl_public.SealedBox(pub_key).encrypt(state_str.encode())
        enc_b64 = base64.b64encode(sealed).decode()

        # 写回 Secret
        put_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/Super_GROK_COOKIES",
            headers=headers,
            json={"encrypted_value": enc_b64, "key_id": key_data["key_id"]},
            timeout=30,
        )
        put_resp.raise_for_status()
        print("[Session] ✅ GitHub Secret Super_GROK_COOKIES 已自动续期", flush=True)

    except ImportError:
        print("[Session] ⚠️ PyNaCl 未安装，续期跳过", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Secret 续期失败：{e}", flush=True)


def check_cookie_expiry():
    """Cookie-Editor 格式时，检查 sso cookie 剩余天数，不足 5 天发飞书提醒"""
    if not GROK_COOKIES_JSON:
        return
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if not isinstance(data, list):
            return
        for c in data:
            if c.get("name") == "sso" and c.get("expirationDate"):
                exp = datetime.fromtimestamp(c["expirationDate"], tz=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).days
                if days_left <= 5:
                    msg = f"⚠️ Grok Cookie 还有 {days_left} 天过期，请及时更新 Super_GROK_COOKIES！"
                    print(f"[Cookie] {msg}", flush=True)
                    for url in get_feishu_webhooks():
                        try:
                            requests.post(
                                url,
                                json={"msg_type": "text", "content": {"text": msg}},
                                timeout=15,
                            )
                        except Exception:
                            pass
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
# 模型选择：开启 Grok 4.20 Beta Toggle
# ════════════════════════════════════════════════════════════════
def enable_grok4_beta(page):
    print("\n[模型] 开启 Grok 4.20 测试版 Toggle...", flush=True)
    try:
        model_btn = page.wait_for_selector(
            "button:has-text('快速模式'), button:has-text('Fast'), "
            "button:has-text('自动模式'), button:has-text('Auto')",
            timeout=15000,
        )
        model_btn.click()
        time.sleep(1)
        page.screenshot(path="01_model_menu.png")

        toggle = page.wait_for_selector(
            "button[role='switch'], input[type='checkbox']", timeout=8000,
        )
        is_on = page.evaluate("""() => {
            const sw = document.querySelector("button[role='switch']");
            if (sw) return sw.getAttribute('aria-checked') === 'true'
                        || sw.getAttribute('data-state') === 'checked';
            const cb = document.querySelector("input[type='checkbox']");
            return cb ? cb.checked : false;
        }""")
        if not is_on:
            toggle.click()
            print("[模型] ✅ Toggle 已开启", flush=True)
            time.sleep(1)
        else:
            print("[模型] ✅ 已是开启状态", flush=True)
        page.keyboard.press("Escape")
        time.sleep(0.5)
        page.screenshot(path="02_model_confirmed.png")
    except Exception as e:
        print(f"[模型] ⚠️ 失败，继续使用当前模型：{e}", flush=True)


# ════════════════════════════════════════════════════════════════
# 发送提示词
# ════════════════════════════════════════════════════════════════
def send_prompt(page, prompt_text, label, screenshot_prefix):
    print(f"\n[{label}] 填入提示词（共 {len(prompt_text)} 字符）...", flush=True)
    page.wait_for_selector("div[contenteditable='true'], textarea", timeout=30000)

    ok = page.evaluate("""(text) => {
        const el = document.querySelector("div[contenteditable='true']")
                || document.querySelector("textarea");
        if (!el) return false;
        el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        document.execCommand('insertText', false, text);
        return true;
    }""", prompt_text)

    if not ok:
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            for i in range(0, len(prompt_text), 500):
                page.keyboard.type(prompt_text[i:i+500])
                time.sleep(0.2)

    time.sleep(1.5)
    page.screenshot(path=f"{screenshot_prefix}_before.png")

    try:
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            time.sleep(0.5)
    except Exception:
        pass

    clicked = False
    try:
        send_btn = page.wait_for_selec
