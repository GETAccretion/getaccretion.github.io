"""Build the Accretion web store into _site/ for GitHub Pages.

Copies the static storefront from site/ and adds data generated from the build:
  data/store.json       one compact row per app, everything the grid and search need
  data/index.json       the same, listing only the Accretion app: what Accretion 1.0.4 and older
                        read, so their store only offers the update (app-config.json sets the
                        minimum version newer copies enforce themselves)
  data/apps/NNN.json    full details, split into SHARDS files so a phone only loads
                        the slice that holds the app it opens
  data/stats.json       the numbers merge.py collected
  source.json, curated.json and the card images, so the store has short links too
  app/<bundle id>/      a full page per app that search engines can read, with link-preview
                        tags (Discord, iMessage, forums); only Accretion's own apps (OWN_PREFIXES)
                        load Google ads. Apps with no working link keep a page that forwards
                        to the store's closest matches
  sitemap.xml, robots.txt  every per-app page with its newest version date (SITE_URL, else the default)

Run after merge.py. Reads _build/origins.json for where each app came from.
"""

import html
import json
import os
import re
import shutil
import urllib.parse
import urllib.request

from common import hide_nsfw_icons, load_json, nsfw_icon_keys, path, read_list
from merge import PLACEHOLDER_ICON

SHARDS = 128
ACCRETION_APP = "io.github.getaccretion.Accretion"
OUT = path("_site")


def private_links():
    """PRIVATE_LINKS (GitHub variable): publish download-server links instead of source URLs"""
    return os.environ.get("PRIVATE_LINKS", "").strip().lower() in ("1", "true", "yes", "on")


def supabase_settings():
    url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not (url and key):  # written by the "Store: set up backend" workflow
        project = load_json("supabase/project.json", {})
        url, key = project.get("url", ""), project.get("anonKey", "")
    return url.rstrip("/"), key


def server_link(base, bundle_id, version):
    """The download server's link for one version: it needs a login or personal key, checks it,
    then sends the file through the download proxy. The source's own URL is never published."""
    q = urllib.parse.urlencode({"app": bundle_id, "version": version})
    return f"{base}/functions/v1/download?{q}"


def private_sources(sources):
    """The store's source list without URLs. "url" stays, as an empty string: every Accretion app
    version requires the field when it reads data/store.json, so dropping it would empty their
    Store tab; the site and newer apps hide an empty one."""
    return [{**s, "url": ""} for s in sources]


def private_source(source, base):
    """A copy of an AltStore source with every download pointed at the download server"""
    copy = json.loads(json.dumps(source))
    for app in copy.get("apps", []):
        bid = app.get("bundleIdentifier", "")
        versions = app.get("versions") or []
        for v in versions:
            v["downloadURL"] = server_link(base, bid, v.get("version", ""))
        if "downloadURL" in app:
            app["downloadURL"] = (
                versions[0]["downloadURL"] if versions else server_link(base, bid, app.get("version", ""))
            )
    return copy


# Apps from this source are built by Accretion: the store shows them with an "Accretion Mod" badge.
MODS_SOURCE = "https://raw.githubusercontent.com/AccretionMods/mods/main/mods.json"


def fnv1a(text):
    """32-bit FNV-1a; site/app.js uses the same function to find an app's shard."""
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def shard_of(bundle_id):
    return fnv1a(bundle_id) % SHARDS


def clip(text, limit):
    """At most `limit` characters, cut at a word with an ellipsis instead of mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:|-")
    return (cut or text[: limit - 1]) + "…"


def screenshot_urls(app):
    shots = app.get("screenshots") or []
    if isinstance(shots, dict):
        shots = shots.get("iphone") or shots.get("ipad") or []
    return [s["imageURL"] if isinstance(s, dict) else s for s in shots]


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]*$")


RELATED = 6  # similar apps linked from each app page


def related_apps(apps, similar=None, limit=RELATED):
    """bundle id -> up to `limit` (bid, name, icon) to link from its page: the AI's similar apps
    first, then the newest other apps of the same category, so every page links onward."""
    by_id = {a["bundleIdentifier"]: a for a in apps if SAFE_ID.match(a["bundleIdentifier"])}
    by_cat = {}
    for a in sorted(by_id.values(), key=newest_date, reverse=True):
        by_cat.setdefault(category_slug(a), []).append(a["bundleIdentifier"])
    out = {}
    for bid, a in by_id.items():
        picks = []
        for other in list((similar or {}).get(bid, [])) + by_cat[category_slug(a)][: limit * 2]:
            if other != bid and other in by_id and other not in picks:
                picks.append(other)
            if len(picks) == limit:
                break
        out[bid] = [(b, by_id[b]["name"], https_url(by_id[b].get("iconURL"))) for b in picks]
    return out


def share_pages(apps, gone=None, extras=None, site_url=None, certificates=(), similar=None, ezoic=False):
    """A full, crawlable page per app (app_page_html) that also gives shared links a name, icon and
    description. Apps with no working download left (gone) keep a small page that forwards to the
    store's closest matches."""
    extras, site_url = extras or {}, (site_url or SITE_URL).rstrip("/")
    related = related_apps(apps, similar)
    for bid, g in (gone or {}).items():
        if SAFE_ID.match(bid):
            write_share_page(
                bid,
                f"{g['n']} (unavailable)",
                "No working download right now. Open to see the closest apps that work.",
                g["ic"],
                g["n"],
            )
    for a in apps:
        bid = a["bundleIdentifier"]
        if not SAFE_ID.match(bid):
            continue
        write_page(
            bid, app_page_html(a, extras.get(bid, {}), site_url, certificates, related.get(bid), ezoic)
        )


def write_page(bid, page):
    folder = os.path.join(OUT, "app", bid)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# Accretion's own apps (same list as OWN_PREFIXES in site/app.js): only their pages carry Google ads
OWN_PREFIXES = ("io.github.getaccretion.", "io.github.accretionmods.")
# own pages: same policy as site/install-without-computer.html (own scripts plus Google's ad hosts)
ADS_CSP = (
    "script-src 'self' https://pagead2.googlesyndication.com https://*.googlesyndication.com "
    "https://*.adtrafficquality.google https://fundingchoicesmessages.google.com https://*.doubleclick.net "
    "https://www.google.com https://www.gstatic.com; object-src 'none'; base-uri 'self'; form-action 'self'"
)
# other sources' apps: no ads, so no outside scripts at all
PLAIN_CSP = "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"


def ezoic_mode():
    """EZOIC (GitHub variable). "header": Ezoic's header scripts on every page next to the unchanged
    AdSense setup, with no Ezoic ad spots (lets Ezoic detect the integration before approval). On
    ("1"/"on"): Ezoic serves the ads on every page instead of the AdSense tags (AdSense bids inside
    Ezoic). Unset: the AdSense setup below, unchanged."""
    value = os.environ.get("EZOIC", "").strip().lower()
    if value == "header":
        return "header"
    return "on" if value in ("1", "true", "yes", "on") else ""


def ezoic_on():
    return ezoic_mode() == "on"


def ezoic_full(mode):
    """True when Ezoic replaces the AdSense tags and places its spots (mode "on"; True in tests)"""
    return mode is True or mode == "on"


# Ezoic's consent (Gatekeeper) and header scripts, first in <head> right after the CSP; the queue stub
# and ad calls live in site/ezoic.js so the CSP needs no inline scripts. Order and data-cfasync are Ezoic's.
EZOIC_HEAD = (
    '<script data-cfasync="false" src="https://cmp.gatekeeperconsent.com/min.js"></script>\n'
    '<script data-cfasync="false" src="https://the.gatekeeperconsent.com/cmp.min.js"></script>\n'
    '<script async src="https://www.ezojs.com/ezoic/sa.min.js"></script>\n'
    '<script src="/ezoic.js"></script>\n'
    '<script src="https://ezoicanalytics.com/analytics.js"></script>'
)
# Pages with Ezoic (owner's call 2026-10-19): Ezoic's stack loads scripts from changing demand-partner
# hosts (e.g. cdn.id5-sync.com), injects inline scripts and evaluates strings, and publishes no host list,
# so script-src allows any https host plus inline and eval. Plugins, <base> and form targets stay locked;
# pages without the EZOIC switch keep ADS_CSP / PLAIN_CSP. Our own code still escapes all source text.
EZOIC_CSP = "script-src 'self' https: 'unsafe-inline' 'unsafe-eval'; object-src 'none'; base-uri 'self'; form-action 'self'"
EZOIC_SLOT = '<div class="ezoicad"></div>'
EZOIC_ADSTXT = "https://srv.adstxtmanager.com/{id}/getaccretion.app"  # Ezoic's Ads.txt Manager file
# AdSense accounts loaded on own pages (same list in site/social.js loadAds; each has a line in site/ads.txt)
ADSENSE_CLIENTS = ("ca-pub-4500603236648893", "ca-pub-1352971207491342")
ADSENSE = "\n".join(
    '<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'
    f'?client={client}" crossorigin="anonymous"></script>'
    for client in ADSENSE_CLIENTS
)
CATEGORY_NAMES = {
    "games": "Games",
    "utilities": "Utilities",
    "photo-video": "Photo & Video",
    "entertainment": "Entertainment",
    "social": "Social",
    "lifestyle": "Lifestyle",
    "developer": "Developer",
    "other": "Other",
}


def is_own(bid):
    return bid.startswith(OWN_PREFIXES)


def category_slug(app):
    """The app's category key (the /apps/<slug>/ page it is listed on); unknown ones are 'other'"""
    cat = app.get("category", "")
    return cat if cat in CATEGORY_NAMES else "other"


def crumbs(site_url, category, slug, name=None):
    """Visible breadcrumb links plus the matching schema.org BreadcrumbList"""
    e = html.escape
    items = [("Accretion", f"{site_url}/"), (category, f"{site_url}/apps/{slug}/")]
    if name:
        items.append((name, None))
    nav = " › ".join(f'<a href="{e(u)}">{e(n)}</a>' if u else e(n) for n, u in items)
    ld = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": n, **({"item": u} if u else {})}
            for i, (n, u) in enumerate(items, 1)
        ],
    }
    return f'<nav class="crumbs muted small" aria-label="Breadcrumb">{nav}</nav>', ld


def ld_script(*nodes):
    """One JSON-LD script with an @graph; text can't open or close a tag"""
    data = json.dumps({"@context": "https://schema.org", "@graph": list(nodes)}, ensure_ascii=False)
    return f'<script type="application/ld+json">{data.replace("<", chr(92) + "u003c")}</script>'


def app_links(items):
    """A list of app links with small icons (similar apps, category pages)"""
    e = html.escape
    return (
        '<ul class="app-links">'
        + "".join(
            f'<li><a href="/app/{e(bid)}/">'
            + (f'<img src="{e(icon)}" width="40" height="40" loading="lazy" alt="">' if icon else "")
            + f"<span>{e(name)}{f'<small>{e(sub)}</small>' if sub else ''}</span></a></li>"
            for bid, name, icon, sub in items
        )
        + "</ul>"
    )


def https_url(url):
    """The URL when it is https, else '' (pages never load from plain http)"""
    url = str(url or "")
    return url if url.startswith("https://") else ""


def text_html(text):
    """Plain text as HTML paragraphs: blank lines split paragraphs, single newlines become <br>"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text or "").replace("\r\n", "\n")) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def cert_url(cert, name, icon):
    """A certificate seller's link for one app: its app_url with {name}/{icon} filled (as
    certUrl() in site/app.js), else its plain url; '' unless https"""
    tpl = cert.get("app_url")
    if isinstance(tpl, str) and tpl:
        url = tpl.replace("{name}", urllib.parse.quote(name, safe="")).replace(
            "{icon}", urllib.parse.quote(icon, safe="")
        )
        if https_url(url):
            return url
    return https_url(cert.get("url"))


def cert_line(certificates, name, icon):
    """'Need a signing certificate?' with each seller's per-app link, as certLine() in site/app.js"""
    links = [
        f'<a href="{html.escape(url)}" target="_blank" rel="sponsored noopener">'
        f"{html.escape(c.get('name', ''))}</a>"
        for c in certificates or ()
        if isinstance(c, dict) and (url := cert_url(c, name, icon))
    ]
    return f'<p class="cert-line">Need a signing certificate? {" · ".join(links)}</p>' if links else ""


def app_page_html(app, extra, site_url, certificates=(), related=None, ezoic=False):
    """A full page for one app: what search engines index for it. It has no redirect; installing
    happens in the store (signed-in downloads), linked from the page. Only Accretion's own apps
    load Google ads and say they are free (other sources' apps may be paid on the App Store)."""
    e = html.escape
    bid, name = app["bundleIdentifier"], app["name"]
    own = is_own(bid)
    versions = app.get("versions") or []
    latest = versions[0] if versions else {}
    version, date = latest.get("version", ""), str(latest.get("date") or "")[:10]
    min_os = latest.get("minOSVersion", "")
    about = app.get("localizedDescription") or ""
    desc = clip(app.get("subtitle") or about, 160)
    site_url = site_url.rstrip("/")
    url = f"{site_url}/app/{bid}/"
    icon = https_url(app.get("iconURL"))
    slug = category_slug(app)
    category = CATEGORY_NAMES[slug]
    crumb_nav, crumb_ld = crumbs(site_url, category, slug, name)
    title = f"{name} {version} for iPhone · Accretion" if version else f"{name} for iPhone · Accretion"
    ld = {
        "@type": "SoftwareApplication",
        "name": name,
        "description": desc,
        "url": url,
        "softwareVersion": version,
        "datePublished": date,
        "operatingSystem": f"iOS {min_os} or later" if min_os else "iOS",
        "applicationCategory": "GameApplication"
        if app.get("category") == "games"
        else "UtilitiesApplication",
        "author": {"@type": "Organization", "name": app.get("developerName", "")},
    }
    if own:
        ld["offers"] = {"@type": "Offer", "price": "0", "priceCurrency": "USD"}
    if icon:
        ld["image"] = icon

    facts = [("Version", version), ("Updated", date)]
    size = latest.get("size") or 0
    if size:
        facts.append(("Size", f"{size / 1_000_000:.1f} MB"))
    if min_os:
        facts.append(("Requires", f"iOS {min_os} or later"))
    parts = [
        crumb_nav,
        f'<p><img src="{e(icon)}" width="96" height="96" alt="{e(name)} icon" style="border-radius:22%"></p>'
        if icon
        else "",
        f'<h1 class="view-title">{e(name)}</h1>',
        f'<p class="muted">{e(app.get("developerName", ""))} · {e(category)}</p>',
        f"<p>{e(app['subtitle'])}</p>" if app.get("subtitle") else "",
        f'<p><a class="btn primary" href="../../#/app/{e(bid)}">Open in the store to install</a></p>',
        cert_line(certificates, name, icon),
        "<ul>" + "".join(f"<li><b>{k}:</b> {e(v)}</li>" for k, v in facts if v) + "</ul>",
        EZOIC_SLOT if ezoic_full(ezoic) else "",
    ]
    notes = latest.get("localizedDescription") or ""
    if notes.strip():
        parts.append(f"<h2>What's new in {e(version)}</h2>{text_html(notes)}")
    if about.strip():
        parts.append(f"<h2>About</h2>{text_html(about)}")
    features = [f for f in (extra or {}).get("features", []) if str(f).strip()]
    if features:
        parts.append("<h2>Features</h2><ul>" + "".join(f"<li>{e(str(f))}</li>" for f in features) + "</ul>")
    shots = [s for s in (https_url(u) for u in screenshot_urls(app)) if s][:5]
    if shots:
        parts.append(
            "<h2>Screenshots</h2><p>"
            + " ".join(
                f'<img src="{e(s)}" loading="lazy" width="230" alt="{e(name)} screenshot {n}">'
                for n, s in enumerate(shots, 1)
            )
            + "</p>"
        )
    if versions:
        parts.append(
            "<h2>Versions</h2><ul>"
            + "".join(
                f"<li>{e(v.get('version', ''))} · {e(str(v.get('date') or '')[:10])}</li>"
                for v in versions[:10]
            )
            + "</ul>"
        )
    if ezoic_full(ezoic):
        parts.append(EZOIC_SLOT)
    if related:
        parts.append("<h2>Similar apps</h2>" + app_links((b, n, i, "") for b, n, i in related))
    parts.append(
        '<p class="muted small"><a href="../../install-without-computer.html">Install without a computer</a>'
        ' · <a href="../../privacy.html">Privacy</a></p>'
    )
    body = "\n".join(p for p in parts if p)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{EZOIC_CSP if ezoic else ADS_CSP if own else PLAIN_CSP}">
{EZOIC_HEAD if ezoic else ""}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:site_name" content="Accretion">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
{f'<meta property="og:image" content="{e(icon)}">' if icon else ""}
<meta name="twitter:card" content="summary">
<meta name="theme-color" content="#07070D">
<link rel="icon" href="../../icon-192.png">
<link rel="stylesheet" href="../../style.css">
{ADSENSE if own and not ezoic_full(ezoic) else ""}
{ld_script(ld, crumb_ld)}
</head>
<body>
<main class="view doc">
{body}
</main>
</body>
</html>
"""


def write_share_page(bid, title, desc, icon, name):
    title = html.escape(title)
    desc = html.escape(desc)
    icon = html.escape(icon)
    target = f"../../#/app/{bid}"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Accretion</title>
<meta name="description" content="{desc}">
<meta property="og:site_name" content="Accretion">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{icon}">
<meta name="twitter:card" content="summary">
<meta name="theme-color" content="#07070D">
<meta http-equiv="refresh" content="0; url={target}">
<link rel="icon" href="../../icon-192.png">
</head><body style="background:#07070D;color:#ECEAFF;font-family:system-ui,sans-serif;padding:24px">
<script>location.replace({json.dumps(target)})</script>
<p><a style="color:#22D3EE" href="{target}">Open {html.escape(name)} in Accretion</a></p>
</body></html>
"""
    write_page(bid, page)


SITE_URL = "https://getaccretion.app"  # used for the sitemap when SITE_URL is not set
SITEMAP_LIMIT = 50000  # most URLs one sitemap file may list
STATIC_PAGES = ["install-without-computer.html"]  # crawlable guide pages in site/ that belong in the sitemap


def newest_date(app):
    """YYYY-MM-DD of the app's newest version, or '' when no version has a usable date"""
    dates = [str(v.get("date") or "")[:10] for v in app.get("versions", [])]
    return max((d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)), default="")


CATEGORY_PAGE = 200  # apps per category page


def category_pages(apps, site_url, out=None, ezoic=False):
    """Crawlable catalogue: /apps/ lists the categories, /apps/<slug>/ (then /apps/<slug>/<n>/) lists
    that category's apps, newest first, 200 a page. Returns (url, lastmod) pairs for the sitemap."""
    out, site_url, e = out or OUT, site_url.rstrip("/"), html.escape
    groups = {slug: [] for slug in CATEGORY_NAMES}
    for a in sorted(apps, key=newest_date, reverse=True):
        if SAFE_ID.match(a["bundleIdentifier"]):
            groups[category_slug(a)].append(a)

    def page(rel, title, desc, body, lds):
        url = f"{site_url}/{rel}"
        folder = os.path.join(out, *rel.strip("/").split("/"))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as f:
            f.write(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{EZOIC_CSP if ezoic else PLAIN_CSP}">
{EZOIC_HEAD if ezoic else ""}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:site_name" content="Accretion">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
<meta name="twitter:card" content="summary">
<meta name="theme-color" content="#07070D">
<link rel="icon" href="/icon-192.png">
<link rel="stylesheet" href="/style.css">
{ld_script({"@type": "CollectionPage", "name": title, "url": url}, *lds)}
</head>
<body>
<main class="view doc">
{body}
</main>
</body>
</html>
""")
        return url

    entries, hub = [], []
    for slug, items in groups.items():
        if not items:
            continue
        name = CATEGORY_NAMES[slug]
        hub.append(f'<li><a href="/apps/{slug}/">{e(name)}</a> <span class="muted">{len(items)}</span></li>')
        pages = [items[i : i + CATEGORY_PAGE] for i in range(0, len(items), CATEGORY_PAGE)]
        for n, chunk in enumerate(pages, 1):
            rel = f"apps/{slug}/" if n == 1 else f"apps/{slug}/{n}/"
            nav, crumb_ld = crumbs(site_url, name, slug)
            more = [
                f'<a href="/apps/{slug}/{"" if n == 2 else f"{n - 1}/"}">← Newer</a>' if n > 1 else "",
                f'<a href="/apps/{slug}/{n + 1}/">Older →</a>' if n < len(pages) else "",
            ]
            body = (
                f'{nav}\n<h1 class="view-title">{e(name)}</h1>\n'
                f'<p class="muted">{len(items)} apps, newest first'
                + (f" · page {n} of {len(pages)}" if len(pages) > 1 else "")
                + "</p>\n"
                + app_links(
                    (
                        a["bundleIdentifier"],
                        a["name"],
                        https_url(a.get("iconURL")),
                        clip(a.get("subtitle") or "", 90),
                    )
                    for a in chunk
                )
                + (f'\n<p class="pager">{" ".join(m for m in more if m)}</p>' if len(pages) > 1 else "")
            )
            title = f"iPhone {name} apps · Accretion" + (f" (page {n})" if n > 1 else "")
            desc = f"{len(items)} {name} apps for iPhone from AltStore and SideStore sources, newest first."
            if ezoic_full(ezoic):
                body = body.replace("</p>\n", f"</p>\n{EZOIC_SLOT}\n", 1) + f"\n{EZOIC_SLOT}"
            url = page(rel, title, desc, body, [crumb_ld])
            entries.append((url, max((newest_date(a) for a in chunk), default="")))
    hub_body = (
        '<nav class="crumbs muted small" aria-label="Breadcrumb">'
        '<a href="/">Accretion</a> › Categories</nav>\n'
        '<h1 class="view-title">All categories</h1>\n<ul class="cats">' + "".join(hub) + "</ul>"
    )
    entries.insert(
        0,
        (
            page(
                "apps/",
                "iPhone apps by category · Accretion",
                "Every app in Accretion by category.",
                hub_body,
                [],
            ),
            "",
        ),
    )
    return entries


def sitemap(site_url, apps, out=None, extra=()):
    """sitemap.xml (a sitemap index plus sitemap-N.xml files above 50,000 URLs) and robots.txt.
    Lists the store, the guide pages, the category pages (extra) and every per-app page; nothing
    public is blocked."""
    out = out or OUT
    site_url = site_url.rstrip("/")
    entries = (
        [(f"{site_url}/", "")]
        + [(f"{site_url}/{page}", "") for page in STATIC_PAGES]
        + list(extra)
        + [
            (f"{site_url}/app/{a['bundleIdentifier']}/", newest_date(a))
            for a in apps
            if SAFE_ID.match(a["bundleIdentifier"])
        ]
    )
    head = '<?xml version="1.0" encoding="UTF-8"?>\n'
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'

    def urlset(items):
        body = "".join(
            f"<url><loc>{html.escape(u)}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>\n"
            for u, d in items
        )
        return f"{head}<urlset {ns}>\n{body}</urlset>\n"

    def write(name, data):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(data)

    if len(entries) <= SITEMAP_LIMIT:
        write("sitemap.xml", urlset(entries))
    else:
        parts = [entries[i : i + SITEMAP_LIMIT] for i in range(0, len(entries), SITEMAP_LIMIT)]
        for n, part in enumerate(parts, 1):
            write(f"sitemap-{n}.xml", urlset(part))
        refs = "".join(
            f"<sitemap><loc>{html.escape(f'{site_url}/sitemap-{n}.xml')}</loc></sitemap>\n"
            for n in range(1, len(parts) + 1)
        )
        write("sitemap.xml", f"{head}<sitemapindex {ns}>\n{refs}</sitemapindex>\n")
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {site_url}/sitemap.xml\n")


WEB_PICK = 8  # an app in popular.txt counts like 8 more sources carrying it


def popularity(bid, carriers, picks):
    """How wanted an app is before anyone searches the store: sources carrying it, plus a boost
    for the apps guides and communities name most (popular.txt)."""
    return carriers.get(bid, 0) + (WEB_PICK if bid in picks else 0)


def public_config(url, key, private=False, download_proxy="", ezoic=False):
    """window.ACCRETION_CONFIG in config.js. Every value is a string: the Accretion app reads this
    file too and treats it as text only, so a true/false value there breaks sign-in in every
    installed copy. Flags are "1" when on and left out when off."""
    config = {"supabaseUrl": url, "supabaseKey": key}
    if private:
        config["privateLinks"] = "1"
    # "Download IPA" for other sources' apps goes through the download proxy once it is live, and
    # the site's download links use its address (GitHub variable DOWNLOAD_PROXY_ORIGIN, the same
    # one the download function gets, e.g. https://dl.getaccretion.app)
    if download_proxy:
        config["downloadProxy"] = "1"
        config["downloadOrigin"] = download_proxy.rstrip("/")
    if ezoic:  # site/social.js: Ezoic instead of the AdSense tags
        config["ezoic"] = "1"
    return config


CSP_META = re.compile(r'<meta http-equiv="Content-Security-Policy" content="[^"]*">')
ADSENSE_TAG = re.compile(
    r'<script async src="https://pagead2\.googlesyndication\.com/pagead/js/adsbygoogle\.js\?client=[^"]*"'
    r' crossorigin="anonymous"></script>\r?\n?'
)


def ezoic_static(html_text, full=True):
    """A page from site/ with Ezoic: its CSP allows Ezoic, Ezoic's tags come right after the CSP (or
    after <meta charset> when there is none), the AdSense tags go, and a page with a <main> gets an ad
    spot at its end. The store page (index.html) places its own spots (site/social.js)."""
    text = ADSENSE_TAG.sub("", html_text) if full else html_text
    csp = f'<meta http-equiv="Content-Security-Policy" content="{EZOIC_CSP}">'
    if CSP_META.search(text):
        text = CSP_META.sub(lambda _: f"{csp}\n{EZOIC_HEAD}", text, count=1)
    else:
        text = text.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n{csp}\n{EZOIC_HEAD}', 1)
    if full and 'id="home"' not in text and "</main>" in text:
        text = text.replace("</main>", f"{EZOIC_SLOT}\n</main>", 1)
    return text


def merge_ads_txt(managed, ours):
    """Ezoic's managed ads.txt plus every line of our own ads.txt it lacks (both AdSense accounts
    stay declared even when the managed file misses one)"""
    have = {line.strip().lower() for line in managed.splitlines()}
    extra = [line for line in ours.splitlines() if line.strip() and line.strip().lower() not in have]
    return managed.rstrip("\n") + "\n" + "".join(f"{line}\n" for line in extra)


def ezoic_pages(out, full=True):
    """Turn the copied site/ pages (home, guide, legal pages) over to Ezoic"""
    for name in os.listdir(out):
        if name.endswith(".html") and not name.startswith("google"):
            file = os.path.join(out, name)
            with open(file, encoding="utf-8") as f:
                text = f.read()
            with open(file, "w", encoding="utf-8") as f:
                f.write(ezoic_static(text, full))


def ezoic_ads_txt(out, adstxt_id):
    """ads.txt from Ezoic's Ads.txt Manager plus our own lines. Runs as soon as EZOIC_ADSTXT_ID is set,
    before the ads switch over: Google's MCM approval needs it, and it changes nothing for AdSense.
    Our own file stays, with a warning, when the manager can't be reached or answers nonsense."""
    ours_path = os.path.join(out, "ads.txt")
    with open(ours_path, encoding="utf-8") as f:
        ours = f.read()
    url = EZOIC_ADSTXT.format(id=adstxt_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Accretion store build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            managed = r.read().decode("utf-8", "replace")
    except OSError as exc:
        print(f"warning: Ezoic ads.txt not fetched ({exc}); keeping site/ads.txt")
        return
    if not re.search(r"^\s*[\w.-]+\.[a-z]{2,},\s*[\w-]+,\s*(DIRECT|RESELLER)", managed, re.I | re.M):
        print("warning: Ezoic ads.txt has no seller lines; keeping site/ads.txt")
        return
    with open(ours_path, "w", encoding="utf-8") as f:
        f.write(merge_ads_txt(managed, ours))


def publish_source(name, dest, private, base):
    """source.json / curated.json as published: as built, or with download-server links"""
    if not private:
        shutil.copy(path(name), dest)
        return
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(
            private_source(load_json(name, {"apps": []}), base), f, ensure_ascii=False, separators=(",", ":")
        )


def main():
    source = load_json("source.json", None)
    if not source:
        raise SystemExit("source.json not built yet")
    info = load_json("_build/origins.json", {"sources": [], "origin": {}, "first": {}})
    for s in info["sources"]:
        if s["url"] == MODS_SOURCE:
            s["mod"] = True
    carriers = info.get("carriers", {})
    undated = set(info.get("undated", []))
    picks = set(read_list("popular.txt"))
    stats = load_json("stats.json", {})
    extras = load_json("_build/ai_extras.json", {})  # features and tags from ai/listings.json
    similar = load_json("ai/similar.json", {})  # bundle id -> similar bundle ids (ai_jobs.py similar)
    known = {a["bundleIdentifier"] for a in source["apps"]}
    private = private_links()
    base, _ = supabase_settings()
    if private and not base:
        raise SystemExit("PRIVATE_LINKS is on but the Supabase URL is unknown")
    if private:  # source names stay public, their URLs don't
        info["sources"] = private_sources(info["sources"])
    # icons flagged nsfw by ai_jobs.py images show the placeholder (the app stays)
    hide_nsfw_icons(source["apps"], PLACEHOLDER_ICON, nsfw_icon_keys())

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(path("site"), OUT)
    os.makedirs(os.path.join(OUT, "data", "apps"))
    os.makedirs(os.path.join(OUT, "assets"))

    rows, shards = [], [[] for _ in range(SHARDS)]
    for a in source["apps"]:
        bid = a["bundleIdentifier"]
        latest = a["versions"][0]
        rows.append(
            {
                "i": bid,
                "n": a["name"],
                "d": a["developerName"],
                "s": clip(a.get("subtitle") or "", 90),
                "c": a["category"],
                "v": latest["version"],
                "t": latest["date"][:10],
                "z": latest.get("size") or 0,
                "ic": a["iconURL"],
                "o": info["origin"].get(bid, -1),
                "m": latest.get("minOSVersion", ""),
                "f": info["first"].get(bid, latest["date"][:10]),
            }
        )
        if extras.get(bid, {}).get("tags"):
            rows[-1]["k"] = extras[bid]["tags"]
        pop = popularity(bid, carriers, picks)
        if pop:
            rows[-1]["p"] = pop
        if bid in undated:
            rows[-1]["u"] = 1  # release date unknown
        shards[shard_of(bid)].append(
            {
                "i": bid,
                "desc": a.get("localizedDescription", ""),
                "feat": extras.get(bid, {}).get("features", []),
                "sim": [b for b in similar.get(bid, []) if b in known and b != bid][:6],
                "tint": a.get("tintColor", ""),
                "shots": screenshot_urls(a)[:5],
                "perms": a.get("appPermissions", {}),
                "versions": [
                    {
                        "v": v["version"],
                        "b": v.get("buildVersion", ""),
                        "t": v["date"][:10],
                        "z": v.get("size") or 0,
                        "min": v.get("minOSVersion", ""),
                        "notes": v.get("localizedDescription", ""),
                        "url": server_link(base, bid, v["version"]) if private else v["downloadURL"],
                    }
                    for v in a["versions"]
                ],
            }
        )

    def dump(rel, data):
        with open(os.path.join(OUT, rel), "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    dump("data/store.json", {"sources": info["sources"], "shards": SHARDS, "apps": rows})
    dump(
        "data/index.json",
        {"sources": info["sources"], "shards": SHARDS, "apps": [r for r in rows if r["i"] == ACCRETION_APP]},
    )
    for i, items in enumerate(shards):
        dump(f"data/apps/{i:03d}.json", {a["i"]: a for a in items})
    dump("data/stats.json", stats)
    for name in ("source.json", "curated.json"):
        publish_source(name, os.path.join(OUT, name), private, base)
    # app icons hosted by Accretion (scripts/icons.py keeps them in cache/icons)
    os.makedirs(os.path.join(OUT, "icons"), exist_ok=True)
    published = 0
    for name in ("source.json", "curated.json"):
        for a in load_json(name, {"apps": []})["apps"]:
            m = re.search(r"/icons/([0-9a-f]{16})\.jpg$", a.get("iconURL", ""))
            src = m and path(f"cache/icons/{m.group(1)}.jpg")
            if (
                src
                and os.path.exists(src)
                and not os.path.exists(os.path.join(OUT, "icons", f"{m.group(1)}.jpg"))
            ):
                shutil.copy(src, os.path.join(OUT, "icons", f"{m.group(1)}.jpg"))
                published += 1
    print(f"{published} icons published")
    # donation and certificate links shown on the store (empty parts stay hidden)
    support = load_json("support.json", {})
    with open(os.path.join(OUT, "support.json"), "w") as f:
        json.dump(support, f, ensure_ascii=False)
    for name in os.listdir(path("assets")):
        shutil.copy(path(f"assets/{name}"), os.path.join(OUT, "assets", name))
    # community features: public Supabase settings (the anon key is meant to be public)
    url, key = supabase_settings()
    if url and key:
        proxy = os.environ.get("DOWNLOAD_PROXY_ORIGIN", "").strip()
        config = public_config(url, key, private=private, download_proxy=proxy, ezoic=ezoic_on())
        with open(os.path.join(OUT, "config.js"), "w") as f:
            f.write(f"window.ACCRETION_CONFIG = {json.dumps(config)};\n")

    gone = {
        b: g
        for b, g in info.get("gone", {}).items()
        if b not in {a["bundleIdentifier"] for a in source["apps"]}
    }
    dump("data/gone.json", gone)
    site_url = os.environ.get("SITE_URL", "").strip() or SITE_URL
    ezoic = ezoic_mode()
    share_pages(source["apps"], gone, extras, site_url, support.get("certificates") or [], similar, ezoic)
    sitemap(site_url, source["apps"], extra=category_pages(source["apps"], site_url, ezoic=ezoic))
    if ezoic:
        ezoic_pages(OUT, full=ezoic == "on")
    adstxt_id = os.environ.get("EZOIC_ADSTXT_ID", "").strip()
    if adstxt_id or ezoic:
        ezoic_ads_txt(OUT, adstxt_id or "19390")

    # the store used to live at /Accretion/: send old links and saved home screen icons home
    legacy = os.path.join(OUT, "Accretion")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "index.html"), "w") as f:
        f.write(
            '<!doctype html><meta charset="utf-8"><title>Accretion</title>'
            '<meta name="robots" content="noindex"><link rel="canonical" href="../">'
            "<script>location.replace('../' + location.hash)</script>"
            '<a href="../">Accretion has moved</a>\n'
        )
    for name in ("source.json", "curated.json"):
        publish_source(name, os.path.join(legacy, name), private, base)

    # GitHub Pages: serve files as-is (no Jekyll processing)
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    size = os.path.getsize(os.path.join(OUT, "data/store.json"))
    print(f"_site: {len(rows)} apps, index {size // 1024} KB, {SHARDS} detail files")


if __name__ == "__main__":
    main()
