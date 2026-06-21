"""OCR 几何健壮性测试台 —— 验证发言人判定/头像检测在不同「渲染尺度 × 明暗主题」下不漂。

不依赖真实 OCR:monkeypatch vision.run_ocr 返回与合成图气泡位置匹配的行,
detect_avatars 仍在真实合成图上跑(图里画了头像方块)。这样能确定性地端到端测几何,
覆盖真机难穷举的尺度/主题组合。真机精度仍需另用真实截图复核。

跑: python tools/ocr_geometry_harness.py
"""
from __future__ import annotations
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont
import numpy as np
import vision

FONT = r"C:\Windows\Fonts\msyh.ttc"


def _font(px):
    try:
        return ImageFont.truetype(FONT, px)
    except Exception:
        return ImageFont.load_default()


def gen_chat(font_px, dark, with_image=False):
    """生成一张聊天图 + 与之匹配的合成 OCR 行。返回 (png, lines, expected_speakers)。
    font_px 模拟不同 DPI/缩放下的像素尺度;dark 模拟深色主题。
    布局:对方在左(有左头像)、我在右(有右头像)、中间一条系统时间戳。"""
    f = _font(font_px)
    line_h = font_px + 10
    av = int(font_px * 2.2)                      # 头像 ~2.2 行高(模拟固定 UI 元素)
    pad = int(font_px * 0.6)
    W = max(560, int(font_px * 18))
    bg = (28, 28, 30) if dark else (237, 237, 237)
    bub_other = (52, 52, 56) if dark else (255, 255, 255)
    bub_me = (40, 110, 70) if dark else (149, 236, 105)
    fg = (228, 228, 228) if dark else (20, 20, 20)
    rng = np.random.RandomState(7)

    rows = [("对方", "在吗在吗"), ("对方", "晚上一起吃饭吗"),
            ("我", "好呀几点见"), ("对方", "六点老地方")]
    if with_image:
        rows.append(("对方", "<IMG>"))           # 图片消息:画图块、无文字

    # 先估算高度
    H = pad * 2 + line_h + len(rows) * (av + pad) + pad
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    def avatar(cx, cy):
        # 彩色 + 有纹理的方块(明暗主题都能被 chroma 或 texture 命中)
        block = rng.randint(40, 220, size=(av, av, 3)).astype("uint8")
        block[:, :, 0] = np.clip(block[:, :, 0].astype(int) + 40, 0, 255)   # 偏色,确保 chroma
        img.paste(Image.fromarray(block), (int(cx - av / 2), int(cy - av / 2)))

    lines, expected = [], []
    y = pad
    # 系统时间戳(居中)
    ts = "今天晚上"
    tb = d.textbbox((0, 0), ts, font=f); tw = tb[2] - tb[0]
    d.text(((W - tw) // 2, y), ts, font=f, fill=(150, 150, 150))
    lines.append(vision._line(ts, (W - tw) // 2, y, (W + tw) // 2, y + line_h, 0.9))
    expected.append("系统")
    y += line_h + pad

    for sender, text in rows:
        me = (sender == "我")
        ax = W - int(av * 0.7) if me else int(av * 0.7)      # 头像在边缘
        avatar(ax, y + av / 2)
        if text == "<IMG>":                                  # 图片消息:画个彩色图块,无文字
            iw, ih = int(W * 0.34), int(av * 1.6)
            ix = (ax - pad - iw) if me else (ax + pad)
            block = rng.randint(30, 230, size=(ih, iw, 3)).astype("uint8")
            img.paste(Image.fromarray(block), (int(ix), int(y)))
            expected.append("我" if me else "对方")          # 期望被判为图片消息、对应发言人
        else:
            tb = d.textbbox((0, 0), text, font=f); tw, th = tb[2] - tb[0], tb[3] - tb[1]
            bw, bh = tw + pad * 2, th + pad * 2
            bx = (ax - int(av * 0.7) - bw) if me else (ax + int(av * 0.7))
            d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=12, fill=bub_me if me else bub_other)
            d.text((bx + pad, y + pad), text, font=f, fill=fg)
            lines.append(vision._line(text, bx + pad, y + pad, bx + pad + tw, y + pad + th, 0.95))
            expected.append("我" if me else "对方")
        y += av + pad

    fd, p = tempfile.mkstemp(suffix=".png", prefix="harness_"); os.close(fd)
    img.save(p)
    return p, lines, expected


def run_case(font_px, dark, with_image=False):
    p, lines, expected = gen_chat(font_px, dark, with_image)
    try:
        n_av = len(vision.detect_avatars(p, *vision.image_size(p)))   # 头像检测(真图)
        orig = vision.run_ocr
        vision.run_ocr = lambda path, b, W, H: list(lines)            # 注入合成行
        try:
            msgs = vision.process_image(p, "auto", me_side="right", crop_top=0.0, crop_bottom=0.0)
        finally:
            vision.run_ocr = orig
    finally:
        os.remove(p)
    label = {"me": "我", "other": "对方", "system": "系统", "unknown": "unknown"}
    got = []
    for m in msgs:
        s = label.get(m.get("speaker"), m.get("speaker"))
        if "〔图片〕" in (m.get("text") or "") or "〔表情〕" in (m.get("text") or ""):
            s += "/图"
        got.append(s)
    exp = list(expected)
    if with_image:                                # 图片行期望被识别为「图片消息」
        exp[-1] += "/图"
    return exp, got, n_av


def main():
    matrix = [(px, dark) for px in (22, 34, 52) for dark in (False, True)]
    allok = True
    print(f"{'scale':>6} {'theme':>6} {'avatars':>8} {'res':>5}  speakers(/图=图片消息)")
    for px, dark in matrix:
        exp, got, n_av = run_case(px, dark, with_image=True)
        spk_ok = (exp == got)
        av_ok = (n_av >= 3)                        # 画了 4 个头像,允许漏 1
        ok = spk_ok and av_ok
        allok &= ok
        print(f"{px:>6} {'dark' if dark else 'light':>6} {n_av:>8} {'OK' if ok else 'FAIL':>5}  "
              f"got={got}")
        if not ok:
            print(f"{'':>22}    exp={exp}  (avatars>=3? {av_ok}, speakers? {spk_ok})")
    print("\n=== 全矩阵", "PASS ✅" if allok else "FAIL ❌",
          "(要求:发言人全对 + 头像检到≥3 + 图片消息识别,跨 3 尺度 × 明暗)===")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
