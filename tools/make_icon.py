"""Рисует значок приложения (assets/icon.ico): несколько цветных блоков, как на карте. Нужен Pillow."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
RADIUS = 30
GAP = 10
PADDING = 14
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# Блоки в долях от рабочей области: (x0, y0, x1, y1, цвет) — цвета те же, что у карты.
BLOCKS = [
    (0.00, 0.00, 0.58, 0.62, "#2a78d6"),
    (0.58, 0.00, 1.00, 0.38, "#1baf7a"),
    (0.58, 0.38, 1.00, 0.62, "#eda100"),
    (0.00, 0.62, 0.34, 1.00, "#eb6834"),
    (0.34, 0.62, 0.72, 1.00, "#6250d6"),
    (0.72, 0.62, 1.00, 1.00, "#e34948"),
]
OUTPUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def draw_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    area = SIZE - 2 * PADDING
    for x0, y0, x1, y1, color in BLOCKS:
        box = (
            PADDING + x0 * area + GAP / 2,
            PADDING + y0 * area + GAP / 2,
            PADDING + x1 * area - GAP / 2,
            PADDING + y1 * area - GAP / 2,
        )
        draw.rounded_rectangle(box, radius=RADIUS / 2, fill=color)
    return image


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    draw_icon().save(OUTPUT, sizes=ICON_SIZES)
