"""生成应用图标 resources/app.png / app.ico（纯 Pillow 绘制，无外部素材、无网络）。

图形含义：蓝底圆角方块上是一排白色声波柱（中间高、两边低），
配色与站内其它工具一致——一眼看出这是「跟声音有关」的工具。

重新生成：
    python tools/make_icon.py
打包时把 resources/app.ico 设为 exe 图标即可。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "resources"
RES.mkdir(exist_ok=True)
S = 512


def rounded_mask(box: tuple[int, int, int, int], radius: int) -> Image.Image:
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def build() -> Image.Image:
    # 1) 蓝色渐变圆角底（与站内其它蓝色系图标同一套色）
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    top, bottom = (59, 130, 255), (24, 52, 148)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    grad.putalpha(rounded_mask((12, 12, S - 12, S - 12), 112))

    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.alpha_composite(grad)
    d = ImageDraw.Draw(canvas)

    # 2) 五根声波柱：中间高、两边低，圆头更柔和，小尺寸下也不糊成一团
    cx, cy = S // 2, S // 2
    bar_w, gap = 38, 20
    heights = [86, 158, 226, 158, 86]
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = cx - total // 2
    for i, h in enumerate(heights):
        # 正中间那根用亮白，其余稍暗，层次感更清楚
        color = (255, 255, 255, 255) if i == 2 else (214, 232, 255, 235)
        d.rounded_rectangle((x, cy - h // 2, x + bar_w, cy + h // 2),
                            radius=bar_w // 2, fill=color)
        x += bar_w + gap

    # 3) 底部一条细弧线，暗示「转换后落地成文件」
    d.arc((cx - 150, cy + 120, cx + 150, cy + 250), start=20, end=160,
          fill=(255, 255, 255, 200), width=16)
    return canvas


def main() -> None:
    icon = build()
    icon.save(RES / "app.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    icon.save(RES / "app.ico", sizes=sizes)
    print("已生成：", RES / "app.png")
    print("已生成：", RES / "app.ico", "（含 16–256 共 7 档）")


if __name__ == "__main__":
    main()
