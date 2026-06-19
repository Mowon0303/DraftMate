"""模型调用层:支持 Ollama(本地)、Anthropic/DeepSeek(云端)后端。"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_CONF = {
    "provider": "anthropic",
    "ollama_host": "http://localhost:11434",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_thinking": "disabled",
    "deepseek_reasoning_effort": "high",
}


def configure(
    provider: str,
    ollama_host: str = "http://localhost:11434",
    deepseek_base_url: str = "https://api.deepseek.com",
    deepseek_thinking: str = "disabled",
    deepseek_reasoning_effort: str = "high",
) -> None:
    _CONF["provider"] = (provider or "anthropic").lower()
    _CONF["ollama_host"] = ollama_host or "http://localhost:11434"
    _CONF["deepseek_base_url"] = deepseek_base_url or "https://api.deepseek.com"
    _CONF["deepseek_thinking"] = (deepseek_thinking or "disabled").lower()
    _CONF["deepseek_reasoning_effort"] = deepseek_reasoning_effort or "high"


# ---------- 对外接口 ----------

def _backend(model: str) -> str:
    """按模型名路由:云端 reply_model 可只接管文本回复,读图仍本地。"""
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("deepseek") or m in {"dsv4", "dsv4-flash", "dsv4-pro"}:
        return "deepseek"
    return _CONF["provider"]


def call_text(model: str, system: str, user: str, max_tokens: int = 1024,
              temperature: float = 0.7) -> str:
    """纯文本调用。"""
    backend = _backend(model)
    if backend == "ollama":
        return _ollama_chat(model, system, user, None, max_tokens, temperature)
    if backend == "deepseek":
        return _deepseek(model, system, user, max_tokens, temperature)
    return _anthropic(model, system, [{"role": "user", "content": user}], max_tokens, temperature)


def call_vision(model: str, system: str, user: str, image_b64: str, max_tokens: int = 1024,
                temperature: float = 0.2) -> str:
    """带一张图片的多模态调用。image_b64 为不带前缀的 base64 PNG。"""
    backend = _backend(model)
    if backend == "ollama":
        return _ollama_chat(model, system, user, image_b64, max_tokens, temperature)
    if backend == "deepseek":
        raise SystemExit("DeepSeek API 只用于文本回复;读图请用本地 OCR 或把 vision_model 设为本地视觉模型。")
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
        {"type": "text", "text": user},
    ]
    return _anthropic(model, system, [{"role": "user", "content": content}], max_tokens, temperature)


# ---------- Anthropic 后端 ----------

_client = None


def _anthropic(model: str, system: str, messages, max_tokens: int, temperature: float) -> str:
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "缺少依赖 anthropic;或在 config.yaml 把 provider 改成 ollama。安装: pip install anthropic"
            )
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit(
                "未设置 ANTHROPIC_API_KEY;若想用本地模型,请在 config.yaml 设 provider: ollama"
            )
        _client = anthropic.Anthropic(api_key=key)
    # 把 system 包成可缓存块(仅 Anthropic 生效;Ollama 路径不会走到这里)
    sys_blocks = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if system else None
    )
    resp = _client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=sys_blocks, messages=messages,
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()


# ---------- DeepSeek 后端(OpenAI-compatible HTTP) ----------

def _deepseek_model(model: str) -> str:
    m = (model or "").strip()
    aliases = {
        "dsv4": "deepseek-v4-flash",
        "dsv4-flash": "deepseek-v4-flash",
        "dsv4-pro": "deepseek-v4-pro",
    }
    return aliases.get(m.lower(), m or "deepseek-v4-flash")


def _deepseek(model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("未设置 DEEPSEEK_API_KEY;无法调用 DeepSeek API。")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": _deepseek_model(model),
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "thinking": {"type": _deepseek_thinking()},
    }
    if payload["thinking"]["type"] == "enabled":
        payload["reasoning_effort"] = _CONF.get("deepseek_reasoning_effort") or "high"
    else:
        payload["temperature"] = temperature
    url = _CONF["deepseek_base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"调用 DeepSeek 失败({url}): HTTP {e.code} {_deepseek_http_error(e)}")
    except Exception as e:
        raise SystemExit(f"调用 DeepSeek 失败({url}): {e}")
    if data.get("error"):
        err = data["error"]
        raise SystemExit(f"调用 DeepSeek 失败: {err.get('message') or err}")
    choices = data.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    return _strip_thinking(str(content)).strip()


def _deepseek_http_error(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        return str(e)
    try:
        data = json.loads(body)
        msg = (((data.get("error") or {}).get("message")) or body).strip()
    except Exception:
        msg = body.strip()
    return _redact_key_suffix(msg) or str(e)


def _redact_key_suffix(text: str) -> str:
    return re.sub(r"(api key:\s*)\*+[A-Za-z0-9_-]+", r"\1[redacted]", text, flags=re.I)


def _deepseek_thinking() -> str:
    v = str(_CONF.get("deepseek_thinking") or "disabled").lower()
    return "enabled" if v in {"enabled", "true", "1", "yes", "on"} else "disabled"


def _strip_thinking(text: str) -> str:
    return re.sub(r"(?is)<think>.*?</think>\s*", "", text or "").strip()


# ---------- Ollama 后端 ----------

def _ollama_chat(model: str, system: str, user: str, image_b64, max_tokens: int,
                 temperature: float) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    um = {"role": "user", "content": user}
    if image_b64:
        um["images"] = [image_b64]  # Ollama 原生格式:图片放消息的 images 字段(不带 data: 前缀)
    msgs.append(um)
    payload = {
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    url = _CONF["ollama_host"].rstrip("/") + "/api/chat"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise SystemExit(f"调用 Ollama 失败({url}): {e}\n请确认 ollama 已启动、且模型已 pull。")
    return (data.get("message", {}).get("content") or "").strip()
