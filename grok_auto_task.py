# -*- coding: utf-8 -*-
"""
youtube_auto_task.py  v3.0 (借刀杀人版：Jina Reader API + 第三方免代理提取)
Architecture: RSS + Native Search -> Jina AI Reader -> Claude 3.7 -> Feishu
"""

import os
import re
import json
import time
import datetime
from datetime import timezone, timedelta
from pathlib import Path

import requests
import feedparser

# ── Environment variables ────────────────────────────────────────────────────
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
KIMI_API_KEY       = os.getenv("KIMI_API_KEY", "")

# ── Tracking & Thresholds ────────────────────────────────────────────────────
MIN_DURATION_SEC = 15 * 60   # 最短 15 分钟
MIN_VIEWS        = 5000      # 搜索轨最低播放量
EVICTION_DAYS    = 30        # 30天无动态自动淘汰

# ── 50 大核心频道 ─────────────────────────────────────────
CORE_CHANNELS = {
    "UC1yNl2E66ZzKApQdRu53wwA": {"name": "Lex Fridman", "cat": "深度播客"},
    "UCcefcZRL2oaA_uBNeo5UOWg": {"name": "a16z", "cat": "顶级VC"},
    "UCaOtN7i8H72E7Uj4P1-QzQA": {"name": "Dwarkesh Patel", "cat": "深度播客"},
    "UC0vOXJzXQGoqYq4n1-YkH-w": {"name": "All-In Podcast", "cat": "商业创投"},
    "UCcefcZRL2oaA_uBNeo5UOWh": {"name": "Y Combinator", "cat": "顶级VC"},
    "UCLNgu_OupwoeESgtab33CCw": {"name": "Andrej Karpathy", "cat": "硬核技术"},
    "UCbfYPyITQ-7l4upoX8nvctg": {"name": "Two Minute Papers", "cat": "学术前沿"},
    "UC3XGzPbbB1_xR0Q8z_K3_ww": {"name": "AI Explained", "cat": "深度评测"},
}

VIP_LIST = [
    "Elon Musk", "Sam Altman", "Jensen Huang", "Ilya Sutskever", 
    "Dario Amodei", "Yann LeCun", "Mark Zuckerberg", "Demis Hassabis", 
    "Andrej Karpathy", "Satya Nadella", "Kevin Scott",
    "王小川 AI", "杨植麟", "朱啸虎 AI", "陆奇", "李彦宏", "傅盛"
]

def load_tracking_state():
    path = Path("data/yt_tracking.json")
    if path.exists():
        try: return json.loads(path.read_text("utf-8"))
        except: pass
    return {"channels": {}, "vips": {}}

def save_tracking_state(state):
    path = Path("data/yt_tracking.json")
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")

def parse_duration(duration_str):
    if not duration_str: return 0
    parts = str(duration_str).split(':')
    secs = 0
    for part in parts:
        secs = secs * 60 + int(part.replace(',', '').strip())
    return secs

def parse_views(view_str):
    if not view_str: return 0
    view_str = str(view_str).lower().replace(',', '')
    if 'k' in view_str: return int(float(re.search(r'[\d\.]+', view_str).group()) * 1000)
    if 'm' in view_str: return int(float(re.search(r'[\d\.]+', view_str).group()) * 1000000)
    if '万' in view_str: return int(float(re.search(r'[\d\.]+', view_str).group()) * 10000)
    num = re.search(r'\d+', view_str)
    return int(num.group()) if num else 0

# ════════════════════════════════════════════════════════════════════════════
# 搜索引擎：使用轻量第三方接口绕过限制
# ════════════════════════════════════════════════════════════════════════════
def fetch_html_bypass(url):
    """利用公共代理站绕过基本墙"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        # 尝试走免翻镜像站抓取搜索结果
        mirror_url = url.replace("youtube.com", "invidious.jing.rocks") 
        return requests.get(mirror_url, headers=headers, timeout=15).text
    except:
        return requests.get(url, headers=headers, timeout=15).text

def native_youtube_search(query, limit=5):
    url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}&sp=CAI%253D"
    results = []
    try:
        html_text = fetch_html_bypass(url)
        match = re.search(r'(?:ytInitialData|window\["ytInitialData"\])\s*=\s*(\{.+?\});', html_text)
        if not match: return results
            
        data = json.loads(match.group(1))
        contents = data.get('contents', {}).get('twoColumnSearchResultsRenderer', {}).get('primaryContents', {}).get('sectionListRenderer', {}).get('contents', [])
        if not contents: return results
        
        video_items = contents[0].get('itemSectionRenderer', {}).get('contents', [])
        for item in video_items:
            if 'videoRenderer' in item:
                vr = item['videoRenderer']
                vid = vr.get('videoId')
                title = vr.get('title', {}).get('runs', [{}])[0].get('text', '')
                channel = vr.get('ownerText', {}).get('runs', [{}])[0].get('text', '')
                pub_time = vr.get('publishedTimeText', {}).get('simpleText', '')
                view_count_str = vr.get('viewCountText', {}).get('simpleText', '0')
                duration_str = vr.get('lengthText', {}).get('simpleText', '0:00')
                
                results.append({
                    "id": vid, "title": title, "channel": channel,
                    "publishedTime": pub_time, "viewCount": view_count_str, "duration": duration_str
                })
                if len(results) >= limit: break
    except: pass
    return results

def scan_rss_channels(tracking_state, time_limit_hours=24):
    print(f"\n[轨道A] 正在通过 RSS 扫描 {len(CORE_CHANNELS)} 个核心频道...")
    now = datetime.datetime.now(timezone.utc)
    deadline = now - timedelta(hours=time_limit_hours)
    results = []
    
    for ch_id, info in CORE_CHANNELS.items():
        last_active = tracking_state["channels"].get(ch_id, now.isoformat())
        if (now - datetime.datetime.fromisoformat(last_active)).days > EVICTION_DAYS: continue

        try:
            feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch_id}")
            has_new = False
            for entry in feed.entries:
                pub_time = datetime.datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%S%z")
                if pub_time > deadline:
                    has_new = True
                    results.append({
                        "video_id": entry.yt_videoid, "title": entry.title,
                        "author": info["name"], "category": info["cat"],
                        "pub_time": pub_time.strftime("%Y-%m-%d"), "source": "RSS"
                    })
            if has_new: tracking_state["channels"][ch_id] = now.isoformat()
        except: pass
    print(f"[轨道A] 扫描完成，发现 {len(results)} 个最新视频。")
    return results

def scan_vip_interviews(tracking_state):
    print(f"\n[轨道B] 正在全网搜索 {len(VIP_LIST)} 位 VIP 大佬的最新访谈...")
    now = datetime.datetime.now(timezone.utc)
    results = []
    
    for vip in VIP_LIST:
        last_active = tracking_state["vips"].get(vip, now.isoformat())
        if (now - datetime.datetime.fromisoformat(last_active)).days > EVICTION_DAYS: continue

        try:
            search_res = native_youtube_search(f'"{vip}" (interview OR podcast OR 访谈)', limit=5)
            has_valid = False
            for vid in search_res:
                pub_time_str = vid.get("publishedTime", "")
                if not pub_time_str or ("year" in pub_time_str or "month" in pub_time_str or "week" in pub_time_str): continue
                if parse_duration(vid.get("duration", "0:00")) < MIN_DURATION_SEC: continue
                if parse_views(vid.get("viewCount", "0")) < MIN_VIEWS: continue
                
                has_valid = True
                results.append({
                    "video_id": vid["id"], "title": vid["title"],
                    "author": vid.get("channel", "Unknown Channel"),
                    "category": f"大佬追踪: {vip}", "pub_time": "Today", "source": "Search"
                })
            if has_valid: tracking_state["vips"][vip] = now.isoformat()
            time.sleep(1) 
        except: pass
    print(f"[轨道B] 扫描完成，捕获 {len(results)} 个 VIP 高质长视频。")
    return results

# ════════════════════════════════════════════════════════════════════════════
# Phase 2: 借刀杀人字幕引擎 (使用第三方大模型阅读器强抓)
# ════════════════════════════════════════════════════════════════════════════
def fetch_transcripts(video_list):
    print("\n[提取] 启动云端云解析引擎 (利用 Jina Reader API 免翻墙抓取)...")

    valid_videos = []
    seen_ids = set()
    
    for v in video_list:
        vid = v["video_id"]
        if vid in seen_ids: continue
        seen_ids.add(vid)
        full_text = ""
        
        # 🔴 主力引擎：Jina AI Reader API (专为大模型喂数据的阅读器，自带强大的过反爬能力)
        try:
            print(f"  ➡️ 正在呼叫 Jina 云端集群提取: {vid}")
            jina_url = f"https://r.jina.ai/https://www.youtube.com/watch?v={vid}"
            headers = {
                "Accept": "text/plain",
                "X-Return-Format": "text"
            }
            resp = requests.get(jina_url, headers=headers, timeout=40)
            
            if resp.status_code == 200 and len(resp.text) > 500:
                # Jina 返回的是 Markdown，里面包含标题、描述，我们暴力清洗提取正文
                raw_text = resp.text
                if "Title:" in raw_text:
                    raw_text = raw_text.split("Title:", 1)[-1]
                
                # 去除 Markdown 链接和多余的换行
                full_text = re.sub(r'\[.*?\]\(.*?\)', '', raw_text)
                full_text = " ".join(full_text.split())
        except Exception as e:
            print(f"  [Debug] Jina 引擎抓取失败: {e}")

        # 🟡 备用引擎：第三方 YouTube Transcript 聚合镜像站
        if not full_text or len(full_text) < 500:
            try:
                print(f"  ➡️ Jina 失败，切换到备用镜像站提取...")
                yt_mirror = f"https://youtubetranscript.com/?server_vid={vid}"
                mirror_resp = requests.get(yt_mirror, timeout=20)
                if "<text" in mirror_resp.text:
                    texts = re.findall(r'<text[^>]*>(.*?)</text>', mirror_resp.text, flags=re.DOTALL)
                    full_text = " ".join([html.unescape(t) for t in texts])
            except Exception as e:
                print(f"  [Debug] 备用镜像站抓取失败: {e}")

        # --- 最终验证与清理录入 ---
        if full_text and len(full_text) > 800:
            v["transcript"] = full_text
            valid_videos.append(v)
            print(f"  ✅ [{v['author'][:10]}] {v['title'][:25]}... ({len(full_text)} 字)")
        else:
            print(f"  ❌ 跳过 [{v['title'][:20]}]: 云端集群也无法获取该视频字幕（视频未开启字幕或限制极严）")
            
    return valid_videos

# ════════════════════════════════════════════════════════════════════════════
# Phase 3: LLM 深度提炼与推送
# ════════════════════════════════════════════════════════════════════════════
def llm_deep_analysis(videos):
    if not videos: return []
    print(f"\n[大脑] 提交 {len(videos)} 个长视频给 LLM 进行金字塔分析...")
    
    # 构建压缩版的 JSON，防止超长
    payload = [{"channel": v["author"], "title": v["title"], "tag": v["category"], "text": v["transcript"][:15000] + "\n...\n" + v["transcript"][-15000:] if len(v["transcript"])>30000 else v["transcript"]} for v in videos]
    
    prompt = f"""你是顶级硅谷创投分析师。以下是过去24小时内YouTube高价值AI对谈/讲座的全量字幕。
请使用「金字塔原理」对这些长内容进行降维打击级的深度拆解。

【原始数据】：
{json.dumps(payload, ensure_ascii=False)}

【处理要求】：
1. 剔除广告、口水话，只保留对商业、技术、创投有真正启发的视频。
2. 严格按以下 JSON 格式输出结果（严禁输出额外文字）：

@@@START@@@
{{
  "videos": [
    {{
      "category": "原数据的 tag",
      "channel": "原频道名",
      "title": "重新拟定一个极具深度的中文标题 (如：Dwarkesh对话Dario：Scaling Law的终局)",
      "tldr": "💡【TL;DR】一句话总结视频的核心结论 (50字内)",
      "core_thesis": "🎯 核心主张：受访者提出的最核心逻辑或预测",
      "arguments": [
        "• [时间锚点] 论点一及证据 (例如：[前期] 算力集群规模将决定生死)",
        "• [时间锚点] 论点二及证据"
      ],
      "counter_consensus": "🧠 反共识认知：打破了哪些大众常规偏见？",
      "implications": "💼 产业与投资推演：对当前市场的具体影响"
    }}
  ]
}}
@@@END@@@
"""
    proxies_bypass = {"http": None, "https": None}
    
    if OPENROUTER_API_KEY:
        try:
            print("  ➡️ 调用 Claude 3.7 Sonnet 中...")
            resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}, json={"model": "anthropic/claude-3.7-sonnet", "messages": [{"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": 10000}, proxies=proxies_bypass, timeout=240)
            resp.raise_for_status()
            return _extract_json(resp.json()["choices"][0]["message"]["content"])
        except Exception as e: print(f"  ❌ Claude 失败: {e}")

    if KIMI_API_KEY:
        try:
            print("  ➡️ 调用 Kimi 32k 兜底中...")
            resp = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k", "messages": [{"role": "user", "content": prompt}], "temperature": 0.5}, proxies=proxies_bypass, timeout=240)
            resp.raise_for_status()
            return _extract_json(resp.json()["choices"][0]["message"]["content"])
        except Exception as e: print(f"  ❌ Kimi 兜底失败: {e}")
    return []

def _extract_json(text):
    try:
        start = text.find("@@@START@@@") + 11
        end = text.find("@@@END@@@")
        json_str = text[start:end].strip() if start > 10 and end > -1 else text.strip('` \njson')
        return json.loads(json_str).get("videos", [])
    except Exception as e:
        print(f"解析 JSON 失败: {e}")
        return []

# ════════════════════════════════════════════════════════════════════════════
# Phase 4: Feishu 研报级卡片排版渲染
# ════════════════════════════════════════════════════════════════════════════
def build_youtube_feishu_card(analyzed_videos):
    if not analyzed_videos: return None
    date_str = datetime.datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "**⚠️ 每日早8点准时更新 | 深度长视频拆解 | 认知升级引擎**"}, "icon": {"tag": "standard_icon", "token": "time_outlined", "color": "blue"}}, {"tag": "hr"}]
    for i, v in enumerate(analyzed_videos, 1):
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"## 🍉 {i}. {v.get('title', '重磅访谈提取')}"}})
        elements.append({"tag": "note", "elements": [{"tag": "lark_md", "content": f"📺 **频道/来源**：{v.get('channel', '未知频道')} | 🏷️ **标签**：{v.get('category', '科技播客')}\n{v.get('tldr', '💡【TL;DR】暂无摘要')}"}], "background_color": "blue"})
        args_text = "\n".join(v.get('arguments', []))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 核心主张**\n<font color='grey'>{v.get('core_thesis', '')}</font>\n\n**🧱 论点与证据链**\n<font color='grey'>{args_text}</font>\n\n**🧠 反共识与认知盲区**\n<font color='grey'>{v.get('counter_consensus', '')}</font>\n\n**💼 产业与投资推演**\n<font color='grey'>{v.get('implications', '')}</font>"}})
        elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "*📅 本内容基于 AI 对长视频自动提取与分析，保留时间轴结构，方便定位原声核实。*"}})
    
    return {"msg_type": "interactive", "card": {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": "🌍 硅谷油管深极客"}, "subtitle": {"tag": "plain_text", "content": f"长内容认知折叠 | {date_str}"}, "template": "purple", "ud_icon": {"tag": "standard_icon", "token": "video_outlined"}}, "elements": elements}}

def push_to_feishu(card_payload):
    if not FEISHU_WEBHOOK_URL or not card_payload: return
    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=card_payload, proxies={"http": None, "https": None}, timeout=10)
        print(f"✅ 飞书推送成功: {resp.status_code}")
    except Exception as e: print(f"❌ 飞书推送异常: {e}")

def main():
    print("=" * 60)
    print("🚀 YouTube 播客深度深研系统 (V3.0 借刀杀人版) 启动")
    print("=" * 60)
    
    track_state = load_tracking_state()
    rss_videos = scan_rss_channels(track_state)
    vip_videos = scan_vip_interviews(track_state)
    all_videos = rss_videos + vip_videos
    
    if not all_videos:
        print("📭 过去24小时没有找到任何符合标准的硬核视频。")
        save_tracking_state(track_state)
        return
        
    ready_videos = fetch_transcripts(all_videos)
    analyzed_data = llm_deep_analysis(ready_videos)
    card = build_youtube_feishu_card(analyzed_data)
    push_to_feishu(card)
    
    save_tracking_state(track_state)
    print("\n🎉 YouTube 深度情报提取流执行完毕！")

if __name__ == "__main__":
    main()
