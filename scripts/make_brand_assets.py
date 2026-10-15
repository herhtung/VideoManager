"""Generate the application mark in PNG and Windows ICO formats."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    side = 512
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 500, 500), radius=112, fill="#14232d")
    draw.rounded_rectangle((12, 12, 500, 500), radius=112, outline="#3b656c", width=8)
    draw.rounded_rectangle((79, 105, 362, 317), radius=32, fill="#224650", outline="#5fb5b1", width=12)
    draw.rounded_rectangle((133, 159, 431, 382), radius=34, fill="#e8f4f0")
    draw.rounded_rectangle((151, 177, 413, 364), radius=24, fill="#19313a")
    draw.polygon(((234, 211), (234, 331), (337, 271)), fill="#70ded0")
    for x in (169, 214, 259):
        draw.rounded_rectangle((x, 409, x + 27, 424), radius=7, fill="#e9aa72")
    output = Path(__file__).resolve().parent.parent / "assets"
    output.mkdir(exist_ok=True)
    image.save(output / "videomanager.png")
    image.save(output / "videomanager.ico", format="ICO",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
