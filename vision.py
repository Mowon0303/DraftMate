"""读屏:截图 → OCR/视觉 → 结构化对话。

  1) 屏幕截图与窗口定位(macOS)      —— 原 capture
  2) OCR 后端 + 头像/几何发言人判定   —— 原 chat_ocr(已去掉其命令行入口)
  3) 截图 → 结构化对话              —— vlm 直读 或 ocr 几何判定;发言人按位置/头像判,不让模型猜
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import llm

try:
    import numpy as np
except Exception:
    np = None
try:
    from PIL import Image
except Exception:
    Image = None


# ════════════════════ 1) 屏幕截图与窗口定位(macOS / Windows)════════════════════
# 读屏是唯一与操作系统强相关的一层:用 sys.platform 分流。macOS 走原生
# screencapture + Quartz;Windows 走 Pillow ImageGrab + pygetwindow + ctypes 滚轮。
# 业务逻辑(模型 / 记忆 / 拼接)与本段无关,全平台共用。
_IS_MAC = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"

# Windows 高 DPI:让窗口坐标(GetWindowRect)与 ImageGrab 截到的物理像素对齐,
# 否则在 125% / 150% 缩放下窗口框会和截图错位。进程级设一次即可。
if _IS_WIN:
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _osascript(script: str) -> str:
    """执行 AppleScript(仅 macOS)。非 mac 上 osascript 不存在,直接返回空串,绝不抛错。"""
    if not _IS_MAC:
        return ""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


# ----------------------------- Windows 读屏后端 ----------------------------- #
_GW = None


def _pygetwindow():
    """惰性加载 pygetwindow(仅 Windows 需要);未安装返回 None。"""
    global _GW
    if _GW is None:
        try:
            import pygetwindow as gw
            _GW = gw
        except Exception:
            _GW = False
    return _GW or None


# 桌面外壳/系统伪窗口,绝不当成聊天软件(否则过宽的别名可能误匹配到桌面)
_WIN_SHELL_TITLES = {"program manager", "windows input experience", "windows 输入体验"}


def _win_match_tier(title_lower: str, cands: list) -> int:
    """标题与候选名的匹配强度,越小越强:0=完全相等,1=前/后缀,2=子串,99=不匹配。
    分级是为了让稳定的强匹配(如标题恰为「微信」)压过浏览器标签那种「标题里碰巧含『微信』」的弱子串,
    避免一个最大化的浏览器窗口因面积大而抢走目标。"""
    best = 99
    for c in cands:
        if title_lower == c:
            return 0
        if title_lower.startswith(c) or title_lower.endswith(c):
            best = min(best, 1)
        elif c in title_lower:
            best = min(best, 2)
    return best


def _win_best_window(process_name: str):
    """挑最佳匹配窗口:先按匹配强度(tier 小优先),同档再按面积最大;排除最小化/无效/外壳窗口。
    app_name 为空 → 不匹配(返回 None,调用方退到全屏)。"""
    gw = _pygetwindow()
    if gw is None:
        return None
    cands = [c for c in ([process_name.lower()] if process_name else []) + _ALIASES if c]
    if not cands:
        return None
    try:
        wins = gw.getAllWindows()
    except Exception:
        return None
    best, best_key = None, None
    for win in wins:
        title = (getattr(win, "title", "") or "").strip()
        if not title or title.lower() in _WIN_SHELL_TITLES:
            continue
        tier = _win_match_tier(title.lower(), cands)
        if tier >= 99:
            continue
        try:
            if getattr(win, "isMinimized", False):
                continue
            x, y, w, h = int(win.left), int(win.top), int(win.width), int(win.height)
        except Exception:
            continue
        if w <= 0 or h <= 0 or x <= -30000 or y <= -30000:   # 无效 / 最小化到屏外
            continue
        key = (-tier, w * h)                                  # tier 小优先,其次面积大
        if best_key is None or key > best_key:
            best_key, best = key, win
    return best


def _win_window_rect(process_name: str):
    """目标窗口的 (x, y, w, h)(物理像素);找不到 / 最小化 → None(调用方退到全屏)。"""
    win = _win_best_window(process_name)
    if win is None:
        return None
    try:
        return (int(win.left), int(win.top), int(win.width), int(win.height))
    except Exception:
        return None


def _win_all_titles() -> list:
    """列出当前所有可见窗口标题(诊断用:确认目标聊天软件窗口到底叫什么)。"""
    gw = _pygetwindow()
    if gw is None:
        return []
    try:
        return sorted({(t or "").strip() for t in gw.getAllTitles() if (t or "").strip()})
    except Exception:
        return []


def _win_activate(process_name: str) -> bool:
    """把目标窗口带到前台(Windows:ShowWindow + SetForegroundWindow)。"""
    win = _win_best_window(process_name)
    if win is None:
        return False
    hwnd = getattr(win, "_hWnd", None)
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)            # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def _win_scroll(cx: int, cy: int, lines: int, direction: int) -> bool:
    """把光标移到 (cx, cy) 后合成滚轮事件(Windows:ctypes mouse_event)。
    direction>0 → 向上滚(看更早历史)。纯只读导航,不输入文字、不点击。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(cx), int(cy))
    except Exception:
        return False
    MOUSEEVENTF_WHEEL = 0x0800
    WHEEL_DELTA = 120
    for _ in range(max(1, lines)):
        try:
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(direction) * WHEEL_DELTA, 0)
        except Exception:
            return False
        time.sleep(0.04)
    return True


def _win_grab(process_name: str, path: str) -> None:
    """Windows 截图:定位目标窗口区域 → Pillow ImageGrab 截该区域。
    注意:ImageGrab 截「屏幕上可见的内容」,目标窗口需在前台、未被遮挡、未最小化。
    找不到目标窗口时**不静默全屏**(那只会把配置错误伪装成识别差),而是明确报错;
    确实想截整屏可把 app_name 设为 "*"。"""
    try:
        from PIL import ImageGrab
    except Exception as e:
        raise RuntimeError("Windows 截图需要 Pillow:pip install pillow") from e
    if process_name == "*":                               # 显式全屏(opt-in)
        ImageGrab.grab(all_screens=True).save(path)
        return
    box = _win_window_rect(process_name)
    if box is None:
        cands = [c for c in ([process_name] if process_name else []) + _ALIASES if c]
        if not cands:
            raise RuntimeError(
                "未配置目标窗口:请在 config.yaml 填 app_name(目标聊天软件的窗口标题)。"
                "若想截整个屏幕,把 app_name 设为 \"*\"。")
        titles = _win_all_titles()
        raise RuntimeError(
            "未找到目标聊天窗口「%s」:请确认它已打开、在前台、未最小化,"
            "并核对 config.yaml 的 app_name / app_aliases 是否匹配窗口标题。\n当前可见窗口标题:%s"
            % (process_name or "/".join(cands), "、".join(titles[:40]) or "(无)"))
    x, y, w, h = box
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)


def window_bounds(process_name: str):
    """返回聊天软件主窗口的 (x, y, w, h);拿不到时返回 None(改为全屏截图)。"""
    script = f'''
    set _info to ""
    tell application "System Events"
        if exists process "{process_name}" then
            tell process "{process_name}"
                set _p to position of window 1
                set _s to size of window 1
                set _info to ((item 1 of _p) as string) & "," & ((item 2 of _p) as string) & "," & ((item 1 of _s) as string) & "," & ((item 2 of _s) as string)
            end tell
        end if
    end tell
    return _info
    '''
    out = _osascript(script)
    if "," not in out:
        return None
    try:
        x, y, w, h = (int(v) for v in out.split(","))
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


_ALIASES = []   # 目标应用的窗口属主名别名(本地化名等),由 configure() 从配置注入


def set_app_aliases(aliases=None) -> None:
    global _ALIASES
    _ALIASES = [str(a).lower() for a in (aliases or []) if a]


def _is_target_owner(owner, process_name: str) -> bool:
    o = (owner or "").lower()
    if not o:
        return False
    cands = ([process_name.lower()] if process_name else []) + _ALIASES
    return any(c and (o == c or c in o) for c in cands)


def window_id(process_name: str):
    """用 Quartz 找目标应用主窗口的 CGWindowID(按窗口抓取,被遮挡也只截它自己)。
    注意:有些应用窗口 owner 名是本地化名,不一定等于进程名,可在配置 app_aliases 里补。"""
    try:
        import Quartz
    except Exception:
        return None
    # 先 OnScreenOnly(最适合截图);权限/沙箱拿不到时退到 All
    for opt in (Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGWindowListOptionAll):
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID) or []
        best_id, best_area = None, 0.0
        for w in wins:
            if not _is_target_owner(w.get("kCGWindowOwnerName"), process_name):
                continue
            if w.get("kCGWindowLayer", 0) != 0:        # 只要普通窗口层(排除菜单/悬浮层)
                continue
            b = w.get("kCGWindowBounds", {})
            area = float(b.get("Width", 0)) * float(b.get("Height", 0))
            if area > best_area:
                best_area, best_id = area, int(w.get("kCGWindowNumber", 0))
        if best_id:
            return best_id
    return None


def window_box(process_name: str):
    """拿目标主窗口的全局坐标 (x, y, w, h)。Windows 用 pygetwindow;
    macOS 用 Quartz(和 window_id 同源,比 AppleScript 可靠,后者需 Apple Events 权限)。"""
    if _IS_WIN:
        return _win_window_rect(process_name)
    try:
        import Quartz
    except Exception:
        return None
    for opt in (Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGWindowListOptionAll):
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID) or []
        best, best_area = None, 0.0
        for w in wins:
            if not _is_target_owner(w.get("kCGWindowOwnerName"), process_name):
                continue
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            area = float(b.get("Width", 0)) * float(b.get("Height", 0))
            if area > best_area:
                best_area = area
                best = (int(b.get("X", 0)), int(b.get("Y", 0)),
                        int(b.get("Width", 0)), int(b.get("Height", 0)))
        if best:
            return best
    return None


def window_pid(process_name: str):
    """目标主窗口的进程 PID(Quartz)。用于 Cocoa NSRunningApplication 激活,绕开 AppleScript。"""
    try:
        import Quartz
    except Exception:
        return None
    for opt in (Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGWindowListOptionAll):
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID) or []
        best_pid, best_area = None, 0.0
        for w in wins:
            if not _is_target_owner(w.get("kCGWindowOwnerName"), process_name):
                continue
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            area = float(b.get("Width", 0)) * float(b.get("Height", 0))
            if area > best_area:
                best_area, best_pid = area, w.get("kCGWindowOwnerPID")
        if best_pid:
            return int(best_pid)
    return None


def list_window_owners() -> list:
    """列出当前所有窗口名(诊断用:确认聊天软件到底叫什么)。
    Windows 返回窗口标题(配 app_name 用);macOS 返回窗口 owner 名。"""
    if _IS_WIN:
        return _win_all_titles()
    try:
        import Quartz
    except Exception:
        return []
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    return sorted({(w.get("kCGWindowOwnerName") or "?") for w in wins})


def grab(process_name: str, activate: bool = False) -> str:
    """截图,返回临时 png 路径。调用方负责删除。
    Windows:截目标窗口所在屏幕区域(Pillow ImageGrab);窗口需在前台、未遮挡、未最小化。
    activate=True 时先把目标窗口提到前台再截(用于手动「读取」,避免截到压在上面的窗口;
    监控轮询不要置 True,否则每隔几秒就抢一次焦点)。
    macOS:按窗口 ID 抓该窗口自身内容(即使被别的窗口挡住)。"""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="draftmate_")
    os.close(fd)
    if _IS_WIN:
        prev = None
        if activate:
            try:
                import ctypes
                prev = ctypes.windll.user32.GetForegroundWindow()   # 记住当前前台(多半是 DraftMate UI)
            except Exception:
                prev = None
            _win_activate(process_name)
            time.sleep(0.12)            # 等窗口真正到前台、重绘完再截
        _win_grab(process_name, path)
        if prev:                        # 截完把焦点还回去,免得结果被目标窗口挡住
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(prev)
            except Exception:
                pass
    else:
        wid = window_id(process_name)
        if wid:
            # -l 按窗口 ID 抓该窗口自身内容(不受遮挡影响);-o 去掉窗口阴影
            subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), path], check=False)
        else:
            bounds = window_bounds(process_name)
            if bounds:
                x, y, w, h = bounds
                subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path], check=False)
            else:
                subprocess.run(["screencapture", "-x", path], check=False)
    # 截图无效(没权限时会失败/产出空文件)→ 明确报错,别把空图喂给模型
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        try:
            os.remove(path)
        except OSError:
            pass
        if _IS_WIN:
            raise RuntimeError(
                "截图失败:请确认目标聊天窗口已打开、在前台且未最小化;"
                "若装了多块显示器,把聊天窗口移到主屏再试。"
            )
        raise RuntimeError(
            "截图失败:多半是终端没有「屏幕录制」权限。"
            "去 系统设置→隐私与安全性→屏幕录制 勾上你的终端,重启终端后再试。"
        )
    return path


# ════════════════════ 2) OCR 后端 + 头像/几何发言人判定 ════════════════════

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
BACKEND_ORDER = ["vision", "easyocr", "paddleocr", "tesseract"]
_CHOSEN = None          # resolved backend name
_PADDLE = None
_EASY = None


# ----------------------------------------------------------------------------- #
# OCR backends — each returns list of lines: {"text","box":(x0,y0,x1,y1),"conf"}
# ----------------------------------------------------------------------------- #
def _line(text, x0, y0, x1, y1, conf):
    return {"text": str(text), "box": (float(x0), float(y0), float(x1), float(y1)),
            "conf": float(conf)}


def ocr_vision(path, W, H):
    """macOS Vision framework via pyobjc. 中文好、无需下载模型。
    注意：在沙箱/受限环境（如某些 managed shell）Vision 请求可能建不出 CVPixelBuffer 而失败。
    这里检查 performRequests:error: 的返回值，失败就**抛错**，绝不把'请求失败'伪装成'0 行文字'。"""
    import Vision, Quartz
    from Foundation import NSURL
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(os.path.abspath(path)), None)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None
    if cg is None:
        raise RuntimeError("Vision: 无法解码图片（CGImage 为空）")
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)               # 0 = accurate（支持中文）；1 = fast（偏拉丁，会把中文识成乱码）
    req.setUsesLanguageCorrection_(True)
    try:
        req.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en"])
    except Exception:
        pass
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(
            "Vision 请求执行失败（此环境可能不支持，如沙箱建不出 CVPixelBuffer/420f）：%s" % err)
    lines = []
    for obs in (req.results() or []):
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        text = cand[0].string()
        conf = float(cand[0].confidence())
        bb = obs.boundingBox()                # normalized, origin bottom-left
        x = bb.origin.x * W
        w = bb.size.width * W
        y = (1.0 - bb.origin.y - bb.size.height) * H
        h = bb.size.height * H
        lines.append(_line(text, x, y, x + w, y + h, conf))
    return lines


def ocr_easyocr(path, W, H):
    import easyocr
    global _EASY
    if _EASY is None:
        _EASY = easyocr.Reader(["ch_sim", "en"], gpu=False)
    out = []
    for box, text, conf in _EASY.readtext(path):
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        out.append(_line(text, min(xs), min(ys), max(xs), max(ys), conf))
    return out


def ocr_paddleocr(path, W, H):
    from paddleocr import PaddleOCR
    global _PADDLE
    if _PADDLE is None:
        _PADDLE = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    res = _PADDLE.ocr(path, cls=True)
    out = []
    for page in (res or []):
        for item in (page or []):
            box, (text, conf) = item[0], item[1]
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            out.append(_line(text, min(xs), min(ys), max(xs), max(ys), conf))
    return out


def ocr_tesseract(path, W, H):
    import pytesseract
    from pytesseract import Output
    lang = os.environ.get("CHAT_OCR_TESS_LANG", "chi_sim+chi_tra+eng")
    d = pytesseract.image_to_data(path, lang=lang, output_type=Output.DICT)  # pass path, not PIL image (avoids a Pillow decode quirk)
    groups = {}
    for i in range(len(d["text"])):
        t = (d["text"][i] or "").strip()
        c = float(d["conf"][i])
        if not t or c < 0:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        groups.setdefault(key, []).append((t, x, y, x + w, y + h, c / 100.0))
    out = []
    for ws in groups.values():
        text = "".join(w[0] for w in ws) if _looks_cjk(ws) else " ".join(w[0] for w in ws)
        x0 = min(w[1] for w in ws); y0 = min(w[2] for w in ws)
        x1 = max(w[3] for w in ws); y1 = max(w[4] for w in ws)
        conf = sum(w[5] for w in ws) / len(ws)
        out.append(_line(text, x0, y0, x1, y1, conf))
    return out


def _looks_cjk(ws):
    s = "".join(w[0] for w in ws)
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    return cjk >= max(1, len(s) // 2)


_BACKENDS = {"vision": ocr_vision, "easyocr": ocr_easyocr,
             "paddleocr": ocr_paddleocr, "tesseract": ocr_tesseract}


def run_ocr(path, backend, W, H):
    global _CHOSEN
    if backend != "auto":
        try:
            return _BACKENDS[backend](path, W, H)
        except Exception as e:
            raise RuntimeError(f"后端 {backend} 不可用：{type(e).__name__}: {e}\n见 references/ocr-backends.md")
    # auto：优先用已选后端；但若它对这张图读出 0 行，自动换别的后端再试（防某后端对某些图失灵）
    order = ([_CHOSEN] if _CHOSEN else []) + [b for b in BACKEND_ORDER if b != _CHOSEN]
    last, ran_any = None, False
    for b in order:
        try:
            lines = _BACKENDS[b](path, W, H)
        except Exception as e:
            last = f"{b}: {type(e).__name__}: {e}"
            continue
        ran_any = True
        if lines:                       # 只采纳真读出文字的后端
            _CHOSEN = b
            return lines
        last = f"{b}: 0 行"
    if ran_any:
        return []                       # 后端能跑但都没读出文字 → 交给上层按"OCR 失败"处理
    raise RuntimeError(
        "没有可用的 OCR 后端。最后 -> %s\n请先安装一个后端，见 references/ocr-backends.md" % last)


# ----------------------------------------------------------------------------- #
# Avatar detection — pure numpy (no cv2). Find colorful square-ish blocks in the
# far-left / far-right margins (where Chat avatars live).
# ----------------------------------------------------------------------------- #
def detect_avatars(path, W, H):
    if Image is None or np is None:
        return []
    try:
        arr = np.asarray(Image.open(path).convert("RGB")).astype("int16")
    except Exception:
        return []
    mx = arr.max(axis=2); mn = arr.min(axis=2)
    colorful = (mx - mn) > 22                  # 有彩色（纯色头像/合成块/绿气泡）
    gray = arr.mean(axis=2)
    bk = max(6, H // 160)                       # 纹理块大小
    Hc, Wc = (H // bk) * bk, (W // bk) * bk
    g = gray[:Hc, :Wc].reshape(Hc // bk, bk, Wc // bk, bk)
    tex = g.std(axis=(1, 3)) > 18              # 照片头像有纹理；扁平 UI/气泡/背景没有
    textured = np.zeros((H, W), dtype=bool)
    textured[:Hc, :Wc] = np.repeat(np.repeat(tex, bk, axis=0), bk, axis=1)
    colorful = colorful | textured            # 头像 = 彩色 或 有纹理（兼顾深色模式真照片，常是哑色）
    left_x, right_x = int(0.14 * W), int(0.86 * W)
    avatars = []
    for side, (xa, xb) in (("left", (0, left_x)), ("right", (right_x, W))):
        band = colorful[:, xa:xb]
        if band.size == 0:
            continue
        rowcov = band.mean(axis=1)
        runs, s = [], None
        for y, v in enumerate(rowcov > 0.18):
            if v and s is None:
                s = y
            elif not v and s is not None:
                runs.append((s, y)); s = None
        if s is not None:
            runs.append((s, len(rowcov)))
        for y0, y1 in runs:
            h = y1 - y0
            if h < 0.03 * H or h > 0.16 * H:   # plausible avatar height
                continue
            sub = band[y0:y1]
            cols = np.where(sub.mean(axis=0) > 0.15)[0]
            if cols.size == 0:
                continue
            bx0, bx1 = xa + int(cols.min()), xa + int(cols.max())
            w = max(bx1 - bx0, 1)
            ar = w / h
            if ar < 0.5 or ar > 1.8:           # roughly square
                continue
            cov = float(sub.mean())
            conf = round(min(0.95, 0.45 + 0.5 * min(1.0, cov / 0.5)), 3)
            avatars.append({"box": (bx0, int(y0), bx1, int(y1)), "side": side, "conf": conf})
    return avatars


def _content_mask(path, W, H, text_boxes):
    """'视觉内容'掩码（彩色 或 有纹理），已抠掉文字与左右头像栏。给图片/表情检测用。"""
    if Image is None or np is None:
        return None
    try:
        arr = np.asarray(Image.open(path).convert("RGB")).astype("float32")
    except Exception:
        return None
    chroma = (arr.max(axis=2) - arr.min(axis=2)) > 22
    gray = arr.mean(axis=2)
    bk = max(6, H // 160)
    Hc, Wc = (H // bk) * bk, (W // bk) * bk
    g = gray[:Hc, :Wc].reshape(Hc // bk, bk, Wc // bk, bk)
    tex = np.zeros((H, W), dtype=bool)
    tex[:Hc, :Wc] = np.repeat(np.repeat(g.std(axis=(1, 3)) > 11, bk, axis=0), bk, axis=1)
    content = chroma | tex
    for (x0, y0, x1, y1) in text_boxes:
        content[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)] = False
    content[:, :int(0.13 * W)] = False
    content[:, int(0.88 * W):] = False
    return content


# ----------------------------------------------------------------------------- #
# Bubble clustering — group OCR lines into bubbles (vertical proximity + side).
# ----------------------------------------------------------------------------- #
def _side(box, W):
    return "L" if (box[0] + box[2]) / 2 < W * 0.5 else "R"


def cluster_bubbles(lines, W, H):
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: (l["box"][1], l["box"][0]))
    heights = sorted(l["box"][3] - l["box"][1] for l in lines)
    mh = heights[len(heights) // 2] or 20.0
    bubbles, cur = [], [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        gap = ln["box"][1] - prev["box"][3]
        same = _side(ln["box"], W) == _side(cur[-1]["box"], W)
        if gap > mh * 0.9 or not same:
            bubbles.append(cur); cur = [ln]
        else:
            cur.append(ln)
    bubbles.append(cur)
    return bubbles


def _bubble_box(b):
    return (min(l["box"][0] for l in b), min(l["box"][1] for l in b),
            max(l["box"][2] for l in b), max(l["box"][3] for l in b))


# ----------------------------------------------------------------------------- #
# Speaker assignment — avatar in same y-band first, bubble-center fallback.
# ----------------------------------------------------------------------------- #
def assign_speaker(box, avatars, W, me_side):
    x0, y0, x1, y1 = box
    cx, w = (x0 + x1) / 2, (x1 - x0)
    tol = (y1 - y0) * 0.6 + 10
    near = [a for a in avatars if y0 - tol <= (a["box"][1] + a["box"][3]) / 2 <= y1 + tol]
    left = [a for a in near if a["side"] == "left"]
    right = [a for a in near if a["side"] == "right"]
    me, other = me_side, ("right" if me_side == "left" else "left")
    # 1) 头像优先：同一 y 带内只有一侧头像
    if left and not right:
        return ("me" if me == "left" else "other"), 0.92, "附近左侧头像", "left"
    if right and not left:
        return ("me" if me == "right" else "other"), 0.92, "附近右侧头像", "right"
    # 2) 头像失败 -> 看气泡贴哪边（比"中心 vs 屏幕中线"稳得多）
    left_gap, right_gap = x0, W - x1
    hugs_left, hugs_right = left_gap < W * 0.30, right_gap < W * 0.30
    if hugs_left and not hugs_right:
        return ("me" if me == "left" else "other"), 0.70, "无头像 fallback：气泡贴左边", None
    if hugs_right and not hugs_left:
        return ("me" if me == "right" else "other"), 0.70, "无头像 fallback：气泡贴右边", None
    # 3) 居中且窄、两侧留白对称 -> 系统/时间
    if w < W * 0.5 and abs(left_gap - right_gap) < W * 0.15:
        return "system", 0.6, "居中且窄、两侧留白对称，疑似时间/系统提示", None
    # 4) 兜底：用中心位置，但低置信
    if cx < W * 0.5:
        return ("me" if me == "left" else "other"), 0.5, "兜底：气泡偏左（低置信）", None
    if cx > W * 0.5:
        return ("me" if me == "right" else "other"), 0.5, "兜底：气泡偏右（低置信）", None
    return "unknown", 0.3, "居中/信号冲突，无法判定", None


# ----------------------------------------------------------------------------- #
def _is_status_bar(text):
    """像手机状态栏：只有数字/冒号/%/空格（时间 06:07、电量 76），且不含中文日期字。
    聊天日期戳（含 年/月/日）会被保留。"""
    t = (text or "").strip()
    return bool(t) and bool(re.fullmatch(r"[\d:%\s]+", t)) and not any(c in t for c in "年月日")


def image_size(path):
    if Image is not None:
        with Image.open(path) as im:
            return im.size
    raise RuntimeError("需要 Pillow 读取图片尺寸：pip install pillow")


def process_image(path, backend, me_side, crop_top=0.0, crop_bottom=0.0):
    W, H = image_size(path)
    lines = run_ocr(path, backend, W, H)

    # 状态栏按"内容"识别后丢弃，而不是按固定比例裁——否则没状态栏的截图首/尾消息会被误删
    def _keep(l):
        yc = (l["box"][1] + l["box"][3]) / 2
        if yc < crop_top * H or yc > (1.0 - crop_bottom) * H:
            return False
        if yc < 0.10 * H and _is_status_bar(l["text"]):   # 顶部且只有时间/电量数字 → 状态栏
            return False
        return True
    lines = [l for l in lines if _keep(l)]
    if not lines:
        # OCR 一个字都没读出来（深色截图/红笔标注可能干扰）——别凭头像硬塞一堆〔图片〕，老实报失败
        return [{
            "speaker": "unknown",
            "text": "⚠️ 这张图没读出任何文字（深色截图 / 标注可能干扰 OCR）——换个 OCR 后端，或让带视觉的模型直接看原图",
            "confidence": 0.2, "reason": "OCR 返回 0 行文字",
            "avatar_side": None, "ocr_confidence": 0.0,
            "bbox": [0, 0, int(W), int(H)], "image": os.path.basename(path),
        }]
    avatars = detect_avatars(path, W, H)
    msgs = []
    for b in cluster_bubbles(lines, W, H):
        text = "\n".join(l["text"] for l in b).strip()
        if not text or (len(text) <= 1 and not any("一" <= c <= "鿿" for c in text)):
            continue   # 跳过空 / 单字符 UI 噪声（如 "+"、"V"）；单个中文字(嗯/好)保留
        box = _bubble_box(b)
        sp, conf, reason, av_side = assign_speaker(box, avatars, W, me_side)
        ocr_conf = round(sum(l["conf"] for l in b) / len(b), 3)
        if ocr_conf < 0.4:
            conf = round(conf * 0.7, 3)
            reason += f"；OCR 置信度低({ocr_conf})"
        msgs.append({
            "speaker": sp,
            "text": text,
            "confidence": round(conf, 3),
            "reason": reason,
            "avatar_side": av_side,
            "ocr_confidence": ocr_conf,
            "bbox": [int(round(v)) for v in box],
            "image": os.path.basename(path),
            "_yc": (box[1] + box[3]) / 2,
        })
    # 图片/表情消息：头像锚定——某侧有头像、该 y 行无文字、但确有图像内容
    text_yc = [m["_yc"] for m in msgs]
    content = _content_mask(path, W, H, [l["box"] for l in lines]) if avatars else None
    xa, xb = int(0.13 * W), int(0.88 * W)
    if content is not None:
        for a in avatars:
            atop = a["box"][1]
            ah = max(a["box"][3] - a["box"][1], 20)
            ay = atop + ah / 2
            if any(abs(ty - ay) < ah * 0.8 for ty in text_yc):
                continue                                    # 该行是文字消息
            y0, y1 = int(max(0, atop - 0.3 * ah)), int(min(H, atop + 3.0 * ah))
            band = content[y0:y1, xa:xb]
            if band.size == 0 or float(band.mean()) < 0.05:
                continue                                    # 没图像内容 → 伪头像，跳过
            rows = np.where(band.mean(axis=1) > 0.06)[0]
            cols = np.where(band.mean(axis=0) > 0.06)[0]
            if rows.size == 0 or cols.size == 0:
                continue
            iy0, iy1 = y0 + int(rows.min()), y0 + int(rows.max())
            ix0, ix1 = xa + int(cols.min()), xa + int(cols.max())
            if (ix1 - ix0) < 0.06 * W or (iy1 - iy0) < 0.04 * H:
                continue
            kind = "图片" if ((ix1 - ix0) > 0.30 * W or (iy1 - iy0) > 0.13 * H) else "表情"
            sp = "me" if a["side"] == me_side else "other"
            msgs.append({
                "speaker": sp, "text": f"〔{kind}〕",
                "confidence": 0.6,
                "reason": f"{a['side']}侧头像、该行无文字、有图像内容 → {kind}",
                "avatar_side": a["side"], "ocr_confidence": 0.0,
                "bbox": [ix0, iy0, ix1, iy1],
                "image": os.path.basename(path), "_yc": (iy0 + iy1) / 2,
            })
    msgs.sort(key=lambda m: m["_yc"])
    for m in msgs:
        m.pop("_yc", None)
    return msgs


# ════════════════════ 3) 截图 → 结构化对话 ════════════════════

# 读取模式:vlm(纯视觉模型,默认) 或 ocr(本地 OCR + 头像/几何判定)
_MODE = {"read_mode": "vlm", "ocr_backend": "auto", "me_side": "right",
         "crop_left": 0.0, "crop_bottom": 0.0}


def configure(read_mode: str = "vlm", ocr_backend: str = "auto", me_side: str = "right",
              crop_left: float = 0.0, crop_bottom: float = 0.0) -> None:
    _MODE["read_mode"] = (read_mode or "vlm").lower()
    _MODE["ocr_backend"] = ocr_backend or "auto"
    _MODE["me_side"] = me_side or "right"
    _MODE["crop_left"] = float(crop_left or 0.0)
    _MODE["crop_bottom"] = float(crop_bottom or 0.0)


_OCR_LABEL = {"me": "我", "other": "对方", "system": "系统", "unknown": "unknown"}

_SYS = "你是解析聊天截图的助手。只输出 JSON,不要任何解释或多余文字。"


def _prompt(last_n: int) -> str:
    return f"""这是一张聊天截图。从上到下列出最近的消息(最多 {last_n} 条)。
严格只输出 JSON(不要 markdown 代码块、不要解释):
{{"chat_title": "顶部标题栏里的聊天名;看不到就填 null", "messages": [{{"text": "消息文字", "cx": 整数}}]}}

要点:
- cx = 该消息气泡(或图片)水平中心占图片总宽度的百分比,0=最左,100=最右;贴左的小、贴右的大、居中约 50。
- **不要判断谁发的**,只如实给出位置 cx。
- 图片或表情包:text 写成 "〔图片〕" 或 "〔表情〕",也要给 cx。
- 时间戳、日期、"撤回了一条消息" 等居中灰字也照常列出(cx 约 50)。"""


def _apply_crop(png_path: str):
    """裁掉左侧会话列表(crop_left)和底部输入框(crop_bottom)。返回 (使用路径, 需清理的临时路径或None)。"""
    left = _MODE.get("crop_left", 0.0)
    bottom = _MODE.get("crop_bottom", 0.0)
    if left <= 0 and bottom <= 0:
        return png_path, None
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            w, h = im.size
            x0 = int(w * left) if left > 0 else 0
            y1 = int(h * (1 - bottom)) if bottom > 0 else h
            if x0 >= w or y1 <= 0:
                return png_path, None
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="draftmate_crop_")
            os.close(fd)
            im.crop((x0, 0, w, y1)).save(tmp)
            return tmp, tmp
    except Exception:
        return png_path, None


def read_messages(png_path: str, model: str, last_n: int = 8) -> dict:
    use_path, tmp = _apply_crop(png_path)
    try:
        if _MODE["read_mode"] == "ocr":
            try:
                return _read_via_ocr(use_path, last_n)
            except Exception as e:
                print(f"  [OCR 读取失败,回退到视觉模型] {e}")
        data = base64.b64encode(Path(use_path).read_bytes()).decode()
        raw = llm.call_vision(model, _SYS, _prompt(last_n), data, max_tokens=1200, temperature=0.1)
        return _parse(raw)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _is_image_ph(t: str) -> bool:
    t = (t or "").strip()
    return t.startswith("〔图片〕") or t.startswith("〔表情〕")


def _y_overlap(a, b) -> bool:
    """两个 [x0,y0,x1,y1] 框是否纵向重叠。"""
    return not (a[3] < b[1] or b[3] < a[1])


def _is_ui_noise(t: str) -> bool:
    """非消息 UI 文本:"N条新消息" / "以下为新消息" / 纯符号(如 •••、···)。"""
    t = (t or "").strip()
    if not t:
        return True
    if re.fullmatch(r"\d+\s*条新消息", t) or "以下为新消息" in t:
        return True
    if not re.search(r"[A-Za-z0-9一-鿿]", t):   # 只有符号/标点,无字母数字汉字
        return True
    return False


def _extract_group_names(items: list):
    """群聊:识别"昵称小字"行并绑到后续消息;另支持"昵称:正文"前缀。
    返回 (items, is_group, title):
    - ≥2 个不同昵称 → 真群聊,应用昵称;
    - 只有 1 个且在最顶上 → 桌面版会话标题栏,当作 title 摘掉(非群聊);
    - 1v1(无此版面)→ 原样返回。"""
    if len(items) < 2:
        return items, False, None
    hs = sorted(b["box"][3] - b["box"][1] for b in items if b["box"][3] > b["box"][1])
    med_h = hs[len(hs) // 2] if hs else 24
    is_name = [False] * len(items)
    for i in range(len(items) - 1):
        a, b = items[i], items[i + 1]
        if a["sender"] != "对方" or b["sender"] != "对方":
            continue
        if (len(a["text"]) <= 12
                and (b["box"][0] - a["box"][0]) > max(8, 0.4 * med_h)   # 下一条气泡更靠右(缩进)
                and 0 <= (b["box"][1] - a["box"][3]) < 2.2 * med_h):     # 紧贴上方(同一轮)
            is_name[i] = True
    name_idx = [i for i, v in enumerate(is_name) if v]
    distinct = {items[i]["text"] for i in name_idx}
    if not name_idx:
        return items, False, None
    if len(distinct) < 2:
        # 只有一个"昵称":若就在最顶上,基本是桌面会话标题栏 → 当 title 摘掉,不算群聊
        if name_idx == [0]:
            return items[1:], False, items[0]["text"]
        return items, False, None      # 信号太弱,不当群聊也不乱改
    out, cur = [], None
    for i, it in enumerate(items):
        if is_name[i]:
            cur = it["text"]
            continue
        if it["sender"] == "对方":
            m = re.match(r"^([A-Za-z一-鿿]{1,8})[:：]\s*(.+)$", it["text"], re.DOTALL)
            if m:
                it = {**it, "sender": m.group(1), "text": m.group(2).strip()}
            elif cur:
                it = {**it, "sender": cur}
        out.append(it)
    return out, True, None


def _read_via_ocr(png_path: str, last_n: int) -> dict:
    """本地 OCR + 头像/几何判定发言人(比让模型猜更稳)。"""
    raw = process_image(png_path, _MODE["ocr_backend"], _MODE["me_side"])
    if any((m.get("text") or "").startswith("⚠️") for m in raw):
        raise RuntimeError("OCR 未读出文字")
    _, img_h = image_size(png_path)   # 截图高度,用于判定顶部标题栏

    # 顶部第一条若是居中短文本(非时间/日期)→ 当作聊天标题(手机版居中标题)
    title = None
    if raw and raw[0].get("speaker") == "system":
        t0 = (raw[0].get("text") or "").strip()
        if t0 and len(t0) <= 20 and not re.search(r"\d{1,2}:\d{2}|\d{4}|[年月日]", t0):
            title = t0
            raw = raw[1:]

    items = []
    for m in raw:
        t = (m.get("text") or "").strip()
        if not t or _is_ui_noise(t):
            continue
        items.append({
            "sender": _OCR_LABEL.get(m.get("speaker"), "unknown"),
            "text": t,
            "box": m.get("bbox") or [0, 0, 0, 0],
            "ocr": float(m.get("ocr_confidence", 1.0)),
            "spk": float(m.get("confidence", 1.0)),   # 发言人判定置信度(几何/头像)
        })

    # 桌面整窗截图(crop_left>0):最顶上、落在顶部 header 带内的短文本(非时间)= 会话名
    if not title and _MODE.get("crop_left", 0) > 0 and items:
        f = items[0]
        if (len(f["text"]) <= 20
                and not re.search(r"\d{1,2}:\d{2}|\d{4}|[年月日]", f["text"])
                and f["box"][1] < img_h * 0.15):
            title = f["text"]
            items = items[1:]

    # 群聊:识别昵称行并绑定发言人(1v1/桌面标题已先摘出,无影响)
    items, is_group, gtitle = _extract_group_names(items)
    if gtitle and not title:
        title = gtitle

    # 图片占位符 + 同发言人且纵向重叠的文字(表情包/截图上的字)→ 合并成一条
    merged: list[dict] = []
    for it in items:
        if merged:
            prev = merged[-1]
            if (prev["sender"] == it["sender"]
                    and (_is_image_ph(prev["text"]) ^ _is_image_ph(it["text"]))
                    and _y_overlap(prev["box"], it["box"])):
                img, txt = (prev, it) if _is_image_ph(prev["text"]) else (it, prev)
                ph = img["text"].split("〕", 1)[0] + "〕"   # 〔图片〕 / 〔表情〕
                prev["text"] = ph + txt["text"]
                prev["box"] = [min(prev["box"][0], it["box"][0]), min(prev["box"][1], it["box"][1]),
                               max(prev["box"][2], it["box"][2]), max(prev["box"][3], it["box"][3])]
                continue
        merged.append(dict(it))

    out = []
    for it in merged:
        # 短文本 + OCR 极不确定、且非图片占位 → 多为头像/图标误读(如 "6只""8~"),丢弃
        if len(it["text"]) <= 4 and it.get("ocr", 1.0) < 0.4 and not _is_image_ph(it["text"]):
            continue
        out.append({"sender": it["sender"], "text": it["text"],
                    "_spk": it.get("spk", 1.0), "_ocr": it.get("ocr", 1.0)})
    final = out[-last_n:]
    # 发言人判定靠几何/头像,深色/复杂排版下易错;只要采用的消息里有低置信项就提示用户核对
    low_conf = any(o["_spk"] <= 0.5 or o["_ocr"] < 0.4 for o in final)
    msgs = [{"sender": o["sender"], "text": o["text"]} for o in final]
    return {"chat_title": title, "is_group": is_group, "messages": msgs,
            "low_confidence": low_conf}


def _sender_from_cx(cx) -> str:
    """几何规则:贴右=我,贴左=对方,居中=系统;无法解析=unknown。"""
    if isinstance(cx, str):
        m = re.search(r"-?\d+(?:\.\d+)?", cx)
        cx = m.group(0) if m else None
    try:
        v = float(cx)
    except (TypeError, ValueError):
        return "unknown"
    if v >= 60:
        return "我"
    if v <= 40:
        return "对方"
    return "系统"


def _empty() -> dict:
    return {"chat_title": None, "is_group": False, "messages": []}


def _parse(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return _empty()
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return _empty()
    if not isinstance(obj, dict):
        return _empty()

    out = []
    for m in obj.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        t = (m.get("text") or "").strip()
        if not t:
            continue
        item = {"sender": _sender_from_cx(m.get("cx")), "text": t}
        if not (out and out[-1] == item):  # 去掉相邻完全重复
            out.append(item)
    return {
        "chat_title": obj.get("chat_title") or None,
        "is_group": bool(obj.get("is_group", False)),
        "messages": out,
    }
