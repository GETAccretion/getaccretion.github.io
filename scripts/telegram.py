"""Posts the store's news to the Telegram channel (publish.yml, after the site build).

- A release of one of Accretion's own apps (the app, mods): one post with an animated branded card
  (scripts/telegram_card.py: the Accretion label, the icon, name, version and a badge) and a caption with
  the version and What's new, linking to the app page; a mod also lists its cheats (the "•" lines of its
  description, under their headings; "Mod menu" for mods that are not games). Each version is posted once.
- Once a day: the catalogue apps first listed the day before, cracked, unlocked, modded and NSFW apps
  left out (channels get taken down over those): an animated card with the count and the first icons, then
  at most DIGEST_LIMIT names with links.
When a card can't be made or sent, the post goes as before (the icon as a photo, or text).

State lives in telegram.json (committed by publish.yml): the last posted version per own app and the
day of the last digest. With no state file the first run only records the current versions, so the
channel never gets the whole back catalogue. TELEGRAM_BOT_TOKEN (secret) and TELEGRAM_CHAT (variable,
@channelname or the numeric id) switch it on; without them it prints what it would post and changes
nothing. A failed post is retried on the next build and never fails this one.
"""

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

from common import load_json, nsfw_icon_keys, path

try:
    import telegram_card
except ImportError:  # Pillow or imageio-ffmpeg missing: posts go without the animated card
    telegram_card = None

SITE = "https://getaccretion.app"
STATE = "telegram.json"
# Same list as OWN_PREFIXES in scripts/site.py and site/app.js
OWN_PREFIXES = ("io.github.getaccretion.", "io.github.accretionmods.")
MOD_PREFIX = "io.github.accretionmods."
DIGEST_LIMIT = 10
NOTES_LIMIT = 600  # characters of What's new in a release post
CAPTION_LIMIT = 1024  # Telegram's photo caption limit; a longer post (a mod with many cheats) goes as text
TEXT_LIMIT = 4096  # Telegram's message limit
RISKY_WORDS = re.compile(
    r"crack|unlock|premium|hack|\+\+|\bmod(s|ded)?\b|ttjb|\bvip\b|patched|no ads|jailbreak|nsfw|18\+",
    re.IGNORECASE,
)
RISKY_TAGS = {"unlocked", "premium", "pro", "mod", "ttjb", "subscription", "cracked", "hack", "nsfw"}


def is_own(bid):
    return bid.startswith(OWN_PREFIXES)


def app_url(bid):
    return f"{SITE}/app/{bid}/"


def icon_key(url):
    # icons.py hosts icons at /icons/<key>.jpg; nsfw_icon_keys() lists those keys
    match = re.search(r"/icons/([^/]+)\.jpg$", url or "")
    return match.group(1) if match else None


def cheat_list(app):
    """A mod's cheats as [(heading, [cheat, ...])], read from its description: the "•" or "-" lines, grouped
    under the line ending in ":" above them (e.g. "Work on any phone, no JIT needed:"). Not a mod -> []."""
    if not app.get("bundleIdentifier", "").startswith(MOD_PREFIX):
        return []
    groups, heading = [], None
    for raw in (app.get("localizedDescription") or "").splitlines():
        line = raw.strip()
        if line.startswith(("•", "- ", "* ")):
            item = line.lstrip("•-* ").strip()
            if not item:
                continue
            if not groups or groups[-1][0] != heading:
                groups.append((heading, []))
            groups[-1][1].append(item)
        elif line.endswith(":"):
            heading = line[:-1].strip()
        elif line:
            heading = None
    return groups


def release_caption(app):
    name = html.escape(app.get("name") or app["bundleIdentifier"])
    version = html.escape(str(app.get("version") or ""))
    versions = app.get("versions") or [{}]
    notes = (versions[0].get("localizedDescription") or "").strip()
    if len(notes) > NOTES_LIMIT:
        notes = notes[:NOTES_LIMIT].rsplit(" ", 1)[0] + "..."
    head = [f"<b>{name} {version}</b>"]
    if app.get("subtitle"):
        head.append(html.escape(app["subtitle"]))
    tail = (["", html.escape(notes)] if notes else []) + [
        "",
        f'<a href="{app_url(app["bundleIdentifier"])}">Get it on Accretion</a>',
    ]
    groups = cheat_list(app)
    count = sum(len(items) for _, items in groups)
    cheats = []  # (line, is a cheat)
    for heading, items in groups:
        if heading:
            cheats.append((f"<i>{html.escape(heading)}</i>", False))
        cheats += [(f"• {html.escape(item)}", True) for item in items]
    title = "Cheats" if app.get("category") == "games" else "Mod menu"
    shown = len(cheats)
    while True:
        body = [line for line, _ in cheats[:shown]]
        left = count - sum(is_cheat for _, is_cheat in cheats[:shown])
        if left:
            body.append(f"… and {left} more on the app page")
        lines = head + (["", f"<b>{title} ({count})</b>", *body] if groups else []) + tail
        text = "\n".join(lines)
        if len(text) <= TEXT_LIMIT or shown == 0:
            return text
        shown -= 1


def releases(apps, posted):
    """Own apps whose current version has not been posted."""
    return [
        a
        for a in apps
        if is_own(a.get("bundleIdentifier", ""))
        and a.get("version")
        and posted.get(a["bundleIdentifier"]) != a["version"]
    ]


def safe_for_digest(app, listings, nsfw):
    bid = app.get("bundleIdentifier", "")
    if is_own(bid) or icon_key(app.get("iconURL")) in nsfw:
        return False
    listing = listings.get(bid) if isinstance(listings.get(bid), dict) else {}
    words = [
        app.get("name"),
        app.get("subtitle"),
        app.get("developerName"),
        app.get("localizedDescription"),
        listing.get("description"),
        listing.get("subtitle"),
        *listing.get("features", []),
    ]
    if any(RISKY_WORDS.search(w) for w in words if isinstance(w, str)):
        return False
    return not RISKY_TAGS.intersection(t.lower() for t in listing.get("tags", []) if isinstance(t, str))


def digest_apps(apps, first_seen, listings, nsfw, day):
    """The apps first listed on day that may go in the digest, by name."""
    new = [
        a
        for a in apps
        if first_seen.get(a.get("bundleIdentifier")) == day and safe_for_digest(a, listings, nsfw)
    ]
    return sorted(new, key=lambda a: (a.get("name") or "").lower())


def digest(apps, first_seen, listings, nsfw, day):
    """Text of the digest of apps first listed on day, or None when there are none."""
    new = digest_apps(apps, first_seen, listings, nsfw, day)
    if not new:
        return None
    lines = [f"<b>New on Accretion ({day})</b>", ""]
    for a in new[:DIGEST_LIMIT]:
        name = html.escape(a.get("name") or a["bundleIdentifier"])
        sub = f" - {html.escape(a['subtitle'])}" if a.get("subtitle") else ""
        lines.append(f'• <a href="{app_url(a["bundleIdentifier"])}">{name}</a>{sub}')
    if len(new) > DIGEST_LIMIT:
        lines += ["", f'And {len(new) - DIGEST_LIMIT} more: <a href="{SITE}/">getaccretion.app</a>']
    return "\n".join(lines)


def send(token, method, payload):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response).get("ok", False)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        # the token is part of the URL: print only the method and the reason
        reason = (
            error.read().decode(errors="replace")[:300]
            if isinstance(error, urllib.error.HTTPError)
            else str(error)
        )
        print(f"Telegram {method} failed: {reason}")
        return False


def send_file(token, method, fields, field, filename, data, mime):
    """A Telegram call with one uploaded file (multipart/form-data); True when Telegram accepted it."""
    boundary = "accretion-" + os.urandom(12).hex()
    body = io.BytesIO()
    for key, value in fields.items():
        body.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    body.write(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response).get("ok", False)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        reason = (
            error.read().decode(errors="replace")[:300]
            if isinstance(error, urllib.error.HTTPError)
            else str(error)
        )
        print(f"Telegram {method} failed: {reason}")
        return False


def card(make, *args):
    """An animated card's MP4 bytes, or None (no renderer, or it failed: the post goes without it)."""
    if telegram_card is None:
        return None
    try:
        return make(*args)
    except Exception as error:  # a card is optional; never lose the post over it
        print(f"Card not made: {error}")
        return None


def release_badge(app):
    return "ACCRETION MOD" if app["bundleIdentifier"].startswith(MOD_PREFIX) else "NEW VERSION"


def save(state):
    with open(path(STATE), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def run(today, token, chat, dry_run):
    apps = load_json("source.json", {}).get("apps", [])
    state = load_json(STATE, None)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    if state is None:
        state = {
            "posted": {a["bundleIdentifier"]: a["version"] for a in releases(apps, {})},
            "digest": yesterday,
        }
        print(f"No {STATE} yet: recorded {len(state['posted'])} current versions, nothing posted.")
        if not dry_run:
            save(state)
        return
    live = bool(token and chat) and not dry_run
    if not live:
        print("Dry run (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT not set, or --dry-run): nothing is sent.")
    changed = False

    for app in releases(apps, state["posted"]):
        caption = release_caption(app)
        print(f"Release post: {app['bundleIdentifier']} {app['version']}")
        if not live:
            print(caption)
            continue
        animation = card(telegram_card.release_card, app, release_badge(app)) if telegram_card else None
        ok = (
            animation is not None
            and len(caption) <= CAPTION_LIMIT
            and send_file(
                token,
                "sendAnimation",
                {"chat_id": chat, "caption": caption, "parse_mode": "HTML", "width": 1280, "height": 720},
                "animation",
                "accretion.mp4",
                animation,
                "video/mp4",
            )
        )
        ok = ok or (
            app.get("iconURL")
            and len(caption) <= CAPTION_LIMIT
            and send(
                token,
                "sendPhoto",
                {"chat_id": chat, "photo": app["iconURL"], "caption": caption, "parse_mode": "HTML"},
            )
        )
        ok = ok or send(
            token,
            "sendMessage",
            {
                "chat_id": chat,
                "text": caption,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        if ok:
            state["posted"][app["bundleIdentifier"]] = app["version"]
            changed = True

    if state.get("digest", "") < yesterday:
        text = digest(
            apps,
            load_json("first_seen.json", {}),
            load_json("ai/listings.json", {}),
            nsfw_icon_keys(),
            yesterday,
        )
        print(f"Digest for {yesterday}: {'nothing new' if text is None else 'posting'}")
        if text and not live:
            print(text)
        if text and live:  # the card first (optional), then the list
            new = digest_apps(
                apps,
                load_json("first_seen.json", {}),
                load_json("ai/listings.json", {}),
                nsfw_icon_keys(),
                yesterday,
            )
            animation = card(telegram_card.digest_card, new, yesterday) if telegram_card else None
            if animation is not None:
                send_file(
                    token,
                    "sendAnimation",
                    {"chat_id": chat, "width": 1280, "height": 720},
                    "animation",
                    "accretion-digest.mp4",
                    animation,
                    "video/mp4",
                )
        if live and (
            text is None
            or send(
                token,
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )
        ):
            state["digest"] = yesterday
            changed = True

    if changed:
        save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the posts, send and save nothing")
    args = parser.parse_args()
    run(
        dt.datetime.now(dt.timezone.utc).date(),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT"),
        args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
