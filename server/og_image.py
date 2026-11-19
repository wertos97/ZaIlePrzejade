"""PNG Open Graph previews for social embeds.

Social crawlers do not render SVG, so route previews are rasterised with
Pillow. Fonts are bundled (DejaVu, see DejaVu-LICENSE.txt); the handler
falls back to the older SVG renderer when Pillow or the fonts are missing.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from pathlib import Path

WIDTH = 1200
HEIGHT = 630

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_FONT_REGULAR = _ASSET_DIR / "DejaVuSans.ttf"
_FONT_BOLD = _ASSET_DIR / "DejaVuSans-Bold.ttf"

_BG_TOP = (13, 33, 55)
_BG_BOTTOM = (26, 58, 82)
_ACCENT = (93, 173, 226)
_HEADING = (236, 240, 241)
_SUBTITLE = (189, 195, 199)
_MUTED = (127, 140, 141)
_WHITE = (255, 255, 255)
_BUS_BODY = (40, 116, 166)
_BUS_WINDOW = (174, 214, 241)
_BUS_WHEEL = (36, 31, 49)
_BUS_RED = (255, 85, 85)
_BUS_YELLOW = (255, 221, 85)

# Native 1200x630 is enough for a social card; 2x supersampling costs ~60%
# more CPU on the single-core VPS without a visible gain at display size.
_SCALE = 1

_PNG_CACHE_MAX = 48
_png_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_cache_lock = threading.Lock()

_font_cache = {}
_font_lock = threading.Lock()
_render_lock = threading.Lock()


def _get_font(size: int, bold: bool):
    key = (size, bold)
    with _font_lock:
        font = _font_cache.get(key)
        if font is None:
            from PIL import ImageFont
            path = _FONT_BOLD if bold else _FONT_REGULAR
            font = ImageFont.truetype(str(path), size)
            _font_cache[key] = font
        return font


def _scaled(value: float) -> int:
    return int(round(value * _SCALE))


def _background():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (WIDTH * _SCALE, HEIGHT * _SCALE))
    draw = ImageDraw.Draw(img)
    height = HEIGHT * _SCALE
    for y in range(height):
        t = y / (height - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(_BG_TOP, _BG_BOTTOM))
        draw.line((0, y, WIDTH * _SCALE, y), fill=color)
    return img, draw


def _fit_font(draw, text: str, bold: bool, max_width: float,
              start_size: int, min_size: int):
    for size in range(start_size, min_size - 1, -1):
        font = _get_font(size * _SCALE, bold)
        if draw.textlength(text, font=font) <= max_width * _SCALE:
            return font
    return _get_font(min_size * _SCALE, bold)


def _text(draw, x: float, y: float, text: str, font, fill, anchor: str = "ls"):
    draw.text((_scaled(x), _scaled(y)), text, font=font, fill=fill, anchor=anchor)


def _draw_logo(draw, x: float = 930, y: float = 400, scale: float = 2.5):
    def bx(value):
        return _scaled(x + value * scale)

    def by(value):
        return _scaled(y + value * scale)

    draw.rounded_rectangle(
        (bx(5), by(10), bx(60), by(45)),
        radius=_scaled(5 * scale), fill=_BUS_BODY)
    for left in (10, 26, 42):
        draw.rounded_rectangle(
            (bx(left), by(15), bx(left + 13), by(28)),
            radius=_scaled(2.1667 * scale), fill=_BUS_WINDOW)
    draw.ellipse(
        (bx(10), by(37), bx(23), by(50)), fill=_BUS_WHEEL)
    draw.ellipse(
        (bx(42), by(37), bx(55), by(50)), fill=_BUS_WHEEL)
    draw.ellipse(
        (bx(6), by(32), bx(9), by(35)), fill=_BUS_RED)
    draw.ellipse(
        (bx(56), by(32), bx(59), by(35)), fill=_BUS_YELLOW)


def _domain(draw):
    font = _get_font(32 * _SCALE, bold=True)
    _text(draw, 1010, 577, "zaileprzeja.de", font, _MUTED, anchor="ms")


def _finish(img) -> bytes:
    from PIL import Image
    if _SCALE != 1:
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def truncate_name(name: str, max_chars: int = 26) -> str:
    if len(name) <= max_chars:
        return name
    cut = name[:max_chars - 3]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.5:
        cut = cut[:last_space]
    return cut.rstrip() + "..."


def _render_route(from_name: str, to_name: str, cost_text: str) -> bytes:
    from_name = truncate_name(from_name)
    to_name = truncate_name(to_name)

    img, draw = _background()

    label = _get_font(32 * _SCALE, bold=True)
    _text(draw, 80, 77, "SKĄD", label, _ACCENT)

    name_font = _fit_font(draw, from_name, True, 820, 55, 34)
    _text(draw, 80, 145, from_name, name_font, _HEADING)

    _text(draw, 80, 230, "DOKĄD", label, _ACCENT)
    name_font = _fit_font(draw, to_name, True, 820, 55, 34)
    _text(draw, 80, 298, to_name, name_font, _HEADING)

    draw.line(
        (_scaled(80), _scaled(346), _scaled(240), _scaled(346)),
        fill=_ACCENT, width=_scaled(4))

    _text(draw, 80, 431, "CENA BILETU", label, _ACCENT)

    if cost_text:
        price_font = _fit_font(draw, cost_text, True, 760, 120, 64)
        _text(draw, 80, 548, cost_text, price_font, _WHITE)
    else:
        price_font = _fit_font(draw, "Sprawdź cenę", True, 760, 96, 64)
        _text(draw, 80, 548, "Sprawdź cenę", price_font, _WHITE)

    _draw_logo(draw)
    _domain(draw)
    return _finish(img)


def render_route_png(from_name: str, to_name: str, cost_text: str) -> bytes:
    """Render a route card. Results are cached in memory; the handler
    rate-limits this endpoint, so the cache only smooths repeated shares."""
    key = (from_name, to_name, cost_text)
    with _cache_lock:
        cached = _png_cache.get(key)
        if cached is not None:
            _png_cache.move_to_end(key)
            return cached

    with _render_lock:
        body = _render_route(from_name, to_name, cost_text)

    with _cache_lock:
        _png_cache[key] = body
        _png_cache.move_to_end(key)
        while len(_png_cache) > _PNG_CACHE_MAX:
            _png_cache.popitem(last=False)
    return body


def render_default_png() -> bytes:
    img, draw = _background()
    label = _get_font(32 * _SCALE, bold=True)
    heading = _get_font(55 * _SCALE, bold=True)
    subtitle = _get_font(46 * _SCALE, bold=False)

    _text(draw, 80, 77, "ZA ILE PRZEJADĘ?", label, _ACCENT)
    _text(draw, 80, 155, "Kalkulator cen biletów", heading, _HEADING)
    _text(draw, 80, 225, "MPK Kraków 2027", heading, _HEADING)
    draw.line(
        (_scaled(80), _scaled(270), _scaled(240), _scaled(270)),
        fill=_ACCENT, width=_scaled(4))
    _text(draw, 80, 340, "Oblicz koszt przejazdu", subtitle, _SUBTITLE)
    _text(draw, 80, 395, "w nowym systemie biletów", subtitle, _SUBTITLE)

    _draw_logo(draw)
    _domain(draw)
    return _finish(img)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate the static OG image")
    parser.add_argument("--default", metavar="PATH", required=True)
    args = parser.parse_args()
    with open(args.default, "wb") as fh:
        fh.write(render_default_png())
