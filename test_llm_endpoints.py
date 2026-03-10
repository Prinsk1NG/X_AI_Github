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
