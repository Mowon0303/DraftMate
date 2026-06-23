"""DraftMate 副驾(本地网页 UI)。

看截图 + 旁边给几条建议回复,点「复制」自己粘贴 —— 全程只读屏 + 剪贴板,
不模拟键鼠、不自动发送,把封号风险降到最低。

运行: source .venv/bin/activate && python copilot.py
然后浏览器打开 http://127.0.0.1:8765(会自动弹)。点「读取当前对话」即可。
仅监听本机 127.0.0.1,不对外暴露。
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import agent
import config
import history
import llm
import skills
import vision

HOST, PORT = "127.0.0.1", 8765

cfg = config.load()
llm.configure(
    cfg.get("provider", "anthropic"),
    cfg.get("ollama_host", "http://localhost:11434"),
    cfg.get("deepseek_base_url", "https://api.deepseek.com"),
    cfg.get("deepseek_thinking", "disabled"),
    cfg.get("deepseek_reasoning_effort", "high"),
)
vision.configure(cfg.get("read_mode", "vlm"), cfg.get("ocr_backend", "auto"),
                 cfg.get("me_side", "right"), cfg.get("crop_left", 0.0), cfg.get("crop_bottom", 0.0),
                 cfg.get("crop_auto", True))
vision.set_app_aliases(cfg.get("app_aliases", []))
if config.DATA_DIR_ERROR:                 # 数据目录写入受阻 → 已回退临时目录,给条可见提示
    try:
        print("[DraftMate] " + config.DATA_DIR_ERROR)
    except Exception:
        pass

# 仅本地的用量计数(隐私承诺内的最低成本度量):累计「读取」次数 + 最近使用日期。
# 只写本机数据目录,无任何上报;周报靠用户自愿截图 UI 角标。
USAGE_PATH = config.base_dir() / "usage.json"
TOKENLOG_PATH = config.base_dir() / "token_usage.log"  # 逐次调用明细(JSONL),供成本分析


def _usage() -> dict:
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"reads": int(data.get("reads", 0)),
            "auto_reads": int(data.get("auto_reads", 0)),
            "last_used": str(data.get("last_used", "")),
            "in_tokens": int(data.get("in_tokens", 0)),
            "out_tokens": int(data.get("out_tokens", 0)),
            "cache_hit_tokens": int(data.get("cache_hit_tokens", 0)),
            "cache_miss_tokens": int(data.get("cache_miss_tokens", 0))}


def _bump_usage(auto: bool = False) -> None:
    # 手动/自动分开计:周留存指标只看手动「读取」,监控触发的不算,防止挂机刷数
    u = _usage()
    u["auto_reads" if auto else "reads"] += 1
    u["last_used"] = datetime.date.today().isoformat()
    try:
        USAGE_PATH.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _log_tokens(op: str) -> None:
    """把上层一次操作产生的逐调用 token 用量落盘(本机,无上报)+ 控制台一行汇总。
    用于成本测量:看清每次操作打了几次模型、input/缓存命中/output 各多少。"""
    calls = llm.drain_calls()
    if not calls:
        return
    tin = sum(c["in"] for c in calls)
    tout = sum(c["out"] for c in calls)
    hit = sum(c["cache_hit"] for c in calls)
    miss = sum(c["cache_miss"] for c in calls)
    u = _usage()  # 累计进 usage.json(保留 reads 等其它字段)
    u["in_tokens"] += tin
    u["out_tokens"] += tout
    u["cache_hit_tokens"] += hit
    u["cache_miss_tokens"] += miss
    try:
        USAGE_PATH.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    try:  # 明细 JSONL,供事后分析缓存命中率随排序优化的变化
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "op": op,
               "in": tin, "out": tout, "hit": hit, "miss": miss, "calls": calls}
        with open(TOKENLOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    rate = (hit / (hit + miss) * 100) if (hit + miss) else 0.0
    try:  # dev 控制台可见;打包 GUI 看不到也无妨(明细已落盘)
        print(f"[tokens] {op}: {len(calls)}次调用 in={tin}(缓存命中 {hit}/{hit + miss}={rate:.0f}%) out={tout}")
    except Exception:
        pass


# 军师阶段判定缓存:关系阶段是慢变量,默认复用上次判定,只在「可能动分」时才重打 DeepSeek。
# 省掉每次读取必跑的一次重调用,顺带让分数不再随机抖动。
STAGE_PATH = config.base_dir() / "stage_cache.json"
_STAGE_REASSESS_NEW_MSGS = 4   # 对方新增 ≥ 这么多条 → 重算
_STAGE_KEYWORDS = re.compile(   # 出现「分数事件」词 → 重算(廉价启发式,无模型调用)
    r"约|见面|喜欢|在一起|表白|分手|女朋友|男朋友|对象|确定关系|奔现|心动|暧昧|"
    r"讨厌|别烦|拉黑|删了|结婚|想你|爱你|生气|对不起")


def _msg_sig(m: dict) -> str:
    return f"{m.get('sender', '')}:{(m.get('text', '') or '')[:24]}"


def _load_stage_cache() -> dict:
    try:
        return json.loads(STAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_stage_cache(data: dict) -> None:
    try:
        STAGE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _staged_analysis(title, msgs, mem, manual, force: bool = False) -> tuple[str, bool]:
    """带缓存的军师阶段判定。返回 (analysis, recomputed)。
    重算条件:手动强刷 / 无缓存 / 对方新增≥N条 / 命中分数关键词。
    title 为 unknown 不落缓存(标题没读准时不串到别的联系人)。失败回退旧判定,不丢分。"""
    cacheable = bool(title) and title != "unknown"
    cache = _load_stage_cache() if cacheable else {}
    entry = cache.get(title) or {}
    prev_sigs = set(entry.get("sigs") or [])
    new_incoming = [m for m in msgs
                    if m.get("sender") not in ("我", "系统", "unknown")
                    and _msg_sig(m) not in prev_sigs]
    keyword_hit = any(_STAGE_KEYWORDS.search(m.get("text", "") or "") for m in new_incoming)
    need = (force or not entry.get("analysis")
            or len(new_incoming) >= _STAGE_REASSESS_NEW_MSGS or keyword_hit)
    if not need:
        return entry.get("analysis", ""), False
    try:
        analysis = agent.assess_stage(msgs, mem, cfg["reply_model"],
                                      cfg.get("read_last_n", 8), manual).strip()
    except Exception:
        return entry.get("analysis", ""), False
    if cacheable and analysis:
        cache[title] = {
            "analysis": analysis,
            "sigs": [_msg_sig(m) for m in msgs if m.get("sender") not in ("系统", "unknown")],
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        _save_stage_cache(cache)
    return analysis, True


def _public_status() -> dict:
    """Return non-secret runtime facts for the local UI."""
    return {
        "app_name": cfg.get("app_name") or "未配置",
        "provider": cfg.get("provider", ""),
        "read_mode": cfg.get("read_mode", ""),
        "ocr_backend": cfg.get("ocr_backend", ""),
        "vision_model": cfg.get("vision_model", ""),
        "reply_model": cfg.get("reply_model", ""),
        "default_persona": cfg.get("default_persona", ""),
        "read_last_n": cfg.get("read_last_n", 0),
        "poll_interval_seconds": cfg.get("poll_interval_seconds", 5),
        "copy_only": True,
        "usage": _usage(),
    }


def _error_text(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code not in (None, 0):
        return str(code)
    text = str(exc)
    return text or exc.__class__.__name__


# 目标模式(取代旧的语气人设):用户开软件主要是「追」,默认它;其次「维持关系」。
# 策略不写死——由模型按军师判定的分数自行拿捏(见 agent._MODE_GUIDE)。
_MODE_LABELS = {"pursue": "追", "maintain": "维持关系"}
_MODE_ORDER = ["pursue", "maintain"]


def list_personas() -> dict:
    """可选「模式」(目标)列表 + 默认。给前端的模式选择器用(类似 list_models)。默认 pursue(追)。"""
    return {"personas": [{"name": n, "label": _MODE_LABELS[n]} for n in _MODE_ORDER],
            "default": "pursue"}


def _resolve_persona(persona: str | None) -> str:
    """选中的模式 → 合法模式名;非法/缺省回退默认(追)。"""
    name = (persona or "").strip()
    return name if name in _MODE_LABELS else "pursue"


def _note_for(suggestions: list, followup) -> str:
    if followup and followup.get("hold"):
        return "💡 建议先不发 —— " + (followup.get("reason") or "发完先等对方") + "(刚发完,先把球留给对方)"
    if suggestions or followup:
        return ""
    return "最后一条是系统消息 / 没读到消息,暂不出建议。"


def _one_suggestion(title, msgs, mem, manual, analysis, persona, regen=False):
    """按用户选中的单一「模式」出一条建议:我方结尾→追加判定,对方结尾→正常回复。
    返回 (suggestions, followup)。只打一次模型(取代旧的 3 人设各一次)。"""
    name = _resolve_persona(persona)
    # 用最后一条**真实**消息(忽略尾部时间戳/系统行)判分支——否则一条结尾时间戳会被当成"系统结尾"而不出建议
    real = [m for m in (msgs or []) if m.get("sender") not in ("系统", "unknown")]
    last_sender = real[-1].get("sender") if real else None
    if last_sender == "我":                   # F1:我方结尾 → 该不该追加
        try:
            fu = agent.draft_followup(msgs, agent.mode_guide(name), mem, cfg["reply_model"],
                                      cfg["read_last_n"], manual,
                                      temperature=agent.temperature_for(name, regen=regen),
                                      stage_hint=analysis)
            followup = {"active": True, "hold": bool(fu["hold"]), "reason": fu["reason"]}
            sugg = ([{"persona": name, "text": fu["text"], "followup": True}]
                    if (not fu["hold"] and fu["text"].strip()) else [])
            return sugg, followup
        except SystemExit as e:
            return [], {"active": True, "hold": True, "reason": f"(生成失败: {_error_text(e)})"}
        except Exception as e:
            return [], {"active": True, "hold": True, "reason": f"(生成失败: {e})"}
    if last_sender:                          # 对方(或群昵称)结尾 → 正常回复
        try:
            text = agent.draft_reply(msgs, agent.mode_guide(name), mem, cfg["reply_model"],
                                     cfg["read_last_n"], manual,
                                     temperature=agent.temperature_for(name, regen=regen),
                                     stage_hint=analysis)
        except SystemExit as e:
            text = f"(生成失败: {_error_text(e)})"
        except Exception as e:
            text = f"(生成失败: {e})"
        return [{"persona": name, "text": text}], None
    return [], None


def read_and_suggest(auto: bool = False, persona: str | None = None) -> dict:
    """截图 → 读取 → 按选中模式生成一条建议。返回给前端的数据。auto=监控触发(计数分开记)。"""
    png = vision.grab(cfg["app_name"], activate=True)  # 手动读取:先把目标窗口提到前台,避免截到压在上面的窗口
    try:
        view, tmp = vision._apply_crop(png)
        img_b64 = base64.b64encode(open(view, "rb").read()).decode()
        data = vision.read_messages(png, cfg["vision_model"], cfg["read_last_n"])
    finally:
        for p in {png, locals().get("tmp")}:
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass

    _bump_usage(auto=auto)
    title = data.get("chat_title") or "unknown"
    msgs = data.get("messages") or []
    profile_was_missing = not skills.profile_exists(title)
    current_text = _memory_query_text(msgs)
    mem = skills.load_memory(title, current_text=current_text)
    manual = skills.manual_context(title)
    analysis = ""
    if msgs:  # 军师层:慢变量,带缓存按需重算(省每次读取一次重调用);失败不挡草稿生成
        analysis, _ = _staged_analysis(title, msgs, mem, manual)
    suggestions, followup = _one_suggestion(title, msgs, mem, manual, analysis, persona)
    warning = ""
    if data.get("low_confidence"):
        warning = ("⚠️ 部分消息的发言人判定置信度较低(OCR 读图在深色主题/复杂排版下易出错),"
                   "发送前请对照左侧截图核对「谁说了什么」。")
    note = _note_for(suggestions, followup)
    _log_tokens("auto" if auto else "read")  # 本次读取(军师判定 + 单条草稿/追加判定)的 token 用量
    return {
        "image": img_b64,
        "title": title,
        "is_group": bool(data.get("is_group")),
        "messages": msgs,
        "suggestions": suggestions,
        "followup": followup,
        "analysis": analysis,
        "note": note,
        "warning": warning,
        "status": _public_status(),
        "profile": {
            "title": title,
            "manual": manual,
            "needs_input": profile_was_missing or not skills.has_manual_context(manual),
        },
    }


def peek() -> dict:
    """监控用的轻量探测:截图 + OCR 读取,不生成回复。返回最后一条非系统消息的指纹,
    前端比对指纹变化且新消息来自对方时,才触发一次完整读取。"""
    png = vision.grab(cfg["app_name"])
    try:
        data = vision.read_messages(png, cfg["vision_model"], cfg["read_last_n"])
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    msgs = data.get("messages") or []
    last = next((m for m in reversed(msgs) if m.get("sender") != "系统"), None) or {}
    return {"title": data.get("chat_title") or "unknown",
            "last_sender": last.get("sender", ""),
            "last_text": last.get("text", "")}


# 历史导入的后台状态(单任务,够用):一次只跑一个导入
_import_state = {"running": False, "done": False, "error": "", "phase": "",
                 "screens": 0, "messages": 0, "title": "", "summary": ""}


def _run_import(days=None) -> None:
    """后台线程:自动滚动采集当前对话「最近 days 天」历史 → 蒸馏成记忆 → 写入 <联系人>.summary.md。"""
    try:
        _import_state.update(running=True, done=False, error="", phase="滚动采集中",
                             screens=0, messages=0, title="", summary="", earliest="")

        def prog(p):
            _import_state.update(screens=p["screens"], messages=p["messages"],
                                 earliest=p.get("earliest", ""))

        res = history.import_history(
            cfg["app_name"], cfg["vision_model"],
            days=days if days is not None else cfg.get("history_days", 7),
            max_screens=cfg.get("history_max_screens", 60),
            scroll_lines=cfg.get("history_scroll_lines", 8),
            on_progress=prog,
        )
        msgs = res.get("messages") or []
        title = res.get("title") or "unknown"
        if not msgs:
            raise RuntimeError("没读到任何消息;确认微信停在某个对话上、聊天区可见。")
        _import_state.update(phase="提取记忆中", title=title, messages=len(msgs))
        manual = skills.manual_context(title)

        def dprog(p):
            if p.get("phase") == "map":
                _import_state.update(phase=f"提取记忆中 · 摘录 {p['i']}/{p['n']} 段")
            else:
                _import_state.update(phase="提取记忆中 · 合并归类")

        summary = agent.distill_memory(msgs, cfg["reply_model"], manual, on_progress=dprog)
        skills.save_summary(title, summary)
        try:
            source = skills.compact_source(title)
            if source:
                _import_state.update(phase="压缩记忆中")
                compact_json = agent.compact_memory(source, cfg["reply_model"])
                skills.save_compacts(title, compact_json)
        except Exception:
            # compact 是内部维护层;失败不影响用户拿到导入摘要。
            pass
        if res.get("reached_target"):
            topped = f"(已覆盖最近 {days if days is not None else cfg.get('history_days', 7)} 天)"
        elif res.get("reached_top"):
            topped = "(已到对话最顶)"
        else:
            topped = "(达滚动上限,可再导一次接着上滚)"
        _import_state.update(running=False, done=True, phase=f"完成 {topped}", summary=summary)
    except SystemExit as e:
        _import_state.update(running=False, done=True, phase="失败", error=_error_text(e))
    except Exception as e:
        _import_state.update(running=False, done=True, phase="失败", error=str(e))
    finally:
        _log_tokens("import")  # 蒸馏/压缩的 token 用量(不让它漏记到下次读取)


def start_import(days=None) -> dict:
    if _import_state.get("running"):
        return {"started": False, "error": "已有导入在进行中"}
    threading.Thread(target=_run_import, args=(days,), daemon=True).start()
    return {"started": True}


_ANTHROPIC_REPLY_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-8"]
_DEEPSEEK_REPLY_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]


def _anthropic_available() -> bool:
    """Anthropic 回复可用 = 有 ANTHROPIC_API_KEY 且 anthropic 包已装。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _deepseek_available() -> bool:
    """DeepSeek 回复可用 = 有 DEEPSEEK_API_KEY。HTTP 后端不需要额外 SDK。"""
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def _cloud_available() -> bool:
    """云端回复可用:Anthropic 或 DeepSeek 任一可用。"""
    return _anthropic_available() or _deepseek_available()


def list_models() -> dict:
    """可选回复模型:本地 Ollama 已 pull 的 +(有 key 时)云端 Claude/DeepSeek。
    云端只用于回复生成、只发对话文字;读图 OCR 永远本地。默认本地不变。"""
    names: list[str] = []
    try:
        url = cfg.get("ollama_host", "http://localhost:11434").rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
    except Exception:
        names = []
    models = [{"name": n, "cloud": False} for n in names]
    anth = _anthropic_available()
    deepseek = _deepseek_available()
    if anth:
        models += [{"name": m, "cloud": True, "provider": "anthropic"} for m in _ANTHROPIC_REPLY_MODELS]
    if deepseek:
        models += [{"name": m, "cloud": True, "provider": "deepseek"} for m in _DEEPSEEK_REPLY_MODELS]
    current = cfg.get("reply_model") or ""
    if current and current not in [m["name"] for m in models]:
        cloud_current = current.lower().startswith(("claude", "deepseek", "dsv4"))
        provider = "anthropic" if current.lower().startswith("claude") else "deepseek" if cloud_current else ""
        models.insert(0, {"name": current, "cloud": cloud_current, "provider": provider})
    return {"models": models, "reply_model": current,
            "vision_model": cfg.get("vision_model") or "", "cloud_available": anth or deepseek,
            "anthropic_available": anth, "deepseek_available": deepseek}


def set_reply_model(name: str) -> None:
    """切换回复模型:内存立即生效,并只改写 config.yaml 的 reply_model 一行(注释与其余内容保留)。"""
    cfg["reply_model"] = name
    p = config.base_dir() / "config.yaml"
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return
    line = f"reply_model: {name}"
    if re.search(r"(?m)^reply_model\s*:", text):
        new = re.sub(
            r"(?m)^reply_model\s*:[^#\n]*(#.*)?$",
            lambda m: line + (f"   {m.group(1)}" if m.group(1) else ""),
            text,
        )
    else:
        new = text.rstrip("\n") + f"\n{line}\n"
    try:
        p.write_text(new, encoding="utf-8")
    except OSError:
        pass


def regenerate(title: str, persona: str, messages: list, analysis: str = "") -> dict:
    """换模式 / 换个说法:对已显示对话用指定模式重出一条(复用已读消息和军师判定,不重新截图)。
    我方结尾走追加判定,对方结尾走正常回复。返回 {suggestions, followup, note}。"""
    mem = skills.load_memory(title, current_text=_memory_query_text(messages))
    manual = skills.manual_context(title)
    sugg, fu = _one_suggestion(title, messages, mem, manual, analysis, persona, regen=True)
    _log_tokens("regenerate")
    return {"suggestions": sugg, "followup": fu, "note": _note_for(sugg, fu)}


def reassess_stage(title: str, messages: list) -> str:
    """手动「重新判定」:对已显示的对话强制重算关系阶段(不重新截图),刷新缓存。"""
    title = title or "unknown"
    mem = skills.load_memory(title, current_text=_memory_query_text(messages))
    manual = skills.manual_context(title)
    analysis, _ = _staged_analysis(title, messages, mem, manual, force=True)
    _log_tokens("reassess")
    return analysis


def _memory_query_text(messages: list) -> str:
    return "\n".join(str(m.get("text", "")) for m in (messages or [])[-cfg["read_last_n"]:])


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DraftMate</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@700;800&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    color-scheme:dark;
    --bg:#131110;
    --bg2:#181513;
    --bg3:#211b17;
    --border:#2c2520;
    --text:#f2ece5;
    --text-dim:#a99e92;
    --text-faint:#6f655b;
    --accent:#e0a23d;
    --accent-text:#1c1305;
    --accent-card:rgba(224,162,61,.11);
    --tag-bg:#1d1814;
    --send-bg:#2a231d;
    --capture-bg:#1d1a17;
    --capture-head:#181512;
    --capture-border:#2c2620;
    --capture-text:#ece6df;
    --capture-dim:#8f8478;
    --bubble-them:#272019;
    --bubble-mine:#3a7d52;
    --bubble-mine-text:#f1fff7;
    --green:#39d98a;
    --red:#ff5f57;
    --radius:12px;
    --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","IBM Plex Sans","Segoe UI",sans-serif;
    --display:-apple-system,BlinkMacSystemFont,"SF Pro Display","Bricolage Grotesque",sans-serif;
    --mono:"SF Mono","IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
    --shadow:0 24px 64px rgba(0,0,0,.42);
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0;overflow:hidden;background:var(--bg);color:var(--text)}
  body{font:14px/1.5 var(--font);letter-spacing:0;text-rendering:geometricPrecision}
  button,textarea{font:inherit;letter-spacing:0}
  button{border:0;cursor:pointer;color:inherit;background:none;padding:0}
  button:disabled{cursor:default;opacity:.6}
  textarea{outline:none}
  strong{font-weight:700}
  svg{display:block;flex:0 0 auto}
  .shell{height:100%;min-width:0;display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
  .titlebar{height:40px;flex:0 0 40px;display:flex;align-items:center;justify-content:center;position:relative;background:var(--bg2);border-bottom:1px solid var(--border);-webkit-app-region:drag}
  .title-center{display:flex;align-items:center;justify-content:center;gap:8px;min-width:0;pointer-events:none}
  .logo-mark{width:15px;height:15px;border-radius:4px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),rgba(224,162,61,.6))}
  .logo-mark::after{content:"";width:6px;height:6px;border-radius:50%;background:var(--accent-text)}
  .app-title{font:700 13px/1 var(--display);color:var(--text);white-space:nowrap}
  .active-contact{font:500 12px/1 var(--mono);color:var(--text-faint);white-space:nowrap}

  .statusbar{height:56px;flex:0 0 56px;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 16px;background:var(--bg2);border-bottom:1px solid var(--border)}
  .status-left,.status-right{display:flex;align-items:center;gap:14px;min-width:0}
  .status-right{gap:10px;justify-content:flex-end}
  .run-pill{height:34px;display:inline-flex;align-items:center;gap:9px;padding:0 6px 0 13px;border-radius:99px;border:1px solid rgba(224,162,61,.33);background:rgba(224,162,61,.08);white-space:nowrap}
  .run-dot-wrap{position:relative;width:8px;height:8px;display:inline-block;flex:0 0 auto}
  .run-dot{position:absolute;inset:0;border-radius:50%;background:var(--accent)}
  .run-dot-pulse{position:absolute;inset:-3px;border-radius:50%;box-shadow:0 0 0 0 var(--accent);animation:atPulse 2.2s infinite}
  .run-pill.paused{border-color:var(--border);background:var(--bg3)}
  .paused .run-dot,.paused .run-dot-pulse{background:var(--text-faint);animation:none;box-shadow:none}
  .run-label{font:700 11.5px/1 var(--mono);color:var(--text)}
  .pause-btn{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;color:var(--text-dim)}
  .pause-btn:hover,.icon-chip:hover,.status-chip:hover,.menu-summary:hover{background:rgba(255,255,255,.04)}
  .v-divider{width:1px;height:22px;background:var(--border);flex:0 0 auto}
  .listen-group{display:flex;align-items:center;gap:8px;white-space:nowrap;min-width:0}
  .mono-label{font:500 11.5px/1 var(--mono);letter-spacing:0;color:var(--text-faint);white-space:nowrap}
  .mini-avatar{width:18px;height:18px;border-radius:6px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),rgba(224,162,61,.53));color:var(--accent-text);font:800 10px/1 var(--font);flex:0 0 auto}
  .listen-name{font:700 11.5px/1 var(--mono);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px}
  .status-chip,.menu-summary,.read-btn{height:30px;display:inline-flex;align-items:center;gap:7px;padding:0 11px;border-radius:10px;border:1px solid var(--border);background:var(--bg3);font:500 11.5px/1 var(--mono);color:var(--text-dim);white-space:nowrap;flex:0 0 auto}
  .status-chip strong,.menu-summary strong{font-weight:700;color:var(--text)}
  .ok-dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:0 0 auto}
  .icon-chip{width:34px;height:30px;border-radius:10px;border:1px solid var(--border);background:var(--bg3);display:grid;place-items:center;color:var(--text-dim);flex:0 0 auto}
  .read-btn{border-color:rgba(224,162,61,.52);background:linear-gradient(180deg,#e8ae42,#d99d31);color:var(--accent-text);font-weight:800;box-shadow:0 10px 22px rgba(224,162,61,.14)}
  .read-btn:hover{background:linear-gradient(180deg,#efbc56,#dfa239)}
  .spin{display:none;width:13px;height:13px;border-radius:50%;border:2px solid rgba(28,19,5,.24);border-top-color:var(--accent-text);animation:spin .72s linear infinite}
  .read-btn.loading .spin{display:block}
  details{position:relative}
  summary{list-style:none}
  summary::-webkit-details-marker{display:none}
  .menu-panel{position:absolute;right:0;top:38px;width:min(520px,calc(100vw - 32px));padding:14px;border:1px solid var(--border);border-radius:14px;background:var(--bg2);box-shadow:var(--shadow);z-index:30}
  .runtime-grid{display:grid;grid-template-columns:1fr;gap:8px}
  .runtime-row{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:9px 10px;border-radius:10px;border:1px solid var(--border);background:var(--bg3);font:500 11.5px/1.2 var(--mono);color:var(--text-dim)}
  .runtime-row strong{color:var(--text);font-weight:700;text-align:right;word-break:break-word}
  .model-list{display:grid;gap:8px;max-height:280px;overflow:auto}
  .model-row{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:9px 10px;border-radius:10px;border:1px solid var(--border);background:var(--bg3);font:500 11.5px/1.2 var(--mono);color:var(--text-dim);cursor:pointer;width:100%;text-align:left}
  .model-row:hover{color:var(--text);border-color:var(--text-faint)}
  .model-row.active{color:var(--text);border-color:var(--accent)}
  .model-row.active::after{content:"✓";color:var(--accent);font-weight:700}
  .analysis-text{white-space:pre-line}
  .context-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;color:var(--text)}
  .context-head strong{font:700 13px/1 var(--display)}
  .context-state{font:500 11px/1 var(--mono);color:var(--text-faint);white-space:nowrap}
  .context-state.need{color:var(--accent);font-weight:700}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .field label{display:block;margin:0 0 6px;font:600 11px/1 var(--mono);color:var(--text-faint)}
  .field textarea{width:100%;min-height:72px;resize:vertical;border:1px solid var(--border);border-radius:10px;background:#141210;color:var(--text);padding:9px 10px;font-size:13px;line-height:1.48}
  .field textarea:focus{border-color:rgba(224,162,61,.58);box-shadow:0 0 0 3px rgba(224,162,61,.1)}
  .goal-presets{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}
  .preset{height:25px;padding:0 9px;border-radius:99px;border:1px solid var(--border);background:var(--tag-bg);color:var(--text-dim);font:600 11px/1 var(--font);white-space:nowrap}
  .settings-actions{display:flex;align-items:center;gap:9px;margin-top:12px;min-width:0}
  .ghost-btn{height:30px;padding:0 12px;border-radius:9px;border:1px solid var(--border);background:var(--bg3);color:var(--text-dim);font:700 12px/1 var(--font);white-space:nowrap}
  .ghost-btn:hover{color:var(--text);border-color:rgba(224,162,61,.42)}
  .save-note{font:500 11px/1 var(--mono);color:var(--text-faint);white-space:nowrap}

  .main{flex:1;min-height:0;display:flex;overflow:hidden;background:var(--bg)}
  .left{width:40%;flex:0 0 40%;min-width:360px;min-height:0;display:flex;flex-direction:column;background:var(--bg2);border-right:1px solid var(--border)}
  .capture-panel-head{height:48px;flex:0 0 48px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px 12px;position:relative}
  .capture-title{display:flex;align-items:center;gap:8px;min-width:0}
  .capture-title span{font:500 11px/1 var(--mono);color:var(--text-faint);white-space:nowrap}
  .capture-tools{display:flex;align-items:center;gap:10px;flex:0 0 auto}
  .rec{display:flex;align-items:center;gap:6px;font:500 11px/1 var(--mono);color:var(--text-dim);white-space:nowrap}
  .rec-dot{width:6px;height:6px;border-radius:50%;background:var(--red);animation:atRec 1.4s infinite}
  .rec.idle .rec-dot{background:var(--text-faint);animation:none}
  .parsed-menu summary{height:24px;padding:0 9px;border-radius:8px;border:1px solid var(--border);background:var(--bg3);font:600 11px/1 var(--mono);color:var(--text-dim);display:flex;align-items:center;gap:6px;cursor:pointer}
  .parsed-panel{position:absolute;right:0;top:32px;width:min(420px,calc(100vw - 44px));max-height:300px;overflow:auto;padding:10px;border-radius:12px;border:1px solid var(--border);background:var(--bg2);box-shadow:var(--shadow);z-index:20}
  .messages{display:flex;flex-direction:column;gap:8px}
  .message-row{display:flex;align-items:flex-end;gap:8px}
  .message-row.me{justify-content:flex-end}
  .message-row.system{justify-content:center}
  .sender{width:42px;flex:0 0 42px;text-align:right;font:600 11px/1 var(--mono);color:var(--text-faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bubble{max-width:78%;padding:8px 10px;border-radius:9px;background:var(--bubble-them);color:var(--capture-text);font-size:12.5px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
  .me .bubble{background:var(--bubble-mine);color:var(--bubble-mine-text)}
  .system .bubble{background:var(--accent-card);color:var(--accent);font-size:12px}
  .capture-stage{flex:1;min-height:0;padding:0 18px 18px;display:flex}
  .capture-card{flex:1;min-height:0;position:relative;border-radius:14px;border:1px solid var(--border);background:var(--capture-bg);overflow:hidden;display:flex;flex-direction:column}
  .corner{position:absolute;width:14px;height:14px;z-index:4;pointer-events:none}
  .tl{top:8px;left:8px;border-top:2px solid var(--accent);border-left:2px solid var(--accent)}
  .tr{top:8px;right:8px;border-top:2px solid var(--accent);border-right:2px solid var(--accent)}
  .bl{bottom:8px;left:8px;border-bottom:2px solid var(--accent);border-left:2px solid var(--accent)}
  .br{bottom:8px;right:8px;border-bottom:2px solid var(--accent);border-right:2px solid var(--accent)}
  .live-shot{display:none;width:100%;height:100%;object-fit:contain;background:var(--capture-bg)}
  .mock-capture{flex:1;min-height:0;display:flex;flex-direction:column;background:var(--capture-bg)}
  .capture-head{height:72px;flex:0 0 72px;display:flex;align-items:center;justify-content:space-between;padding:13px 16px;border-bottom:1px solid var(--capture-border);background:var(--capture-head)}
  .capture-name{font:700 14.5px/1.1 var(--font);color:var(--capture-text)}
  .capture-meta{margin-top:4px;font:400 11.5px/1 var(--font);color:var(--capture-dim);white-space:nowrap}
  .capture-actions{display:flex;align-items:center;gap:16px;color:var(--capture-dim)}
  .capture-stream{flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;padding:16px 16px 8px;overflow:hidden;background:var(--capture-bg)}
  .time-split{text-align:center;font:400 11px/1 var(--font);color:var(--capture-dim);margin:2px 0 4px}
  .capture-empty{margin:auto;max-width:260px;text-align:center;color:var(--capture-dim);font:500 13px/1.6 var(--font)}
  .capture-empty strong{display:block;margin-bottom:4px;color:var(--capture-text);font:700 14px/1.3 var(--font)}
  .chat-line{display:flex;gap:9px;align-items:flex-start;justify-content:flex-start}
  .chat-line.me{justify-content:flex-end}
  .chat-avatar{width:30px;height:30px;border-radius:5px;display:grid;place-items:center;flex:0 0 30px;background:var(--capture-border);color:var(--capture-text);font:700 12px/1 var(--font)}
  .chat-line.me .chat-avatar{background:var(--accent);color:var(--accent-text)}
  .chat-bubble{max-width:74%;padding:9px 12px;border-radius:9px 9px 9px 3px;background:var(--bubble-them);color:var(--capture-text);font:400 13.5px/1.5 var(--font);word-break:break-word}
  .chat-line.me .chat-bubble{border-radius:9px 9px 3px 9px;background:var(--bubble-mine);color:var(--bubble-mine-text)}
  .capture-input{flex:0 0 112px;border-top:1px solid var(--capture-border);padding:10px 14px;background:var(--capture-head)}
  .input-icons{display:flex;gap:13px;color:var(--capture-dim);margin:0 0 9px 2px}
  .input-space{height:30px}
  .send-line{display:flex;justify-content:flex-end}
  .send-disabled{padding:5px 14px;border-radius:4px;background:var(--capture-border);color:var(--capture-dim);font:400 11.5px/1 var(--font);white-space:nowrap}

  .right{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--bg);overflow:auto}
  .replies-head{height:61px;flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 22px 0}
  .reply-title{display:flex;align-items:baseline;gap:10px;min-width:0}
  .reply-title h1{margin:0;font:700 21px/1.1 var(--display);color:var(--text)}
  .count,.gen-meta{font:500 12px/1 var(--mono);color:var(--text-faint);white-space:nowrap}
  .gen-meta{font-size:11px}
  .error{display:none;margin:0 22px 10px;padding:10px 12px;border-radius:10px;border:1px solid rgba(255,95,87,.35);background:rgba(255,95,87,.08);color:#f2aaa4;font-size:12px}
  .ai-card{margin:14px 22px 4px;padding:13px 15px;border-radius:var(--radius);background:var(--bg2);border:1px solid var(--border)}
  .ai-label{display:flex;align-items:center;gap:7px;margin-bottom:8px;color:var(--accent);font:700 11.5px/1 var(--mono);text-transform:uppercase;white-space:nowrap}
  .reassess-btn{margin-left:auto;background:var(--bg3);border:1px solid var(--border);color:var(--text-dim);font:500 10.5px/1 var(--mono);text-transform:none;padding:4px 9px;border-radius:7px;cursor:pointer}
  .reassess-btn:hover{color:var(--text);border-color:var(--accent)}
  .reassess-btn:disabled{opacity:.5;cursor:default}
  .analysis-text{margin:0;color:var(--text-dim);font:400 13px/1.62 var(--font);text-wrap:pretty}
  .suggestion-list{flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;padding:12px 22px 20px}
  .suggestion{position:relative;display:flex;flex-direction:column;gap:11px;padding:17px 18px;border-radius:var(--radius);background:var(--bg3);border:1px solid var(--border)}
  .suggestion.recommended{background:var(--accent-card);border-color:rgba(224,162,61,.4)}
  .suggestion-top{display:flex;align-items:center;justify-content:space-between;gap:14px}
  .tag-row{display:flex;gap:7px;flex-wrap:wrap;min-width:0}
  .tag{padding:3px 9px;border-radius:99px;background:var(--tag-bg);border:1px solid var(--border);color:var(--text-dim);font:600 11px/1 var(--font);white-space:nowrap}
  .recommend{display:flex;align-items:center;gap:5px;color:var(--accent);font:700 10.5px/1 var(--mono);white-space:nowrap;text-transform:uppercase}
  .bubbles{display:flex;flex-direction:column;gap:7px;margin:9px 0 3px}
  .bubble-row{display:flex;align-items:flex-start;gap:9px;padding:8px 11px;border-radius:13px;background:var(--bg3);border:1px solid var(--border);transition:opacity .18s,border-color .18s}
  .bubble-row.current{border-color:rgba(224,162,61,.55)}
  .bubble-row.copied{opacity:.42}
  .bubble-text{flex:1;min-height:22px;resize:none;overflow:hidden;border:0;background:transparent;color:var(--text);padding:0;margin:0;font:600 15px/1.5 var(--font);text-wrap:pretty}
  .bubble-copy{flex:0 0 auto;height:28px;display:inline-flex;align-items:center;gap:5px;padding:0 11px;border-radius:9px;border:1px solid var(--border);background:transparent;color:var(--text-dim);font:600 12px/1 var(--font);white-space:nowrap;cursor:pointer}
  .bubble-copy:hover{color:var(--text);border-color:rgba(224,162,61,.36)}
  .bubble-copy.copied{color:var(--accent);border-color:rgba(224,162,61,.5)}
  .suggestion-bottom{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:1px}
  .char-count{font:500 11px/1 var(--mono);color:var(--text-faint);white-space:nowrap}
  .suggestion-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}
  .card-btn{height:30px;display:inline-flex;align-items:center;gap:6px;padding:0 12px;border-radius:9px;border:1px solid var(--border);background:transparent;color:var(--text-dim);font:600 12.5px/1 var(--font);white-space:nowrap}
  .card-btn:hover{color:var(--text);border-color:rgba(224,162,61,.36);background:rgba(255,255,255,.03)}
  .card-btn.primary{padding:0 13px;border-color:transparent;background:var(--send-bg);color:var(--text);font-weight:700}
  .recommended .card-btn.primary{background:var(--accent);color:var(--accent-text)}
  .card-btn.copied{background:#265d3c;color:#fff;border-color:#347c52}
  .regen-btn{width:30px;padding:0;justify-content:center;color:var(--text-faint)}
  .empty-state{min-height:124px;display:grid;place-items:center;text-align:center;border:1px dashed var(--border);border-radius:var(--radius);background:var(--bg2);color:var(--text-faint);font-size:13px;padding:18px}
  .hidden-state{display:none}

  @keyframes atPulse{70%{box-shadow:0 0 0 8px rgba(224,162,61,0)}100%{box-shadow:0 0 0 0 rgba(224,162,61,0)}}
  @keyframes atRec{0%,100%{opacity:1}50%{opacity:.35}}
  @keyframes spin{to{transform:rotate(360deg)}}
  @media (max-width:1040px){
    html,body{overflow:auto}
    .shell{min-height:100%;height:auto;overflow:visible}
    .statusbar{height:auto;min-height:56px;align-items:flex-start;flex-direction:column;padding:12px 16px}
    .status-right{width:100%;overflow-x:auto;padding-bottom:2px;justify-content:flex-start}
    .main{display:block;overflow:visible}
    .left{width:100%;min-width:0;height:62vh;border-right:0;border-bottom:1px solid var(--border)}
    .right{overflow:visible}
    .menu-panel{right:auto;left:0}
  }
  @media (max-width:680px){
    .status-left{width:100%;overflow-x:auto;padding-bottom:2px}
    .settings-grid{grid-template-columns:1fr}
    .replies-head{align-items:flex-start;height:auto;min-height:61px;flex-direction:column;gap:8px;padding-bottom:4px}
    .suggestion-bottom{align-items:flex-start;flex-direction:column}
    .suggestion-actions{width:100%;justify-content:flex-end;flex-wrap:wrap}
  }
</style>
</head>
<body>
<main class="shell">
  <header class="titlebar">
    <div class="title-center">
      <span class="logo-mark" aria-hidden="true"></span>
      <span class="app-title">DraftMate</span>
      <span class="active-contact" id="windowContact">— 未读取</span>
    </div>
  </header>

  <section class="statusbar">
    <div class="status-left">
      <div class="run-pill paused" id="runPill" title="盯着当前打开的对话:每隔几秒截屏比对,这个会话里对方发新消息就自动出草稿。想盯谁,先点开谁的聊天(没点开的会话读不到)。全程只读屏,不输入、不发送。">
        <span class="run-dot-wrap"><span class="run-dot"></span><span class="run-dot-pulse"></span></span>
        <span class="run-label" id="runLabel">未监控</span>
        <button class="pause-btn" id="pauseBtn" type="button" title="开始监控" onclick="toggleRunning()">
          <svg id="monitorIcon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10-6.5z"/></svg>
        </button>
      </div>
      <div class="v-divider"></div>
      <div class="listen-group">
        <span class="mono-label">当前</span>
        <span class="mini-avatar" id="contactAvatar">?</span>
        <span class="listen-name" id="contactName">等待读取</span>
      </div>
    </div>

    <div class="status-right">
      <span class="status-chip" id="captureChip">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 8.5A1.5 1.5 0 0 1 4.5 7h2L8 5h8l1.5 2h2A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/><circle cx="12" cy="13" r="3.2"/></svg>
        <span id="captureModeText">手动读取</span>
      </span>
      <span class="status-chip" id="usageChip" title="仅本地统计,不上传;周报截图发群即可">已读取 0 次</span>
      <span class="status-chip"><span class="ok-dot"></span><span>已连接</span></span>
      <details class="model-menu" ontoggle="if(this.open)loadPersonas()">
        <summary class="menu-summary"><span>模式</span><strong id="modeName">追</strong><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></summary>
        <div class="menu-panel">
          <div class="context-head"><strong>目标模式</strong><span class="context-state">默认「追」,也可只「维持关系」;策略由军师按分数自动拿捏。切换即按当前对话重出</span></div>
          <div class="model-list" id="modeList"><div class="empty-state">读取中</div></div>
        </div>
      </details>
      <details class="model-menu" ontoggle="if(this.open)loadModels()">
        <summary class="menu-summary"><span>模型</span><strong id="modelName">读取中</strong><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></summary>
        <div class="menu-panel">
          <div class="context-head"><strong>回复模型</strong><span class="context-state" id="modelHint">点选即切换,写回配置</span></div>
          <div class="model-list" id="modelList"><div class="empty-state">读取中</div></div>
        </div>
      </details>
      <details class="settings">
        <summary class="icon-chip" title="上下文和设置"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7 5.6 5.6"/></svg></summary>
        <div class="menu-panel">
          <div class="context-head"><strong>给 agent 的上下文</strong><span class="context-state" id="contextState">先读取对话</span></div>
          <div class="settings-grid">
            <div class="field">
              <label for="personInfo">对方信息</label>
              <textarea id="personInfo" placeholder="例如: 刚加的租房中介 / USC 同学 / 朋友介绍的人"></textarea>
            </div>
            <div class="field">
              <label for="replyIntent">目标</label>
              <textarea id="replyIntent" placeholder="例如: 从认识推进到暧昧 / 约对方出来 / 维持朋友别越界"></textarea>
              <div class="goal-presets">
                <button type="button" class="preset" onclick="setGoal('从认识慢慢推进到暧昧')">认识到暧昧</button>
                <button type="button" class="preset" onclick="setGoal('找个自然的由头约对方出来')">约出来</button>
                <button type="button" class="preset" onclick="setGoal('推进到确定关系')">确定关系</button>
                <button type="button" class="preset" onclick="setGoal('维持朋友关系,别越界')">维持朋友</button>
              </div>
            </div>
            <div class="field">
              <label for="replyAvoid">不要提 / 边界</label>
              <textarea id="replyAvoid" placeholder="例如: 不要透露太多个人信息 / 不要暧昧"></textarea>
            </div>
            <div class="field">
              <label for="contextNotes">备注</label>
              <textarea id="contextNotes" placeholder="例如: 语气自然一点,先确认对方能不能帮忙"></textarea>
            </div>
          </div>
          <div class="settings-actions">
            <button class="ghost-btn" id="saveContextBtn" type="button" onclick="saveContext()">保存上下文</button>
            <button class="ghost-btn" id="saveReadBtn" type="button" onclick="saveContext(true)">保存并重生</button>
            <span class="save-note" id="saveNote"></span>
          </div>
          <div class="context-head" style="margin-top:14px"><strong>导入历史记忆</strong><span class="context-state" id="importState">自动滚读当前对话,蒸馏成长期记忆</span></div>
          <div class="settings-actions">
            <select id="importDays" class="ghost-btn" style="padding:0 8px">
              <option value="3">最近 3 天</option>
              <option value="7" selected>最近 7 天</option>
              <option value="14">最近 14 天</option>
              <option value="30">最近 30 天</option>
              <option value="all">全部(滚到顶)</option>
            </select>
            <button class="ghost-btn" id="importBtn" type="button" onclick="startImport()">开始导入(自动滚动)</button>
            <span class="save-note" id="importNote"></span>
          </div>
          <div class="context-head" style="margin-top:14px"><strong>运行信息</strong></div>
          <div class="runtime-grid" id="runtimePills"></div>
        </div>
      </details>
      <button class="read-btn" id="readBtn" type="button" onclick="readNow()"><span class="spin"></span><span id="readLabel">读取</span></button>
    </div>
  </section>

  <section class="main">
    <aside class="left">
      <div class="capture-panel-head">
        <div class="capture-title"><span>实时截图</span><span id="shotMeta">· 未读取</span></div>
        <div class="capture-tools">
          <details class="parsed-menu">
            <summary>OCR <span id="chatMeta">0 条</span></summary>
            <div class="parsed-panel"><div class="messages" id="messages"><div class="empty-state">等待读取真实对话</div></div></div>
          </details>
          <span class="rec idle" id="imageState"><span class="rec-dot"></span>待命</span>
        </div>
      </div>
      <div class="capture-stage">
        <div class="capture-card">
          <span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>
          <img class="live-shot" id="shot" alt="当前聊天截图">
          <div class="mock-capture" id="mockCapture">
            <div class="capture-head">
              <div><div class="capture-name" id="mockContactName">未读取</div><div class="capture-meta" id="mockContactMeta">等待当前聊天</div></div>
              <div class="capture-actions" aria-hidden="true">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 5 5"/></svg>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
              </div>
            </div>
            <div class="capture-stream">
              <div class="capture-empty"><strong>等待读取当前对话</strong>点击右上角读取后，这里会显示实际截图预览。</div>
            </div>
            <div class="capture-input">
              <div class="input-icons" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M8 10h.01M16 10h.01M8 15c1.2 1.1 2.5 1.6 4 1.6s2.8-.5 4-1.6"/></svg>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h6l2 2h8v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M4 7V5a2 2 0 0 1 2-2h4l2 2h4"/></svg>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="6" cy="6" r="2.8"/><circle cx="6" cy="18" r="2.8"/><path d="M8.4 7.6 20 19M8.4 16.4 20 5"/></svg>
              </div>
              <div class="input-space"></div>
              <div class="send-line"><span class="send-disabled">发送(S)</span></div>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <section class="right">
      <div class="replies-head">
        <div class="reply-title"><h1>建议回复</h1><span class="count" id="suggestionMeta">0 条候选</span></div>
        <div class="gen-meta" id="statusText">就绪</div>
      </div>
      <div class="error" id="errorBox"></div>
      <div class="ai-card">
        <div class="ai-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8z"/></svg>AI 分析<button type="button" class="reassess-btn" id="reassessBtn" onclick="reassess()" title="关系阶段默认复用上次判定以省钱;关系刚有变化时点这个强制重判">重新判定</button></div>
        <p class="analysis-text" id="analysisText">读取当前聊天后，这里会说明对方意图和回复策略。</p>
      </div>
      <div class="suggestion-list" id="suggestions">
        <div class="empty-state">等待生成</div>
      </div>
      <span id="appSub" class="hidden-state"></span><span id="chatTitle" class="hidden-state"></span><span id="groupBadge" class="hidden-state"></span><span id="previewHint" class="hidden-state"></span><span id="statusDot" class="hidden-state"></span>
    </section>
  </section>
</main>
<script>
const els={
  readBtn:document.getElementById('readBtn'),
  readLabel:document.getElementById('readLabel'),
  runtimePills:document.getElementById('runtimePills'),
  appSub:document.getElementById('appSub'),
  shot:document.getElementById('shot'),
  mockCapture:document.getElementById('mockCapture'),
  previewHint:document.getElementById('previewHint'),
  shotMeta:document.getElementById('shotMeta'),
  imageState:document.getElementById('imageState'),
  chatTitle:document.getElementById('chatTitle'),
  chatMeta:document.getElementById('chatMeta'),
  groupBadge:document.getElementById('groupBadge'),
  statusDot:document.getElementById('statusDot'),
  statusText:document.getElementById('statusText'),
  contextState:document.getElementById('contextState'),
  personInfo:document.getElementById('personInfo'),
  replyIntent:document.getElementById('replyIntent'),
  replyAvoid:document.getElementById('replyAvoid'),
  contextNotes:document.getElementById('contextNotes'),
  saveContextBtn:document.getElementById('saveContextBtn'),
  saveReadBtn:document.getElementById('saveReadBtn'),
  saveNote:document.getElementById('saveNote'),
  importBtn:document.getElementById('importBtn'),
  importDays:document.getElementById('importDays'),
  importNote:document.getElementById('importNote'),
  importState:document.getElementById('importState'),
  messages:document.getElementById('messages'),
  suggestions:document.getElementById('suggestions'),
  suggestionMeta:document.getElementById('suggestionMeta'),
  errorBox:document.getElementById('errorBox'),
  windowContact:document.getElementById('windowContact'),
  contactName:document.getElementById('contactName'),
  contactAvatar:document.getElementById('contactAvatar'),
  modelName:document.getElementById('modelName'),
  modelList:document.getElementById('modelList'),
  modelHint:document.getElementById('modelHint'),
  modeName:document.getElementById('modeName'),
  modeList:document.getElementById('modeList'),
  captureModeText:document.getElementById('captureModeText'),
  usageChip:document.getElementById('usageChip'),
  analysisText:document.getElementById('analysisText'),
  runPill:document.getElementById('runPill'),
  runLabel:document.getElementById('runLabel'),
  pauseBtn:document.getElementById('pauseBtn'),
  monitorIcon:document.getElementById('monitorIcon')
};
let currentProfileTitle='';
let lastMessages=[];
let lastTitle='';
let lastAnalysis='';
let running=false;
let lastStatus=null;
let monitorTimer=null;
let lastSeen='';
let readingNow=false;
function esc(value){
  return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function icon(name){
  if(name==='copy')return '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
  if(name==='send')return '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 12 20 4l-6 16-3.5-6.5L4 12Z"/></svg>';
  return '';
}
function setBusy(isBusy){
  readingNow=isBusy;
  els.readBtn.disabled=isBusy;
  els.readBtn.classList.toggle('loading',isBusy);
  els.readLabel.textContent=isBusy?'读取中':'读取';
  if(isBusy)els.statusText.textContent='读取和生成中';
  renderCaptureState();
}
function showError(text){
  els.errorBox.style.display=text?'block':'none';
  els.errorBox.textContent=text || '';
}
function renderCaptureState(){
  // 优先级:读取中 > 监控中 > 待命
  if(readingNow){els.imageState.className='rec';els.imageState.innerHTML='<span class="rec-dot"></span>读取中';}
  else if(running){els.imageState.className='rec';els.imageState.innerHTML='<span class="rec-dot"></span>监控中';}
  else{els.imageState.className='rec idle';els.imageState.innerHTML='<span class="rec-dot"></span>待命';}
}
function renderMonitorState(){
  els.runPill.classList.toggle('paused',!running);
  els.runLabel.textContent=running?'监控中':'未监控';
  els.pauseBtn.title=running?'停止监控':'开始监控';
  els.monitorIcon.outerHTML=running
    ? '<svg id="monitorIcon" width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor"/><rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor"/></svg>'
    : '<svg id="monitorIcon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10-6.5z"/></svg>';
  els.monitorIcon=document.getElementById('monitorIcon');
  renderCaptureState();
}
function pollIntervalMs(){
  return Math.max(3,(lastStatus&&lastStatus.poll_interval_seconds)||5)*1000;
}
function scheduleMonitor(delayMs){
  if(!running)return;
  clearTimeout(monitorTimer);
  monitorTimer=setTimeout(monitorTick,delayMs);
}
function syncSeenFromRead(){
  const lm=lastMessages[lastMessages.length-1]||{};
  lastSeen=`${lastTitle}|${lm.sender||''}|${lm.text||''}`;
}
async function monitorTick(){
  if(!running)return;
  if(readingNow){scheduleMonitor(pollIntervalMs());return;}  // 正在读取,跳过本轮
  try{
    const res=await fetch('/api/peek',{cache:'no-store'});
    const data=await res.json();
    const now=new Date().toLocaleTimeString();
    if(data.error){
      els.statusText.textContent=`监控探测失败 · ${now} · ${data.error}`;
    }else{
      const sig=`${data.title}|${data.last_sender}|${data.last_text}`;
      const fromOther=data.last_sender&&data.last_sender!=='我'&&data.last_sender!=='系统'&&data.last_sender!=='unknown';
      if(!lastSeen){
        lastSeen=sig;
        if(fromOther){
          // 开启监控时屏幕上就有对方的未回消息 → 立刻先出一版草稿
          els.statusText.textContent=`监控中 · 「${data.title}」有未回消息,先出一版草稿`;
          await readNow(true);
          syncSeenFromRead();
        }else{
          els.statusText.textContent=`监控中 · 已盯上当前对话「${data.title}」,对方在这个会话发新消息会自动出草稿`;
        }
      }else if(sig!==lastSeen&&fromOther){
        await readNow(true);
        syncSeenFromRead();
      }else{
        lastSeen=sig;
        els.statusText.textContent=`监控中 · ${now} 已探测,无新消息`;
      }
    }
  }catch(err){
    els.statusText.textContent='监控探测失败: '+String(err);
  }
  scheduleMonitor(pollIntervalMs());
}
function toggleRunning(){
  running=!running;
  if(running){
    lastSeen='';            // 开启时先记基线,不为旧消息触发生成
    scheduleMonitor(200);
  }else{
    clearTimeout(monitorTimer);
    monitorTimer=null;
  }
  renderMonitorState();
  els.statusText.textContent=running?`监控已开启 · 每 ${pollIntervalMs()/1000}s 探测一次(只读屏,不发送)`:'监控已停止';
}
function renderRuntime(status){
  if(!status)return;
  lastStatus=status;
  const mode=status.read_mode==='ocr' ? `ocr/${status.ocr_backend || 'auto'}` : (status.read_mode || 'vlm');
  els.modelName.textContent=status.reply_model || '未配置';
  els.captureModeText.textContent=mode || '截图模式';
  if(status.usage){
    const u=status.usage;
    const recent=u.last_used?` · 最近 ${esc(String(u.last_used).slice(5))}`:'';
    const auto=Number(u.auto_reads)||0;
    els.usageChip.innerHTML=`已读取 <strong>${Number(u.reads)||0}</strong> 次${auto?` (自动 ${auto})`:''}${recent}`;
  }
  const rows=[
    ['目标 App',status.app_name || '未配置'],
    ['Provider',status.provider || ''],
    ['读取模式',mode],
    ['视觉模型',status.vision_model || ''],
    ['回复模型',status.reply_model || ''],
    ['上下文',`${status.read_last_n || 0} 条`],
    ['监控轮询',`${status.poll_interval_seconds || 5}s`],
    ['输出',status.copy_only ? '复制/手动粘贴' : '发送']
  ];
  els.runtimePills.innerHTML=rows.map(([k,v])=>`<div class="runtime-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
}
async function loadModels(){
  try{
    const res=await fetch('/api/models',{cache:'no-store'});
    renderModelList(await res.json());
  }catch(_){
    els.modelList.innerHTML='<div class="empty-state">读取模型列表失败</div>';
  }
}
function renderModelList(data){
  const models=(data&&data.models)||[];
  if(!models.length){
    els.modelList.innerHTML='<div class="empty-state">没找到本地模型。确认 Ollama 已启动,或 ollama pull 一个模型。</div>';
    return;
  }
  els.modelList.innerHTML=models.map(m=>{
    const name=(typeof m==='string')?m:m.name;
    const cloud=(typeof m==='object')&&m.cloud;
    const tag=cloud?' <span style="color:var(--accent);font-weight:700">☁ 云端</span>':'';
    const provider=(typeof m==='object'&&m.provider)?m.provider:'云端服务';
    return `<button type="button" class="model-row ${name===data.reply_model?'active':''}" data-model="${esc(name)}" data-cloud="${cloud?1:0}" data-provider="${esc(provider)}" onclick="pickModel(this)"><span>${esc(name)}${tag}</span></button>`;
  }).join('');
  if(data&&!data.cloud_available){
    els.modelList.innerHTML+='<div class="empty-state" style="text-align:left;line-height:1.5">想要更强?设 <code>DEEPSEEK_API_KEY</code> 或 <code>ANTHROPIC_API_KEY</code> 后重启,这里会出现云端回复模型(只发对话文字,截图不上传)。</div>';
  }
}
async function pickModel(btn){
  const name=btn.getAttribute('data-model');
  const cloud=btn.getAttribute('data-cloud')==='1';
  if(!name||((lastStatus&&lastStatus.reply_model)===name))return;
  const provider=btn.getAttribute('data-provider')||'云端服务';
  if(cloud&&!window.confirm(`切到云端模型「${name}」:回复生成会把**对话文字**发往 ${provider}(读图/截图仍全本地、不上传)。继续?`))return;
  els.modelHint.textContent='切换中';
  try{
    const res=await fetch('/api/model',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({reply_model:name})
    });
    const data=await res.json();
    if(data.error){showError('切换模型失败: '+data.error);els.modelHint.textContent='切换失败';return;}
    renderRuntime(data.status);
    els.modelList.querySelectorAll('.model-row').forEach(b=>b.classList.toggle('active',b.getAttribute('data-model')===name));
    els.modelHint.textContent='已切换并写回配置';
    showError('');
  }catch(err){
    showError('切换模型失败: '+String(err));
    els.modelHint.textContent='切换失败';
  }
}
const MODE_LABEL={pursue:'追',maintain:'维持关系'};
let selectedPersona='pursue';
try{const s=localStorage.getItem('draftmate_mode');if(s&&MODE_LABEL[s])selectedPersona=s;}catch(_){}
function saveMode(){try{localStorage.setItem('draftmate_mode',selectedPersona);}catch(_){}}
async function loadPersonas(){
  try{
    const data=await (await fetch('/api/personas',{cache:'no-store'})).json();
    const list=(data&&data.personas)||[];
    els.modeList.innerHTML=list.map(p=>{
      const desc=tagText(p.name,0).join(' · ');
      return `<button type="button" class="model-row ${p.name===selectedPersona?'active':''}" data-mode="${esc(p.name)}" onclick="pickMode(this)"><span><strong>${esc(p.label)}</strong> <span style="color:var(--text-faint)">${esc(desc)}</span></span></button>`;
    }).join('')||'<div class="empty-state">没有可用人设</div>';
  }catch(_){els.modeList.innerHTML='<div class="empty-state">读取模式失败</div>';}
}
function applyResult(suggestions,followup,note){
  renderSuggestions(suggestions,note);
  if(followup&&followup.active&&followup.hold)els.suggestionMeta.textContent='追加 · 建议先不发';
}
async function generateFor(persona){
  if(!lastMessages.length){showError('先读取一次对话，再切换模式。');return;}
  setBusy(true);showError('');
  try{
    const res=await fetch('/api/regenerate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:lastTitle,persona:persona,messages:lastMessages,analysis:lastAnalysis})});
    const data=await res.json();
    if(data.error){showError(data.error);return;}
    applyResult(data.suggestions||[],data.followup,data.note);
    els.statusText.textContent=`${new Date().toLocaleTimeString()} · ${MODE_LABEL[persona]||persona}模式已生成`;
  }catch(err){showError(String(err));}
  finally{setBusy(false);}
}
async function pickMode(btn){
  const name=btn.getAttribute('data-mode');
  if(!name)return;
  selectedPersona=name;saveMode();
  els.modeName.textContent=MODE_LABEL[name]||name;
  els.modeList.querySelectorAll('.model-row').forEach(b=>b.classList.toggle('active',b.getAttribute('data-mode')===name));
  const menu=btn.closest('details');if(menu)menu.open=false;
  if(lastMessages.length)await generateFor(name);   // 已有对话 → 立刻按新模式重出
}
function manualValues(){
  return {
    person_info:els.personInfo.value.trim(),
    goal:els.replyIntent.value.trim(),
    avoid:els.replyAvoid.value.trim(),
    notes:els.contextNotes.value.trim()
  };
}
function renderProfile(profile){
  if(!profile){
    currentProfileTitle='';
    els.contextState.textContent='先读取对话';
    els.contextState.className='context-state';
    return;
  }
  currentProfileTitle=profile.title || '';
  const manual=profile.manual || {};
  els.personInfo.value=manual.person_info || '';
  els.replyIntent.value=manual.goal || '';
  els.replyAvoid.value=manual.avoid || '';
  els.contextNotes.value=manual.notes || '';
  els.contextState.textContent=profile.needs_input ? '信息不足,建议手动补充' : `已加载 ${currentProfileTitle}`;
  els.contextState.className='context-state '+(profile.needs_input ? 'need' : '');
  els.saveNote.textContent='';
}
function renderMessages(messages){
  if(!messages.length){
    els.messages.innerHTML='<div class="empty-state">没有读到消息</div>';
    return;
  }
  els.messages.innerHTML=messages.map(m=>{
    const sender=m.sender || 'unknown';
    const cls=sender==='我'?'me':(sender==='系统'?'system':'other');
    const label=(cls==='other') ? `<div class="sender">${esc(sender)}</div>` : '';
    return `<div class="message-row ${cls}">${label}<div class="bubble">${esc(m.text)}</div></div>`;
  }).join('');
}
function tagText(persona,index){
  const map={pursue:['追','循序渐进'],maintain:['维持关系','稳住温度']};
  return map[persona] || ['建议',''];
}
function autoGrow(el){
  if(!el)return;
  el.style.height='auto';
  el.style.height=`${Math.max(22,el.scrollHeight)}px`;
}
function resizeAllSuggestions(){
  document.querySelectorAll('.bubble-text').forEach(autoGrow);
}
function bubbleRows(si,bubbles){
  return bubbles.map((b,bi)=>`
    <div class="bubble-row ${bi===0?'current':''}" id="brow-${si}-${bi}">
      <textarea class="bubble-text" id="bubble-${si}-${bi}" rows="1" spellcheck="false" oninput="autoGrow(this)">${esc(b)}</textarea>
      <button class="bubble-copy" type="button" onclick="copyBubble(${si},${bi},this)">${icon('copy')}复制</button>
    </div>`).join('');
}
function splitBubbles(text){
  return String(text||'').split('\n').map(s=>s.trim()).filter(Boolean);
}
function renderBubbles(si,text){
  const box=document.getElementById(`bubbles-${si}`);
  if(!box)return 0;
  const bubbles=splitBubbles(text);
  box.innerHTML=bubbleRows(si,bubbles);
  box.querySelectorAll('.bubble-text').forEach(autoGrow);
  const hint=document.getElementById(`bhint-${si}`);
  if(hint)hint.textContent=`${bubbles.length} 条 · 复制一条→发出去→点下一条`;
  return bubbles.length;
}
function renderSuggestions(items,note){
  els.suggestionMeta.textContent=`${items.length} 条候选`;
  if(!items.length){
    els.suggestions.innerHTML=`<div class="empty-state">${esc(note || '暂无建议')}</div>`;
    return;
  }
  els.suggestions.innerHTML=items.map((item,index)=>{
    const fu=!!item.followup;
    const persona=item.persona || 'persona';
    const tags=fu?'<span class="tag">追加 · 可发可不发</span>':tagText(persona,index).map(t=>`<span class="tag">${esc(t)}</span>`).join('');
    const bubbles=splitBubbles(item.text);
    const star='';   // 单框模式:不再有「推荐」排名
    const hint=fu?`${bubbles.length} 条 · 追加(可发可不发),觉得没必要就别发`:`${bubbles.length} 条 · 复制一条→发出去→点下一条`;
    const actions=fu?'':`<button class="card-btn regen-btn" type="button" title="换个说法" data-persona="${esc(persona)}" onclick="regenOne(${index},this)">↻ 换个说法</button>`;
    return `<article class="suggestion ${index===0&&!fu?'recommended':''}">
      <div class="suggestion-top"><div class="tag-row">${tags}</div>${star}</div>
      <div class="bubbles" id="bubbles-${index}">${bubbleRows(index,bubbles)}</div>
      <div class="suggestion-bottom">
        <span class="char-count" id="bhint-${index}">${hint}</span>
        <div class="suggestion-actions">${actions}</div>
      </div>
    </article>`;
  }).join('');
  resizeAllSuggestions();
}
function analysisFrom(messages,profile,suggestions){
  if(!messages.length)return '没有读到消息。先确认截图区域是否正确，必要时调整 crop_left/crop_bottom。';
  const last=messages[messages.length-1]||{};
  const manual=(profile&&profile.manual)||{};
  if(profile&&profile.needs_input)return '这是新联系人或信息不足。建议先在右上角设置里补充对方信息和阶段性目标，再生成更像你的回复。';
  if(manual.goal)return `当前目标是“${manual.goal}”。建议先接住对方最后一句，再轻微推进目标，避免突然转向。`;
  if(last.sender&&last.sender!=='我'&&last.sender!=='系统')return '对方刚发来新消息。建议先直接回应最后一句，不要复述对方已经说过的内容。';
  return '最后一条不是对方消息，当前建议会偏保守。';
}
function renderPayload(data){
  renderRuntime(data.status);
  const messages=Array.isArray(data.messages)?data.messages:[];
  const suggestions=Array.isArray(data.suggestions)?data.suggestions:[];
  lastMessages=messages;
  lastTitle=data.title || '';
  const title=data.title || '当前对话';
  const initial=(title||'?').trim().slice(0,1).toUpperCase();
  els.windowContact.textContent=`— ${title}`;
  els.contactName.textContent=title;
  els.contactAvatar.textContent=initial;
  els.chatTitle.textContent=title;
  els.chatMeta.textContent=`${messages.length} 条`;
  els.groupBadge.textContent=data.is_group?'群聊':'1v1';
  document.getElementById('mockContactName').textContent=title;
  renderProfile(data.profile);
  renderMessages(messages);
  applyResult(suggestions,data.followup,data.note);
  const fu=data.followup&&data.followup.active;
  lastAnalysis=data.analysis||'';
  els.analysisText.textContent=lastAnalysis||analysisFrom(messages,data.profile,suggestions);
  els.statusText.textContent=`${new Date().toLocaleTimeString()} · 生成完成`+(fu?' · 追加模式（你刚发完，对方还没回）':'')+(data.warning?(' · '+data.warning):'');
  if(data.image){
    els.shot.src='data:image/png;base64,'+data.image;
    els.shot.style.display='block';
    els.mockCapture.style.display='none';
    els.shotMeta.textContent=`· ${new Date().toLocaleTimeString()}`;
    renderMonitorState();
  }
}
async function saveContext(thenRead=false){
  if(!currentProfileTitle){
    showError('请先读取一次对话，让 DraftMate 知道要保存到哪个联系人。');
    return;
  }
  els.saveContextBtn.disabled=true;
  els.saveReadBtn.disabled=true;
  els.saveNote.textContent='保存中';
  try{
    const res=await fetch('/api/context',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:currentProfileTitle,manual:manualValues()})
    });
    const data=await res.json();
    if(!data.ok){
      showError(data.error || '保存失败');
      els.saveNote.textContent='保存失败';
      return;
    }
    els.contextState.textContent=`已保存 ${currentProfileTitle}`;
    els.contextState.className='context-state';
    els.saveNote.textContent='已保存';
    showError('');
    if(thenRead) await readNow();
  }catch(err){
    showError('保存失败: '+String(err));
    els.saveNote.textContent='保存失败';
  }finally{
    els.saveContextBtn.disabled=false;
    els.saveReadBtn.disabled=false;
    setTimeout(()=>{ if(els.saveNote.textContent==='已保存') els.saveNote.textContent=''; },1600);
  }
}
async function readNow(auto=false){
  setBusy(true);
  showError('');
  try{
    const res=await fetch('/api/read?persona='+encodeURIComponent(selectedPersona)+(auto?'&auto=1':''),{cache:'no-store'});
    const data=await res.json();
    if(data.error){
      renderRuntime(data.status);
      showError(data.error);
      els.statusText.textContent='读取失败';
      return;
    }
    renderPayload(data);
  }catch(err){
    showError(String(err));
    els.statusText.textContent='请求失败';
  }finally{
    setBusy(false);
  }
}
function fallbackCopy(text){
  const t=document.createElement('textarea');
  t.value=text;t.setAttribute('readonly','');t.style.position='fixed';t.style.left='-9999px';
  document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);
}
async function copyBubble(si,bi,button){
  const ta=document.getElementById(`bubble-${si}-${bi}`);
  if(!ta){showError('没找到这条气泡。');return;}
  const text=ta.value;
  try{
    if(navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else fallbackCopy(text);
    button.innerHTML='✓ 已复制';button.classList.add('copied');
    const row=document.getElementById(`brow-${si}-${bi}`);
    if(row){row.classList.add('copied');row.classList.remove('current');}
    // 复制一条→自动把下一条未复制的气泡高亮为"当前",引导逐条发
    const next=document.getElementById(`brow-${si}-${bi+1}`);
    if(next && !next.classList.contains('copied'))next.classList.add('current');
  }catch(err){showError('复制失败: '+String(err));}
}
function setGoal(text){els.replyIntent.value=text;els.replyIntent.focus();}
async function reassess(){
  if(!lastMessages.length){showError('先读取一次对话，再重新判定。');return;}
  const btn=document.getElementById('reassessBtn');
  const prev=els.analysisText.textContent;
  if(btn)btn.disabled=true;
  els.analysisText.textContent='重新判定中…';
  try{
    const res=await fetch('/api/reassess',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:lastTitle,messages:lastMessages})
    });
    const data=await res.json();
    if(data.error){showError('重新判定失败: '+data.error);els.analysisText.textContent=prev;}
    else{lastAnalysis=data.analysis||'';els.analysisText.textContent=lastAnalysis||prev;showError('');}
  }catch(err){showError('重新判定失败: '+String(err));els.analysisText.textContent=prev;}
  finally{if(btn)btn.disabled=false;}
}
async function regenOne(index,button){
  await generateFor(selectedPersona);   // 换个说法 = 同模式重出(后端 regen=True 加随机,避免重复)
}
let importPollTimer=null;
async function startImport(){
  const days=els.importDays?els.importDays.value:'7';
  const label=days==='all'?'全部历史(滚到顶)':`最近 ${days} 天`;
  if(!window.confirm(`将自动滚动「当前打开的对话」读取${label}(只滚动浏览,不发送任何消息)。\n\n请先确认:微信停在目标对话、聊天区可见、且已在 系统设置→隐私与安全性→辅助功能 勾上本程序。\n\n开始?`))return;
  els.importBtn.disabled=true;
  els.importNote.textContent='启动中';
  try{
    const res=await fetch('/api/import_history',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({days:days})
    });
    const data=await res.json();
    if(!data.started){els.importNote.textContent=data.error||'启动失败';els.importBtn.disabled=false;return;}
    pollImport();
  }catch(err){els.importNote.textContent='启动失败: '+String(err);els.importBtn.disabled=false;}
}
function pollImport(){
  clearTimeout(importPollTimer);
  importPollTimer=setTimeout(async()=>{
    try{
      const res=await fetch('/api/import_status',{cache:'no-store'});
      const s=await res.json();
      if(s.phase==='滚动采集中'){els.importNote.textContent=`滚动采集中 · ${s.screens} 屏 · ${s.messages} 条${s.earliest?` · 已滚到 ${s.earliest.slice(5)}`:''}`;}
      else if(s.phase&&s.phase.indexOf('提取记忆中')===0){els.importNote.textContent=`${s.phase} ·「${s.title}」${s.messages} 条`;}
      if(s.done){
        els.importBtn.disabled=false;
        if(s.error){els.importNote.textContent='失败: '+s.error;}
        else{
          els.importNote.textContent=`${s.phase} · 已存入「${s.title}」记忆(${s.messages} 条)`;
          els.importState.textContent='已导入,下次读取自动带上这段记忆';
          if(s.summary)els.analysisText.textContent='【导入的记忆档案】\n'+s.summary;
        }
        return;
      }
      pollImport();
    }catch(_){pollImport();}
  },800);
}
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&(e.key==='r'||e.key==='R')){
    e.preventDefault();
    if(!els.readBtn.disabled)readNow();
  }
});
async function boot(){
  els.modeName.textContent=MODE_LABEL[selectedPersona]||selectedPersona;
  renderMonitorState();
  resizeAllSuggestions();
  try{
    const res=await fetch('/api/status',{cache:'no-store'});
    renderRuntime(await res.json());
  }catch(_){
    els.modelName.textContent='本地服务';
  }
}
boot();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload) -> None:
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/api/status":
            self._json(_public_status())
        elif path == "/api/models":
            self._json(list_models())
        elif path == "/api/personas":
            self._json(list_personas())
        elif path == "/api/import_status":
            self._json(_import_state)
        elif path == "/api/peek":
            try:
                payload = peek()
            except SystemExit as e:
                payload = {"error": _error_text(e)}
            except Exception as e:
                payload = {"error": str(e)}
            self._json(payload)
        elif path == "/api/read":
            qs = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            try:
                payload = read_and_suggest(auto=qs.get("auto") == "1", persona=qs.get("persona"))
            except SystemExit as e:
                payload = {"error": _error_text(e), "status": _public_status()}
            except Exception as e:
                payload = {"error": str(e), "status": _public_status()}
            self._json(payload)
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/api/import_history":
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                body = {}
            days = body.get("days")
            self._json(start_import(None if days in (None, "", "all") else int(days)))
            return
        if self.path not in ("/api/context", "/api/regenerate", "/api/model", "/api/reassess"):
            self._send(404, "text/plain", b"not found")
            return
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path == "/api/context":
                title = data.get("title")
                saved = skills.save_manual_context(title, data.get("manual") or {})
                payload = {"ok": True, "profile": {"title": title, "manual": saved}}
            elif self.path == "/api/model":
                name = (data.get("reply_model") or "").strip()
                if not name:
                    raise ValueError("缺少模型名")
                set_reply_model(name)
                payload = {"ok": True, "status": _public_status()}
            elif self.path == "/api/reassess":
                analysis = reassess_stage(data.get("title") or "unknown",
                                          data.get("messages") or [])
                payload = {"analysis": analysis}
            else:  # /api/regenerate(换模式 / 换个说法)
                payload = regenerate(data.get("title") or "unknown",
                                     data.get("persona") or "", data.get("messages") or [],
                                     data.get("analysis") or "")
        except SystemExit as e:
            payload = {"ok": False, "error": _error_text(e)}
        except Exception as e:
            payload = {"ok": False, "error": str(e)}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _serve_forever() -> None:
    HTTPServer((HOST, PORT), Handler).serve_forever()


def main() -> None:
    import sys

    # Windows 控制台默认编码可能不是 UTF-8(如 cp936/cp1252),直接 print 中文会 UnicodeEncodeError。
    # 切到 UTF-8 并对无法编码的字符降级替换,避免打印日志时把整个程序带崩。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    vision.warm_ocr()       # 后台预热 OCR 模型,首次读取不再卡几秒(read_mode=ocr 时才生效)

    url = f"http://{HOST}:{PORT}"
    if "--window" in sys.argv or getattr(sys, "frozen", False):   # 打包后默认走原生窗口
        try:
            import webview
            threading.Thread(target=_serve_forever, daemon=True).start()
            webview.create_window("DraftMate 副驾", url, width=1120, height=740, min_size=(820, 520))
            webview.start()             # 阻塞,直到关闭窗口
            return
        except Exception as e:
            # pywebview 缺失或后端(Windows 需 WebView2)不可用 → 不崩,回退浏览器模式
            print(f"[原生窗口不可用,回退浏览器模式] {e}")
    print(f"DraftMate 副驾 → {url}(只读屏 + 复制,不自动发送)。Ctrl+C 退出。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    _serve_forever()


if __name__ == "__main__":
    main()
