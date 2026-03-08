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
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
SF_API_KEY         = os.getenv("SF_API_KEY", "")
KIMI_API_KEY       = os.getenv("KIMI_API_KEY", "")
GROK_COOKIES_JSON  = os.getenv("GROK_COOKIES", "")


# ── 日期工具 ─────────────────────────────────────────────────────
def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════════════
# 加载 Cookie（从 GitHub Secret 保持 Grok 登录态）
# ════════════════════════════════════════════════════════════════
def load_cookies(context):
    if not GROK_COOKIES_JSON:
        print("[Cookie] ⚠️ 未配置 GROK_COOKIES，将以未登录状态访问", flush=True)
        return
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
            if "httpOnly" in c:
                cookie["httpOnly"] = c["httpOnly"]
            if "secure" in c:
                cookie["secure"] = c["secure"]
            ss = c.get("sameSite", "")
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
            formatted.append(cookie)
        context.add_cookies(formatted)
        print(f"[Cookie] ✅ 已加载 {len(formatted)} 条 Cookie", flush=True)
    except Exception as e:
        print(f"[Cookie] ❌ Cookie 加载失败：{e}", flush=True)


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
            "button[role='switch'], input[type='checkbox']",
            timeout=8000,
        )
        is_on = page.evaluate(
            """
            () => {
              const sw = document.querySelector("button[role='switch']");
              if (sw) {
                return sw.getAttribute('aria-checked') === 'true'
                    || sw.getAttribute('data-state') === 'checked';
              }
              const cb = document.querySelector("input[type='checkbox']");
              return cb ? cb.checked : false;
            }
            """
        )
        if not is_on:
            toggle.click()
            print("[模型] ✅ Toggle 已开启", flush=True)
            time.sleep(1)
        else:
            print("[模型] ✅ Toggle 已是开启状态", flush=True)

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

    ok = page.evaluate(
        """
        (text) => {
          const el = document.querySelector("div[contenteditable='true']")
                  || document.querySelector("textarea");
          if (!el) return false;
          el.focus();
          document.execCommand('selectAll', false, null);
          document.execCommand('delete', false, null);
          document.execCommand('insertText', false, text);
          return true;
        }
        """,
        prompt_text,
    )

    if not ok:
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            for i in range(0, len(prompt_text), 500):
                page.keyboard.type(prompt_text[i : i + 500])
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
            timeout=30000,
            state="visible",
        )
        send_btn.click()
        clicked = True
    except Exception as e:
        print(f"[{label}] ⚠️ 常规点击失败（{e}），尝试 JS 点击...", flush=True)

    if not clicked:
        result = page.evaluate(
            """
            () => {
              const btn = document.querySelector("button[type='submit']")
                || document.querySelector("button[aria-label='Submit']")
                || document.querySelector("button[aria-label='Send message']");
              if (btn) { btn.click(); return true; }
              return false;
            }
            """
        )
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
    return page.evaluate(
        """
        () => {
          const msgs = document.querySelectorAll(
            '[data-testid="message"], .message-bubble, .response-content'
          );
          return msgs.length ? msgs[msgs.length - 1].innerText : "";
        }
        """
    )


def wait_and_extract(
    page,
    label,
    screenshot_prefix,
    interval=3,
    stable_rounds=4,
    max_wait=120,
    extend_if_growing=False,
    min_len=80,
):
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
            print(f"[{label}] ⚠️ 页面异常，返回最后有效内容：{e}", flush=True)
            return last_text.strip()

        last_text = text
        cur_len = len(text.strip())
        print(f"  {elapsed}s | 字符数: {cur_len}", flush=True)

        if cur_len == last_len and cur_len >= min_len:
            stable += 1
            if stable >= stable_rounds:
                print(f"[{label}] ✅ 回复完毕（连续 {stable_rounds} 次稳定，{cur_len} 字符）", flush=True)
                try:
                    page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception:
                    pass
                return text.strip()
        else:
            stable   = 0
            last_len = cur_len

    if extend_if_growing:
        print(f"[{label}] ⏳ 到达 {max_wait}s，开始每 5s 延长（最多 300s）...", flush=True)
        prev_len  = last_len
        prev_text = last_text
        ext_elapsed = 0
        while ext_elapsed < 300:
            time.sleep(5)
            ext_elapsed += 5
            try:
                text = _get_last_msg(page)
            except Exception as e:
                print(f"[{label}] ⚠️ 延长阶段页面异常：{e}", flush=True)
                return prev_text.strip()
            cur_len = len(text.strip())
            print(f"  延长 +{ext_elapsed}s | 字符数: {cur_len}", flush=True)
            if cur_len == prev_len:
                print(f"[{label}] ✅ 已停止生成，取结果", flush=True)
                try:
                    page.screenshot(path=f"{screenshot_prefix}_done.png")
                except Exception:
                    pass
                return text.strip()
            prev_len  = cur_len
            prev_text = text

        print(f"[{label}] ⚠️ 延长 300s 到达上限，强制取结果", flush=True)
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return prev_text.strip()
    else:
        print(f"[{label}] ⚠️ 超时，强制取结果", flush=True)
        try:
            page.screenshot(path=f"{screenshot_prefix}_timeout.png")
        except Exception:
            pass
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return last_text.strip()


# ════════════════════════════════════════════════════════════════
# 阶段 A 提示词（原版保留）
# ════════════════════════════════════════════════════════════════
def build_prompt_a():
    return (
        "执行Tiered Scan模式：你现在是X商业情报深度分析师。\n\n"
        "【Step 0：时间戳（必须第一步执行）】\n"
        "立即调用 code_execution 执行以下代码：\n"
        "import time\n"
        "now = int(time.time())\n"
        "since_ts = now - 86400\n"
        "print(f\"since_time:{since_ts} until_time:{now}\")\n"
        "后续所有 x_keyword_search 必须复用这两个整数时间戳（since_time/until_time）。\n\n"
        "【核心策略】\n"
        "Tier1（全量）：搜索所有推文 + 重点帖调用 x_thread_fetch 拉完整线程。\n"
        "Tier2（活跃）：仅保留赞>=30的帖做互动分析。\n"
        "Tier3（泛列）：仅保留赞>=100或大事件帖。\n"
        "使用 parallel 调用（一次最多同时发3个工具请求）。\n\n"
        "【第一轮搜索：3批并行】\n"
        "批次1 (Tier1 巨头18人)：@elonmusk @sama @karpathy @demishassabis @darioamodei @OpenAI @AnthropicAI @GoogleDeepMind @GaryMarcus @xAI @AIatMeta @GoogleAI @MSFTResearch @IlyaSutskever @gregbrockman @rowancheung @clmcleod @bindureddy\n"
        "批次2 (Tier2 中文KOL16人)：@dotey @oran_ge @vista8 @imxiaohu @Sxsyer @K_O_D_A_D_A @tualatrix @linyunqiu @garywong @web3buidl @AI_Era @AIGC_News @jiangjiang @hw_star @mranti @nishuang\n"
        "批次3 (Tier3 VC媒体16人)：@a16z @ycombinator @lightspeedvp @sequoia @foundersfund @eladgil @pmarca @bchesky @chamath @paulg @TheInformation @TechCrunch @verge @WIRED @Scobleizer @bentossell\n\n"
        "【强制规则】\n"
        "1. 所有搜索优先带 since_time/until_time；若返回0条，立即去掉时间参数重试同一批次（必须成功）。\n"
        "2. 重点推文（赞>100或含争论）立即调用 x_thread_fetch 拉完整互动。\n"
        "3. 分析只关注：新观点、吵架记录、市场反馈强度。\n"
        "4. 所有引用的 X 帖子原文必须翻译成中文，严禁保留英文原文。\n\n"
        "【输出限制（严格遵守）】\n"
        "搜索完成后，只输出一段<=200字的\"内部情报摘要\"（含核心洞察+数据缓存），最后一行必须是：\n"
        "第一轮扫描完毕，等待第二轮输入。\n"
        "禁止任何其他文字、解释、日报、代码块。"
    )


# ════════════════════════════════════════════════════════════════
# 阶段 B 提示词（原版保留）
# ════════════════════════════════════════════════════════════════
def build_prompt_b():
    date_today, _ = get_dates()
    return (
        "执行Tiered Scan模式：这是第二轮搜索（覆盖后50个核心账号），整个任务不得超过 130 秒，超时必须立即输出当前结果。\n\n"
        "【时间戳复用（必须第一步确认）】\n"
        "直接复用第一轮Step 0输出的 since_time 和 until_time 整数时间戳。\n"
        "所有 x_keyword_search 必须优先带这两个参数；若返回0条，立即去掉时间参数重试同一批次（必须成功）。\n\n"
        "【核心策略（复用第一轮）】\n"
        "Tier1：全量搜索 + 重点帖立即调用 x_thread_fetch 拉完整线程和互动。\n"
        "Tier2：仅保留赞>=30的帖做深度分析。\n"
        "Tier3：仅保留赞>=100或重大事件。\n"
        "优先并行调用工具（一次最多同时发3个请求）。\n\n"
        "【第二轮搜索：3批并行】\n"
        "批次4 (Tier1 开源与基础设施 18人)：\n"
        "@HuggingFace @MistralAI @Perplexity_AI @GroqInc @Cohere @TogetherCompute @runwayml @Midjourney @StabilityAI @Scale_AI @CerebrasSystems @tenstorrent @weights_biases @langchainai @llama_index @supabase @vllm_project @huggingface_hub\n\n"
        "批次5 (Tier2 硬件与空间计算 16人)：\n"
        "@nvidia @AMD @Intel @SKhynix @tsmc @magicleap @NathieVR @PalmerLuckey @ID_AA_Carmack @boz @rabovitz @htcvive @XREAL_Global @RayBan @MetaQuestVR @PatrickMoorhead\n\n"
        "批次6 (Tier3 研究员与硬核圈 16人)：\n"
        "@jeffdean @chrmanning @hardmaru @goodfellow_ian @feifeili @_akhaliq @promptengineer @AI_News_Tech @siliconvalley @aithread @aibreakdown @aiexplained @aipubcast @lexfridman @hubermanlab @swyx\n\n"
        "【最终成稿指令（严格执行）】\n"
        "完成检索后，综合第一轮+第二轮所有高价值情报，挑选最震撼的10个话题（不必强行凑够10个，如果没有足够多高价值话题，可以空缺不输出任何内容）严格按以下格式输出日报：\n\n"
        "输出必须以 @@@START@@@ 开头，以 @@@END@@@ 单独成行结束，其后不得有任何其他内容。\n"
        "禁止代码块、额外文字、思考过程。\n\n"
        "严格模板（注意：@账号行与引用行之间禁止空行）：\n"
        "@@@START@@@\n"
        f"📡 昨夜，X上硅谷AI圈都在聊啥 | {date_today}\n\n"
        "**🏰巨头宫斗**\n\n"
        "**🍉 1. 话题标题**\n"
        "**🗣️ 极客原声态：**\n"
        "@账号 | 姓名 | 身份\n"
        "> \"中文翻译内容\"(❤️赞/💬评)\n"
        "**📝 严肃吃瓜：**\n"
        "• 📌 涨姿势：（补充增量事实和知识等）...\n"
        "• 🧠 猜博弈：（推测分析背后的隐性博弈等）...\n"
        "• 🎯 识风向：（一二级资本市场影响，如有）...\n\n"
        "（按此格式完成剩余话题，合理分配 巨头宫斗 中文圈 硬件与空间计算 投资人 等维度，也可按抓取内容总结各种热点维度）\n"
        "@@@END@@@"
    )


# ════════════════════════════════════════════════════════════════
# 阶段 C 提示词（原版保留）
# ════════════════════════════════════════════════════════════════
def build_prompt_c():
    return (
        "执行阶段C：标题 + 封面图提示词生成（从当前10条新闻中提炼）。\n\n"
        "【核心任务（一步完成）】\n"
        "从以上10条新闻中，挑选最具冲突感、炸裂感或吃瓜属性的1~2个核心事件，生成以下三项输出：\n\n"
        "输出一：微信公众号文章标题\n"
        "要求：\n"
        "- 极度抓眼球，制造强烈好奇心或情绪冲击\n"
        "- 风格参考：XXX公开撕XXX：这场战争刚刚开始 / AI圈最大瓜：XXX当众打脸XXX\n"
        "- 允许用数字、破折号、感叹号增强张力\n"
        "- 长度严格15~30个汉字\n"
        "- 禁止平淡陈述、学术腔\n\n"
        "输出二：封面图英文提示词\n"
        "- 风格：American comic book style，Marvel/DC panel感，bold black ink outlines，flat vibrant colors，halftone dot shading\n"
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
# Kimi 兜底（阶段C超时备用）
# ════════════════════════════════════════════════════════════════
def kimi_fallback(raw_b_text):
    if not KIMI_API_KEY:
        print("[Kimi兜底] KIMI_API_KEY 未配置，跳过", flush=True)
        return "", "", ""
    if not raw_b_text or len(raw_b_text) < 100:
        print("[Kimi兜底] 阶段B内容过短，跳过", flush=True)
        return "", "", ""

    print("\n[Kimi兜底] 阶段C无结果，调用 Kimi API...", flush=True)
    system_msg = "你是一位擅长撰写爆款公众号标题和封面文案的资深编辑，熟悉AI和科技圈动态。"
    user_msg = (
        "以下是今日硅谷AI圈热点日报内容：\n\n"
        + raw_b_text[:6000]
        + "\n\n请根据以上内容，挑选最具冲突感或吃瓜属性的1~2个核心事件，严格只输出以下三行，禁止任何解释：\n"
        "TITLE: <中文标题，极度抓眼球，15~30个汉字，允许破折号/感叹号，禁止学术腔>\n"
        "PROMPT: <英文文生图提示词，American comic book style，两股势力对抗，抽象符号代替人脸，横版16:9，<=150词>\n"
        "INSIGHT: <150~200字深度解读，分析对中国AI从业者/VC/散户的影响，幽默风趣，流畅段落，禁止列表>"
    )
    try:
        resp = requests.post(
            "https://api.moonshot.cn/v1/chat/completions",
            headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "moonshot-v1-8k",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.7,
                "max_tokens": 1000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[Kimi兜底] 生成成功，长度：{len(result)} 字符", flush=True)

        title_m   = re.search(r"TITLE[:：]\s*(.+)", result)
        prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", result)
        insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", result)

        title   = title_m.group(1).strip()   if title_m   else ""
        prompt  = prompt_m.group(1).strip()  if prompt_m  else ""
        insight = insight_m.group(1).strip() if insight_m else ""

        print(f"[Kimi兜底] 标题：{title}", flush=True)
        return title, prompt, insight
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
    print(f"[生图] 提示词：{prompt[:120]}...", flush=True)
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


# ════════════════════════════════════════════════════════════════
# 下载 / 上传图片
# ════════════════════════════════════════════════════════════════
def download_image(url, save_path="cover.png"):
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(resp.content)
        print(f"[下载] ✅ 已保存到 {save_path}（{len(resp.content) // 1024} KB）", flush=True)
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
            params={"key": imgbb_key},
            data={"image": img_b64},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            url = data["data"]["url"]
            print(f"[图床] ✅ ImgBB URL：{url}", flush=True)
            return url
        print(f"[图床] ❌ ImgBB 上传失败：{data}", flush=True)
        return ""
    except Exception as e:
        print(f"[图床] ❌ ImgBB 上传异常：{e}", flush=True)
        return ""


# ════════════════════════════════════════════════════════════════
# 正文后处理
# ════════════════════════════════════════════════════════════════
def _remove_blank_before_quote(text):
    return re.sub(r"(@\S[^\n]*)\n\n(> )", r"\1\n\2", text)


# ════════════════════════════════════════════════════════════════
# 飞书卡片
# ════════════════════════════════════════════════════════════════
def build_feishu_card(text, title, cover_url="", insight=""):
    text = _remove_blank_before_quote(text)
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
    if not FEISHU_WEBHOOK_URL:
        print("⚠️ FEISHU_WEBHOOK_URL 未配置，跳过", flush=True)
        return
    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=card_payload, timeout=30)
        print(f"飞书推送：{resp.status_code} | {resp.text[:80]}", flush=True)
    except Exception as e:
        print(f"飞书推送异常：{e}", flush=True)


# ════════════════════════════════════════════════════════════════
# 微信 HTML
# ════════════════════════════════════════════════════════════════
def _md_to_html(text):
    text = re.sub(r"\*\*([^*]+?)\*\*", r"\1", text)
    return text.replace("\n", "")


def build_wechat_html(text, cover_url="", insight=""):
    text = _remove_blank_before_quote(text)
    cover_block = (
        f'<p style="text-align:center;margin:0 0 16px 0;">'
        f'<img src="{cover_url}" style="max-width:100%;border-radius:8px;" /></p>'
        if cover_url else ""
    )
    insight_block = (
        '<div style="border-radius:8px;background:#FFF7E6;padding:12px 14px;margin:0 0 16px 0;">'
        '<div style="font-weight:bold;margin-bottom:6px;">🔍 深度解读</div>'
        f'<div>{insight.replace(chr(10), "")}</div></div>'
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
            json={"title": title, "author": "大尉Prinski", "html_content": html_content, "cover_jpg": cover_url},
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


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════
def main():
    print("=" * 60, flush=True)
    print("🚀 AI吃瓜日报自动化任务启动（GitHub Actions 本地浏览器版）", flush=True)
    print("=" * 60, flush=True)

    raw_b_text    = ""
    cover_prompt  = ""
    cover_title_c = ""
    cover_insight = ""

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
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        load_cookies(context)
        page = context.new_page()

        # Step 1：打开 Grok
        print("\n打开 grok.com...", flush=True)
        page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        page.screenshot(path="00_opened.png")

        # 检查登录状态
        if "sign" in page.url.lower() or "login" in page.url.lower():
            print("❌ Cookie 已过期，需要重新导出 Cookie 并更新 GROK_COOKIES Secret", flush=True)
            page.screenshot(path="00_login_required.png")
            raise SystemExit(1)
        print("✅ 已登录 Grok", flush=True)

        # Step 2：开启 Grok 4.20 Beta
        enable_grok4_beta(page)

        # Step 3：阶段 A
        send_prompt(page, build_prompt_a(), "阶段A", "03_stage_a")
        print("[阶段A] ⏳ 强制等待 50s，确保 Grok 工具调用完成...", flush=True)
        time.sleep(50)
        _ = wait_and_extract(
            page, "阶段A", "03_stage_a",
            interval=3, stable_rounds=4, max_wait=120,
            extend_if_growing=True, min_len=100,
        )

        # Step 4：阶段 B
        send_prompt(page, build_prompt_b(), "阶段B", "04_stage_b")
        print("[阶段B] ⏳ 强制等待 60s，等待工具调用启动...", flush=True)
        time.sleep(60)
        raw_b_text = wait_and_extract(
            page, "阶段B", "04_stage_b",
            interval=5, stable_rounds=3, max_wait=200,
            extend_if_growing=True, min_len=1000,
        )
        print(f"\n阶段B 内容长度：{len(raw_b_text)} 字符", flush=True)

        # Step 5：阶段 C
        cover_raw = ""
        try:
            send_prompt(page, build_prompt_c(), "阶段C", "05_stage_c")
            cover_raw = wait_and_extract(
                page, "阶段C", "05_stage_c",
                interval=3, stable_rounds=3, max_wait=60,
                extend_if_growing=False, min_len=80,
            )
        except Exception as e:
            print(f"[阶段C] ⚠️ 执行异常：{e}", flush=True)

        title_match   = re.search(r"TITLE[:：]\s*(.+)", cover_raw)
        prompt_match  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", cover_raw)
        insight_match = re.search(r"INSIGHT[:：]\s*([\s\S]+)", cover_raw)

        cover_title_c = title_match.group(1).strip()   if title_match   else ""
        cover_prompt  = prompt_match.group(1).strip()  if prompt_match  else ""
        cover_insight = insight_match.group(1).strip() if insight_match else ""

        if not cover_title_c and not cover_prompt and not cover_insight:
            print("[阶段C] 三项均为空，启动 Kimi 兜底...", flush=True)
            cover_title_c, cover_prompt, cover_insight = kimi_fallback(raw_b_text)

        print(f"\n[阶段C] 动态标题：{cover_title_c}", flush=True)
        print(f"[阶段C] 封面图提示词：{cover_prompt[:80]}...", flush=True)
        print(f"[阶段C] 深度解读：{cover_insight[:60]}...", flush=True)

        browser.close()

    # 内容质量守卫
    if not is_valid_content(raw_b_text):
        print("\n❌ 日报内容质量不达标（内容过短或缺少标识符），终止推送。", flush=True)
        print(f" 原始内容前200字：{raw_b_text[:200]}", flush=True)
        raise SystemExit(1)

    final_markdown = extract_markdown_block(raw_b_text) or raw_b_text.strip()

    # Step 6：生图
    cover_url = generate_cover_image(cover_prompt)
    download_image(cover_url, "cover.png")

    # Step 7：标题
    if cover_title_c:
        title = cover_title_c
    else:
        m = re.search(r"昨夜，X上[^\n]*", final_markdown)
        title = m.group(0).strip() if m else "AI圈极客吃瓜日报"
    print(f"\n标题：{title}", flush=True)

    # Step 8：ImgBB
    imgbb_url = upload_to_imgbb("cover.png")
    final_cover_url = imgbb_url if imgbb_url else cover_url
    print(f"封面图最终 URL：{final_cover_url[:80] if final_cover_url else '无'}", flush=True)

    # Step 9：推送飞书
    print("\n推送飞书...", flush=True)
    push_to_feishu(build_feishu_card(final_markdown, title, final_cover_url, cover_insight))

    # Step 10：推送极简云
    print("推送极简云...", flush=True)
    push_to_jijyun(build_wechat_html(final_markdown, final_cover_url, cover_insight), title, final_cover_url)

    print("\n🎉 全部完成！", flush=True)


if __name__ == "__main__":
    main()
