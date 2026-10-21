"""Posts the store's news to the Telegram channel (publish.yml, after the site build).

- A release of one of Accretion's own apps (the app, mods): one photo post with the icon, version and
  What's new, linking to the app page. Each version is posted once.
- Once a day: the catalogue apps first listed the day before, cracked, unlocked, modded and NSFW apps
  left out (channels get taken down over those), at most DIGEST_LIMIT names with links.

State lives in telegram.json (committed by publish.yml): the last posted version per own app and the
day of the last digest. With no state file the first run only records the current versions, so the
channel never gets the whole back catalogue. TELEGRAM_BOT_TOKEN (secret) and TELEGRAM_CHAT (variable,
@channelname or the numeric id) switch it on; without them it prints what it would post and changes
nothing. A failed post is retried on the next build and never fails this one.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

from common import load_json, nsfw_icon_keys, path

SITE = "https://getaccretion.app"
STATE = "telegram.json"
# Same list as OWN_PREFIXES in scripts/site.py and site/app.js
OWN_PREFIXES = ("io.github.getaccretion.", "io.github.accretionmods.")
DIGEST_LIMIT = 10
NOTES_LIMIT = 600  # characters of What's new in a release caption (Telegram allows 1024 in all)
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


def release_caption(app):
    name = html.escape(app.get("name") or app["bundleIdentifier"])
    version = html.escape(str(app.get("version") or ""))
    versions = app.get("versions") or [{}]
    notes = (versions[0].get("localizedDescription") or "").strip()
    if len(notes) > NOTES_LIMIT:
        notes = notes[:NOTES_LIMIT].rsplit(" ", 1)[0] + "..."
    lines = [f"<b>{name} {version}</b>"]
    if app.get("subtitle"):
        lines.append(html.escape(app["subtitle"]))
    if notes:
        lines += ["", html.escape(notes)]
    lines += ["", f'<a href="{app_url(app["bundleIdentifier"])}">Get it on Accretion</a>']
    return "\n".join(lines)


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


def digest(apps, first_seen, listings, nsfw, day):
    """Text of the digest of apps first listed on day, or None when there are none."""
    new = [
        a
        for a in apps
        if first_seen.get(a.get("bundleIdentifier")) == day and safe_for_digest(a, listings, nsfw)
    ]
    if not new:
        return None
    new.sort(key=lambda a: (a.get("name") or "").lower())
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
        ok = app.get("iconURL") and send(
            token,
            "sendPhoto",
            {"chat_id": chat, "photo": app["iconURL"], "caption": caption, "parse_mode": "HTML"},
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
        elif live and (
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
