#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_llm_endpoints.py

Diagnostic tool to exercise Kimi (Moonshot) and Claude (OpenRouter) endpoints and capture
detailed request/response information for debugging.

Usage:
  python3 test_llm_endpoints.py --help
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
from typing import Any, Dict, Optional

import requests

# Environment variable names used by grok_auto_task.py
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

DEBUG_DIR = Path("debug_outputs")
DEBUG_DIR.mkdir(exist_ok=True)

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
    """
    Sends POST and returns a dict with:
      - service, url, timestamp_utc
      - request_headers (redacted)
      - request_payload_chars / request_est_tokens
      - response_status (if any)
      - response_headers
      - response_text_snippet / response_text_full (if needed)
      - response_json (if parsed)
      - exception / traceback (if any)
    Also saves full dump to debug_outputs.
    """
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
        safe_print_request_info(name, url, headers, json_payload)

        resp = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
        out["response_status"] = resp.status_code
        out["response_headers"] = {k: (v if isinstance(v, str) else str(v)) for k, v in resp.headers.items()}
        body_text = safe_decode_content(resp)
        out["response_text_snippet"] = body_text[:10000]
        try:
            out["response_json"] = resp.json()
        except Exception as e:
            out["response_json"] = None
            out["response_json_error"] = str(e)
            out["response_text_full"] = body_text

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
    """
    Load prompt text from a given path or fall back to existing data/ files or a built-in prompt.

    Behavior:
    - If prompt_file is None or empty: try to pick a recent file from data/ (same as before).
    - If prompt_file exists and is a file: try to read it (with an encoding fallback).
    - If prompt_file exists and is a directory: pick the newest readable file inside it.
    - If anything fails, fall back to the small built-in prompt.

    Returns the prompt text (string). Caps large results when reading from data/ to avoid huge payloads.
    """
    if prompt_file:
        p = Path(prompt_file)
        if not p.exists():
            print(f"[WARN] prompt path {prompt_file} not found, falling back to built-in prompt.")
        elif p.is_dir():
            # 如果传入目录，尝试从目录里选最新的可读文件
            files = sorted(p.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
            for f in files:
                if f.is_file():
                    try:
                        txt = f.read_text(encoding="utf-8")
                        if txt and len(txt) > 0:
                            print(f"[INFO] Using prompt from file: {f}")
                            return txt
                    except Exception:
                        # 尝试下一文件
                        continue
            print(f"[WARN] prompt directory {prompt_file} contains no readable files, falling back to built-in prompt.")
        else:
            # 传入的是文件，安全读取（带回退）
            try:
                txt = p.read_text(encoding="utf-8")
                print(f"[INFO] Using prompt file: {p}")
                return txt
            except Exception:
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    print(f"[WARN] Read prompt file {p} with replacement errors.")
                    return txt
                except Exception:
                    print(f"[WARN] Failed to read prompt file {prompt_file}, falling back to built-in prompt.")

    # Try to use existing combined data if present (data/*)
    candidate = Path("data")
    if candidate.exists() and candidate.is_dir():
        # try to pick latest file inside data
        files = sorted(candidate.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files:
            if f.is_file():
                try:
                    txt = f.read_text(encoding="utf-8")
                    if len(txt) > 200:
                        print(f"[INFO] Using prompt from data file: {f}")
                        return txt[:20000]  # cap to avoid huge payloads
                except Exception:
                    continue

    # fallback small prompt
    print("[INFO] Using built-in fallback prompt.")
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
    }
    payload = {
        "model": "claude-2.1",  # adjust as appropriate for your OpenRouter model alias
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": max_output_tokens,
    }
    return send_post_with_capture("Claude_OpenRouter", url, headers, payload, timeout=300)

def parse_args():
    p = argparse.ArgumentParser(description="Test LLM endpoints and capture debug outputs.")
    p.add_argument("--prompt-file", help="Path to prompt file or directory (if directory, choose newest readable file).", default=None)
    p.add_argument("--kmax", type=int, help="Kimi max tokens", default=512)
    p.add_argument("--cmax", type=int, help="Claude max tokens", default=512)
    p.add_argument("--save-json", action="store_true", help="Save summary JSON to debug_outputs/summary.json")
    return p.parse_args()

def main():
    args = parse_args()
    try:
        prompt = build_prompt_from_file_or_default(args.prompt_file)
        print(f"[INFO] Prompt length: {len(prompt)} chars")

        results = {}
        kimi_out = run_kimi(prompt, args.kmax)
        results["kimi"] = kimi_out

        claude_out = run_claude(prompt, args.cmax)
        results["claude"] = claude_out

        if args.save_json:
            summary_path = DEBUG_DIR / "summary.json"
            safe_dump(results, summary_path)
            print(f"[INFO] Saved summary to {summary_path}")

        # print brief summary
        print("\n=== Summary ===")
        for svc, out in results.items():
            if out is None:
                print(f"{svc}: skipped")
            else:
                status = out.get("response_status") or out.get("exception") or "unknown"
                print(f"{svc}: {status} (dump: {out.get('dump_file')})")

        return 0
    except Exception as exc:
        print("[ERROR] Unhandled exception in main:", exc)
        print(traceback.format_exc())
        return 1

if __name__ == "__main__":
    sys.exit(main())
