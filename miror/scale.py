"""Fit a device size inside a 1080p box without changing the aspect ratio."""

from __future__ import annotations

import math


def iround(value: float) -> int:
    return int(math.floor(value + 0.5))


def align8(value: int) -> int:
    if value < 16:
        value = 16
    return value & ~7


def fit_1080p(
    src_w: int,
    src_h: int,
    max_long: int = 1920,
    max_short: int = 1080,
) -> tuple[int, int]:
    """Return even encoder dimensions. The long side stays <= 1920 and the short side <= 1080."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"invalid size {src_w}x{src_h}")
    long_side = max(src_w, src_h)
    short_side = min(src_w, src_h)
    scale = 1.0
    if long_side > max_long:
        scale = min(scale, max_long / long_side)
    if short_side > max_short:
        scale = min(scale, max_short / short_side)
    width = align8(iround(src_w * scale))
    height = align8(iround(src_h * scale))
    while width >= 16 and height >= 16 and (
        max(width, height) > max_long or min(width, height) > max_short
    ):
        if width >= height:
            width -= 8
        else:
            height -= 8
    return max(16, width), max(16, height)


def clean_half(src_w: int, src_h: int) -> tuple[int, int]:
    """Nearest 8-pixel size to exactly half. Matches Agent.cleanHalf."""
    width = (src_w // 2 + 4) & ~7
    height = (src_h // 2 + 4) & ~7
    return max(16, width), max(16, height)


def light_bitrate(
    width: int,
    height: int,
    fps: int,
    base_width: int = 424,
    base_height: int = 960,
) -> int:
    """Bits for one intra picture, scaled from the clear 424×960 / 4 Mbps / 30 fps picture."""
    fps = 30 if int(fps) <= 30 else 60
    base = base_width * base_height
    pixels = width * height
    if base < 1:
        base = pixels
    return (4_000_000 * fps * pixels) // (30 * base)


def present_box(src_w: int, src_h: int, draw_w: int, draw_h: int) -> tuple[int, int]:
    """Pixels to color for a frame whose long side is above 1400.

    424 and 544 return (0, 0) and keep the smooth window scale. The window then
    blits this box 1:1. A box from the other orientation is rejected so one
    rotated frame is not squashed. Never upscale.
    """

    if src_w <= 0 or src_h <= 0 or max(src_w, src_h) <= 1400:
        return 0, 0
    if draw_w < 160 or draw_h < 160:
        return 0, 0
    if draw_w > src_w or draw_h > src_h:
        return 0, 0
    if draw_w == src_w and draw_h == src_h:
        return 0, 0
    if abs((src_w / src_h) - (draw_w / draw_h)) > 0.05:
        return 0, 0
    return int(draw_w), int(draw_h)


def bitrate_for(width: int, height: int) -> int:
    ratio = (width * height) / (1920 * 1080)
    bitrate = iround(12_000_000 * ratio)
    return max(2_000_000, min(16_000_000, bitrate))
