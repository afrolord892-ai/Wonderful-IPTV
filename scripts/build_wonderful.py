#!/usr/bin/env python3
"""Build Wonderful from public free-TV catalogues and curated directories.

Source-listed entries are labelled by their origin and verification/status.
Unknown or offline status is not silently represented as verified working.
Known denied hosts and NSFW entries are excluded.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "playlists" / "Wonderful.m3u"
REPORT = ROOT / "playlists" / "Wonderful-report.json"

SOURCES = [
    ("IPTV-org", "https://iptv-org.github.io/iptv/index.m3u", "catalog"),
    ("Free-TV/IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", "curated"),
    ("FreeCastHub", "https://raw.githubusercontent.com/freecasthub/public-iptv/main/playlist.m3u", "curated"),
    ("LegalFreeIPTV", "https://biskyt.github.io/legalFreeIPTVplaylist/live.m3u", "curated"),
    ("Doubo Public Directory", "https://cdn.jsdelivr.net/gh/streamline-iptv/iptv-playlists@dist/index.m3u", "curated"),
    ("WizakorHD Curated Public TV", "https://raw.githubusercontent.com/wizakorhd/iptv/main/playlist.m3u", "curated"),
    ("Tubi FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/tubi_all.m3u", "curated"),
    ("Roku FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/roku_all.m3u", "curated"),
    ("Plex FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/plex_all.m3u", "curated"),
    ("Pluto TV FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/plutotv_all.m3u", "curated"),
    ("Samsung TV Plus FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/samsungtvplus_all.m3u", "curated"),
    ("Apsattv DistroTV", "https://www.apsattv.com/distro.m3u", "curated"),
    ("Apsattv Vizio", "https://www.apsattv.com/vizio.m3u", "curated"),
    ("Apsattv Vidaa", "https://www.apsattv.com/vidaa.m3u", "curated"),
    ("Apsattv Metax", "https://www.apsattv.com/metax.m3u", "curated"),
    ("Apsattv FreeLiveSports", "https://www.apsattv.com/freelivesports.m3u", "curated"),
    ("Apsattv GalxyTV", "https://www.apsattv.com/galxytv.m3u", "curated"),
    ("Apsattv LocalNow", "https://www.apsattv.com/localnow.m3u", "curated"),
    ("Apsattv TCL TV Plus", "https://www.apsattv.com/tclplus.m3u", "curated"),
    ("Apsattv HP FAST", "https://www.apsattv.com/hp.m3u", "curated"),
    ("Apsattv RewardedTV", "https://www.apsattv.com/rewardedtv.m3u", "curated"),
    ("Apsattv Xumo", "https://www.apsattv.com/xumo.m3u", "curated"),
    ("Apsattv Cineverse", "https://www.apsattv.com/cineverse.m3u", "curated"),
    ("Apsattv Whale TV Plus", "https://www.apsattv.com/whaletvplus_all.m3u", "curated"),
]
CHANNELS_DB_URL = "https://iptv-org.github.io/api/channels.json"
IPTVCAT_SITEMAP = "https://iptvcat.com/sitemap"
IPTVCAT_EXPORT = "https://iptvcat.com/ajax/streams_a?action=playlist_download"
USER_AGENT = "Wonderful-IPTV-Playlist/1.0 (+https://github.com/afrolord892-ai/Wonderful-IPTV)"
MAX_CAT_PAGES = 500
REQUEST_DELAY = 0.05
CAT_WORKERS = 8
MINIMUM_OUTPUT_ENTRIES = 30000

HTTP_LOCAL = threading.local()

def get_http_opener():
    opener = getattr(HTTP_LOCAL, "opener", None)
    if opener is None:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        HTTP_LOCAL.opener = opener
    return opener

BROADCASTER_HOSTS = {
    "getaj.net", "thehlive.com", "rtve.es", "nhkworld.jp", "trt.com.tr", "ard-mcdn.de",
}
DENIED_HOSTS = {
    "freeott.top", "freem3u.xyz", "dethich.pw", "pastebin.com",
}
BLOCKED_EXTINF = (
    "adult", "xxx", "porn", "erotic", "18+",
    "casino", "gambling", "betting", "sportsbook", "sports book",
    "bookmaker", "wager", "poker",
)
QUALITY_SUFFIX = re.compile(
    r"\s*[\[(]\s*(?:\d{3,4}\s*p|sd|hd|fhd|uhd|4k|8k|not\s+24/7|hevc|h\.?264|h\.?265|"
    r"aac|avc|mpeg[\w -]*)\s*[\])]\s*$",
    re.IGNORECASE,
)


@dataclass
class Entry:
    name: str
    url: str
    extinf: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    trust: str = ""
    country: str = ""
    channel_key: str = ""
    name_key: str = ""


def fetch(url: str, *, method: str = "GET", body: str | None = None,
          referer: str | None = None, accept: str | None = None,
          timeout: int = 45, attempts: int = 3) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept or "*/*",
    }
    if referer:
        headers["Referer"] = referer
    data = body.encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Origin"] = "https://iptvcat.com"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = Request(url, data=data, headers=headers, method=method)
            with get_http_opener().open(req, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Fetch failed for {url}: {last_error}")


def fetch_text(url: str, **kwargs) -> str:
    return fetch(url, **kwargs).decode("utf-8-sig", errors="replace")


def attributes(extinf: str) -> dict[str, str]:
    # Quoted M3U attributes; title itself is not parsed as an attribute.
    return {k.lower(): html.unescape(v) for k, v in
            re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def clean_name(name: str) -> str:
    name = html.unescape(name).strip()
    while True:
        cleaned = QUALITY_SUFFIX.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    name = re.sub(r"\s+", " ", name)
    return name


def normalized_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", clean_name(name))
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = name.casefold()
    name = re.sub(r"[^\w]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def country_from_id(channel_id: str) -> str:
    base = channel_id.split("@", 1)[0]
    match = re.search(r"\.([a-z]{2,3})$", base, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def normalized_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    # Keep the original path and query: signed URLs and alternate stream paths
    # may represent distinct feeds. Normalize only URL casing and fragments.
    query = parsed.query
    if query.casefold() in ("checkedby:iptvcat.com", "checkedby=iptvcat.com"):
        query = ""
    return urlunparse((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
        parsed.params, query, ""
    ))


def parse_m3u(text: str, source: str, trust: str) -> list[Entry]:
    lines = [line.strip() for line in text.replace("\ufeff", "").splitlines()]
    entries: list[Entry] = []
    extinf: str | None = None
    tags: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            extinf = line
            tags = []
            continue
        if line.startswith("#"):
            if extinf is not None:
                tags.append(line)
            continue
        if extinf is None:
            continue
        url = normalized_url(line)
        if url:
            title = extinf.split(",", 1)[1].strip() if "," in extinf else "Unknown channel"
            entries.append(Entry(
                name=clean_name(title),
                url=url,
                extinf=extinf,
                tags=list(tags),
                source=source,
                trust=trust,
            ))
        extinf = None
        tags = []
    return entries


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "a":
            values = dict(attrs)
            self._href = values.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


class OnlineChannelParser(HTMLParser):
    """Parse IPTV Cat's channel rows without parsing markup embedded in data-content attributes."""
    def __init__(self) -> None:
        super().__init__()
        self.current_name: str | None = None
        self.current_online = False
        self.items: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag.lower() == "span" and "channel_name" in classes:
            self.current_name = values.get("title") or None
            self.current_online = False
        elif self.current_name and tag.lower() == "div" and "state" in classes and "online" in classes:
            self.current_online = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "tr" and self.current_name:
            self.items.append((self.current_name, self.current_online))
            self.current_name = None
            self.current_online = False


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""


def host_matches(host: str, domain: str) -> bool:
    host, domain = host.lower().strip("."), domain.lower().strip(".")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def set_extinf_attribute(extinf: str, key: str, value: str) -> str:
    prefix, separator, title = extinf.partition(",")
    if not separator:
        return extinf
    escaped = html.escape(value, quote=True)
    pattern = re.compile(rf'\\b{re.escape(key)}="[^"]*"', re.IGNORECASE)
    if pattern.search(prefix):
        prefix = pattern.sub(f'{key}="{escaped}"', prefix, count=1)
    else:
        prefix = f'{prefix} {key}="{escaped}"'
    return prefix + "," + title


def load_channel_database():
    by_id: dict[str, set[str]] = {}
    by_name_country: dict[tuple[str, str], set[str]] = {}
    ids_by_name_country: dict[tuple[str, str], set[str]] = {}
    ids_by_name: dict[str, set[str]] = {}
    nsfw_ids: set[str] = set()
    try:
        data = json.loads(fetch_text(CHANNELS_DB_URL, timeout=60))
    except Exception as exc:
        print(f"WARNING: channel database unavailable: {exc}", file=sys.stderr)
        return by_id, by_name_country, nsfw_ids, ids_by_name_country, ids_by_name
    for channel in data if isinstance(data, list) else []:
        cid = str(channel.get("id") or "").split("@", 1)[0]
        cid_key = cid.casefold()
        if channel.get("is_nsfw") and cid:
            nsfw_ids.add(cid_key)
        country = str(channel.get("country") or "").upper()
        names = [channel.get("name", ""), *(channel.get("alt_names") or [])]
        for name in names:
            nkey = normalized_name(str(name))
            if not nkey:
                continue
            if cid:
                ids_by_name_country.setdefault((nkey, country), set()).add(cid)
                ids_by_name.setdefault(nkey, set()).add(cid)
        website = channel.get("website") or ""
        domain = host_of(website)
        if domain and cid:
            by_id.setdefault(cid_key, set()).add(domain)
        if domain:
            for name in names:
                nkey = normalized_name(str(name))
                if nkey:
                    by_name_country.setdefault((nkey, country), set()).add(domain)
    return by_id, by_name_country, nsfw_ids, ids_by_name_country, ids_by_name


def enrich_and_key(entry: Entry, by_id, by_name_country,
                   ids_by_name_country, ids_by_name) -> bool:
    attrs = attributes(entry.extinf)
    tvg_id = (attrs.get("tvg-id") or "").strip()
    country = (attrs.get("tvg-country") or country_from_id(tvg_id)).upper()
    if not country and attrs.get("group-title"):
        match = re.match(r"^([A-Z]{2,3})\\s*\\|", attrs["group-title"])
        country = match.group(1) if match else ""
    entry.name_key = normalized_name(entry.name)

    inferred = False
    if not tvg_id and entry.name_key:
        candidates = set(ids_by_name_country.get((entry.name_key, country), set())) if country else set()
        if not candidates:
            candidates = set(ids_by_name.get(entry.name_key, set()))
        if len(candidates) == 1:
            tvg_id = next(iter(candidates))
            entry.extinf = set_extinf_attribute(entry.extinf, "tvg-id", tvg_id)
            inferred = True

    channel_id = tvg_id.split("@", 1)[0]
    if not country:
        country = country_from_id(tvg_id)
    entry.country = country
    if channel_id:
        entry.channel_key = f"id:{channel_id.casefold()}"
    else:
        entry.channel_key = f"name:{country}:{entry.name_key}"
    return inferred


def is_verified_catalog_entry(entry: Entry, by_id, by_name_country, nsfw_ids: set[str], curated_urls: set[str]) -> bool:
    if entry.url in curated_urls:
        return True
    host = host_of(entry.url)
    if not host or any(host_matches(host, denied) for denied in DENIED_HOSTS):
        return False
    if host in BROADCASTER_HOSTS or any(host_matches(host, d) for d in BROADCASTER_HOSTS):
        return True
    attrs = attributes(entry.extinf)
    channel_id = attrs.get("tvg-id", "").split("@", 1)[0].casefold()
    if channel_id and channel_id in nsfw_ids:
        return False
    domains = set(by_id.get(channel_id, set())) if channel_id else set()
    if not domains:
        country = entry.country
        domains.update(by_name_country.get((entry.name_key, country), set()))
        if not domains and not country:
            # A name-only match is allowed only when it identifies one domain.
            for (name, _country), matched in by_name_country.items():
                if name == entry.name_key:
                    domains.update(matched)
    return any(host_matches(host, domain) for domain in domains)


def discover_iptvcat_pages() -> list[str]:
    sitemap_html = fetch_text(IPTVCAT_SITEMAP, timeout=60)
    parser = LinkParser()
    parser.feed(sitemap_html)
    pages: set[str] = set()
    excluded = {
        "home_22", "new_channels-41", "sport__13", "sport__5", "faq",
        "contact_us", "privacy", "cookies", "sitemap",
    }
    for href, label in parser.links:
        absolute = urljoin(IPTVCAT_SITEMAP, href)
        parsed = urlparse(absolute)
        if parsed.hostname not in ("iptvcat.com", "www.iptvcat.com"):
            continue
        path = parsed.path.strip("/")
        if not path or path in excluded or path.startswith(("mark/", "s/")):
            continue
        if path.split("/", 1)[0] in excluded:
            continue
        if re.fullmatch(r"[a-z0-9_%.-]+(?:/[0-9]+)?", path, re.IGNORECASE):
            pages.add("https://iptvcat.com/" + path)
    return sorted(pages)


def page_and_pagination_urls(page_url: str, page_html: str) -> list[str]:
    parser = LinkParser()
    parser.feed(page_html)
    base_path = urlparse(page_url).path.rstrip("/")
    result = {page_url}
    for href, _label in parser.links:
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.hostname != "iptvcat.com":
            continue
        candidate = parsed.path.rstrip("/")
        if re.fullmatch(re.escape(base_path) + r"/[0-9]+", candidate):
            result.add("https://iptvcat.com" + candidate)
    return sorted(result)


def export_iptvcat_page(sessionless_page_url: str) -> tuple[str, set[str]]:
    page_html = fetch_text(sessionless_page_url, timeout=45)
    status_parser = OnlineChannelParser()
    status_parser.feed(page_html)
    online_status = {
        normalized_name(name): is_online
        for name, is_online in status_parser.items
    }
    page_path = urlparse(sessionless_page_url).path
    # Mirrors IPTV Cat's public "Download this list" button:
    # POST /ajax/streams_a?action=playlist_download with the page path as body.
    raw = fetch(
        IPTVCAT_EXPORT,
        method="POST",
        body=page_path,
        referer=sessionless_page_url,
        accept="application/json, text/javascript, */*; q=0.01",
        timeout=60,
    )
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise RuntimeError("IPTV Cat country export did not return JSON")
    if not payload.get("status") or not isinstance(payload.get("data"), str):
        raise RuntimeError("IPTV Cat country export returned no playlist data")
    return payload["data"], online_status


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    counters = {
        "source_entries": {},
        "iptvcat_pages_seen": 0,
        "iptvcat_pages_exported": 0,
        "iptvcat_export_errors": [],
        "iptvcat_entries_unverified": 0,
        "iptvcat_entries_accepted": 0,
        "iptvcat_offline_entries_retained": 0,
        "non_http_rejected": 0,
        "duplicate_same_channel_same_url": 0,
        "duplicate_url_other_name": 0,
    }
    all_entries: list[Entry] = []
    fetched_texts: dict[str, str] = {}

    def fetch_source(source):
        name, url, trust = source
        try:
            body = fetch_text(url, timeout=90)
            return name, trust, parse_m3u(body, name, trust), None
        except Exception as exc:
            return name, trust, [], str(exc)[:300]

    # Fetch many catalogues concurrently so regional/community endpoints do
    # not make the run unnecessarily slow. The parser still validates each row.
    with ThreadPoolExecutor(max_workers=12) as pool:
        for name, trust, parsed, error in pool.map(fetch_source, SOURCES):
            counters["source_entries"][name] = len(parsed)
            all_entries.extend(parsed)
            if error:
                print(f"WARNING: {name} unavailable: {error}", file=sys.stderr)
            else:
                print(f"{name}: parsed {len(parsed)} HTTP/HTTPS entries")

    (channels_by_id, channels_by_name_country, nsfw_ids,
     ids_by_name_country, ids_by_name) = load_channel_database()
    counters["tvg_id_inferred"] = 0
    curated_urls = {
        e.url for e in all_entries if e.trust == "curated"
    }

    # Keep the complete IPTV-org catalogue. Domain matching is retained as a
    # provenance label, not used as a reason to discard public catalogue rows.
    verified_base: list[Entry] = []
    counters["iptvorg_unverified_retained"] = 0
    for entry in all_entries:
        if entry.trust == "catalog":
            if enrich_and_key(entry, channels_by_id, channels_by_name_country, ids_by_name_country, ids_by_name):
                counters[\"tvg_id_inferred\"] += 1
            if is_verified_catalog_entry(entry, channels_by_id, channels_by_name_country, nsfw_ids, curated_urls):
                entry.trust = "IPTV-org-officially-matched"
            else:
                entry.trust = "IPTV-org-catalog-listed-unverified"
                counters["iptvorg_unverified_retained"] += 1
            verified_base.append(entry)
        else:
            verified_base.append(entry)
    all_entries = verified_base
    curated_urls = {e.url for e in all_entries if e.trust == "curated"}

    # IPTV Cat: use its country/region sitemap and native one-page M3U export.
    # The page list is re-discovered at each run, so new countries are picked up.
    try:
        cat_pages = discover_iptvcat_pages()
    except Exception as exc:
        cat_pages = []
        print(f"WARNING: IPTV Cat sitemap unavailable: {exc}", file=sys.stderr)

    expanded_pages: set[str] = set()
    def expand_cat_page(page_url: str):
        try:
            page_html = fetch_text(page_url, timeout=45)
            return page_url, page_and_pagination_urls(page_url, page_html), None
        except Exception as exc:
            return page_url, [], str(exc)[:250]

    # Bounded parallelism prevents hundreds of country pages from serializing
    # into a multi-hour build while avoiding an excessive burst at IPTV Cat.
    with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
        for page_url, page_results, error in pool.map(expand_cat_page, cat_pages):
            expanded_pages.update(page_results)
            if error:
                counters["iptvcat_export_errors"].append({"page": page_url, "error": error})

    cat_entries: list[Entry] = []
    def export_cat_page(page_url: str):
        accepted: list[Entry] = []
        offline_count = 0
        unverified_count = 0
        try:
            page_m3u, online_status = export_iptvcat_page(page_url)
            parsed = parse_m3u(page_m3u, "IPTV Cat", "discovery")
            for entry in parsed:
                if enrich_and_key(entry, channels_by_id, channels_by_name_country, ids_by_name_country, ids_by_name):
                counters[\"tvg_id_inferred\"] += 1
                listed_status = online_status.get(entry.name_key)
                if listed_status is False:
                    offline_count += 1
                    availability = "offline-listed"
                elif listed_status is True:
                    availability = "online-listed"
                else:
                    availability = "status-unknown"
                matched = is_verified_catalog_entry(
                    entry, channels_by_id, channels_by_name_country,
                    nsfw_ids, curated_urls
                )
                verification = "officially-matched" if matched else "unverified-source"
                if not matched:
                    unverified_count += 1
                entry.trust = f"IPTV-Cat-{availability}-{verification}"
                # Retain the source row, but mark its availability accurately.
                accepted.append(entry)
            return page_url, accepted, offline_count, unverified_count, None
        except Exception as exc:
            return page_url, [], offline_count, unverified_count, str(exc)[:250]

    selected_cat_pages = sorted(expanded_pages)[:MAX_CAT_PAGES]
    with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
        for page_url, accepted, offline_count, unverified_count, error in pool.map(export_cat_page, selected_cat_pages):
            counters["iptvcat_pages_exported"] += 1 if error is None else 0
            counters["iptvcat_offline_entries_retained"] += offline_count
            counters["iptvcat_entries_unverified"] += unverified_count
            counters["iptvcat_entries_accepted"] += len(accepted)
            cat_entries.extend(accepted)
            if error:
                counters["iptvcat_export_errors"].append({"page": page_url, "error": error})
    counters["iptvcat_pages_seen"] = len(expanded_pages)
    counters["source_entries"]["IPTV Cat (all exported entries)"] = len(cat_entries)
    all_entries.extend(cat_entries)

    # Enrich identity, reject non-HTTP(S), obvious denied hosts and NSFW groups.
    kept: list[Entry] = []
    for entry in all_entries:
        if not entry.url:
            counters["non_http_rejected"] += 1
            continue
        attrs = attributes(entry.extinf)
        channel_id = attrs.get("tvg-id", "").split("@", 1)[0].casefold()
        if channel_id and channel_id in nsfw_ids:
            continue
        group = attrs.get("group-title", "").casefold()
        title = entry.name.casefold()
        if any(term in group or term in title for term in BLOCKED_EXTINF):
            continue
        if enrich_and_key(entry, channels_by_id, channels_by_name_country, ids_by_name_country, ids_by_name):
            counters[\"tvg_id_inferred\"] += 1
        host = host_of(entry.url)
        if not host or any(host_matches(host, d) for d in DENIED_HOSTS):
            continue
        kept.append(entry)

    # Exact duplicate URLs are removed globally; alternate URLs for one
    # channel remain. Prefer carefully curated source metadata when the same
    # URL appears more than once.
    source_rank = {
        "LegalFreeIPTV": 0, "Free-TV/IPTV": 1,
        "WizakorHD Curated Public TV": 2, "Doubo Public Directory": 3,
        "FreeCastHub": 4, "Tubi FAST": 5, "Roku FAST": 5,
        "Plex FAST": 5, "Pluto TV FAST": 5, "Samsung TV Plus FAST": 5,
        "IPTV-org": 6, "IPTV Cat": 7,
    }
    kept.sort(key=lambda e: (source_rank.get(e.source, 9), e.country, e.name_key, e.url))
    deduped: list[Entry] = []
    seen_urls: set[str] = set()
    seen_channel_urls: set[tuple[str, str]] = set()
    for entry in kept:
        channel_url_key = (entry.channel_key, entry.url)
        if entry.url in seen_urls:
            counters["duplicate_url_other_name"] += 1
            continue
        if channel_url_key in seen_channel_urls:
            counters["duplicate_same_channel_same_url"] += 1
            continue
        seen_urls.add(entry.url)
        seen_channel_urls.add(channel_url_key)
        deduped.append(entry)

    if len(deduped) < MINIMUM_OUTPUT_ENTRIES:
        raise RuntimeError(
            f"Only {len(deduped)} unique source-listed entries were found; refusing to overwrite the last good playlist."
        )

    # Keep groups/countries readable in IPTV Smarters. Channel-name duplicates
    # with genuinely different URLs intentionally appear as separate entries.
    deduped.sort(key=lambda e: (
        e.country or "ZZ",
        attributes(e.extinf).get("group-title", "Other").casefold(),
        e.name_key,
        e.url,
    ))

    # IPTV Smarters and other strict M3U parsers are most compatible when
    # each EXTINF record is followed immediately by its stream URL. Keep
    # provenance and verification detail in the JSON report, not between
    # playlist records.
    def smarters_extinf(entry: Entry) -> str:
        source_attrs = attributes(entry.extinf)
        allowed_attrs = ("tvg-id", "tvg-name", "tvg-logo", "group-title", "tvg-country", "tvg-language")
        rendered_attrs = []
        for key in allowed_attrs:
            value = source_attrs.get(key)
            if value:
                value = re.sub(r"[\r\n]+", " ", value).replace("&", "&amp;").replace('"', "&quot;")
                rendered_attrs.append(f'{key}="{value}"')
        title = re.sub(r"[\r\n]+", " ", clean_name(entry.name)).strip() or "Unknown channel"
        suffix = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""
        return f"#EXTINF:-1{suffix},{title}"

    output_lines = ["#EXTM3U"]
    for entry in deduped:
        output_lines.append(smarters_extinf(entry))
        output_lines.append(entry.url)
    OUT.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    counters["final_channels_or_streams"] = len(deduped)
    counters["playlist_entries_with_tvg_id"] = sum(1 for e in deduped if attributes(e.extinf).get("tvg-id"))
    counters["playlist_entries_without_tvg_id"] = len(deduped) - counters["playlist_entries_with_tvg_id"]
    counters["unique_tvg_ids"] = len({attributes(e.extinf).get("tvg-id") for e in deduped if attributes(e.extinf).get("tvg-id")})
    counters["unique_channel_identities"] = len({e.channel_key for e in deduped})
    counters["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    counters["output_bytes"] = OUT.stat().st_size
    counters["playlist_format"] = "Strict M3U: #EXTM3U header; each #EXTINF record is immediately followed by its HTTP(S) stream URL; source verification details are retained in this JSON report."
    counters["source_notes"] = {
        "IPTV-org": "Complete HTTP/HTTPS source catalogue retained except NSFW/blocked/denied-host entries; broadcaster-domain matches are labelled.",
        "IPTV Cat": "Exported HTTP/HTTPS rows retained except NSFW/blocked/denied-host entries; online/offline/unknown status and domain-match labels are included.",
        "Doubo Public Directory": "Separate directory advertised as broadcaster-provided free public streams; availability and regional restrictions can change.",
        "WizakorHD Curated Public TV": "Community-curated list described by its maintainer as original URLs from free services; personal-use terms and attribution apply.",
        "Tubi/Roku/Plex/Pluto/Samsung FAST": "Community-generated service catalogues; stream availability and territorial restrictions vary. Use remains subject to the respective service terms.",
        "Apsattv FAST catalogues": "Community-maintained free-service M3U lists; its catalogue notes that some feeds may be stale or geo-restricted.",

        "Free-TV/IPTV": "Curated free-TV source; subject to broadcaster and geographic restrictions.",
        "FreeCastHub": "Curated free public-broadcaster source.",
        "LegalFreeIPTV": "Curated legal-free feed with published acceptance criteria.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(counters, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(counters, indent=2))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        raise
