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
                try:
                    page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception:
                    pass
                return text.strip()
        else:
            stable   = 0
            last_len = cur_len

    if extend_if_growing:
        print(f"[{label}] ⏳ 继续延长等待（最多 300s）...", flush=True)
        prev_len = last_len; prev_text = last_text; ext = 0
        while ext < 300:
            time.sleep(5); ext += 5
            try:
                text = _get_last_msg(page)
            except Exception as e:
                print(f"[{label}] ⚠️ 延长阶段异常：{e}", flush=True)
                return prev_text.strip()
            cur_len = len(text.strip())
            print(f"  延长 +{ext}s | 字符数: {cur_len}", flush=True)
            if cur_len == prev_len:
                try:
                    page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception:
                    pass
                return text.strip()
            prev_len = cur_len; prev_text = text
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return prev_text.strip()
    else:
        try:
            page.screenshot(path=f"{screenshot_prefix}_timeout.png")
            return _get_last_msg(page).strip()
        except Exception:
            return last_text.strip()


# ════════════════════════════════════════════════════════════════
# 阶段 A 提示词（无时间限制）
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
# 阶段 B 提示词（无时间限制，最少10条）
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
        "完成检索后，综合第一轮+第二轮所有高价值情报，输出不少于10条最震撼的话题。\n"
        "严格按以下格式输出日报：\n\n"
        "输出必须以 @@@START@@@ 开头，以 @@@END@@@ 单独成行结束，其后不得有任何其他内容。\n"
        "禁止代码块、额外文字、思考过程。\n\n"
        "严格模板（注意：@账号行与引用行之间禁止空行，引用行与📝之间禁止空行，各bullet之间禁止空行）：\n"
        "@@@START@@@\n"
        f"📡 昨夜，X上硅谷AI圈都在聊啥 | {date_today}\n\n"
        "**🏰巨头宫斗**\n\n"
        "**🍉 1. 话题标题**\n"
        "**🗣️ 极客原声态：**\n"
        "@账号 | 姓名 | 身份\n"
        "> \"中文翻译内容\"(❤️赞/💬评)\n"
        "**📝 严肃吃瓜：**\n"
        "• 📌 增量事实和知识...\n"
        "• 🧠 背后隐性博弈分析...\n"
        "• 🎯 一二级资本市场影响（如有）...\n\n"
        "（按此格式完成剩余话题，不少于10条，合理分配 巨头宫斗 中文圈 硬件与空间计算 投资人 等维度）\n"
        "@@@END@@@"
    )


# ════════════════════════════════════════════════════════════════
# 阶段 C 提示词（原版保留）
# ════════════════════════════════════════════════════════════════
def build_prompt_c():
    return (
        "执行阶段C：标题 + 封面图提示词生成（从当前不少于10条新闻中提炼）。\n\n"
        "【核心任务（一步完成）】\n"
        "从以上新闻中，挑选最具冲突感、炸裂感或吃瓜属性的1~2个核心事件，生成以下三项输出：\n\n"
        "输出一：微信公众号文章标题\n"
        "- 极度抓眼球，制造强烈好奇心或情绪冲击\n"
        "- 风格参考：XXX公开撕XXX：这场战争刚刚开始 / AI圈最大瓜：XXX当众打脸XXX\n"
        "- 允许用数字、破折号、感叹号增强张力\n"
        "- 长度严格15~30个汉字\n"
        "- 禁止平淡陈述、学术腔\n\n"
        "输出二：封面图英文提示词\n"
        "- 风格：American comic book style，Marvel/DC panel感，bold black ink outlines，"
        "flat vibrant colors，halftone dot shading\n"
        "- 构图：两股势力正面对抗，表情极度夸张，动作感强烈\n"
        "- 象征物：用抽象符号（芯片/机器人/火箭/巨型拳头/美元等）代表主角，禁止真实人脸和公司Logo\n"
        "- 对话气泡：一句<=10个英文单词的台词，点出冲突核心\n"
        "- 画幅：横版16:9，适合公众号封面\n"
        "- 长度：英文提示词<=150词\n"
        "- 禁止：中文文字、水印、写实感\n\n"
        "输出三：深度解读\n"
        "- 字数：150~200字以内\n"
        "- 重点分析对以下三类群体的影响：\n"
        "(a) 中国AI行业及从业人员\n"
        "(b) 中国VC一级市场投资人\n"
        "(c) 散户和普通用户\n"
        "- 语言风格幽默风趣，每个维度1~2句话点明核心，如果该维度没啥影响就不必强行分析\n"
        "- 整体为流畅段落，禁止列表、禁止标题\n\n"
        "【输出铁闸（必须严格遵守）】\n"
        "只输出以下三行，禁止任何解释、思考、额外文字：\n"
        "TITLE: <中文标题>\n"
        "PROMPT: <英文提示词>\n"
        "INSIGHT: <150~200字深度解读>"
    )


# ════════════════════════════════════════════════════════════════
# 格式清洗（修改2：删多余空行 + 去掉前缀标签）
# ════════════════════════════════════════════════════════════════
def clean_format(text: str) -> str:
    # @账号行 与 > 引用行之间去掉空行
    text = re.sub(r'(@\S[^\n]*)\n\n+(> )', r'\1\n\2', text)
    # > 引用行 与 📝 之间去掉空行
    text = re.sub(r'(> "[^\n]*"[^\n]*)\n\n+(\*\*📝)', r'\1\n\2', text)
    # bullet 之间去掉空行
    text = re.sub(r'(• [^\n]+)\n\n+(• )', r'\1\n\2', text)
    # 去掉 bullet 里的前缀标签
    text = re.sub(r'(• 📌 )涨姿势：\s*', r'\1', text)
    text = re.sub(r'(• 🧠 )猜博弈：\s*', r'\1', text)
    text = re.sub(r'(• 🎯 )识风向：\s*', r'\1', text)
    return text


# ════════════════════════════════════════════════════════════════
# Kimi 兜底（阶段C无结果时）
# ════════════════════════════════════════════════════════════════
def kimi_fallback(raw_b_text):
    if not KIMI_API_KEY or not raw_b_text or len(raw_b_text) < 100:
        print("[Kimi兜底] 跳过（无 API Key 或内容过短）", flush=True)
        return "", "", ""
    print("\n[Kimi兜底] 阶段C无结果，调用 Kimi API...", flush=True)
    try:
        resp = requests.post(
            "https://api.moonshot.cn/v1/chat/completions",
            headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "moonshot-v1-8k",
                "messages": [
                    {"role": "system", "content": "你是一位擅长撰写爆款公众号标题和封面文案的资深编辑，熟悉AI和科技圈动态。"},
                    {"role": "user", "content": (
                        "以下是今日硅谷AI圈热点日报内容：\n\n"
                        + raw_b_text[:6000]
                        + "\n\n请根据以上内容，挑选最具冲突感的1~2个核心事件，严格只输出以下三行：\n"
                        "TITLE: <中文标题，15~30个汉字，极度抓眼球>\n"
                        "PROMPT: <英文文生图提示词，American comic book style，两股势力对抗，<=150词>\n"
                        "INSIGHT: <150~200字深度解读，分析对中国AI从业者/VC/散户的影响，幽默风趣>"
                    )},
                ],
                "temperature": 0.7, "max_tokens": 1000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[Kimi兜底] 生成成功，长度：{len(result)} 字符", flush=True)
        title_m   = re.search(r"TITLE[:：]\s*(.+)", result)
        prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", result)
        insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", result)
        return (
            title_m.group(1).strip()   if title_m   else "",
            prompt_m.group(1).strip()  if prompt_m  else "",
            insight_m.group(1).strip() if insight_m else "",
        )
    except Exception as e:
        print(f"[Kimi兜底] 调用失败：{e}", flush=True)
        return "", "", ""


# ════════════════════════════════════════════════════════════════
# 硅基流动生图
# ════════════════════════════════════════════════════════════════
def generate_cover_image(prompt):
    if not SF_API_KEY or not prompt:
        print("生图跳过（SF_API_KEY 或提示词为空）", flush=True)
        return ""
    print("\n[生图] 调用硅基流动 FLUX.1-schnell...", flush=True)
    try:
        resp = requests.post(
            "https://api.siliconflow.cn/v1/images/generations",
            headers={"Authorization": f"Bearer {SF_API_KEY}", "Content-Type": "application/json"},
            json={"model": "black-forest-labs/FLUX.1-schnell", "prompt": prompt, "n": 1, "image_size": "1280x720"},
            timeout=120,
        )
        resp.raise_for_status()
        image_url = resp.json()["data"][0]["url"]
        print(f"[生图] ✅ 封面图生成成功：{image_url[:80]}...", flush=True)
        return image_url
    except Exception as e:
        print(f"[生图] ❌ 生图失败：{e}", flush=True)
        return ""


def download_image(url, save_path="cover.png"):
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(resp.content)
        print(f"[下载] ✅ 已保存到 {save_path}（{len(resp.content)//1024} KB）", flush=True)
        return True
    except Exception as e:
        print(f"[下载] ❌ 下载失败：{e}", flush=True)
        return False


def upload_to_imgbb(image_path):
    imgbb_key = os.getenv("IMGBB_API_KEY", "")
    if not imgbb_key or not os.path.exists(image_path):
        print("[图床] 跳过 ImgBB 上传", flush=True)
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
            url = data["data"]["url"]
            print(f"[图床] ✅ ImgBB URL：{url}", flush=True)
            return url
        print(f"[图床] ❌ 上传失败：{data}", flush=True)
        return ""
    except Exception as e:
        print(f"[图床] ❌ 上传异常：{e}", flush=True)
        return ""


# ════════════════════════════════════════════════════════════════
# 飞书卡片推送（多 Webhook）
# ════════════════════════════════════════════════════════════════
def build_feishu_card(text, title, cover_url="", insight=""):
    text     = clean_format(text)
    elements = []

    if insight:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**🔍 深度解读**\n{insight}"},
        })
        elements.append({"tag": "hr"})

    for part in re.split(r"(?=\*\*🍉)", text):
        if not part.strip():
            continue
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": part[:3800]},
        })

    if cover_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"🖼️ [点击查看封面图]({cover_url})"},
        })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📡 {title}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def push_to_feishu(card_payload):
    webhooks = get_feishu_webhooks()
    if not webhooks:
        print("⚠️ 未配置任何 FEISHU_WEBHOOK_URL，跳过", flush=True)
        return
    for i, url in enumerate(webhooks, 1):
        try:
            resp = requests.post(url, json=card_payload, timeout=30)
            print(f"飞书推送 #{i}：{resp.status_code} | {resp.text[:80]}", flush=True)
        except Exception as e:
            print(f"飞书推送 #{i} 异常：{e}", flush=True)


# ════════════════════════════════════════════════════════════════
# 微信 HTML（集简云，个人号暂不可用，保留备用）
# ════════════════════════════════════════════════════════════════
def _md_to_html(text):
    text = re.sub(r"\*\*([^*]+?)\*\*", r"\1", text)
    return text.replace("\n", "<br/>")


def build_wechat_html(text, cover_url="", insight=""):
    text = clean_format(text)
    cover_block = (
        f'<p style="text-align:center;margin:0 0 16px 0;">'
        f'<img src="{cover_url}" style="max-width:100%;border-radius:8px;" /></p>'
        if cover_url else ""
    )
    insight_block = (
        '<div style="border-radius:8px;background:#FFF7E6;padding:12px 14px;margin:0 0 16px 0;">'
        '<div style="font-weight:bold;margin-bottom:6px;">🔍 深度解读</div>'
        f'<div>{insight.replace(chr(10), "<br/>")}</div></div>'
        if insight else ""
    )
    return cover_block + insight_block + _md_to_html(text)


def push_to_jijyun(html_content, title, cover_url=""):
    if not JIJYUN_WEBHOOK_URL:
        print("⚠️ JIJYUN_WEBHOOK_URL 未配置，跳过", flush=True)
        return
    try:
        resp = requests.post(
            JIJYUN_WEBHOOK_URL,
            json={"title": title, "author": "大尉Prinski",
                  "html_content": html_content, "cover_jpg": cover_url},
            timeout=30,
        )
        print(f"极简云推送：{resp.status_code} | {resp.text[:120]}", flush=True)
    except Exception as e:
        print(f"极简云推送异常：{e}", flush=True)


# ════════════════════════════════════════════════════════════════
# 提取正文 / 质量检查
# ════════════════════════════════════════════════════════════════
def extract_markdown_block(text):
    start = text.find("@@@START@@@")
    end   = text.find("@@@END@@@")
    if start == -1:
        return ""
    cs = start + len("@@@START@@@")
    return text[cs:end].strip() if (end != -1 and end > start) else text[cs:].strip()


def is_valid_content(text):
    return bool(text) and len(text) >= 300 and "@@@START@@@" in text and "🍉" in text


def _is_placeholder(text):
    return not text or (text.startswith("<") and text.endswith(">"))


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════
def main():
    print("=" * 60, flush=True)
    print("🚀 AI吃瓜日报自动化任务启动（Session 自我续期版）", flush=True)
    print("=" * 60, flush=True)

    check_cookie_expiry()

    is_storage_state = prepare_session_file()

    raw_b_text    = ""
    cover_prompt  = ""
    cover_title_c = ""
    cover_insight = ""
    saved_context = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800",
            ],
        )

        ctx_opts = {
            "viewport":   {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
        }
        if is_storage_state:
            ctx_opts["storage_state"] = "session_state.json"

        context = browser.new_context(**ctx_opts)

        if not is_storage_state:
            load_raw_cookies(context)

        page = context.new_page()

        # Step 1：打开 Grok
        print("\n打开 grok.com...", flush=True)
        page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        page.screenshot(path="00_opened.png")

        # 检查登录状态
        if "sign" in page.url.lower() or "login" in page.url.lower():
            print("❌ 未登录，Cookie/Session 已失效，请更新 Super_GROK_COOKIES", flush=True)
            page.screenshot(path="00_login_required.png")
            browser.close()
            raise SystemExit(1)
        print("✅ 已登录 Grok", flush=True)

        # Step 2：开启 Grok 4.20 Beta
        enable_grok4_beta(page)

        # Step 3：阶段 A
        send_prompt(page, build_prompt_a(), "阶段A", "03_stage_a")
        print("[阶段A] ⏳ 强制等待 50s...", flush=True)
        time.sleep(50)
        wait_and_extract(
            page, "阶段A", "03_stage_a",
            interval=3, stable_rounds=4, max_wait=120,
            extend_if_growing=True, min_len=100,
        )

        # Step 4：阶段 B
        send_prompt(page, build_prompt_b(), "阶段B", "04_stage_b")
        print("[阶段B] ⏳ 强制等待 60s...", flush=True)
        time.sleep(60)
        raw_b_text = wait_and_extract(
            page, "阶段B", "04_stage_b",
            interval=5, stable_rounds=3, max_wait=300,
            extend_if_growing=True, min_len=1000,
        )
        print(f"\n阶段B 内容长度：{len(raw_b_text)} 字符", flush=True)

        # Step 5：阶段 C
        cover_raw = ""
        try:
            send_prompt(page, build_prompt_c(), "阶段C", "05_stage_c")
            cover_raw = wait_and_extract(
                page, "阶段C", "05_stage_c",
                interval=3, stable_rounds=3, max_wait=90,
                extend_if_growing=False, min_len=80,
            )
        except Exception as e:
            print(f"[阶段C] ⚠️ 执行异常：{e}", flush=True)

        title_m   = re.search(r"TITLE[:：]\s*(.+)", cover_raw)
        prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", cover_raw)
        insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", cover_raw)
        cover_title_c = title_m.group(1).strip()   if title_m   else ""
        cover_prompt  = prompt_m.group(1).strip()  if prompt_m  else ""
        cover_insight = insight_m.group(1).strip() if insight_m else ""

        # 检测占位符，触发 Kimi 兜底
        if _is_placeholder(cover_title_c) or _is_placeholder(cover_prompt):
            print("[阶段C] 检测到占位符，启动 Kimi 兜底...", flush=True)
            cover_title_c, cover_prompt, cover_insight = kimi_fallback(raw_b_text)
        elif not cover_title_c and not cover_prompt:
            print("[阶段C] 三项均为空，启动 Kimi 兜底...", flush=True)
            cover_title_c, cover_prompt, cover_insight = kimi_fallback(raw_b_text)

        print(f"\n[阶段C] 动态标题：{cover_title_c}", flush=True)
        print(f"[阶段C] 封面图提示词：{cover_prompt[:80]}...", flush=True)
        print(f"[阶段C] 深度解读：{cover_insight[:60]}...", flush=True)

        # 保存 context 引用，供续期使用
        saved_context = context

        # ── Session 自我续期 ─────────────────────────────────────
        save_and_renew_session(saved_context)

        browser.close()

    # 内容质量守卫
    if not is_valid_content(raw_b_text):
        print("\n❌ 日报内容质量不达标，终止推送。", flush=True)
        print(f" 原始内容前200字：{raw_b_text[:200]}", flush=True)
        raise SystemExit(1)

    final_markdown = extract_markdown_block(raw_b_text) or raw_b_text.strip()
    final_markdown = clean_format(final_markdown)

    # Step 6：生图
    cover_url = generate_cover_image(cover_prompt)
    download_image(cover_url, "cover.png")

    # Step 7：标题
    if cover_title_c and not _is_placeholder(cover_title_c):
        title = cover_title_c
    else:
        m = re.search(r"昨夜，X上[^\n]*", final_markdown)
        title = m.group(0).strip() if m else "AI圈极客吃瓜日报"
    print(f"\n标题：{title}", flush=True)

    # Step 8：ImgBB
    imgbb_url = upload_to_imgbb("cover.png")
    final_cover_url = imgbb_url if imgbb_url else cover_url
    print(f"封面图最终 URL：{final_cover_url[:80] if final_cover_url else '无'}", flush=True)

    # Step 9：推送飞书（全部 Webhook）
    print("\n推送飞书...", flush=True)
    push_to_feishu(build_feishu_card(final_markdown, title, final_cover_url, cover_insight))

    # Step 10：推送极简云（微信，个人号暂不可用，配置了才推）
    if JIJYUN_WEBHOOK_URL:
        print("推送极简云...", flush=True)
        push_to_jijyun(
            build_wechat_html(final_markdown, final_cover_url, cover_insight),
            title, final_cover_url,
        )

    print("\n🎉 全部完成！", flush=True)


if __name__ == "__main__":
    main()
