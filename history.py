"""历史导入:自动滚动当前对话 + 多屏截图 OCR 去重拼接 → 一段连续历史。

红线:这里的键鼠模拟**仅限只读导航(滚动浏览)**,绝不模拟输入文字或点击发送
(copilot-only 身份不变)。自动滚动需要「辅助功能」权限;截图沿用「屏幕录制」权限。
"""
from __future__ import annotations

import datetime
import os
import re
import time

import vision

# 滚轮方向:正值=向上看更早的历史。macOS「自然滚动」开关会影响实际方向,
# 实测若反了把这个改成 -1。
SCROLL_DIR = 1


# ════════════════════ 微信系统时间戳解析(按天数决定采集范围)════════════════════
_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_wechat_date(text: str, today: datetime.date):
    """微信系统戳 → 绝对日期。相对戳("昨天"/"星期三"/纯时间)基准=today(必须用采集当下的日期)。
    容错 OCR("昨大"=昨天、"星里"≈星期);解析不出返回 None。实测真实噪声戳 100% 命中。"""
    t = (text or "").strip()
    if re.fullmatch(r"\d{1,2}[:：]\d{2}", t):                 # 纯时间 = 今天
        return today
    if t.startswith(("昨天", "昨大", "昨")):
        return today - datetime.timedelta(days=1)
    if t.startswith("前天"):
        return today - datetime.timedelta(days=2)
    m = re.search(r"[星里][期朋].?([一二三四五六日天])|周([一二三四五六日天])", t)   # 星期X(容错)
    if m:
        wd = _WD[m.group(1) or m.group(2)]
        return today - datetime.timedelta(days=(today.weekday() - wd) % 7)
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*[月.]\s*(\d{1,2})", t)        # 年月日
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*[.月]\s*(\d{1,2})", t)                         # 月.日(当年)
    if m:
        try:
            return datetime.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _earliest_in_screen(msgs: list, today: datetime.date):
    """本屏系统戳里能解析出的最早日期(没有则 None)。"""
    dates = [parse_wechat_date(m.get("text"), today)
             for m in msgs if m.get("sender") == "系统"]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


# ════════════════════ 自动滚动(M3,只读导航)════════════════════
def accessibility_ok() -> bool:
    """是否已授予「辅助功能」权限(合成鼠标/滚轮事件需要)。检测不到时放行,让实际滚动去试。"""
    for mod in ("ApplicationServices", "HIServices"):
        try:
            m = __import__(mod, fromlist=["AXIsProcessTrusted"])
            return bool(m.AXIsProcessTrusted())
        except Exception:
            continue
    return True


def activate(process_name: str) -> bool:
    """把目标 App 窗口带到前台。自动滚动要求窗口可见且在最上层,否则滚轮会落到别的窗口。
    优先 Cocoa NSRunningApplication(绕开 AppleScript;后者需 Apple Events 权限,实测常废),退路 AppleScript。
    Windows:ShowWindow + SetForegroundWindow。"""
    if vision._IS_WIN:
        return vision._win_activate(process_name)
    pid = vision.window_pid(process_name)
    if pid:
        try:
            import AppKit
            ra = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if ra and ra.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps):
                return True
        except Exception:
            pass
    names = [process_name] + [a for a in vision._ALIASES if a]
    for name in names:
        if vision._osascript(f'tell application "System Events" to set frontmost of process "{name}" to true'):
            return True
    if process_name:
        vision._osascript(f'tell application "{process_name}" to activate')
    return False


def scroll_up(process_name: str, lines: int = 8, direction: int = SCROLL_DIR) -> bool:
    """把鼠标移到目标窗口聊天区,向 direction 方向滚 lines 行。成功发出事件返回 True。"""
    bounds = vision.window_box(process_name) or vision.window_bounds(process_name)
    if not bounds:
        return False
    x, y, w, h = bounds
    cx, cy = x + int(w * 0.6), y + int(h * 0.5)   # 偏右,避开桌面版左侧会话列表
    if vision._IS_WIN:
        return vision._win_scroll(cx, cy, lines, direction)
    try:
        import Quartz
    except Exception:
        return False
    Quartz.CGEventPost(
        Quartz.kCGHIDEventTap,
        Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (cx, cy), 0),
    )
    time.sleep(0.05)
    for _ in range(max(1, lines)):
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, direction)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)
    return True


# ════════════════════ 去重拼接(M2)════════════════════
def _key(m: dict) -> str:
    """消息归一化指纹:发言人 + 文本去空白后前 24 字。容忍 OCR 末尾抖动。"""
    sender = (m.get("sender") or "")[:6]
    text = re.sub(r"\s+", "", m.get("text") or "")
    return sender + "|" + text[:24]


def _overlap_len(earlier: list, known: list, max_probe: int = 14) -> int:
    """earlier(更早一屏)的「后缀」与 known(已知较早段)的「前缀」最长重叠条数。
    earlier 底部应与 known 顶部是同几条消息。"""
    cap = min(len(earlier), len(known), max_probe)
    for k in range(cap, 0, -1):
        if all(_key(earlier[-k + i]) == _key(known[i]) for i in range(k)):
            return k
    return 0


def stitch(known: list, earlier: list) -> tuple[list, int]:
    """把更早的一屏 earlier 拼到 known 前面,去掉重叠。返回(新列表, 本屏新增条数)。
    known/earlier 均按时间从早到晚排列。"""
    if not earlier:
        return known, 0
    if not known:
        return list(earlier), len(earlier)
    k = _overlap_len(earlier, known)
    added = earlier if k == 0 else earlier[: len(earlier) - k]
    return added + known, len(added)


# ════════════════════ 采集编排(M3 + M2)════════════════════
def import_history(process_name: str, model: str, *, days: int | None = 7,
                   max_screens: int = 60, scroll_lines: int = 8, settle: float = 0.7,
                   on_progress=None, today: datetime.date | None = None) -> dict:
    """自动往上滚、拼出历史。按 days 决定范围(滚到「最近 days 天」就停;None=滚到顶);
    max_screens 是硬上限兜底。返回 {title, messages, screens, reached_top, reached_target, earliest}。
    on_progress(dict) 每屏回调,带 earliest(已滚到的最早日期)。"""
    if not accessibility_ok():
        raise RuntimeError(
            "自动滚动需要「辅助功能」权限:系统设置→隐私与安全性→辅助功能,"
            "勾上 DraftMate(开发态勾你的终端),重开后再试。"
        )
    activate(process_name)
    time.sleep(0.3)
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=days) if days else None
    earliest = today                 # 已滚到的最早可信日期(单调,只接受更早,吸收 OCR ±1 天抖动)
    known: list = []
    title = None
    no_gain = 0
    screens = 0
    direction = SCROLL_DIR
    flipped = False           # 方向自适应:先试一个方向,没采到新内容就翻转(应对「自然滚动」开关)
    for i in range(max(1, max_screens)):
        png = vision.grab(process_name)
        try:
            data = vision.read_messages(png, model, 9999)   # 大 last_n = 拿整屏全部
        finally:
            try:
                os.remove(png)
            except OSError:
                pass
        msgs = data.get("messages") or []
        title = title or data.get("chat_title")
        screens += 1
        if i == 0:
            known, added = msgs, len(msgs)
        else:
            known, added = stitch(known, msgs)
        # 按天:本屏最早戳更新全局最早(单调,晚跳的判为 OCR 噪声忽略)
        d = _earliest_in_screen(msgs, today)
        if d and d < earliest:
            earliest = d
        if on_progress:
            on_progress({"screens": screens, "messages": len(known), "added": added,
                         "earliest": earliest.isoformat()})
        if cutoff and earliest < cutoff:      # 已滚过「最近 days 天」→ 达标停止
            return {"title": title, "messages": known, "screens": screens,
                    "reached_top": False, "reached_target": True, "earliest": earliest.isoformat()}
        if added == 0:
            no_gain += 1
            if no_gain >= 2:
                if not flipped:           # 可能方向反了(往下滚到底了)→ 翻转再试
                    direction, flipped, no_gain = -direction, True, 0
                else:                     # 翻转后仍没新增 = 真到顶
                    return {"title": title, "messages": known, "screens": screens,
                            "reached_top": True, "reached_target": False, "earliest": earliest.isoformat()}
        else:
            no_gain = 0
        if not scroll_up(process_name, scroll_lines, direction):
            return {"title": title, "messages": known, "screens": screens,
                    "reached_top": False, "reached_target": False, "earliest": earliest.isoformat()}
        time.sleep(settle)        # 等微信渲染稳定再截下一屏
    return {"title": title, "messages": known, "screens": screens,
            "reached_top": False, "reached_target": False, "earliest": earliest.isoformat()}
