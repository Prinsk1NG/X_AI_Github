# -*- coding: utf-8 -*-
"""
grok_auto_task.py  v3.1
Architecture: Grok (pure search, per-account) + Kimi-k2.5 / Claude (analyse & summarise)

Phase 1 – Tiered scan:
  All 100 accounts searched individually (from:account, limit=10, mode=Latest).
  Collect 3 newest posts + 1 metadata row per account.
  Auto-classify accounts into S / A / B / inactive.

Phase 2 – Differential collection + report:
  S-tier (~5-8):  10 posts + x_thread_fetch for likes >5000
  A-tier (~20-25): 5 posts, qt field for retweets
  B-tier (rest):   reuse Phase 1 data (3 posts)
  Kimi-k2.5 AND Claude each generate one version of the daily report independently.
  Both versions pushed to Feishu (interactive card), each labelled with the model name.
"""

import os
import re
import json
import time
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout
from playwright.sync_api import sync_playwright

# ── Environment variables ────────────────────────────────────────────────────
JIJYUN_WEBHOOK_URL  = os.getenv("JIJYUN_WEBHOOK_URL", "")
SF_API_KEY          = os.getenv("SF_API_KEY", "")
KIMI_API_KEY        = os.getenv("KIMI_API_KEY", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
GROK_COOKIES_JSON  = os.getenv("SUPER_GROK_COOKIES", "")   # unified all-caps
PAT_FOR_SECRETS    = os.getenv("PAT_FOR_SECRETS", "")
GITHUB_REPOSITORY  = os.getenv("GITHUB_REPOSITORY", "")

# ── Global timeout tracking ──────────────────────────────────────────────────
_START_TIME      = time.time()
PHASE1_DEADLINE  = 20 * 60   # 20 min → trigger degradation (skip remaining batches)
GLOBAL_DEADLINE  = 45 * 60   # 45 min → stop Grok, hand off to Kimi

# ── 100 accounts – ordered high-value first so degradation truncates B-tier ──
ALL_ACCOUNTS = [
    # ── Tier-1 giants (likely S / A after classification) ──────────────────
    "elonmusk", "sama", "karpathy", "demishassabis", "darioamodei",
    "OpenAI", "AnthropicAI", "GoogleDeepMind", "xAI", "AIatMeta",
    "GoogleAI", "MSFTResearch", "IlyaSutskever", "gregbrockman",
    "GaryMarcus", "rowancheung", "clmcleod", "bindureddy",
    # ── Chinese KOL / VC / Media (likely A / B) ────────────────────────────
    "dotey", "oran_ge", "vista8", "imxiaohu", "Sxsyer",
    "K_O_D_A_D_A", "tualatrix", "linyunqiu", "garywong", "web3buidl",
    "AI_Era", "AIGC_News", "jiangjiang", "hw_star", "mranti", "nishuang",
    "a16z", "ycombinator", "lightspeedvp", "sequoia", "foundersfund",
    "eladgil", "pmarca", "bchesky", "chamath", "paulg",
    "TheInformation", "TechCrunch", "verge", "WIRED", "Scobleizer", "bentossell",
    # ── Open source + infrastructure ──────────────────────────────────────
    "HuggingFace", "MistralAI", "Perplexity_AI", "GroqInc", "Cohere",
    "TogetherCompute", "runwayml", "Midjourney", "StabilityAI", "Scale_AI",
    "CerebrasSystems", "tenstorrent", "weights_biases", "langchainai", "llama_index",
    "supabase", "vllm_project", "huggingface_hub",
    # ── Hardware / spatial computing ──────────────────────────────────────
    "nvidia", "AMD", "Intel", "SKhynix", "tsmc",
    "magicleap", "NathieVR", "PalmerLuckey", "ID_AA_Carmack", "boz",
    "rabovitz", "htcvive", "XREAL_Global", "RayBan", "MetaQuestVR", "PatrickMoorhead",
    # ── Researchers / niche – placed last for graceful degradation ─────────
    "jeffdean", "chrmanning", "hardmaru", "goodfellow_ian", "feifeili",
    "_akhaliq", "promptengineer", "AI_News_Tech", "siliconvalley", "aithread",
    "aibreakdown", "aiexplained", "aipubcast", "lexfridman", "hubermanlab", "swyx",
]


# ════════════════════════════════════════════════════════════════════════════
# Feishu multi-webhook
# ════════════════════════════════════════════════════════════════════════════
def get_feishu_webhooks() -> list:
    urls = []
    for suffix in ["", "_1", "_2", "_3"]:
        url = os.getenv(f"FEISHU_WEBHOOK_URL{suffix}", "")
        if url:
            urls.append(url)
    return urls


# ════════════════════════════════════════════════════════════════════════════
# Date utilities
# ════════════════════════════════════════════════════════════════════════════
def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════════════════════════
# Session management: load + auto-renew
# ════════════════════════════════════════════════════════════════════════════
def prepare_session_file() -> bool:
    """
    Write SUPER_GROK_COOKIES to local session_state.json.
    Returns True  = Playwright storage-state format (post-renewal)
    Returns False = raw Cookie-Editor array (first-time manual import)
    """
    if not GROK_COOKIES_JSON:
        print("[Session] ⚠️ SUPER_GROK_COOKIES not configured", flush=True)
        return False
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if isinstance(data, dict) and "cookies" in data:
            with open("session_state.json", "w", encoding="utf-8") as f:
                json.dump(data, f)
            print("[Session] ✅ Playwright storage-state format (renewed)", flush=True)
            return True
        else:
            print(f"[Session] ✅ Cookie-Editor array format ({len(data)} entries)", flush=True)
            return False
    except Exception as e:
        print(f"[Session] ❌ Parse failed: {e}", flush=True)
        return False


def load_raw_cookies(context):
    """Cookie-Editor array → inject into Playwright context (first-time use)."""
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
        print(f"[Session] ✅ Injected {len(formatted)} cookies", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Cookie injection failed: {e}", flush=True)


def save_and_renew_session(context):
    """
    Save current Playwright storage-state locally, then write back to
    the SUPER_GROK_COOKIES GitHub secret via API (session renewal).
    """
    try:
        context.storage_state(path="session_state.json")
        print("[Session] ✅ Storage state saved locally", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Save storage state failed: {e}", flush=True)
        return

    if not PAT_FOR_SECRETS or not GITHUB_REPOSITORY:
        print("[Session] ⚠️ PAT_FOR_SECRETS or GITHUB_REPOSITORY not configured, skip renewal",
              flush=True)
        return

    try:
        from nacl import encoding, public as nacl_public

        with open("session_state.json", "r", encoding="utf-8") as f:
            state_str = f.read()

        headers = {
            "Authorization": f"token {PAT_FOR_SECRETS}",
            "Accept": "application/vnd.github.v3+json",
        }

        key_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_resp.raise_for_status()
        key_data = key_resp.json()

        pub_key = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed  = nacl_public.SealedBox(pub_key).encrypt(state_str.encode())
        enc_b64 = base64.b64encode(sealed).decode()

        put_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/SUPER_GROK_COOKIES",
            headers=headers,
            json={"encrypted_value": enc_b64, "key_id": key_data["key_id"]},
            timeout=30,
        )
        put_resp.raise_for_status()
        print("[Session] ✅ GitHub Secret SUPER_GROK_COOKIES auto-renewed", flush=True)

    except ImportError:
        print("[Session] ⚠️ PyNaCl not installed, skip renewal", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Secret renewal failed: {e}", flush=True)


def check_cookie_expiry():
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
                    msg = (f"⚠️ Grok Cookie expires in {days_left} days, "
                           f"please update SUPER_GROK_COOKIES!")
                    print(f"[Cookie] {msg}", flush=True)
                    for url in get_feishu_webhooks():
                        try:
                            requests.post(url,
                                          json={"msg_type": "text", "content": {"text": msg}},
                                          timeout=15)
                        except Exception:
                            pass
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# Model selection: enable Grok 4.20 Beta Toggle
# ════════════════════════════════════════════════════════════════════════════
def enable_grok4_beta(page):
    print("\n[Model] Enabling Grok 4.20 Beta Toggle...", flush=True)
    try:
        model_btn = page.wait_for_selector(
            "button:has-text('快速模式'), button:has-text('Fast'), "
            "button:has-text('自动模式'), button:has-text('Auto')",
            timeout=15000,
        )
        model_btn.click()
        time.sleep(1)

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
            print("[Model] ✅ Toggle enabled", flush=True)
            time.sleep(1)
        else:
            print("[Model] ✅ Already enabled", flush=True)
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception as e:
        print(f"[Model] ⚠️ Failed, using current model: {e}", flush=True)


# ════════════════════════════════════════════════════════════════════════════
# Send prompt
# ════════════════════════════════════════════════════════════════════════════
def send_prompt(page, prompt_text, label, screenshot_prefix):
    print(f"\n[{label}] Filling prompt ({len(prompt_text)} chars)...", flush=True)
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

    try:
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            time.sleep(0.5)
    except Exception:
        pass

    clicked = False
    try:
        send_btn = page.wait_for_selector(
            "button[aria-label='Submit']:not([disabled]), "
            "button[aria-label='Send message']:not([disabled]), "
            "button[type='submit']:not([disabled])",
            timeout=30000, state="visible",
        )
        send_btn.click()
        clicked = True
    except Exception as e:
        print(f"[{label}] ⚠️ Normal click failed ({e}), trying JS...", flush=True)

    if not clicked:
        result = page.evaluate("""() => {
            const btn = document.querySelector("button[type='submit']")
                     || document.querySelector("button[aria-label='Submit']")
                     || document.querySelector("button[aria-label='Send message']");
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if result:
            print(f"[{label}] ✅ JS fallback click succeeded", flush=True)
        else:
            raise RuntimeError(f"[{label}] ❌ Submit button not found, aborting")

    print(f"[{label}] ✅ Sent", flush=True)
    time.sleep(5)


# ════════════════════════════════════════════════════════════════════════════
# Wait for Grok to finish generating
# ════════════════════════════════════════════════════════════════════════════
def _get_last_msg(page):
    return page.evaluate("""() => {
        const msgs = document.querySelectorAll(
            '[data-testid="message"], .message-bubble, .response-content'
        );
        return msgs.length ? msgs[msgs.length - 1].innerText : "";
    }""")


def wait_and_extract(page, label, screenshot_prefix,
                     interval=3, stable_rounds=4, max_wait=120,
                     extend_if_growing=False, min_len=80):
    print(f"[{label}] Waiting for reply (max {max_wait}s, min len {min_len})...", flush=True)
    last_len  = -1
    stable    = 0
    elapsed   = 0
    last_text = ""

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        try:
            text = _get_last_msg(page)
        except Exception as e:
            print(f"[{label}] ⚠️ Page error: {e}", flush=True)
            return last_text.strip()
        last_text = text
        cur_len = len(text.strip())
        print(f"  {elapsed}s | chars: {cur_len}", flush=True)

        if cur_len == last_len and cur_len >= min_len:
            stable += 1
            if stable >= stable_rounds:
                print(f"[{label}] ✅ Done ({cur_len} chars)", flush=True)
                return text.strip()
        else:
            stable   = 0
            last_len = cur_len

    if extend_if_growing:
        print(f"[{label}] ⏳ Extending wait (up to 300s)...", flush=True)
        prev_len = last_len; prev_text = last_text; ext = 0
        while ext < 300:
            time.sleep(5); ext += 5
            try:
                text = _get_last_msg(page)
            except Exception:
                return prev_text.strip()
            cur_len = len(text.strip())
            print(f"  +{ext}s | chars: {cur_len}", flush=True)
            if cur_len == prev_len:
                return text.strip()
            prev_len = cur_len; prev_text = text
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return prev_text.strip()
    else:
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return last_text.strip()


# ════════════════════════════════════════════════════════════════════════════
# JSON Lines parser (tolerates non-JSON lines in Grok output)
# ════════════════════════════════════════════════════════════════════════════
def parse_jsonlines(text: str) -> list:
    """Return list of dicts parsed from valid JSON Lines in text."""
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith('{') or not line.endswith('}'):
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 prompt: metadata scan (all accounts, B-level strategy)
# ════════════════════════════════════════════════════════════════════════════
def build_phase1_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"第{i+1}轮：{' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "你是X平台数据采集工具。执行以下账号搜索，输出纯JSON Lines格式。\n\n"
        "【搜索规则】\n"
        "1. 每个账号单独调用 x_keyword_search：query=from:账号名，mode=Latest，limit=10\n"
        "2. 按轮次并行执行（每轮同时搜索3个账号）\n"
        "3. 不加任何关键词，不加 since 时间参数\n"
        "4. 每个账号输出：最新3条帖子 + 1行元数据\n\n"
        f"【账号列表（共{len(accounts)}个，按轮次执行）】\n"
        f"{rounds_text}\n\n"
        "【输出格式（只输出JSON Lines，严禁输出任何其他文字）】\n"
        '帖子行：{"a":"账号名","l":点赞数,"t":"MMDD","s":"英文原文摘要50词内","tag":"raw"}\n'
        '元数据行：{"a":"账号名","type":"meta","total":返回总条数,"max_l":最高点赞数,"latest":"MMDD"}\n'
        '不活跃账号：{"a":"账号名","type":"meta","total":0,"max_l":0,"latest":"NA"}\n\n'
        "【严格限制】\n"
        "- 账号名不带@符号，与from:查询中的账号名完全一致\n"
        "- t字段格式MMDD（如0309=3月9日）\n"
        "- 每个账号先输出帖子行（最多3行），再输出1行元数据行\n"
        "- 不翻译、不解释、不总结、不过滤\n"
        "- 第一行到最后一行全部是JSON"
    )


# ════════════════════════════════════════════════════════════════════════════
# Phase 2 prompts: tier-specific collection
# ════════════════════════════════════════════════════════════════════════════
def build_phase2_s_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"第{i+1}轮：{' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "你是X平台数据采集工具。执行以下S级账号深度采集，输出纯JSON Lines格式。\n\n"
        "【S级规则】\n"
        "1. 每个账号调用 x_keyword_search：query=from:账号名，mode=Latest，limit=10\n"
        "2. 输出全部10条帖子（不截断）\n"
        "3. 对点赞>5000的帖子，额外调用 x_thread_fetch 获取完整线程（每账号最多5次）\n"
        "4. 转发/引用帖（RT或QT）：在qt字段记录被引用原帖的作者和内容摘要\n"
        "5. 每轮并行3个账号\n\n"
        f"【S级账号（共{len(accounts)}个）】\n"
        f"{rounds_text}\n\n"
        "【输出格式（只输出JSON Lines）】\n"
        '普通帖：{"a":"账号名","l":点赞数,"t":"MMDD","s":"英文摘要50词内","tag":"raw"}\n'
        '引用帖：{"a":"账号名","l":点赞数,"t":"MMDD","s":"评论内容摘要","qt":"@原作者: 原帖摘要","tag":"raw"}\n'
        '线程帖：{"a":"账号名","l":点赞数,"t":"MMDD","s":"原文摘要","tag":"raw",'
        '"replies":[{"from":"回复者账号","text":"回复内容","l":点赞数}]}\n\n'
        "【严格限制】\n"
        "- 账号名不带@，与from:查询完全一致\n"
        "- 不翻译、不解释，只输出JSON\n"
        "- s字段用英文"
    )


def build_phase2_a_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"第{i+1}轮：{' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "你是X平台数据采集工具。执行以下A级账号采集，输出纯JSON Lines格式。\n\n"
        "【A级规则】\n"
        "1. 每个账号调用 x_keyword_search：query=from:账号名，mode=Latest，limit=10\n"
        "2. 只输出最新5条帖子\n"
        "3. 转发/引用帖（RT或QT）：在qt字段记录被引用原帖的作者和内容摘要\n"
        "4. 每轮并行3个账号\n\n"
        f"【A级账号（共{len(accounts)}个）】\n"
        f"{rounds_text}\n\n"
        "【输出格式（只输出JSON Lines）】\n"
        '普通帖：{"a":"账号名","l":点赞数,"t":"MMDD","s":"英文摘要50词内","tag":"raw"}\n'
        '引用帖：{"a":"账号名","l":点赞数,"t":"MMDD","s":"评论内容摘要","qt":"@原作者: 原帖摘要","tag":"raw"}\n\n'
        "【严格限制】\n"
        "- 账号名不带@，与from:查询完全一致\n"
        "- 不翻译、不解释，只输出JSON\n"
        "- s字段用英文"
    )


# ════════════════════════════════════════════════════════════════════════════
# Account classification
# ════════════════════════════════════════════════════════════════════════════
def classify_accounts(meta_results: dict) -> dict:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    classification = {}

    for account, meta in meta_results.items():
        total  = meta.get("total", 0)
        max_l  = meta.get("max_l", 0)
        latest = meta.get("latest", "NA")

        if total == 0 or latest == "NA":
            classification[account] = "inactive"
            continue

        try:
            mm = int(latest[:2])
            dd = int(latest[2:])
            latest_date = today.replace(month=mm, day=dd)
            if latest_date > today:
                latest_date = latest_date.replace(year=today.year - 1)
            days_since = (today - latest_date).days
        except (ValueError, TypeError):
            days_since = 999

        if days_since > 30:
            classification[account] = "inactive"
        elif max_l > 10000 and days_since <= 7:
            classification[account] = "S"
        elif max_l > 1000 and days_since <= 14:
            classification[account] = "A"
        else:
            classification[account] = "B"

    return classification


# ════════════════════════════════════════════════════════════════════════════
# Open a new Grok conversation page
# ════════════════════════════════════════════════════════════════════════════
def open_grok_page(context):
    page = context.new_page()
    try:
        page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        if "sign" in page.url.lower() or "login" in page.url.lower():
            print("❌ Not logged in – session expired", flush=True)
            page.close()
            return None
        enable_grok4_beta(page)
        return page
    except Exception as e:
        print(f"❌ Failed to open Grok page: {e}", flush=True)
        try:
            page.close()
        except Exception:
            pass
        return None


# ════════════════════════════════════════════════════════════════════════════
# Run one Grok batch conversation
# ════════════════════════════════════════════════════════════════════════════
def run_grok_batch(context, accounts: list, prompt_builder, label: str,
                   initial_wait: int = 60) -> list:
    if not accounts:
        return []

    elapsed = time.time() - _START_TIME
    print(f"\n[{label}] Starting batch ({len(accounts)} accounts, "
          f"elapsed: {elapsed:.0f}s)...", flush=True)

    page = open_grok_page(context)
    if page is None:
        return []

    try:
        prompt = prompt_builder(accounts)
        send_prompt(page, prompt, label, label.lower().replace(" ", "_"))

        print(f"[{label}] ⏳ Waiting {initial_wait}s for Grok to start...", flush=True)
        time.sleep(initial_wait)

        raw_text = wait_and_extract(
            page, label, label.lower().replace(" ", "_"),
            interval=5, stable_rounds=5, max_wait=360,
            extend_if_growing=True, min_len=50,
        )
        results = parse_jsonlines(raw_text)
        print(f"[{label}] ✅ Parsed {len(results)} JSON objects", flush=True)
        return results

    except Exception as e:
        print(f"[{label}] ❌ Error: {e}", flush=True)
        return []
    finally:
        try:
            page.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# LLM Prompt Builder (投资分析师版)
# ════════════════════════════════════════════════════════════════════════════
def _build_llm_prompt(combined_jsonl: str, today_str: str) -> str:
    return f"""
# Role
你是一位拥有 10 年经验的顶级 AI 行业一级市场投资分析师，专门为高级合伙人撰写"每日内参"，同时这份每日内参还有一份事实相同，语言风格更风趣幽默、富有网感的个人公众号版本。

# Task
分析过去 24 小时 X 平台上的 60+ 位科技领袖、投资人及硬件专家的推文数据（数据见文末 JSONL 部分）。
你需要过滤掉琐碎的技术参数和日常社交噪音，提炼出具有"投资参考价值"的深度内参，以公众号版本输出。

# Output Structure (必须严格遵守 Markdown 格式)

## ⚡️ 今日看板 (The Pulse)
> 用一句话总结今日最核心的 1-2 个行业定调信号。

---

## 🧠 深度叙事追踪 (Thematic Narratives)
将零散的推文按"主题/赛道"进行聚合（如：计算成本、具身智能、Agent 商业化、SaaS 演进等）。
每个主题下需要：
1. **提炼：** 描述该赛道目前正在发生的叙事转向（Narrative Shift）。
2. **关键证据：** 引用具体的账号（如 @sama, @natfriedman）及其核心观点，解释其对行业格局的影响。

---

## 💰 资本与估值雷达 (Investment Radar)
1. **投融资快讯：** 扫描数据中提到的具体融资额、估值以及领投机构。
2. **VC 偏好：** 提炼顶级机构（如 a16z, Sequoia, Benchmark）合伙人透露出的投资风向或对估值泡沫的警示。

---

## 📊 风险与中国视角 (Risk & China View)
1. **中国 AI 评价：** 汇总海外大佬/专家对中国大模型（如 DeepSeek, Zhipu, Kimi）的技术评价、成本优势或竞争压力。
2. **地缘与监管：** 提示关于芯片出口、合规审计或版权诉讼的潜在风险。

---

## 🔗 原始来源索引 (Top Sources)
挑选 3-5 条今日"必读"的推文原始链接（格式：[推文简述](链接)）。

# Constraints
- **禁止技术堆砌：** 不要解释算法原理，只需说该技术如何影响商业竞争或降低成本。
- **投资视角：** 重点关注"钱的流向"和"估值逻辑的变化"。
- **语言风格：** 专业、干脆、利落，适合在飞书移动端快速扫读。

# Input Data (JSONL)
{combined_jsonl}

# Date
{today_str}
"""


# ════════════════════════════════════════════════════════════════════════════
# LLM 辅助工具函数
# ════════════════════════════════════════════════════════════════════════════
def _get_proxies_from_env():
    """从系统环境变量获取代理配置"""
    proxy_url = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                 or os.getenv("HTTP_PROXY") or os.getenv("http_proxy"))
    if proxy_url:
        return {"https": proxy_url, "http": proxy_url}
    return None


def _get_openrouter_endpoints() -> list:
    """获取 OpenRouter 的 API 端点（支持多个备份地址）"""
    env_eps = os.getenv("OPENROUTER_ENDPOINTS")
    if env_eps:
        return [e.strip() for e in env_eps.split(",") if e.strip()]
    return [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://api.openrouter.ai/v1/chat/completions",
    ]


def extract_markdown_block(text: str) -> str:
    """从 LLM 输出中提取 ```markdown 或 ```json 代码块内容"""
    m = re.search(r'```(?:markdown|json)?\s*([\s\S]+?)```', text)
    return m.group(1).strip() if m else ""


# ════════════════════════════════════════════════════════════════════════════
# Kimi 模型调用逻辑 (Moonshot API) – kimi-k2.5
# ════════════════════════════════════════════════════════════════════════════
def llm_call_kimi(combined_jsonl: str, today_str: str):
    if not KIMI_API_KEY:
        print("[LLM/Kimi] ⚠️ KIMI_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)

    try:
        env_temp = os.getenv("KIMI_TEMPERATURE")
        temperature = float(env_temp) if env_temp is not None else 1.0
    except Exception:
        temperature = 1.0

    for attempt in range(1, 4):
        try:
            print(f"[LLM/Kimi] Calling kimi-k2.5 (attempt {attempt}/3, temp={temperature})", flush=True)
            resp = requests.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {KIMI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "kimi-k2.5",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 16000,
                },
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"[LLM/Kimi] ✅ Response received ({len(result)} chars)", flush=True)
            return _parse_llm_result(result)
        except Exception as e:
            print(f"[LLM/Kimi] attempt {attempt} failed: {e}", flush=True)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return "", "", "", ""


# ════════════════════════════════════════════════════════════════════════════
# Claude 模型调用逻辑 (OpenRouter API) – 多端点 + 代理 + DNS 容错
# ════════════════════════════════════════════════════════════════════════════
def llm_call_claude(combined_jsonl: str, today_str: str):
    if not OPENROUTER_API_KEY:
        print("[LLM/Claude] ⚠️ OPENROUTER_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)

    proxies = _get_proxies_from_env()
    endpoints = _get_openrouter_endpoints()

    for ep in endpoints:
        print(f"[LLM/Claude] Trying endpoint: {ep}", flush=True)
        for attempt in range(1, 4):
            try:
                print(f"[LLM/Claude] POST to {ep} (attempt {attempt}/3)", flush=True)
                resp = requests.post(
                    ep,
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/Prinsk1NG/X_AI_Github",
                        "X-Title": "AI吃瓜日报",
                    },
                    json={
                        "model": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6"),
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 16000,
                    },
                    timeout=300,
                    proxies=proxies,
                )
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[LLM/Claude] ✅ Response received ({len(result)} chars)", flush=True)
                return _parse_llm_result(result)
            except Exception as e:
                print(f"[LLM/Claude] attempt {attempt} at {ep} failed: {e}", flush=True)
                if attempt < 3:
                    time.sleep((2 ** attempt) + 0.5)
                else:
                    if isinstance(e, (ConnectionError, Timeout)) or "NameResolutionError" in str(e):
                        print(f"[LLM/Claude] Network/DNS error on {ep}, trying next endpoint", flush=True)
                        break
    return "", "", "", ""


# ════════════════════════════════════════════════════════════════════════════
# LLM result parser
# ════════════════════════════════════════════════════════════════════════════
def _parse_llm_result(result: str):
    """Extract report text and cover metadata from raw LLM output."""
    report_text = extract_markdown_block(result) or result

    try:
        data = json.loads(report_text)
        return (
            report_text,
            data.get("cover_title", ""),
            data.get("cover_prompt", ""),
            data.get("cover_insight", ""),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    after_end = result[result.find("@@@END@@@") + 9:] if "@@@END@@@" in result else result
    title_m   = re.search(r"TITLE[:：]\s*(.+)", after_end)
    prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", after_end)
    insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", after_end)

    cover_title   = title_m.group(1).strip()   if title_m   else ""
    cover_prompt  = prompt_m.group(1).strip()  if prompt_m  else ""
    cover_insight = insight_m.group(1).strip() if insight_m else ""

    return report_text, cover_title, cover_prompt, cover_insight


# ════════════════════════════════════════════════════════════════════════════
# LLM fallback (TITLE / PROMPT / INSIGHT only)
# ════════════════════════════════════════════════════════════════════════════
def llm_fallback(raw_b_text):
    if not raw_b_text or len(raw_b_text) < 100:
        return "", "", ""

    fallback_prompt = (
        "根据以下内容生成三行结果：\n" + raw_b_text[:6000] +
        "\nTITLE: <标题>\nPROMPT: <英文提示词>\nINSIGHT: <100字以内解读>"
    )

    def _extract(text):
        title_m   = re.search(r"TITLE[:：]\s*(.+)", text)
        prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", text)
        insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", text)
        return (
            title_m.group(1).strip()   if title_m   else "",
            prompt_m.group(1).strip()  if prompt_m  else "",
            insight_m.group(1).strip() if insight_m else "",
        )

    if OPENROUTER_API_KEY:
        proxies = _get_proxies_from_env()
        endpoints = _get_openrouter_endpoints()
        for ep in endpoints:
            for attempt in range(1, 4):
                try:
                    resp = requests.post(
                        ep,
                        headers={
                            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://github.com/Prinsk1NG/X_AI_Github",
                            "X-Title": "AI吃瓜日报",
                        },
                        json={
                            "model": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6"),
                            "messages": [{"role": "user", "content": fallback_prompt}],
                            "temperature": 0.7,
                            "max_tokens": 2000,
                        },
                        timeout=60,
                        proxies=proxies,
                    )
                    resp.raise_for_status()
                    return _extract(resp.json()["choices"][0]["message"]["content"].strip())
                except Exception as e:
                    print(f"[LLM] ❌ OpenRouter fallback attempt {attempt}/3 at {ep}: {e}", flush=True)
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                    else:
                        if isinstance(e, (ConnectionError, Timeout)) or "NameResolutionError" in str(e):
                            break

    if KIMI_API_KEY:
        try:
            env_temp = os.getenv("KIMI_TEMPERATURE")
            temperature = float(env_temp) if env_temp is not None else 1.0
        except Exception:
            temperature = 1.0
        try:
            resp = requests.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={"Authorization": f"Bearer {KIMI_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "kimi-k2.5",
                    "messages": [{"role": "user", "content": fallback_prompt}],
                    "temperature": temperature, "max_tokens": 1000,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return _extract(resp.json()["choices"][0]["message"]["content"].strip())
        except Exception:
            pass

    return "", "", ""


# ════════════════════════════════════════════════════════════════════════════
# Format cleanup
# ════════════════════════════════════════════════════════════════════════════
def clean_format(text: str) -> str:
    text = re.sub(r'(@\S[^\n]*)\n\n+(> )', r'\1\n\2', text)
    text = re.sub(r'(> "[^\n]*"[^\n]*)\n\n+(\*\*📝)', r'\1\n\2', text)
    text = re.sub(r'(• [^\n]+)\n\n+(• )', r'\1\n\2', text)
    text = re.sub(r'(• 📌 )涨姿势：\s*', r'\1', text)
    text = re.sub(r'(• 🧠 )猜博弈：\s*', r'\1', text)
    text = re.sub(r'(• 🎯 )识风向：\s*', r'\1', text)
    return text


def generate_cover_image(prompt):
    if not SF_API_KEY or not prompt:
        return ""
    try:
        resp = requests.post(
            "https://api.siliconflow.cn/v1/images/generations",
            headers={"Authorization": f"Bearer {SF_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "black-forest-labs/FLUX.1-schnell",
                  "prompt": prompt, "n": 1, "image_size": "1280x720"},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["url"]
    except Exception:
        return ""


def upload_to_imgbb(image_path):
    imgbb_key = os.getenv("IMGBB_API_KEY", "")
    if not imgbb_key or not os.path.exists(image_path):
        return ""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": imgbb_key}, data={"image": img_b64}, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]["url"]
        return ""
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════════════════════
# ✅ 【已修改】飞书交互式卡片推送 – 新增 model_label 参数
# ════════════════════════════════════════════════════════════════════════════
def send_to_feishu_card(content_md: str, today_str: str, model_label: str = "Kimi-k2.5"):
    """
    将 LLM 生成的 Markdown 转换为飞书交互式卡片并发送。
    model_label: 标注生成该版本的模型名称，显示在卡片底部备注及卡片标题中。
    """
    webhooks = get_feishu_webhooks()
    if not webhooks:
        print(f"[Push/{model_label}] ⚠️ No Feishu webhooks found.")
        return

    # 样式预处理：在每一个二级标题前增加分割线
    formatted_content = content_md.replace("## ", "\n---\n**## ")

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    # ✅ 标题中加入模型标识，方便在消息列表直接区分
                    "content": f"🚀 AI 投资人内参 [{model_label}] | {today_str}",
                    "tag": "plain_text",
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": formatted_content,
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            # ✅ 底部备注动态显示模型名
                            "content": f"💡 此内参由 Grok 实时抓取并经 {model_label} 深度提炼",
                        }
                    ],
                },
            ],
        },
    }

    for url in webhooks:
        try:
            resp = requests.post(url, json=card_payload, timeout=20)
            resp.raise_for_status()
            print(f"[Push/{model_label}] ✅ Card sent to Feishu: {url.split('/')[-1][:8]}...")
        except Exception as e:
            print(f"[Push/{model_label}] ❌ Failed to send card: {e}")


# ════════════════════════════════════════════════════════════════════════════
# Legacy Feishu multi-card builder (保留兼容)
# ════════════════════════════════════════════════════════════════════════════

_CATEGORY_COLORS = {
    "巨头宫斗": "indigo", "宫斗": "indigo",
    "中文圈":   "orange",
    "开源基建": "green",  "开源": "green", "基建": "green",
    "硬件":     "purple", "空间计算": "purple",
    "投资":     "blue",
    "研究员":   "grey",   "研究": "grey",
}


def _category_color(text: str):
    for kw, color in _CATEGORY_COLORS.items():
        if kw in text:
            return color
    return None


def build_feishu_cards(text: str, title: str, insight: str = "") -> list:
    try:
        data = json.loads(text)
        return _build_feishu_cards_json(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return _build_feishu_cards_legacy(text, title, insight)


_CATEGORY_SECTION_ICONS = {
    "巨头宫斗": "🏰",
    "开源生态": "🌳",
    "芯片硬件": "💾",
    "资本市场": "💰",
    "学术前沿": "🔬",
}


def _build_feishu_cards_json(data: dict) -> list:
    date_str = data.get("date", "")
    topics = data.get("topics", [])
    elements = []

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": " **⚠️ 每日早8点准时更新 | 全网一手信源 | 深度行业解码 | 无广告无引流** ",
        },
        "icon": {"tag": "standard_icon", "token": "time_outlined", "color": "blue"},
    })
    elements.append({"tag": "hr"})

    seen_categories: list = []
    category_groups: dict = {}
    for t in topics:
        cat = t.get("category", "其他")
        if cat not in category_groups:
            seen_categories.append(cat)
            category_groups[cat] = []
        category_groups[cat].append(t)

    topic_num = 0
    for cat in seen_categories:
        icon = _CATEGORY_SECTION_ICONS.get(cat, "📌")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"# {icon} {cat}板块"},
        })
        elements.append({"tag": "hr"})

        for t in category_groups[cat]:
            topic_num += 1
            topic_title = t.get("title", "话题")
            account     = t.get("account", "")
            real_name   = t.get("real_name", "")
            likes       = t.get("likes", "-")
            comments    = t.get("comments", "-")
            translation = t.get("translation", "")
            pub_time    = t.get("publish_time", "")
            facts       = t.get("facts", "-")
            strategy    = t.get("strategy", "-")
            capital     = t.get("capital", "-")

            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"## 🍉 {topic_num}号事件 | {topic_title}",
                },
            })

            note_content = (
                f" **🗣️ 极客原声态 | 一手信源** \n"
                f"> **@{account} | {real_name}** (❤️ {likes}赞 | 💬 {comments}评)\n"
                f"> \"{translation}\"\n"
                f"> *原文发布于 {pub_time}*"
            )
            elements.append({
                "tag": "note",
                "elements": [{"tag": "lark_md", "content": note_content}],
                "background_color": "blue",
            })

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": " **📝 深度解码** "},
            })

            elements.append({
                "tag": "column_set",
                "flex_mode": "bisect",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "vertical_align": "top",
                        "elements": [{"tag": "div", "text": {
                            "tag": "lark_md",
                            "content": f" **📌 增量事实 | 客观中立补充** \n{facts}",
                        }}],
                    },
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "vertical_align": "top",
                        "elements": [{"tag": "div", "text": {
                            "tag": "lark_md",
                            "content": f" **🧠 隐性博弈 | 行业暗战剖析** \n{strategy}",
                        }}],
                    },
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "vertical_align": "top",
                        "elements": [{"tag": "div", "text": {
                            "tag": "lark_md",
                            "content": f" **🎯 资本风向标 | 商业趋势研判** \n{capital}",
                        }}],
                    },
                ],
            })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                "*📅 本日报每日早8点更新，内容均来自X平台公开信源，"
                "解读仅代表行业观察，不构成任何投资建议*"
            ),
        },
    })
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看往期日报"},
                "type": "default", "complex_interaction": True,
                "width": "default", "size": "medium",
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "话题投稿"},
                "type": "primary", "complex_interaction": True,
                "width": "default", "size": "medium",
            },
        ],
    })

    return [{
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True, "update_multi": False},
            "header": {
                "title":    {"tag": "plain_text", "content": "🌍 昨晚，X上硅谷AI圈在聊啥"},
                "subtitle": {"tag": "plain_text", "content": f"📡 AI圈极客吃瓜日报 | {date_str}"},
                "template": "blue",
                "ud_icon":  {"tag": "standard_icon", "token": "chat-forbidden_outlined"},
            },
            "elements": elements,
        },
    }]


def _build_feishu_cards_legacy(text: str, title: str, insight: str = "") -> list:
    text = clean_format(text)
    cards = []
    elements = []

    if insight:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"<font color='orange'>**💡 Insight**</font>\n{insight}"}
        })
        elements.append({"tag": "hr"})

    data_panel_match = re.search(r"【数据看板】\s*([\s\S]*?)(?=【执行摘要】)", text)
    if data_panel_match:
        data_str = data_panel_match.group(1).replace('\n', '')
        parts = [p.strip() for p in data_str.split('|')]
        fields = []
        for p in parts:
            if ':' in p or '：' in p:
                k, v = re.split(r'[:：]', p, 1)
                fields.append({
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{k.strip()}**\n<font color='grey'>{v.strip()}</font>"
                    }
                })
        if fields:
            elements.append({"tag": "div", "fields": fields})
            elements.append({"tag": "hr"})

    section_pattern = re.compile(
        r"【(.*?)】\s*([\s\S]*?)(?=【|\Z)", re.MULTILINE
    )
    for m in section_pattern.finditer(text):
        section_title = m.group(1).strip()
        section_body  = m.group(2).strip()
        if not section_body or section_title == "数据看板":
            continue
        color = _category_color(section_title)
        header_content = (
            f"<font color='{color}'>**{section_title}**</font>"
            if color else f"**{section_title}**"
        )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": header_content},
        })
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": section_body},
        })
        elements.append({"tag": "hr"})

        if len(elements) >= 40:
            cards.append(_wrap_legacy_card(elements, title))
            elements = []

    if elements:
        cards.append(_wrap_legacy_card(elements, title))

    return cards if cards else [_wrap_legacy_card(
        [{"tag": "div", "text": {"tag": "lark_md", "content": text}}], title
    )]


def _wrap_legacy_card(elements: list, title: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title or "📡 AI吃瓜日报"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════
def main():
    today_str, yesterday_str = get_dates()
    print(f"\n{'='*60}", flush=True)
    print(f"  grok_auto_task.py v3.1  |  {today_str}", flush=True)
    print(f"{'='*60}\n", flush=True)

    check_cookie_expiry()

    is_storage_state = prepare_session_file()

    all_raw_results: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])

        if is_storage_state:
            context = browser.new_context(storage_state="session_state.json")
        else:
            context = browser.new_context()
            load_raw_cookies(context)

        # ── Phase 1: metadata scan ──────────────────────────────────────────
        elapsed = time.time() - _START_TIME
        accounts_for_p1 = ALL_ACCOUNTS
        if elapsed > PHASE1_DEADLINE:
            print(f"[Main] ⚠️ Already past Phase1 deadline ({elapsed:.0f}s), skipping Phase1", flush=True)
            phase1_results = []
        else:
            phase1_results = run_grok_batch(
                context, accounts_for_p1,
                build_phase1_prompt, "Phase1-Scan",
                initial_wait=90,
            )

        # Extract meta rows and post rows
        meta_results: dict = {}
        post_rows: list = []
        for row in phase1_results:
            if row.get("type") == "meta":
                meta_results[row["a"]] = row
            else:
                post_rows.append(row)

        all_raw_results.extend(post_rows)

        # ── Classify accounts ───────────────────────────────────────────────
        classification = classify_accounts(meta_results)
        s_accounts = [a for a, t in classification.items() if t == "S"]
        a_accounts = [a for a, t in classification.items() if t == "A"]
        b_accounts = [a for a, t in classification.items() if t == "B"]

        print(f"\n[Main] Classification: S={len(s_accounts)}, A={len(a_accounts)}, "
              f"B={len(b_accounts)}, inactive={sum(1 for t in classification.values() if t=='inactive')}",
              flush=True)

        elapsed = time.time() - _START_TIME

        # ── Phase 2-S ───────────────────────────────────────────────────────
        if s_accounts and elapsed < GLOBAL_DEADLINE:
            s_results = run_grok_batch(
                context, s_accounts,
                build_phase2_s_prompt, "Phase2-S",
                initial_wait=60,
            )
            all_raw_results.extend(s_results)
        else:
            print("[Main] ⚠️ Skipping Phase2-S (timeout or no S accounts)", flush=True)

        elapsed = time.time() - _START_TIME

        # ── Phase 2-A ───────────────────────────────────────────────────────
        if a_accounts and elapsed < GLOBAL_DEADLINE:
            a_results = run_grok_batch(
                context, a_accounts,
                build_phase2_a_prompt, "Phase2-A",
                initial_wait=60,
            )
            all_raw_results.extend(a_results)
        else:
            print("[Main] ⚠️ Skipping Phase2-A (timeout or no A accounts)", flush=True)

        # B-tier: reuse Phase1 data (already in all_raw_results)
        print(f"[Main] B-tier ({len(b_accounts)} accounts): reusing Phase1 data", flush=True)

        save_and_renew_session(context)
        browser.close()

    # ── Build combined JSONL ────────────────────────────────────────────────
    combined_jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in all_raw_results)
    print(f"\n[Main] Total raw rows collected: {len(all_raw_results)}", flush=True)
    print(f"[Main] Combined JSONL size: {len(combined_jsonl)} chars", flush=True)

    if not combined_jsonl.strip():
        print("[Main] ❌ No data collected, aborting LLM step", flush=True)
        return

    # ════════════════════════════════════════════════════════════════════════
    # ✅ 【核心改动】Kimi 和 Claude 各自独立生成一版，均推送飞书
    # ════════════════════════════════════════════════════════════════════════
    print("\n[LLM] 🚀 同时调用 Kimi 和 Claude 各自生成一版内参...", flush=True)

    # — Kimi 版 —
    kimi_report, kimi_title, kimi_cover_prompt, kimi_insight = llm_call_kimi(
        combined_jsonl, today_str
    )

    # — Claude 版 —
    claude_report, claude_title, claude_cover_prompt, claude_insight = llm_call_claude(
        combined_jsonl, today_str
    )

    # — 推送 Kimi 版到飞书 —
    if kimi_report:
        kimi_report_labeled = (
            kimi_report
            + "\n\n---\n> 🤖 *本版内参由 **Kimi-k2.5** 生成*"
        )
        print("[LLM] 📤 推送 Kimi 版到飞书...", flush=True)
        send_to_feishu_card(kimi_report_labeled, today_str, model_label="Kimi-k2.5")
    else:
        print("[LLM] ⚠️ Kimi 未返回内容，跳过推送", flush=True)

    # — 推送 Claude 版到飞书 —
    if claude_report:
        claude_model_name = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6").split("/")[-1]
        claude_report_labeled = (
            claude_report
            + f"\n\n---\n> 🤖 *本版内参由 **{claude_model_name}** 生成*"
        )
        print("[LLM] 📤 推送 Claude 版到飞书...", flush=True)
        send_to_feishu_card(claude_report_labeled, today_str, model_label=claude_model_name)
    else:
        print("[LLM] ⚠️ Claude 未返回内容，跳过推送", flush=True)

    # — 两者均失败时的兜底提示 —
    if not kimi_report and not claude_report:
        print("[LLM] ❌ Kimi 和 Claude 均未返回内容，本次无法生成内参", flush=True)

    # — 封面图生成（优先用 Kimi 的 cover_prompt，其次 Claude）—
    cover_prompt  = kimi_cover_prompt  or claude_cover_prompt
    cover_title   = kimi_title         or claude_title
    cover_insight = kimi_insight       or claude_insight

    if cover_prompt:
        print(f"\n[Cover] Generating cover image...", flush=True)
        cover_url = generate_cover_image(cover_prompt)
        if cover_url:
            print(f"[Cover] ✅ Cover image URL: {cover_url}", flush=True)
        else:
            print("[Cover] ⚠️ Cover image generation failed", flush=True)

    print(f"\n[Main] ✅ All done | {today_str}", flush=True)


if __name__ == "__main__":
    main()
