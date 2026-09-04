#!/usr/bin/env python3
"""Render the Android launcher icons and splash artwork from one geometry source.

audioreap's mark (service/static/icons/icon.svg) is five rounded bars on a rounded
panel — simple enough that the shapes are restated here as data and rendered directly,
which is why this needs no SVG rasteriser, no Pillow and no npm package. Everything the
Android project shows is generated from the tuples below:

  mipmap-*/ic_launcher.png          legacy square icon (API 24-25)
  mipmap-*/ic_launcher_round.png    legacy round icon
  mipmap-*/ic_launcher_foreground.png   adaptive-icon foreground (API 26+)
  drawable/ic_splash_logo.xml       vector mark for the splash layer-list
  drawable/ic_notification.xml      status-bar silhouette for notifications

Run it after changing the mark:  npm run android:icons
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

RES = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "res"

# ── The mark, in the 512×512 space of service/static/icons/icon.svg ──────────
CANVAS = 512
BACKGROUND = (0x25, 0x63, 0xEB)          # --primary
PANEL = (96, 128, 320, 256, 32, (0x1D, 0x4E, 0xD8))   # x, y, w, h, radius, rgb
BARS = (
    (128, 252, 36, 104, 18, (0xFF, 0xFF, 0xFF)),
    (184, 196, 36, 160, 18, (0xFF, 0xFF, 0xFF)),
    (240, 156, 36, 200, 18, (0x93, 0xC5, 0xFD)),
    (296, 212, 36, 144, 18, (0xFF, 0xFF, 0xFF)),
    (352, 180, 36, 176, 18, (0xFF, 0xFF, 0xFF)),
)
CORNER = 112                              # the SVG's background rx
FOREGROUND_SHAPES = (PANEL, *BARS)
# Everything the foreground draws, as one box: the panel's bounds.
FOREGROUND_BOX = (PANEL[0], PANEL[1], PANEL[2], PANEL[3])
# Just the bars, as one box — what the notification silhouette is drawn from.
BARS_BOX = (
    BARS[0][0],
    min(bar[1] for bar in BARS),
    BARS[-1][0] + BARS[-1][2] - BARS[0][0],
    max(bar[1] + bar[3] for bar in BARS) - min(bar[1] for bar in BARS),
)

# Android's launcher-icon buckets. Legacy icons are 48dp, adaptive ones 108dp.
DENSITIES = (("mdpi", 1), ("hdpi", 1.5), ("xhdpi", 2), ("xxhdpi", 3), ("xxxhdpi", 4))
SUPERSAMPLE = 4   # anti-aliasing: render this many samples per axis, then average.


# ── Rasteriser ───────────────────────────────────────────────────────────────

def _rounded_rect_coverage(px: float, py: float, x: float, y: float,
                           w: float, h: float, r: float) -> bool:
    """Is the sample point inside this rounded rectangle?"""
    if px < x or py < y or px > x + w or py > y + h:
        return False
    r = min(r, w / 2, h / 2)
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def _circle_coverage(px: float, py: float, cx: float, cy: float, radius: float) -> bool:
    return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius


def _render(size: int, shapes: list, background=None, background_shape: str = "none",
            scale: float = 1.0, offset: tuple[float, float] = (0.0, 0.0)) -> bytes:
    """Render to a raw RGBA buffer.

    `shapes` are in the 512-space; `scale` and `offset` place that space inside the
    output. Colours are composited in straight (non-premultiplied) alpha, which is what
    PNG stores, and every shape here is fully opaque so the arithmetic stays exact.
    """
    step = 1.0 / SUPERSAMPLE
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    # Sample at pixel centres of the supersampled grid.
                    dx = px + (sx + 0.5) * step
                    dy = py + (sy + 0.5) * step
                    colour = None
                    if background is not None:
                        if background_shape == "rounded":
                            radius = CORNER * size / CANVAS
                            if _rounded_rect_coverage(dx, dy, 0, 0, size, size, radius):
                                colour = background
                        elif background_shape == "circle":
                            if _circle_coverage(dx, dy, size / 2, size / 2, size / 2):
                                colour = background
                    # Shape space -> pixel space.
                    ux = (dx - offset[0]) / scale
                    uy = (dy - offset[1]) / scale
                    for x, y, w, h, r, rgb in shapes:
                        if _rounded_rect_coverage(ux, uy, x, y, w, h, r):
                            colour = rgb
                    if colour is not None:
                        acc_r += colour[0]
                        acc_g += colour[1]
                        acc_b += colour[2]
                        acc_a += 255.0
            total = SUPERSAMPLE * SUPERSAMPLE
            alpha = acc_a / total
            if alpha <= 0:
                row += b"\x00\x00\x00\x00"
                continue
            # Average the covered samples only, so an edge keeps its own colour
            # instead of fading through black.
            covered = acc_a / 255.0
            row += bytes((
                round(acc_r / covered),
                round(acc_g / covered),
                round(acc_b / covered),
                round(alpha),
            ))
        rows.append(bytes(row))
    return b"".join(rows)


def _write_png(path: Path, size: int, pixels: bytes) -> None:
    raw = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"  {path.relative_to(RES.parent)}  {size}×{size}")


# ── Vector drawable ──────────────────────────────────────────────────────────

def _rounded_rect_path(x: float, y: float, w: float, h: float, r: float) -> str:
    r = min(r, w / 2, h / 2)

    def n(value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".")

    return (
        f"M{n(x + r)},{n(y)}"
        f"H{n(x + w - r)}A{n(r)},{n(r)} 0 0 1 {n(x + w)},{n(y + r)}"
        f"V{n(y + h - r)}A{n(r)},{n(r)} 0 0 1 {n(x + w - r)},{n(y + h)}"
        f"H{n(x + r)}A{n(r)},{n(r)} 0 0 1 {n(x)},{n(y + h - r)}"
        f"V{n(y + r)}A{n(r)},{n(r)} 0 0 1 {n(x + r)},{n(y)}Z"
    )


def _vector_drawable(size_dp: int, shapes: list, scale: float,
                     offset: tuple[float, float], comment: str) -> str:
    paths = []
    for x, y, w, h, r, rgb in shapes:
        data = _rounded_rect_path(
            x * scale + offset[0], y * scale + offset[1],
            w * scale, h * scale, r * scale,
        )
        colour = "#{:02X}{:02X}{:02X}".format(*rgb)
        paths.append(f'    <path\n        android:fillColor="{colour}"\n'
                     f'        android:pathData="{data}" />')
    body = "\n".join(paths)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<!-- {comment} -->\n"
        '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    android:width="{size_dp}dp"\n'
        f'    android:height="{size_dp}dp"\n'
        f'    android:viewportWidth="{size_dp}"\n'
        f'    android:viewportHeight="{size_dp}">\n'
        f"{body}\n"
        "</vector>\n"
    )


def _fit(box: tuple[float, float, float, float], canvas: float,
         fraction: float) -> tuple[float, tuple[float, float]]:
    """Scale and offset that centre `box` (in 512-space) at `fraction` of `canvas`."""
    bx, by, bw, bh = box
    scale = canvas * fraction / max(bw, bh)
    return scale, (canvas / 2 - (bx + bw / 2) * scale, canvas / 2 - (by + bh / 2) * scale)


def main() -> None:
    print("Launcher icons")
    for bucket, factor in DENSITIES:
        legacy = round(48 * factor)
        adaptive = round(108 * factor)
        mipmap = RES / f"mipmap-{bucket}"

        _write_png(mipmap / "ic_launcher.png", legacy,
                   _render(legacy, [(*s[:5], s[5]) for s in FOREGROUND_SHAPES],
                           background=BACKGROUND, background_shape="rounded",
                           scale=legacy / CANVAS, offset=(0, 0)))
        _write_png(mipmap / "ic_launcher_round.png", legacy,
                   _render(legacy, [(*s[:5], s[5]) for s in FOREGROUND_SHAPES],
                           background=BACKGROUND, background_shape="circle",
                           scale=legacy / CANVAS, offset=(0, 0)))
        # The adaptive foreground is masked and parallaxed by the launcher, so the mark
        # only occupies the inner safe zone — 66 of the 108dp canvas.
        scale, offset = _fit(FOREGROUND_BOX, adaptive, 66 / 108)
        _write_png(mipmap / "ic_launcher_foreground.png", adaptive,
                   _render(adaptive, list(FOREGROUND_SHAPES), scale=scale, offset=offset))

    print("Notification icon")
    # Android renders a notification's small icon as a silhouette: every non-transparent
    # pixel is painted in the system tint, so only the shape survives. The bars alone say
    # audioreap at 24dp; the panel behind them would flatten the whole mark into a block,
    # which is also why the launcher icon cannot serve here.
    bars_white = [(*bar[:5], (0xFF, 0xFF, 0xFF)) for bar in BARS]
    scale, offset = _fit(BARS_BOX, 24, 0.85)
    (RES / "drawable").mkdir(parents=True, exist_ok=True)
    (RES / "drawable" / "ic_notification.xml").write_text(_vector_drawable(
        24, bars_white, scale, offset,
        "Generated by scripts/generate-android-icons.py — do not edit by hand.",
    ).replace(
        'android:viewportHeight="24">',
        'android:viewportHeight="24"\n    android:tint="#FFFFFFFF">',
    ), encoding="utf-8")
    print("  res/drawable/ic_notification.xml")

    print("Splash mark")
    scale, offset = _fit(FOREGROUND_BOX, 160, 1.0)
    (RES / "drawable").mkdir(parents=True, exist_ok=True)
    (RES / "drawable" / "ic_splash_logo.xml").write_text(_vector_drawable(
        160, list(FOREGROUND_SHAPES), scale, offset,
        "Generated by scripts/generate-android-icons.py — do not edit by hand.",
    ), encoding="utf-8")
    print("  res/drawable/ic_splash_logo.xml")


if __name__ == "__main__":
    main()
