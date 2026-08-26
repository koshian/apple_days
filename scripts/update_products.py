#!/usr/bin/env python3
"""
Update Apple product release history from the global Apple Newsroom RSS.

- The global (English) Newsroom RSS is the only discovery source.
- The article body is fetched to extract an explicit retail availability date.
- If an article cannot be classified or no reliable sale date can be found,
  it is written to data/unmatched-newsroom.json instead of guessing.

No third-party packages are required (Python 3.10+).
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# CONFIG — normally this is the only section you need to edit.
# ---------------------------------------------------------------------------

RSS_URL = "https://www.apple.com/newsroom/rss-feed.rss"

# Product IDs that may be added to products.json automatically.
# Remove an ID to ignore future announcements for that family; add it back to
# resume tracking. This does not remove existing history from products.json.
TRACKED_PRODUCTS = {
    # iPhone
    "iphone",
    "iphone-pro",
    "iphone-entry",

    # iPad
    "ipad",
    "ipad-air",
    "ipad-pro",
    "ipad-mini",

    # MacBook
    "macbook-air",
    "macbook-pro-14",
    "macbook-pro-16",
    "macbook-neo",

    # Desktop Mac
    "imac",
    "mac-mini",
    "mac-studio",
    "mac-pro",

    # Other
    "apple-tv",
    "homepod",
    "airpods",
    "airpods-pro",
}

USER_AGENT = "apple-days-rss-updater/5.0 (+personal project)"

# ---------------------------------------------------------------------------
# End of user configuration.
# ---------------------------------------------------------------------------

PRODUCTISH = re.compile(
    r"(MacBook|iMac|Mac\s+Pro|Mac\s+Studio|Mac\s+mini|iPhone|iPad|Apple\s+TV|HomePod|AirPods)",
    re.I,
)

# Press-release-ish words. This keeps ordinary software/service Newsroom items out.
LAUNCHISH = re.compile(
    r"(introduc|unveil|debut|available|announc)",
    re.I,
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass(frozen=True)
class FeedItem:
    guid: str
    title: str
    link: str
    published: datetime
    feed_url: str = ""


def fetch_bytes(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def normalize_text(s: str) -> str:
    # Apple Newsroom frequently uses NBSP / narrow NBSP in product names.
    # Normalize those before regex matching.
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ").replace("\u202f", " ")).strip()


def parse_feed(raw: bytes, feed_url: str = "") -> list[FeedItem]:
    """Parse either Atom (current Apple Newsroom feed) or RSS 2.0.

    Apple currently serves https://www.apple.com/newsroom/rss-feed.rss as
    Atom despite the .rss filename, so supporting both formats avoids silently
    returning zero items if Apple changes formats again.
    """
    root = ET.fromstring(raw)
    items: list[FeedItem] = []

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def child_text(node: ET.Element, name: str) -> str:
        for child in node:
            if local_name(child.tag) == name:
                return (child.text or "").strip()
        return ""

    root_name = local_name(root.tag).lower()

    if root_name == "feed":  # Atom
        entries = [node for node in root if local_name(node.tag) == "entry"]
        for node in entries:
            title = normalize_text(html.unescape(child_text(node, "title")))
            guid = child_text(node, "id") or title
            updated = child_text(node, "updated") or child_text(node, "published")

            link = ""
            for child in node:
                if local_name(child.tag) != "link":
                    continue
                href = (child.attrib.get("href") or "").strip()
                rel = (child.attrib.get("rel") or "alternate").lower()
                # Ignore image enclosure links; prefer the article itself.
                if href and rel != "enclosure":
                    link = href
                    if rel == "alternate":
                        break

            if not (title and link and updated):
                continue

            try:
                pub = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub.tzinfo is None:
                pub = pub.astimezone()

            items.append(
                FeedItem(guid=guid, title=title, link=link, published=pub, feed_url=feed_url)
            )

    else:  # RSS 2.0 fallback
        for node in root.iter():
            if local_name(node.tag) != "item":
                continue

            title = normalize_text(html.unescape(child_text(node, "title")))
            link = child_text(node, "link")
            guid = child_text(node, "guid") or link or title
            pub_text = child_text(node, "pubDate")
            if not (title and link and pub_text):
                continue

            try:
                pub = email.utils.parsedate_to_datetime(pub_text)
            except (TypeError, ValueError):
                continue
            if pub.tzinfo is None:
                pub = pub.astimezone()

            items.append(
                FeedItem(guid=guid, title=title, link=link, published=pub, feed_url=feed_url)
            )

    return items


# Backward-compatible name used by older tests/callers.
def parse_rss(raw: bytes, feed_url: str = "") -> list[FeedItem]:
    return parse_feed(raw, feed_url)


def html_to_text(raw: bytes) -> str:
    parser = TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return normalize_text(parser.text())


def infer_year(pub: date, month: int, day: int) -> int:
    # Apple usually announces days/weeks before availability.
    # If a December article says "1月..." it is almost certainly next year.
    year = pub.year
    candidate = date(year, month, day)
    if (candidate - pub).days < -180:
        year += 1
    return year


def extract_release_date(text: str, published: datetime) -> date | None:
    pub = published.date()

    candidates: list[date] = []

    # Common global Newsroom wording.
    # Examples Apple actually uses:
    #   "with availability beginning September 22"
    #   "available beginning Wednesday, March 11"
    #   "will begin arriving ... starting September 22"
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    weekday = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    month_pat = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    english_patterns = [
        rf"(?:with\s+)?availability\s+beginning\s+{weekday}{month_pat}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?",
        rf"available(?:\s+in\s+stores)?\s+beginning\s+{weekday}{month_pat}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?",
        rf"will\s+begin\s+arriving[^.]{{0,180}}?starting\s+{weekday}{month_pat}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?",
        rf"(?:in\s+Apple\s+Store\s+locations[^.]{{0,120}}?)starting\s+{weekday}{month_pat}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?",
    ]
    for pat in english_patterns:
        for en in re.finditer(pat, text, re.I):
            month = months[en.group(1).lower()]
            day = int(en.group(2))
            explicit_year = int(en.group(3)) if en.lastindex and en.lastindex >= 3 and en.group(3) else None
            try:
                candidates.append(date(explicit_year or infer_year(pub, month, day), month, day))
            except ValueError:
                pass

    # Pick a plausible availability date nearest to publication, biased to future/on-date.
    plausible = [d for d in candidates if -2 <= (d - pub).days <= 120]
    if not plausible:
        return None
    plausible.sort(key=lambda d: (0 if d >= pub else 1, abs((d - pub).days)))
    return plausible[0]


def classify(title: str, text: str) -> list[str]:
    # Classify primarily from the RSS/article title. Apple pages contain global
    # navigation text naming many products, so using the whole page for product
    # identity causes false positives. Article text is only used to disambiguate
    # MacBook Pro screen sizes.
    t = normalize_text(title)
    text = normalize_text(text)
    ids: list[str] = []

    def add(pid: str) -> None:
        if pid not in ids:
            ids.append(pid)

    if re.search(r"AirPods\s*Pro", t, re.I):
        add("airpods-pro")
    elif re.search(r"\bAirPods\b", t, re.I):
        add("airpods")

    if re.search(r"iPhone\s*(?:\d+)?e(?![A-Za-z0-9])|iPhone\s*SE(?![A-Za-z0-9])", t, re.I):
        add("iphone-entry")
    if re.search(r"iPhone\s*\d+\s*Pro|iPhone\s*Pro", t, re.I):
        add("iphone-pro")
    # Base iPhone: number is not immediately followed by Pro/e.
    if re.search(r"iPhone\s*\d+(?!\d)(?!\s*(?:Pro|e)(?![A-Za-z0-9]))", t, re.I):
        add("iphone")

    if re.search(r"iPad\s*Air", t, re.I):
        add("ipad-air")
    if re.search(r"iPad\s*Pro", t, re.I):
        add("ipad-pro")
    if re.search(r"iPad\s*mini", t, re.I):
        add("ipad-mini")
    # Generic/base iPad only when the title names iPad without Air/Pro/mini nearby.
    if re.search(r"\biPad\b(?!\s*(?:Air|Pro|mini))", t, re.I):
        add("ipad")

    if re.search(r"MacBook\s*Neo", t, re.I):
        add("macbook-neo")
    if re.search(r"MacBook\s*Air", t, re.I):
        add("macbook-air")
    if re.search(r"MacBook\s*Pro", t, re.I):
        context = f"{t} {text[:12000]}"
        has14 = bool(re.search(r"14(?:インチ|-inch)", context, re.I))
        has16 = bool(re.search(r"16(?:インチ|-inch)", context, re.I))
        if has14:
            add("macbook-pro-14")
        if has16:
            add("macbook-pro-16")
        # If Apple names MacBook Pro but no size can be established, don't guess.

    if re.search(r"Mac\s*Studio", t, re.I):
        add("mac-studio")
    if re.search(r"Mac\s*mini", t, re.I):
        add("mac-mini")
    if re.search(r"(?<!Book\s)\bMac\s*Pro\b", t, re.I):
        add("mac-pro")
    if re.search(r"\biMac\b", t, re.I):
        add("imac")

    if re.search(r"\bHomePod\b", t, re.I):
        add("homepod")
    if re.search(r"\bApple\s*TV\b", t, re.I):
        add("apple-tv")

    return ids


def short_label(title: str) -> str:
    t = re.sub(r"^Apple[、,]\s*", "", title).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:140]


def product_index(data: dict) -> dict[str, dict]:
    return {p["id"]: p for p in data["products"]}


def latest_date(product: dict) -> str | None:
    dates = [r.get("date") for r in product.get("releases", []) if r.get("date")]
    return max(dates) if dates else None


def already_has(product: dict, release_date: date) -> bool:
    iso = release_date.isoformat()
    return any(r.get("date") == iso for r in product.get("releases", []))


def add_release(product: dict, release_date: date, item: FeedItem) -> bool:
    if already_has(product, release_date):
        return False
    product.setdefault("releases", []).append(
        {
            "date": release_date.isoformat(),
            "label": short_label(item.title),
            "source": item.link,
            "announcementTitle": item.title,
            "announcedAt": item.published.date().isoformat(),
            "discoveredFrom": item.feed_url,
        }
    )
    product["releases"].sort(key=lambda r: r["date"], reverse=True)
    return True


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/products.json")
    ap.add_argument(
        "--rss",
        default=RSS_URL,
        help="Override the global Apple Newsroom RSS URL (normally unnecessary).",
    )
    ap.add_argument("--unmatched", default="data/unmatched-newsroom.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    data_path = Path(args.data)
    unmatched_path = Path(args.unmatched)
    data = load_json(data_path, None)
    if not data:
        print(f"missing data file: {data_path}", file=sys.stderr)
        return 2

    products = product_index(data)
    print(f"data: {data_path.resolve()}")
    if args.verbose and "mac-mini" in products:
        print(f"  current mac-mini latest: {latest_date(products['mac-mini'])}")
    missing_tracked = TRACKED_PRODUCTS - set(products)
    if missing_tracked:
        print(
            "warning: TRACKED_PRODUCTS contains IDs missing from products.json: "
            + ", ".join(sorted(missing_tracked)),
            file=sys.stderr,
        )

    try:
        items = parse_feed(fetch_bytes(args.rss), feed_url=args.rss)
        newest = max(items, key=lambda x: x.published) if items else None
        print(f"rss: {len(items)} item(s)" + (f", newest={newest.published.date()} {newest.title}" if newest else ""))
        if args.verbose:
            for it in sorted(items, key=lambda x: x.published, reverse=True)[:10]:
                print(f"  feed: {it.published.isoformat()}  {it.title}")
    except Exception as e:
        print(f"error: RSS fetch failed: {args.rss}: {e}", file=sys.stderr)
        return 3

    unmatched = load_json(unmatched_path, [])
    unmatched_keys = {(x.get("guid"), x.get("reason")) for x in unmatched if isinstance(x, dict)}

    changed = 0

    # Oldest first so multi-article launch sequences settle deterministically.
    for item in sorted(items, key=lambda x: x.published):
        normalized_title = normalize_text(item.title)
        productish = bool(PRODUCTISH.search(normalized_title))
        launchish = bool(LAUNCHISH.search(normalized_title))
        if not productish or not launchish:
            if args.verbose and productish:
                print(f"skip prefilter: launchish={launchish}: {item.title}")
            continue

        if args.verbose:
            print(f"candidate: {item.published.isoformat()}  {item.title}")

        try:
            article_raw = fetch_bytes(item.link)
            article_text = html_to_text(article_raw)
        except Exception as e:
            key = (item.guid, "fetch-error")
            if key not in unmatched_keys:
                unmatched.append({
                    "guid": item.guid, "title": item.title, "link": item.link,
                    "published": item.published.isoformat(), "reason": "fetch-error",
                    "feed": item.feed_url,
                    "detail": str(e),
                })
                unmatched_keys.add(key)
            continue

        classified_ids = classify(item.title, article_text)
        ids = [pid for pid in classified_ids if pid in TRACKED_PRODUCTS]
        if args.verbose:
            print(f"  classified: {classified_ids or '-'}; tracked: {ids or '-'}")

        # A known but deliberately disabled product should be silently ignored.
        if classified_ids and not ids:
            if args.verbose:
                print(f"ignore: disabled product {classified_ids}: {item.title}")
            continue

        release_date = extract_release_date(article_text, item.published)
        if args.verbose:
            print(f"  release date: {release_date.isoformat() if release_date else '-'}")

        if not ids or release_date is None:
            reason = "unclassified" if not ids else "release-date-not-found"
            key = (item.guid, reason)
            if key not in unmatched_keys:
                unmatched.append({
                    "guid": item.guid,
                    "title": item.title,
                    "link": item.link,
                    "published": item.published.isoformat(),
                    "feed": item.feed_url,
                    "reason": reason,
                    "classifiedAs": ids,
                })
                unmatched_keys.add(key)
            if args.verbose:
                print(f"skip: {reason}: {item.title}")
            continue

        for pid in ids:
            product = products.get(pid)
            if not product:
                continue
            if add_release(product, release_date, item):
                changed += 1
                print(f"+ {pid}: {release_date.isoformat()}  {item.title}")
            elif args.verbose:
                print(f"= {pid}: {release_date.isoformat()} already present")

    data["updatedAt"] = date.today().isoformat()

    if args.dry_run:
        print(f"dry-run: {changed} release(s) would be added")
        return 0

    if changed == 0:
        macmini_feed = [it for it in items if re.search(r"Mac\s+mini", normalize_text(it.title), re.I)]
        if macmini_feed:
            latest_mm = max(macmini_feed, key=lambda x: x.published)
            print(f"diagnostic: newest Mac mini item in RSS: {latest_mm.published.date()} {latest_mm.title}")
        else:
            print("diagnostic: RSS currently contains no Mac mini item")

    if changed:
        save_json(data_path, data)
    # Save unmatched even when products.json did not change.
    save_json(unmatched_path, unmatched[-300:])
    print(f"done: {changed} release(s) added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
