#!/usr/bin/env python3
"""Minimal terminal chat client for OpenAI-compatible chat APIs.

Default target is DeepSeek. It reads DraftMate's local .env via config.py,
keeps conversation history in memory, and never prints API keys.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def main() -> int:
    # Windows 控制台可能非 UTF-8,打印中文回复会崩;切 UTF-8 并降级替换。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    cfg = config.load()
    parser = argparse.ArgumentParser(description="Talk to an API model from terminal.")
    parser.add_argument("prompt", nargs="*", help="One-shot prompt. Omit for interactive chat.")
    parser.add_argument("--model", default=cfg.get("reply_model") or "deepseek-v4-flash")
    parser.add_argument("--base-url", default=cfg.get("deepseek_base_url") or "https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--system", default="你是一个简洁、直接的中文助手。")
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--thinking", choices=["enabled", "disabled"], default=cfg.get("deepseek_thinking", "disabled"))
    args = parser.parse_args()

    key = os.environ.get(args.api_key_env)
    if not key:
        print(f"Missing {args.api_key_env}. Put it in .env or export it first.", file=sys.stderr)
        return 2

    messages = [{"role": "system", "content": args.system}]
    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            return 0
        messages.append({"role": "user", "content": prompt})
        return _print_reply(args, key, messages)

    print(f"API chat ready. model={args.model} base={args.base_url.rstrip('/')}  (type q/quit/exit to stop)")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in {"q", "quit", "exit"}:
            return 0
        if not text:
            continue
        messages.append({"role": "user", "content": text})
        rc = _print_reply(args, key, messages)
        if rc != 0:
            messages.pop()
            continue


def _print_reply(args: argparse.Namespace, key: str, messages: list[dict[str, str]]) -> int:
    try:
        reply = call_chat(
            base_url=args.base_url,
            api_key=key,
            model=args.model,
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            thinking=args.thinking,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    messages.append({"role": "assistant", "content": reply})
    print(f"ai> {reply}")
    return 0


def call_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 800,
    temperature: float = 0.3,
    thinking: str = "disabled",
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
    }
    if model.startswith("deepseek"):
        payload["thinking"] = {"type": thinking}
    if thinking != "enabled":
        payload["temperature"] = temperature

    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {_redact(body)}") from None
    except Exception as e:
        raise RuntimeError(f"Request failed: {e}") from None

    if data.get("error"):
        raise RuntimeError(f"API error: {_redact(json.dumps(data['error'], ensure_ascii=False))}")
    choices = data.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    return _strip_thinking(str(content)).strip()


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()


def _redact(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[redacted]", text)
    return re.sub(r"(api key:\s*)\*+[A-Za-z0-9_-]+", r"\1[redacted]", text, flags=re.I)


if __name__ == "__main__":
    raise SystemExit(main())
