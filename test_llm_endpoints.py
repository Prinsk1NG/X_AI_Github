#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_llm_endpoints.py

Diagnostic tool to exercise Kimi (Moonshot) and Claude (OpenRouter/Anthropic via OpenRouter)
endpoints and capture detailed request/response information for debugging.

Usage:
  python3 test_llm_endpoints.py --help
"""
from __future__ import annotations
import os
import sys
import json
import traceback
import argparse
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.exceptions import RequestException, ConnectionError, Timeout

# Environment variable names used elsewhere
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

def send_post_with_capture(name: str, url: str, headers: Dict[str, Any], json_payload: Dict[str, Any], timeout: int = 300,
                           max_retries: int = 4, backoff_base: float = 1.0) -> Dict[str, Any]:
    """
    Sends POST and returns a dict capturing:
      - request metadata
      - response status, headers, body (snippet), parsed JSON (if any)
      - exception & traceback if raised

    Retries on network errors and on retryable status codes (429, 502, 503, 504).
    Exponential backoff with jitter is used.
    """
    out: Dict[str, Any] = {
        "service": name,
        "url": url,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "request_headers": redact_headers(headers),
    }

    # metadata about attempts
    attempts = []
    for attempt in range(1, max_retries + 1):
        attempt_meta = {"attempt": attempt, "timestamp": datetime.utcnow().isoformat() + "Z"}
        try:
            try:
                out["request_payload_chars"] = len(json.dumps(json_payload, ensure_ascii=False))
            except Exception:
                out["request_payload_chars"] = len(str(json_payload))
            out["request_est_tokens"] = estimate_tokens(json.dumps(json_payload, ensure_ascii=False))
            safe_print_request_info(name, url, headers, json_payload)

            resp = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
            attempt_meta["status_code"] = resp.status_code
            body_text = safe_decode_content(resp)
            attempt_meta["response_snippet"] = body_text[:500]
            attempts.append(attempt_meta)

            # If success (2xx) or non-retryable client error, capture and return
            if 200 <= resp.status_code < 300:
                out.update({
                    "response_status": resp.status_code,
                    "response_headers": {k: (v if isinstance(v, str) else str(v)) for k, v in resp.headers.items()},
                    "response_text_snippet": body_text[:10000],
                    "response_text_full": body_text,
                })
                try:
                    out["response_json"] = resp.json()
                except Exception:
                    out["response_json"] = None
                out["attempts"] = attempts
                out["dump_file"] = str(save_response_dump(name, out))
                print(f"[DEBUG] {name} -> HTTP {resp.status_code} saved to {out['dump_file']}")
                return out

            # Retryable status codes
            if resp.status_code in (429, 502, 503, 504):
                print(f"[WARN] {name} -> attempt {attempt} received retryable status {resp.status_code}.")
                # fall through to retry logic below

            else:
                # Non-retryable error (e.g., 400) — return immediately with response content
                out.update({
                    "response_status": resp.status_code,
                    "response_headers": {k: (v if isinstance(v, str) else str(v)) for k, v in resp.headers.items()},
                    "response_text_snippet": body_text[:10000],
                    "response_text_full": body_text,
                })
                try:
                    out["response_json"] = resp.json()
                except Exception:
                    out["response_json"] = None
                out["attempts"] = attempts
                out["dump_file"] = str(save_response_dump(name, out))
                print(f"[DEBUG] {name} -> HTTP {resp.status_code} saved to {out['dump_file']}")
                return out

        except (ConnectionError, Timeout) as net_exc:
            # Network-level error — eligible for retry
            attempt_meta["exception"] = str(net_exc)
            attempts.append(attempt_meta)
            print(f"[WARN] {name} -> network error on attempt {attempt}: {net_exc}")
        except RequestException as req_exc:
            attempt_meta["exception"] = str(req_exc)
            attempts.append(attempt_meta)
            print(f"[WARN] {name} -> request error on attempt {attempt}: {req_exc}")
        except Exception as exc:
            # Unexpected error — capture and return
            tb = traceback.format_exc()
            out["exception"] = str(exc)
            out["traceback"] = tb
            out["attempts"] = attempts + [attempt_meta]
            out["dump_file"] = str(save_response_dump(name, out))
            print(f"[ERROR] {name} -> Unexpected exception saved to {out['dump_file']}")
            print(tb)
            return out

        # if we reach here, we will retry unless attempts exhausted
        if attempt < max_retries:
            # exponential backoff with jitter
            sleep_seconds = backoff_base * (2 ** (attempt - 1))
            jitter = random.uniform(0, sleep_seconds * 0.2)
            sleep_for = sleep_seconds + jitter
            print(f"[INFO] {name} -> sleeping {sleep_for:.2f}s before retry (attempt {attempt}/{max_retries})")
            time.sleep(sleep_for)
        else:
            print(f"[ERROR] {name} -> exhausted {max_retries} attempts without success.")

    # All retries exhausted: assemble final out
    out.update({
        "response_status": attempts[-1].get("status_code") if attempts else None,
        "attempts": attempts,
    })
    out["dump_file"] = str(save_response_dump(name, out))
    print(f"[ERROR] {name} -> final failure saved to {out['dump_file']}")
    return out

def build_prompt_from_file_or_default(prompt_file: Optional[str]) -> str:
    """
    Load prompt text from a given path or fall back to existing data/ files or a built-in prompt.

    Behavior:
    - If prompt_file is None or empty: try to pick a recent file from data/.
    - If prompt_file exists and is a file: read it (with encoding fallback).
    - If prompt_file exists and is a directory: pick the newest readable file inside it.
    - If anything fails, fall back to a built-in small prompt.
    """
    if prompt_file:
        p = Path(prompt_file)
        if not p.exists():
            print(f"[WARN] prompt path {prompt_file} not found, falling back to built-in prompt.")
        elif p.is_dir():
            # If passed a directory, choose newest readable file inside
            files = sorted(p.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
            for f in files:
                if f.is_file():
                    try:
                        txt = f.read_text(encoding="utf-8")
                        if txt and len(txt) > 0:
                            print(f"[INFO] Using prompt from file: {f}")
                            return txt
                    except Exception:
                        continue
            print(f"[WARN] prompt directory {prompt_file} contains no readable files, falling back to built-in prompt.")
        else:
            # Passed a file: read safely
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

    # fallback prompt
    print("[INFO] Using built-in fallback prompt.")
    return "测试: 请基于以下示例数据生成三行简短输出。\n" + ("这是一个测试行。\n" * 50)

def run_kimi(prompt: str, max_output_tokens: int):
    """
    Send request to Kimi (Moonshot).
    Fix: this model only allows temperature = 1; default to 1 but allow override via KIMI_TEMPERATURE env var.
    """
    if not KIMI_API_KEY:
        print("[Kimi] KIMI_API_KEY not set in environment; skipping Kimi test.")
        return None
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"}

    # Default to 1 (service requires it); allow explicit override via env (use with caution)
    try:
        env_temp = os.getenv("KIMI_TEMPERATURE")
        if env_temp is not None:
            temperature = float(env_temp)
        else:
            temperature = 1
    except Exception:
        temperature = 1

    payload = {
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    # Use retries inside send_post_with_capture to handle 429/overload
    return send_post_with_capture("Kimi_Moonshot", url, headers, payload, timeout=300, max_retries=5, backoff_base=1.0)

def run_claude(prompt: str, max_output_tokens: int, x_title_safe: str = "AI_Daily_Report"):
    """
    Send request to OpenRouter (Claude).
    Attempt a list of possible endpoints for resilience against DNS/server differences.
    """
    if not OPENROUTER_API_KEY:
        print("[Claude] OPENROUTER_API_KEY not set in environment; skipping Claude test.")
        return None

    openrouter_model = os.getenv("OPENROUTER_MODEL", "claude-2.1")
    # Try endpoints in order; some environments resolve one but not the other
    endpoints = [
        "https://api.openrouter.ai/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",  # fallback if api host fails
    ]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}",
        "X-Title": x_title_safe,
    }
    payload = {
        "model": openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": max_output_tokens,
    }

    last_resp = None
    for ep in endpoints:
        print(f"[INFO] Claude_OpenRouter -> trying endpoint: {ep}")
        resp = send_post_with_capture("Claude_OpenRouter", ep, headers, payload, timeout=300, max_retries=3, backoff_base=1.0)
        last_resp = resp
        # If network-level exception present, try next endpoint
        if resp.get("exception") or ("response_status" not in resp and resp.get("response_json") is None):
            print(f"[WARN] Claude_OpenRouter -> attempt to {ep} failed (see dump). Trying next endpoint if any.")
            continue
        # If we got a retryable status recorded in response_status that is network-like, try next
        status = resp.get("response_status")
        if status and status >= 500:
            print(f"[WARN] Claude_OpenRouter -> server error {status} at {ep}, trying next endpoint if any.")
            continue
        # success or client error (400-series), stop trying further endpoints
        break

    # If last_resp indicates model-not-found (404 with specific message), log hint
    try:
        err = (last_resp.get("response_json") or {})
        if isinstance(err, dict):
            msg = err.get("error", {}).get("message") if err.get("error") else err.get("message")
            if msg and "No endpoints found" in msg:
                print("[WARN] OpenRouter model not found. Consider running list_openrouter_models() or set OPENROUTER_MODEL to an available alias.")
    except Exception:
        pass

    return last_resp

def list_openrouter_models():
    """
    Helper to list OpenRouter models available to your API key and save to debug_outputs/openrouter_models.json.
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
        print("=" * 60)
        print("LLM endpoints diagnostic - capturing request/response details")
        print("=" * 60)
        print("Timestamp (UTC):", datetime.utcnow().isoformat() + "Z")
        print()
        print(f"Prompt chars: {len(prompt)} Estimated tokens: {estimate_tokens(prompt)}")
        print("KIMI_API_KEY set:", bool(KIMI_API_KEY))
        print("OPENROUTER_API_KEY set:", bool(OPENROUTER_API_KEY))
        print()

        results = {}

        print("[INFO] Running Kimi (Moonshot) test...")
        kimi_out = run_kimi(prompt, args.kmax)
        results["kimi"] = kimi_out

        print("[INFO] Running Claude (OpenRouter) test...")
        claude_out = run_claude(prompt, args.cmax)
        results["claude"] = claude_out

        if args.save_json:
            summary_path = DEBUG_DIR / "summary.json"
            safe_dump(results, summary_path)
            print(f"[INFO] Saved summary to {summary_path}")

        # brief summary print
        print("\n========================================")
        print("Summary")
        print("========================================")
        for svc, out in results.items():
            if out is None:
                print(f"{svc}: skipped")
            else:
                status = out.get("response_status") or out.get("exception") or out.get("response_status_code") or "unknown"
                dump = out.get("dump_file")
                print(f"{svc}: HTTP {status}, saved: {dump}")
                snippet = out.get("response_text_snippet", "")
                if snippet:
                    print("  snippet (first 500 chars):")
                    print(snippet[:500])
                print("----------------------------------------")

        print("Done.")
        return 0
    except Exception as exc:
        print("[ERROR] Unhandled exception in main:", exc)
        print(traceback.format_exc())
        return 1

if __name__ == "__main__":
    sys.exit(main())
