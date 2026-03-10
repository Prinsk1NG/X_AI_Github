#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_llm_endpoints.py (modified)
- Ensure debug_outputs exists at startup
- Print presence of API keys (REDACTED)
- Catch unhandled exceptions and write debug_outputs/error.txt
- Always write debug_outputs/summary.json (even if skipped)
"""
from __future__ import annotations
import os
import sys
import json
import time
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

# Ensure debug dir exists right away so Actions can collect artifacts even on early exit
DEBUG_DIR = Path("debug_outputs")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# Environment variable names used by grok_auto_task.py
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Heuristic token estimate (rough)
def estimate_tokens(text: str) -> int:
    # average 4 chars per token (coarse)
    return max(1, int(len(text) / 4))

def redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    safe = {}
    for k, v in (headers or {}).items():
        if not isinstance(v, str):
            v = str(v)
        if k.lower() == "authorization":
            safe[k] = "REDACTED"
        else:
            safe[k] = v
    return safe

def safe_decode_content(resp: requests.Response) -> str:
    try:
        # resp.text does decoding; if it fails, use content decode fallback
        return resp.text
    except Exception:
        try:
            return resp.content.decode("utf-8", "replace")
        except Exception:
            return repr(resp.content[:2000])

def safe_dump(obj: Any, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(str(obj))

def save_response_dump(service: str, info: Dict[str, Any]):
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    p = DEBUG_DIR / f"{service}_response_{ts}.json"
    safe_dump(info, p)
    return p

def safe_print_request_info(prefix: str, url: str, headers: Dict[str, Any], payload: Dict[str, Any]):
    try:
        payload_chars = len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        payload_chars = len(str(payload))
    est_tokens = estimate_tokens(json.dumps(payload, ensure_ascii=False))
    print(f"[DEBUG] {prefix} -> URL: {url}")
    print(f"[DEBUG] {prefix} -> headers: {redact_headers(headers)}")
    print(f"[DEBUG] {prefix} -> payload chars: {payload_chars}, est tokens: {est_tokens}")

def send_post_with_capture(name: str, url: str, headers: Dict[str, Any], json_payload: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "service": name,
        "url": url,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "request_headers": redact_headers(headers),
    }
    try:
        try:
            out["request_payload_chars"] = len(json.dumps(json_payload, ensure_ascii=False))
        except Exception:
            out["request_payload_chars"] = len(str(json_payload))
        out["request_est_tokens"] = estimate_tokens(json.dumps(json_payload, ensure_ascii=False))
        # print request info
        safe_print_request_info(name, url, headers, json_payload)

        resp = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
        out["response_status_code"] = resp.status_code
        # response headers might contain non-string values
        out["response_headers"] = {k: (v if isinstance(v, str) else str(v)) for k, v in resp.headers.items()}
        # safe decode
        body_text = safe_decode_content(resp)
        out["response_text_snippet"] = body_text[:10000]  # avoid huge logs, but keep lots of context
        # attempt to parse JSON
        try:
            out["response_json"] = resp.json()
        except Exception as e:
            out["response_json"] = None
            out["response_json_error"] = str(e)
            # include longer fallback
            out["response_text_full"] = body_text

        # save full dump
        dump_path = save_response_dump(name, out)
        out["dump_file"] = str(dump_path)
        print(f"[DEBUG] {name} -> HTTP {resp.status_code} saved to {dump_path}")
        return out

    except Exception as exc:
        tb = traceback.format_exc()
        out["exception"] = str(exc)
        out["traceback"] = tb
        dump_path = save_response_dump(name, out)
        out["dump_file"] = str(dump_path)
        print(f"[ERROR] {name} -> Exception captured and saved to {dump_path}")
        print(tb)
        return out

def build_prompt_from_file_or_default(prompt_file: Optional[str]) -> str:
    if prompt_file:
        p = Path(prompt_file)
        if not p.exists():
            print(f"[WARN] prompt file {prompt_file} not found, falling back to built-in prompt.")
        else:
            try:
                txt = p.read_text(encoding="utf-8")
                return txt
            except Exception:
                return p.read_text(encoding="utf-8", errors="replace")
    # Try to use existing combined data if present (data/*.jsonl)
    candidate = Path("data")
    if candidate.exists() and candidate.is_dir():
        # try to pick latest file inside data
        files = sorted(candidate.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files:
            if f.is_file():
                try:
                    txt = f.read_text(encoding="utf-8")
                    if len(txt) > 200:
                        return txt[:20000]  # cap to avoid huge payloads
                except Exception:
                    continue
    # fallback small prompt
    return "测试: 请基于以下示例数据生成三行简短输出。\n" + ("这是一个测试行。\n" * 50)

def run_kimi(prompt: str, max_output_tokens: int):
    if not KIMI_API_KEY:
        print("[Kimi] KIMI_API_KEY not set in environment; skipping Kimi test.")
        return None
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": max_output_tokens,
    }
    return send_post_with_capture("Kimi_Moonshot", url, headers, payload, timeout=300)

def run_claude(prompt: str, max_output_tokens: int, x_title_safe: str = "AI_Daily_Report"):
    if not OPENROUTER_API_KEY:
        print("[Claude] OPENROUTER_API_KEY not set in environment; skipping Claude test.")
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Prinsk1NG/X_AI_Github",
        "X-Title": x_title_safe,  # IMPORTANT: ASCII-only header to avoid latin-1 issues
    }
    payload = {
        "model": "anthropic/claude-sonnet-4-6",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": max_output_tokens,
    }
    return send_post_with_capture("Claude_OpenRouter", url, headers, payload, timeout=300)

def parse_args():
    p = argparse.ArgumentParser(description="Diagnostic tester for Kimi (Moonshot) and Claude (OpenRouter) endpoints.")
    p.add_argument("--prompt-file", "-f", help="Path to prompt file (jsonl or text). If omitted, use a small default or data/* file.")
    p.add_argument("--prompt", help="Direct prompt string (overrides --prompt-file).")
    p.add_argument("--kmax", type=int, default=4000, help="max_tokens to request from Kimi (default 4000)")
    p.add_argument("--cmax", type=int, default=4000, help="max_tokens to request from Claude (default 4000)")
    p.add_argument("--x-title", default="AI_Daily_Report", help="ASCII-safe X-Title header for OpenRouter (default 'AI_Daily_Report')")
    p.add_argument("--save-json", action="store_true", help="Save a small summary JSON in debug_outputs/summary.json")
    return p.parse_args()

def main():
    args = parse_args()
    if args.prompt:
        prompt = args.prompt
    else:
        prompt = build_prompt_from_file_or_default(args.prompt_file)

    # Print presence of keys (do NOT print values)
    print("=" * 60)
    print("LLM endpoints diagnostic - capturing request/response details")
    print("Timestamp (UTC):", datetime.utcnow().isoformat() + "Z")
    print("=" * 60)
    print("Prompt chars:", len(prompt), "Estimated tokens:", estimate_tokens(prompt))
    print("KIMI_API_KEY set:", bool(KIMI_API_KEY))
    print("OPENROUTER_API_KEY set:", bool(OPENROUTER_API_KEY))
    print()

    results = {}
    # Run Kimi
    print("[INFO] Running Kimi (Moonshot) test...")
    res_kimi = run_kimi(prompt, max_output_tokens=args.kmax)
    results["kimi"] = res_kimi

    # Run Claude
    print("[INFO] Running Claude (OpenRouter) test...")
    res_claude = run_claude(prompt, max_output_tokens=args.cmax, x_title_safe=args.x_title)
    results["claude"] = res_claude

    # Summary print
    print("\n" + "=" * 40)
    print("Summary")
    print("=" * 40)
    for name, r in results.items():
        if r is None:
            print(f"{name}: skipped (missing API key)")
            continue
        status = r.get("response_status_code", "EXC")
        dump = r.get("dump_file", "")
        print(f"{name}: HTTP {status}, saved: {dump}")
        snippet = r.get("response_text_snippet") or ""
        print(f"  snippet (first 500 chars):\n{snippet[:500]}")
        print("-" * 40)

    if args.save_json:
        summary_path = DEBUG_DIR / "summary.json"
        safe_dump(results, summary_path)
        print(f"Saved summary to {summary_path}")

    print("\nDone.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Ensure any unhandled exception is captured to a file for Actions debugging
        tb = traceback.format_exc()
        err_path = DEBUG_DIR / "error.txt"
        err_path.write_text(tb, encoding="utf-8", errors="replace")
        print(f"[FATAL] Unhandled exception; traceback written to {err_path}", file=sys.stderr)
        raise
