"""Animated branded cards for the Telegram channel (scripts/telegram.py).

A 3-second 1280x720 loop in the store's colours (design/system/tokens.json): the Accretion logo and name
come in, then the app's icon, name, version and a badge (a release), or the day's new icons (the digest),
then everything fades back to the background so the loop restarts cleanly. Frames are drawn with Pillow
and encoded as an H.264 MP4 with imageio-ffmpeg's bundled ffmpeg: Telegram turns GIFs into silent MP4s
anyway, and an MP4 keeps the gradients without GIF banding at a fraction of the size. Sent with
sendAnimation, it plays and loops like a GIF.
"""

import io
import math
import os
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.join(os.path.dirname(__file__), "..")
FONTS = os.path.join(ROOT, "design", "system", "fonts")
LOGO = os.path.join(ROOT, "design", "system", "assets", "Logos", "accretion-icon.png")

W, H = 1280, 720
FPS = 30
SECONDS = 3.0
FRAMES = int(FPS * SECONDS)

# design/system/tokens.json, dark theme
BG = (7, 7, 13)
FG = (236, 234, 255)
MUTED = (139, 135, 179)
LINE = (38, 33, 69)
ACCENT = (139, 92, 246)
ACCENT_2 = (34, 211, 238)
FILL = (124, 77, 238)
FILL_END = (58, 111, 230)


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def ease_out(t):
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def ease_back(t):
    """Ease out with a small overshoot (icons pop in)."""
    t = min(max(t, 0.0), 1.0)
    c = 1.70158
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def phase(t, start, length):
    return (t - start) / length


def fetch_image(url):
    """An image from the web, or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Accretion store build"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGBA")
    except Exception as exc:  # any bad or missing icon just leaves the slot empty
        print(f"card: icon not loaded ({exc})")
        return None


def rounded(img, size, radius_ratio=0.225):
    """`img` scaled to a square `size` with iOS-like rounded corners (mask drawn at 4x for smooth edges)."""
    img = img.resize((size, size), Image.LANCZOS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * radius_ratio), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def background():
    """Dark background with the store's faint grid and two soft glows (violet top-left, cyan bottom-right)."""
    bg = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(bg)
    for x in range(0, W, 48):
        draw.line((x, 0, x, H), fill=(14, 14, 22))
    for y in range(0, H, 48):
        draw.line((0, y, W, y), fill=(14, 14, 22))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-260, -320, 620, 420), fill=(54, 32, 110))
    g.ellipse((780, 380, 1560, 1060), fill=(10, 70, 92))
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    return ImageChops.add(bg, glow)  # the glows light the background


def gradient_pill(text, fnt, pad_x=22, pad_y=10):
    """A badge: white text on the accent gradient, fully rounded."""
    left, top, right, bottom = fnt.getbbox(text)
    w, h = right - left + pad_x * 2, bottom - top + pad_y * 2
    scale = 4
    grad = Image.new("RGB", (w * scale, h * scale))
    gd = ImageDraw.Draw(grad)
    for x in range(w * scale):
        k = x / max(w * scale - 1, 1)
        gd.line(
            (x, 0, x, h * scale), fill=tuple(int(FILL[i] + (FILL_END[i] - FILL[i]) * k) for i in range(3))
        )
    mask = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1), radius=h * scale // 2, fill=255
    )
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pill.paste(grad.resize((w, h), Image.LANCZOS), (0, 0), mask.resize((w, h), Image.LANCZOS))
    ImageDraw.Draw(pill).text((pad_x - left, pad_y - top), text, font=fnt, fill=(255, 255, 255))
    return pill


def with_alpha(img, alpha):
    if alpha >= 1:
        return img
    out = img.copy()
    a = out.getchannel("A").point(lambda v: int(v * max(alpha, 0)))
    out.putalpha(a)
    return out


def text_layer(text, fnt, color):
    left, top, right, bottom = fnt.getbbox(text)
    layer = Image.new("RGBA", (right - left + 4, bottom - top + 8), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((2 - left, 4 - top), text, font=fnt, fill=color)
    return layer


def fit_text(text, name, size, max_width, min_size=34):
    """The largest font size (down to min_size) that fits `max_width`; cut with with … if still too wide."""
    while size > min_size and font(name, size).getlength(text) > max_width:
        size -= 2
    fnt = font(name, size)
    while fnt.getlength(text) > max_width and len(text) > 1:
        text = text[:-2].rstrip() + "…"
    return text, fnt


def paste(frame, layer, x, y, alpha=1.0, scale=1.0):
    if alpha <= 0:
        return
    if scale != 1.0:
        w, h = layer.size
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        layer = layer.resize((sw, sh), Image.LANCZOS)
        x, y = x + (w - sw) // 2, y + (h - sh) // 2
    frame.alpha_composite(with_alpha(layer, alpha), (int(x), int(y)))


class Card:
    """Shared pieces: background, the Accretion label (logo + name) and the site address."""

    def __init__(self):
        self.bg = background().convert("RGBA")
        logo = Image.open(LOGO).convert("RGBA")
        self.logo = rounded(logo, 76)
        self.brand = text_layer("Accretion", font("Sora-Bold.ttf", 40), FG)
        self.site = text_layer("getaccretion.app", font("JetBrainsMono-Medium.ttf", 24), MUTED)

    def base(self, t):
        """Background plus the label, which slides in from the left during the first 0.6 s."""
        frame = self.bg.copy()
        k = ease_out(phase(t, 0.0, 0.6))
        paste(frame, self.logo, 64 - 40 * (1 - k), 56, k)
        paste(frame, self.brand, 156 - 40 * (1 - k), 74, k)
        paste(frame, self.site, W - 64 - self.site.width, H - 64, ease_out(phase(t, 0.4, 0.6)))
        return frame


def fade_out(frame, bg, t):
    """The last 0.35 s fade to the plain background so the loop restarts without a jump."""
    k = phase(t, SECONDS - 0.35, 0.35)
    if k <= 0:
        return frame
    return Image.blend(frame, bg, min(k, 1.0))


def release_frames(icon, name, version, badge):
    card = Card()
    icon_layer = rounded(icon, 300) if icon is not None else None
    name_text, name_font = fit_text(name, "Sora-Bold.ttf", 76, W - 520 - 80)
    name_layer = text_layer(name_text, name_font, FG)
    version_layer = text_layer(f"v{version}", font("JetBrainsMono-Medium.ttf", 34), MUTED)
    pill = gradient_pill(badge, font("JetBrainsMono-Medium.ttf", 24))
    ring = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((10, 10, 350, 350), fill=(*ACCENT, 90))
    ring = ring.filter(ImageFilter.GaussianBlur(40))
    for n in range(FRAMES):
        t = n / FPS
        frame = card.base(t)
        if icon_layer is not None:
            pulse = 0.75 + 0.25 * math.sin(t * 2.2)
            paste(frame, ring, 150, 150 + 40, ease_out(phase(t, 0.35, 0.7)) * pulse)
            paste(
                frame,
                icon_layer,
                180,
                220,
                ease_out(phase(t, 0.3, 0.5)),
                0.6 + 0.4 * ease_back(phase(t, 0.3, 0.7)),
            )
        x = 540
        k = ease_out(phase(t, 0.7, 0.6))
        paste(frame, name_layer, x, 250 + 30 * (1 - k), k)
        k = ease_out(phase(t, 0.9, 0.6))
        paste(frame, version_layer, x, 350 + 30 * (1 - k), k)
        paste(frame, pill, x, 420, ease_out(phase(t, 1.1, 0.4)), 0.7 + 0.3 * ease_back(phase(t, 1.1, 0.6)))
        yield fade_out(frame, card.bg, t).convert("RGB")


def digest_frames(icons, title, subtitle):
    card = Card()
    title_layer = text_layer(title, font("Sora-Bold.ttf", 72), FG)
    sub_layer = text_layer(subtitle, font("JetBrainsMono-Medium.ttf", 32), MUTED)
    tiles = [rounded(i, 150) for i in icons[:6] if i is not None]
    gap = 34
    row_w = len(tiles) * 150 + max(len(tiles) - 1, 0) * gap
    x0 = (W - row_w) // 2
    for n in range(FRAMES):
        t = n / FPS
        frame = card.base(t)
        k = ease_out(phase(t, 0.3, 0.6))
        paste(frame, title_layer, (W - title_layer.width) // 2, 190 + 30 * (1 - k), k)
        k = ease_out(phase(t, 0.5, 0.6))
        paste(frame, sub_layer, (W - sub_layer.width) // 2, 290 + 20 * (1 - k), k)
        for i, tile in enumerate(tiles):
            start = 0.8 + 0.12 * i
            paste(
                frame,
                tile,
                x0 + i * (150 + gap),
                400,
                ease_out(phase(t, start, 0.4)),
                0.5 + 0.5 * ease_back(phase(t, start, 0.6)),
            )
        yield fade_out(frame, card.bg, t).convert("RGB")


def encode(frames):
    """H.264 MP4 bytes (yuv420p, faststart) from RGB frames, through imageio-ffmpeg's bundled ffmpeg."""
    import tempfile

    import imageio_ffmpeg

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "card.mp4")
        writer = imageio_ffmpeg.write_frames(
            out,
            (W, H),
            fps=FPS,
            codec="libx264",
            pix_fmt_out="yuv420p",
            output_params=["-crf", "18", "-preset", "slow", "-movflags", "+faststart", "-an"],
            macro_block_size=16,
        )
        writer.send(None)
        for frame in frames:
            writer.send(frame.tobytes())
        writer.close()
        with open(out, "rb") as f:
            return f.read()


def release_card(app, badge):
    """The MP4 for one of our releases."""
    return encode(release_frames(fetch_image(app.get("iconURL", "")), app["name"], app["version"], badge))


def digest_card(apps, day):
    """The MP4 heading the daily digest: the day's count and up to six of its icons."""
    icons = [fetch_image(a.get("iconURL", "")) for a in apps[:6]]
    count = len(apps)
    return encode(
        digest_frames(icons, "New on Accretion", f"{count} new app{'s' if count != 1 else ''} · {day}")
    )
