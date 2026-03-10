# 替换或插入以下函数到 test_llm_endpoints.py 中相应位置

def run_kimi(prompt: str, max_output_tokens: int):
    """
    使用 Moonshot / Kimi 接口发送请求。
    修复点：将 temperature 设置为 1（该 model 要求只有 1）。
    """
    if not KIMI_API_KEY:
        print("[Kimi] KIMI_API_KEY not set in environment; skipping Kimi test.")
        return None
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"}

    # 注意：此 model 只允许 temperature=1（来自服务返回的错误）
    payload = {
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": prompt}],
        # 强制使用 1（整型或 1.0 均可），避免 0.7 导致 400
        "temperature": 1,
        "max_tokens": max_output_tokens,
    }
    return send_post_with_capture("Kimi_Moonshot", url, headers, payload, timeout=300)


def run_claude(prompt: str, max_output_tokens: int, x_title_safe: str = "AI_Daily_Report"):
    """
    使用 OpenRouter (Claude) 接口发送请求。
    修复点：使��正确的 API host（api.openrouter.ai）并允许从环境变量覆盖 model alias。
    如果仍返回 404，请调用 list_openrouter_models() 检查可用模型 alias。
    """
    if not OPENROUTER_API_KEY:
        print("[Claude] OPENROUTER_API_KEY not set in environment; skipping Claude test.")
        return None

    # 推荐从环境变量读取 model alias，这样便于在不同账户下调整
    openrouter_model = os.getenv("OPENROUTER_MODEL", "claude-2.1")

    # 使用 api.openrouter.ai 前缀（避免 404 因 host 路径错误）
    url = "https://api.openrouter.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": max_output_tokens,
    }

    resp = send_post_with_capture("Claude_OpenRouter", url, headers, payload, timeout=300)

    # 如果收到 404 且错误信息提示没有该 model，给出可选建议日志
    try:
        if resp and resp.get("response_status") == 404:
            err = resp.get("response_json") or {}
            msg = err.get("error", {}).get("message") if isinstance(err, dict) else None
            if msg and "No endpoints found" in msg:
                print("[WARN] OpenRouter model not found. Try listing available models via list_openrouter_models().")
    except Exception:
        pass

    return resp


def list_openrouter_models():
    """
    辅助：列出 OpenRouter 可用模型（便于确认 model alias）。
    用法：在本地或 CI 里运行此函数以把列表保存到 debug_outputs。
    """
    if not OPENROUTER_API_KEY:
        print("[ListModels] OPENROUTER_API_KEY not set; cannot list models.")
        return None
    url = "https://api.openrouter.ai/v1/models"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        info = {
            "service": "OpenRouter_ListModels",
            "url": url,
            "timestamp_utc": datetime.utcnow().isoformat() + "Z",
            "response_status": r.status_code,
        }
        try:
            info["response_json"] = r.json()
        except Exception:
            info["response_text"] = r.text
        p = DEBUG_DIR / "openrouter_models.json"
        safe_dump(info, p)
        print(f"[INFO] Saved OpenRouter models to {p}")
        return info
    except Exception as exc:
        print("[ERROR] Failed to list OpenRouter models:", exc)
        return None
