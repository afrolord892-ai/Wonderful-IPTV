#!/usr/bin/env python3
"""Merge country XMLTV guides into one playlist-matched Wonderful EPG.

Uses exact TVG/XMLTV IDs first, then a conservative exact display-name match
within the same country. It never creates synthetic programme listings.
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parents[1]
PLAYLIST = ROOT / "playlists" / "Wonderful.m3u"
EPG_DIR = ROOT / "epg"
BASE_XML = EPG_DIR / "guide.xml"
GUIDE_XML = EPG_DIR / "guide.xml"
GUIDE_GZ = EPG_DIR / "guide.xml.gz"
REPORT_OUT = EPG_DIR / "country-merge-report.json"

# Country feeds currently listed by the public IPTV-EPG guide directory.
COUNTRY_CODES = set("""
AL AR AM AU AT BS BY BE BO BA BR BG CA CL CO CR HR CW CZ DK DO EG SV FI FR
GE DE GH GR GT HN HK HU IS IN ID IL IT JM LV LB LT LU MK MY MT MX ME NL NZ
NI NG NO PA PY PE PH PL PT RO RU RS SG SI ZA KR ES SE CH TW TH TT TR UG UA
AE GB US UY VE ZW
""".split())

BLOCKED_TERMS = (
    "adult", "xxx", "porn", "erotic", "18+", "casino", "gambling",
    "betting", "sportsbook", "sports book", "bookmaker", "wager", "poker",
)
QUALITY_SUFFIX = re.compile(
    r"\s*[\[(]\s*(?:sd|hd|fhd|uhd|4k|8k|hevc|h\.?264|h\.?265)\s*[\])]\s*$",
    re.I,
)
USER_AGENT = "Wonderful-IPTV-EPG/1.0 (+https://github.com/afrolord892-ai/Wonderful-IPTV)"
MAX_WORKERS = 4
HTTP_TIMEOUT = 120


def unsafe(value: str) -> bool:
    text = (value or "").casefold()
    return any(term in text for term in BLOCKED_TERMS)


def normalize_name(value: str) -> str:
    value = html.unescape(value or "").strip()
    while True:
        cleaned = QUALITY_SUFFIX.sub("", value).strip()
        if cleaned == value:
            break
        value = cleaned
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def attrs_from_extinf(line: str) -> dict[str, str]:
    return {
        key.casefold(): html.unescape(value)
        for key, value in re.findall(r'([\w-]+)="([^"]*)"', line)
    }


def country_for(attrs: dict[str, str]) -> str:
    explicit = (attrs.get("tvg-country") or "").strip().upper()
    if explicit in COUNTRY_CODES:
        return explicit
    group = attrs.get("group-title", "")
    m = re.match(r"^\s*([A-Z]{2})\s*\|", group, re.I)
    if m and m.group(1).upper() in COUNTRY_CODES:
        return m.group(1).upper()
    channel_id = attrs.get("tvg-id", "")
    # Covers IDs such as Channel.ng and US local IDs such as Station.us_locals1.
    m = re.search(r"\.([a-z]{2})(?:_[a-z0-9]+)?$", channel_id, re.I)
    if m and m.group(1).upper() in COUNTRY_CODES:
        return m.group(1).upper()
    return ""


def load_playlist():
    if not PLAYLIST.is_file():
        raise RuntimeError(f"Playlist not found: {PLAYLIST}")
    ids: set[str] = set()
    titles_by_id: dict[str, str] = {}
    by_country_name: dict[tuple[str, str], set[str]] = defaultdict(set)
    global_name_ids: dict[str, set[str]] = defaultdict(set)
    countries: set[str] = set()
    current = None

    with PLAYLIST.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.strip()
            if line.startswith("#EXTINF:"):
                current = line
                continue
            if not line or line.startswith("#") or current is None:
                continue
            attrs = attrs_from_extinf(current)
            tvg_id = attrs.get("tvg-id", "").strip()
            title = current.split(",", 1)[1].strip() if "," in current else attrs.get("tvg-name", "")
            if not tvg_id or not title or unsafe(tvg_id + " " + title + " " + attrs.get("group-title", "")):
                current = None
                continue
            ids.add(tvg_id)
            titles_by_id.setdefault(tvg_id, title)
            country = country_for(attrs)
            if country:
                countries.add(country)
                key = normalize_name(title)
                if key:
                    by_country_name[(country, key)].add(tvg_id)
            name_alias = normalize_name(attrs.get("tvg-name", ""))
            title_key = normalize_name(title)
            for key in {name_alias, title_key}:
                if key:
                    global_name_ids[key].add(tvg_id)
            current = None

    return ids, titles_by_id, by_country_name, global_name_ids, countries


def download_country(code: str, directory: Path):
    url = f"https://iptv-epg.org/files/epg-{code.casefold()}.xml"
    target = directory / f"epg-{code.casefold()}.xml"
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/xml,text/xml,*/*",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                ctype = response.headers.get("Content-Type", "").casefold()
                if "xml" not in ctype and "text" not in ctype and "octet-stream" not in ctype:
                    raise RuntimeError(f"Unexpected content type: {ctype}")
                with target.open("wb") as out:
                    shutil.copyfileobj(response, out, length=1024 * 1024)
            if target.stat().st_size < 128:
                raise RuntimeError("Guide feed is unexpectedly small")
            return code, target, target.stat().st_size, None
        except Exception as exc:
            last_error = str(exc)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(1.0 + attempt)
    return code, None, 0, last_error


def local_tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def safe_programme(elem, channel_name: str) -> bool:
    if unsafe(channel_name):
        return False
    for child in list(elem):
        tag = local_tag(child)
        if tag in {"title", "sub-title", "desc"}:
            if unsafe("".join(child.itertext())):
                return False
    return True


def add_programme(elem, target_id: str, spool, seen: set[tuple[str, str]], channel_name: str):
    if not safe_programme(elem, channel_name):
        return False
    start = elem.get("start", "")
    if not start:
        return False
    key = (target_id, start)
    if key in seen:
        return False
    seen.add(key)
    elem.set("channel", target_id)
    spool.write(ET.tostring(elem, encoding="utf-8"))
    spool.write(b"\n")
    return True


def parse_base_guide(base_xml: Path, ids: set[str], titles_by_id: dict[str, str], spool, seen):
    if not base_xml.is_file():
        return 0
    count = 0
    root = None
    try:
        for event, elem in ET.iterparse(base_xml, events=("start", "end")):
            if event == "start" and root is None:
                root = elem
                continue
            if event != "end":
                continue
            tag = local_tag(elem)
            if tag == "programme":
                target_id = elem.get("channel", "")
                if target_id in ids and add_programme(
                    elem, target_id, spool, seen, titles_by_id.get(target_id, target_id)
                ):
                    count += 1
                if root is not None:
                    root.clear()
            elif tag == "channel":
                if root is not None:
                    root.clear()
    except ET.ParseError as exc:
        raise RuntimeError(f"Base iptv-org EPG XML is malformed: {exc}") from exc
    return count


def map_country_channels(path: Path, code: str, ids, by_country_name, global_name_ids):
    feed_to_playlist: dict[str, str] = {}
    root = None
    for event, elem in ET.iterparse(path, events=("start", "end")):
        if event == "start" and root is None:
            root = elem
            continue
        if event != "end" or local_tag(elem) != "channel":
            continue
        feed_id = (elem.get("id") or "").strip()
        display_names = [
            "".join(child.itertext()).strip()
            for child in list(elem)
            if local_tag(child) == "display-name"
        ]
        names = [x for x in display_names if x]
        if feed_id and not unsafe(feed_id + " " + " ".join(names)):
            if feed_id in ids:
                feed_to_playlist[feed_id] = feed_id
            else:
                candidates: set[str] = set()
                for name in names:
                    key = normalize_name(name)
                    if not key:
                        continue
                    candidates.update(by_country_name.get((code, key), set()))
                # Country-specific exact names take priority. A global exact
                # name is allowed only if it resolves to one playlist TVG ID.
                if not candidates:
                    for name in names:
                        key = normalize_name(name)
                        if key:
                            global_candidates = global_name_ids.get(key, set())
                            if len(global_candidates) == 1:
                                candidates.update(global_candidates)
                if len(candidates) == 1:
                    feed_to_playlist[feed_id] = next(iter(candidates))
        if root is not None:
            root.clear()
    return feed_to_playlist


def parse_country_programmes(path: Path, feed_to_playlist, titles_by_id, spool, seen):
    count = 0
    root = None
    for event, elem in ET.iterparse(path, events=("start", "end")):
        if event == "start" and root is None:
            root = elem
            continue
        if event != "end" or local_tag(elem) != "programme":
            continue
        feed_id = elem.get("channel", "")
        target_id = feed_to_playlist.get(feed_id)
        if target_id:
            if add_programme(elem, target_id, spool, seen, titles_by_id.get(target_id, target_id)):
                count += 1
        if root is not None:
            root.clear()
    return count


def write_merged_guide(spool_path: Path, output_xml: Path, output_gz: Path, titles_by_id, seen):
    covered_ids = {channel_id for channel_id, _start in seen}
    output_xml.parent.mkdir(parents=True, exist_ok=True)
    with output_xml.open("wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(b'<tv generator-info-name="Wonderful IPTV EPG merger">\n')
        for channel_id in sorted(covered_ids):
            if unsafe(channel_id + " " + titles_by_id.get(channel_id, "")):
                continue
            out.write(f"<channel id={quoteattr(channel_id)}><display-name lang=\"en\">{escape(titles_by_id.get(channel_id, channel_id))}</display-name></channel>\n".encode("utf-8"))
        with spool_path.open("rb") as programmes:
            shutil.copyfileobj(programmes, out, length=1024 * 1024)
        out.write(b"</tv>\n")
    with output_xml.open("rb") as src, output_gz.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed:
            shutil.copyfileobj(src, compressed, length=1024 * 1024)
    return len(covered_ids), output_xml.stat().st_size, output_gz.stat().st_size


def main():
    ids, titles_by_id, by_country_name, global_name_ids, countries = load_playlist()
    if not ids:
        raise RuntimeError("No usable TVG IDs were found in Wonderful.m3u.")
    selected = sorted(countries & COUNTRY_CODES)
    print(f"Playlist TVG IDs: {len(ids):,}; country codes found: {len(countries)}; country feeds selected: {len(selected)}")

    EPG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wonderful-epg-") as temp:
        tempdir = Path(temp)
        country_files = {}
        failures = {}
        with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(download_country, code, tempdir) for code in selected]
            for future in cf.as_completed(futures):
                code, path, size, error = future.result()
                if path:
                    country_files[code] = path
                    print(f"Downloaded {code}: {size:,} bytes")
                else:
                    failures[code] = error
                    print(f"WARNING: {code} guide download failed: {error}", file=sys.stderr)

        seen: set[tuple[str, str]] = set()
        spool_path = tempdir / "programmes.xml.part"
        with spool_path.open("wb") as spool:
            base_count = parse_base_guide(BASE_XML, ids, titles_by_id, spool, seen)
            country_counts = {}
            for code, path in sorted(country_files.items()):
                mapping = map_country_channels(path, code, ids, by_country_name, global_name_ids)
                added = parse_country_programmes(path, mapping, titles_by_id, spool, seen)
                country_counts[code] = {
                    "mapped_channels": len(set(mapping.values())),
                    "programmes_added": added,
                }
                print(f"{code}: mapped {country_counts[code]['mapped_channels']} channels; added {added} programmes")

        covered, xml_bytes, gz_bytes = write_merged_guide(
            spool_path, GUIDE_XML, GUIDE_GZ, titles_by_id, seen
        )
        report = {
            "playlist_tvg_ids": len(ids),
            "playlist_entries": sum(1 for _ in PLAYLIST.open("r", encoding="utf-8", errors="replace") if _.startswith("#EXTINF:")),
            "playlist_countries_found": sorted(countries),
            "country_feeds_selected": selected,
            "country_feeds_downloaded": sorted(country_files),
            "country_feed_failures": failures,
            "base_iptv_org_programmes_kept": base_count,
            "country_feed_results": country_counts,
            "programme_records_in_merged_guide": len(seen),
            "unique_playlist_channels_with_programmes": covered,
            "coverage_percent_of_tvg_ids": round(100 * covered / len(ids), 2),
            "xmltv_size_bytes": xml_bytes,
            "xmltv_gzip_size_bytes": gz_bytes,
            "match_policy": "Exact TVG/XMLTV ID, or exact normalized display-name match in the same country when unambiguous. No fuzzy name matching and no synthetic programmes.",
            "guide_source": "iptv-epg.org country XMLTV feeds plus existing iptv-org/epg guide.",
            "filtered_categories": "Unsafe or inappropriate channel labels and programme titles/descriptions are excluded.",
        }
        REPORT_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"COUNTRY EPG MERGE FAILED: {exc}", file=sys.stderr)
        raise
