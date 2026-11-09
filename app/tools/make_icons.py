"""アプリアイコン（web/icon-180.png, web/icon-512.png）を生成する開発用スクリプト。

実行には Pillow が必要（利用者が実行する必要はない。生成済み PNG を同梱する）:
    python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "web"
SIZE = 1024  # 高解像度で描いて縮小する


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def draw_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 背景: 角丸の縦グラデーション（青 → 水色）
    top, bottom = (37, 99, 235), (14, 165, 233)
    gradient = Image.new("RGBA", (SIZE, SIZE))
    gdraw = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        gdraw.line([(0, y), (SIZE, y)], fill=lerp(top, bottom, y / SIZE) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=224, fill=255)
    image.paste(gradient, (0, 0), mask)

    # クリップボード本体（白）とクリップ（濃紺）
    draw = ImageDraw.Draw(image)
    body = [232, 232, 792, 832]
    draw.rounded_rectangle(body, radius=72, fill=(255, 255, 255, 255))
    draw.rounded_rectangle([392, 176, 632, 300], radius=48, fill=(15, 40, 92, 255))
    draw.rounded_rectangle([452, 150, 572, 230], radius=36, fill=(15, 40, 92, 255))

    # 双方向の矢印（iPhone ↔ PC）
    blue = (37, 99, 235, 255)
    teal = (13, 148, 136, 255)
    # 上向き矢印（左）
    draw.polygon([(392, 486), (480, 376), (568, 486), (516, 486), (516, 700), (444, 700), (444, 486)], fill=blue)
    # 下向き矢印（右）
    draw.polygon([(632, 590), (544, 700), (456, 590), (508, 590), (508, 376), (580, 376), (580, 590)], fill=teal)
    return image


def main() -> None:
    icon = draw_icon()
    for size in (512, 180):
        icon.resize((size, size), Image.LANCZOS).convert("RGB").save(WEB / f"icon-{size}.png", optimize=True)
        print(WEB / f"icon-{size}.png")


if __name__ == "__main__":
    main()
