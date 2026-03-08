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
    返回 True  = Playwright storage state 格式（自动续期后的格式）
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
    """Cookie-Editor 数组 → 注入 Playwright context"""
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
    """保存 storage state，通过 GitHub API 自动更新 Secret（终极续期）"""
    try:
        context.storage_state(path="session_state.json")
        print("[Session] ✅ storage state 已保存到本地", flush=True)
    except Exception as e:
        print(f"[Session] ❌ 保存失败：{e}", flush=True)
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
        pub_key   = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed    = nacl_public.SealedBox(pub_key).encrypt(state_str.encode())
        enc_b64   = base64.b64encode(sealed).decode()

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
        print("[Session] ⚠️ PyNaCl 未安装，Secret 续期跳过（请确认 yml 里已安装 PyNaCl）", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Secret 续期失败：{e}", flush=True)


def check_cookie_expiry():
    """检查 sso cookie 剩余天数，不足 5 天发飞书提醒"""
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
                            requests.post(url, json={"msg_type": "text", "content": {"text": msg}}, timeout=15)
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
        send_btn = page.wait_for_selector(
            "button[aria-label='Submit']:not([disabled]), "
            "button[aria-label='Send message']:not([disabled]), "
            "button[type='submit']:not([disabled])",
            timeout=30000, state="visible",
        )
        send_btn.click()
        clicked = True
    except Exception as e:
        print(f"[{label}] ⚠️ 常规点击失败（{e}），尝试 JS 点击...", flush=True)

    if not clicked:
        result = page.evaluate("""() => {
            const btn = document.querySelector("button[type='submit']")
                     || document.querySelector("button[aria-label='Submit']")
                     || document.querySelector("button[aria-label='Send message']");
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if result:
            print(f"[{label}] ✅ JS 兜底点击成功", flush=True)
        else:
            raise RuntimeError(f"[{label}] ❌ 找不到发送按钮，流程中止")

    print(f"[{label}] ✅ 已发送", flush=True)
    time.sleep(5)


# ════════════════════════════════════════════════════════════════
# 等待 Grok 生成完毕
# ════════════════════════════════════════════════════════════════
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
    print(f"[{label}] 等待回复（最长 {max_wait}s，最小有效长度 {min_len}）...", flush=True)
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
            print(f"[{label}] ⚠️ 页面异常：{e}", flush=True)
            return last_text.strip()
        last_text = text
        cur_len = len(text.strip())
        print(f"  {elapsed}s | 字符数: {cur_len}", flush=True)

        if cur_len == last_len and cur_len >= min_len:
            stable += 1
            if stable >= stable_rounds:
                print(f"[{label}] ✅ 完毕（{cur_len} 字符）", flush=True)
                try: page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception: pass
                return text.strip()
        else:
            stable   = 0
            last_len = cur_len

    if extend_if_growing:
        print(f"[{label}] ⏳ 继续延长等待（最多 300s）...", flush=True)
        prev_len = last_len; prev_text = last_text; ext = 0
        while ext < 300:
            time.sleep(5); ext += 5
            try: text = _get_last_msg(page)
            except Exception as e:
                print(f"[{label}] ⚠️ 延长阶段异常：{e}", flush=True)
                return prev_text.strip()
            cur_len = len(text.strip())
            print(f"  延长 +{ext}s | 字符数: {cur_len}", flush=True)
            if cur_len == prev_len:
                try: page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception: pass
                return text.strip()
            prev_len = cur_len; prev_text = text
        try: return _get_last_msg(page).strip()
        except Exception: return prev_text.strip()
    else:
        try:
            page.screenshot(path=f"{screenshot_prefix}_timeout.png")
            return _get_last_msg(page).strip()
        except Exception:
            return last_text.strip()


# ════════════════════════════════════════════════════════════════
# 阶段 A 提示词（去除时间限制）
# ════════════════════════════════════════════════════════════════
def build_prompt_a():
    return (
        "执行Tiered Scan模式：你现在是X商业情报深度分析师。\n\n"
        "【核心策略】\n"
        "Tier1（全量）：搜索所有推文 + 重点帖调用 x_thread_fetch 拉完整线程。\n"
        "Tier2（活跃）：仅保留赞>=30的帖做互动分析。\n"
        "Tier3（泛列）：仅保留赞>=100或大事件帖。\n"
        "使用 parallel 调用（一次最多同时发3个工具请求）。\n\n"
        "【第一轮搜索：3批并行】\n"
        "批次1 (Tier1 巨头18人)：@elonmusk @sama @karpathy @demishassabis @darioamodei "
        "@OpenAI @AnthropicAI @GoogleDeepMind @GaryMarcus @xAI @AIatMeta @GoogleAI "
        "@MSFTResearch @IlyaSutskever @gregbrockman @rowancheung @clmcleod @bindureddy\n"
        "批次2 (Tier2 中文KOL16人)：@dotey @oran_ge @vista8 @imxiaohu @Sxsyer "
        "@K_O_D_A_D_A @tualatrix @linyunqiu @garywong @web3buidl @AI_Era @AIGC_News "
        "@jiangjiang @hw_star @mranti @nishuang\n"
        "批次3 (Tier3 VC媒体16人)：@a16z @ycombinator @lightspeedvp @sequoia "
        "@foundersfund @eladgil @pmarca @bchesky @chamath @paulg @TheInformation "
        "@TechCrunch @verge @WIRED @Scobleizer @bentossell\n\n"
        "【强制规则】\n"
        "1. 每批次搜索如返回0条，立即去掉时间参数重试（必须成功）。\n"
        "2. 重点推文（赞>100或含争论）立即调用 x_thread_fetch 拉完整互动。\n"
        "3. 分析只关注：新观点、吵架记录、市场反馈强度。\n"
        "4. 所有引用的 X 帖子原文必须翻译成中文，严禁保留英文原文。\n\n"
        "【输出限制（严格遵守）】\n"
        "搜索完成后，只输出一段<=200字的\"内部情报摘要\"（含核心洞察+数据缓存），最后一行必须是：\n"
        "第一轮扫描完毕，等待第二轮输入。\n"
        "禁止任何其他文字、解释、日报、代码块。"
    )


# ════════════════════════════════════════════════════════════════
# 阶段 B 提示词（去除时间限制，最少10条）
# ════════════════════════════════════════════════════════════════
def build_prompt_b():
    date_today, _ = get_dates()
    return (
        "执行Tiered Scan模式：这是第二轮搜索（覆盖后50个核心账号）。\n\n"
        "【数据复用】\n"
        "直接复用第一轮已搜索到的数据缓存，继续补充以下账号的最新动态。\n"
        "每批次搜索如返回0条，立即去掉时间参数重试（必须成功）。\n\n"
        "【核心策略（复用第一轮）】\n"
        "Tier1：全量搜索 + 重点帖立即调用 x_thread_fetch 拉完整线程和互动。\n"
        "Tier2：仅保留赞>=30的帖做深度分析。\n"
        "Tier3：仅保留赞>=100或重大事件。\n"
        "优先并行调用工具（一次最多同时发3个请求）。\n\n"
        "【第二轮搜索：3批并行】\n"
        "批次4 (Tier1 开源与基础设施 18人)：\n"
        "@HuggingFace @MistralAI @Perplexity_AI @GroqInc @Cohere @TogetherCompute "
        "@runwayml @Midjourney @StabilityAI @Scale_AI @CerebrasSystems @tenstorrent "
        "@weights_biases @langchainai @llama_index @supabase @vllm_project @huggingface_hub\n\n"
        "批次5 (Tier2 硬件与空间计算 16人)：\n"
        "@nvidia @AMD @Intel @SKhynix @tsmc @magicleap @NathieVR @PalmerLuckey "
        "@ID_AA_Carmack @boz @rabovitz @htcvive @XREAL_Global @RayBan @MetaQuestVR @PatrickMoorhead\n\n"
        "批次6 (Tier3 研究员与硬核圈 16人)：\n"
        "@jeffdean @chrmanning @hardmaru @goodfellow_ian @feifeili @_akhaliq "
        "@promptengineer @AI_News_Tech @siliconvalley @aithread @aibreakdown "
        "@aiexplained @aipubcast @lexfridman @hubermanlab @swyx\n\n"
        "【最终成稿指令（严格执行）】\n"
        "完成检索后，综合第一轮+第二轮所有高价值情报，输出不少于10条最震撼的话题，"
        "严格按以下格式输出日报：\n\n"
        "输出必须以 @@@START@@@ 开头，以 @@@END@@@ 单独成行结束，其后不得有任何其他内容。\n"
        "禁止代码块、额外文字、思考过程。\n\n"
        "严格模板（@账号行与引用行之间禁止空行，引用行与📝之间禁止空行，各bullet之间禁止空行）：\n"
        "@@@START@@@\n"
        f"📡 昨夜，X上硅谷AI圈都在聊啥 | {date_today}\n\n"
        "**🏰巨头宫斗**\n\n"
        "**🍉 1. 话题标题**\n"
        "**🗣️ 极客原声态：**\n"
        "@账号 | 姓名 | 身份\n"
        "> \"中文翻译内
