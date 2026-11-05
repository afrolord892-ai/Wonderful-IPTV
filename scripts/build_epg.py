#!/usr/bin/env python3
"""Build a safe, playlist-matched XMLTV EPG from public iptv-org/epg mappings."""
from __future__ import annotations

import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parents[1]
PLAYLIST = ROOT / "playlists" / "Wonderful.m3u"
REPORT = ROOT / "playlists" / "Wonderful-report.json"
EPG_DIR = ROOT / "epg"
CHANNELS_OUT = EPG_DIR / "channels.xml"
GUIDE_OUT = EPG_DIR / "guide.xml"
GUIDE_GZ = EPG_DIR / "guide.xml.gz"
EPG_URL = "https://raw.githubusercontent.com/afrolord892-ai/Wonderful-IPTV/main/epg/guide.xml.gz"

BLOCKED_TERMS = (
    "adult", "xxx", "porn", "erotic", "18+", "casino", "gambling",
    "betting", "sportsbook", "sports book", "bookmaker", "wager", "poker",
)

# Only active, public/free-TV guide families are considered. DStv, pay-TV
# provider-only feeds and gambling-branded feed packages are deliberately not used.
IMJH_FILES = (
    "i.mjh.nz_au.channels.xml",
    "i.mjh.nz_nz.channels.xml",
    "i.mjh.nz_pbs.channels.xml",
    "i.mjh.nz_plex.channels.xml",
    "i.mjh.nz_pluto.channels.xml",
    "i.mjh.nz_roku.channels.xml",
    "i.mjh.nz_samsung.channels.xml",
)
EPGSHARE_TAGS = {
    "AR1", "AU1", "BA1", "BE2", "BG1", "BR1", "CA1", "CH1", "CL1",
    "CO1", "CR1", "CY1", "CZ1", "DE1", "DK1", "DO1", "EC1", "EG1",
    "ES1", "FI1", "FR1", "GR1", "HK1", "HR1", "HU1", "ID1", "IE1",
    "IL1", "IN4", "IT1", "JM1", "JP1", "JP2", "KE1", "KR1", "MT1",
    "MX1", "MY1", "NL1", "NO1", "NZ1", "PA1", "PE1", "PH1", "PH2",
    "PK1", "PL1", "PT1", "RO1", "RO2", "SA1", "SA2", "SE1", "SG1",
    "SK1", "SV1", "TR1", "TR3", "UK1", "US1", "US_LOCALS2", "UY1",
    "VN1", "ZA1",
}
OTHER_FILES = (
    ("sites/distro.tv/distro.tv.channels.xml", "distro.tv"),
    ("sites/freeview.co.uk/freeview.co.uk.channels.xml", "freeview.co.uk"),
    ("sites/aljazeera.com/aljazeera.com.channels.xml", "aljazeera.com"),
    ("sites/arirang.com/arirang.com.channels.xml", "arirang.com"),
    ("sites/rtp.pt/rtp.pt.channels.xml", "rtp.pt"),
)

def unsafe(text: str) -> bool:
    value = (text or "").casefold()
    return any(term in value for term in BLOCKED_TERMS)

def parse_attrs(line: str) -> dict[str, str]:
    return {k.lower(): v for k, v in re.findall(r'([\w-]+)="([^"]*)"', line)}

def load_playlist() -> tuple[list[dict[str, str]], set[str]]:
    if not PLAYLIST.is_file():
        raise RuntimeError(f"Playlist not found: {PLAYLIST}")
    records = []
    current = None
    for line in PLAYLIST.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            attrs = parse_attrs(line)
            title = line.split(",", 1)[1].strip() if "," in line else ""
            current = {"attrs": attrs, "title": title}
        elif line.startswith("#") or not line:
            continue
        elif current is not None:
            attrs = current["attrs"]
            tvg_id = attrs.get("tvg-id", "").strip()
            category = attrs.get("group-title", "")
            label = current["title"]
            if tvg_id and not unsafe(tvg_id + " " + label + " " + category):
                records.append({"tvg_id": tvg_id, "title": label})
            current = None
    return records, {row["tvg_id"] for row in records}

def mapping_files(epg_repo: Path) -> list[tuple[Path, str, str]]:
    sites = epg_repo / "sites"
    selected: list[tuple[Path, str, str]] = []
    for name in IMJH_FILES:
        path = sites / "i.mjh.nz" / name
        if path.is_file():
            selected.append((path, "i.mjh.nz", "i.mjh.nz"))
    epgshare_dir = sites / "epgshare01.online"
    if epgshare_dir.is_dir():
        for path in sorted(epgshare_dir.glob("epgshare01.online_*.channels.xml")):
            match = re.fullmatch(r"epgshare01\.online_(.+)\.channels\.xml", path.name)
            if match and match.group(1) in EPGSHARE_TAGS:
                selected.append((path, "epgshare01.online", match.group(1)))
    for relpath, site in OTHER_FILES:
        path = sites / relpath.removeprefix("sites/")
        if path.is_file():
            selected.append((path, site, site))
    return selected

def prepare(epg_repo: Path) -> None:
    records, wanted_ids = load_playlist()
    if not wanted_ids:
        raise RuntimeError("No safe playlist entries with tvg-id were found; cannot create a reliable EPG mapping.")
    candidates: dict[str, tuple[str, str, str, str]] = {}
    source_counts: Counter[str] = Counter()
    for path, site, tag in mapping_files(epg_repo):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            print(f"WARNING: skipping malformed EPG channel map {path.name}: {exc}", file=sys.stderr)
            continue
        for channel in root.findall(".//channel"):
            xmltv_id = (channel.get("xmltv_id") or "").strip()
            name = "".join(channel.itertext()).strip()
            if not xmltv_id or xmltv_id not in wanted_ids or unsafe(xmltv_id + " " + name):
                continue
            lang = (channel.get("lang") or "en").strip() or "en"
            source_site_id = (channel.get("site_id") or "").strip()
            if not source_site_id:
                continue
            if xmltv_id not in candidates:
                candidates[xmltv_id] = (site, source_site_id, lang, name or xmltv_id)
                source_counts[site] += 1
    if not candidates:
        raise RuntimeError("No exact tvg-id/xmltv_id matches were found in the selected public guide maps.")
    EPG_DIR.mkdir(parents=True, exist_ok=True)
    root = ET.Element("channels")
    for xmltv_id in sorted(candidates):
        site, site_id, lang, name = candidates[xmltv_id]
        attrs = {
            "site": site,
            "site_id": site_id,
            "lang": lang,
            "xmltv_id": xmltv_id,
        }
        node = ET.SubElement(root, "channel", attrs)
        node.text = name
    ET.ElementTree(root).write(CHANNELS_OUT, encoding="utf-8", xml_declaration=True)
    metrics = {
        "playlist_stream_entries": len(records),
        "playlist_entries_with_safe_tvg_id": len(records),
        "playlist_unique_tvg_ids": len(wanted_ids),
        "matched_guide_channel_ids": len(candidates),
        "matched_source_mapping_counts": dict(sorted(source_counts.items())),
        "guide_days_requested": 2,
        "match_method": "Exact tvg-id to XMLTV xmltv_id only; no fuzzy programme matching.",
    }
    (EPG_DIR / "prepare-report.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))

def finalize() -> None:
    records, wanted_ids = load_playlist()
    if not GUIDE_GZ.is_file() or GUIDE_GZ.stat().st_size == 0:
        raise RuntimeError(f"Generated compressed XMLTV guide is missing or empty: {GUIDE_GZ}")
    channel_ids: set[str] = set()
    programme_ids: set[str] = set()
    with gzip.open(GUIDE_GZ, "rb") as stream:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "channel":
                cid = elem.get("id")
                if cid:
                    channel_ids.add(cid)
            elif tag == "programme":
                cid = elem.get("channel")
                if cid:
                    programme_ids.add(cid)
            elem.clear()
    if not programme_ids:
        raise RuntimeError("XMLTV output contains no programme records; refusing to publish an empty EPG.")
    covered_ids = wanted_ids & programme_ids
    playlist_entry_coverage = sum(
        1 for row in records if row["tvg_id"] in programme_ids
    )
    pct = round(100 * len(covered_ids) / len(wanted_ids), 2) if wanted_ids else 0.0
    report = {
        "playlist_stream_entries": len(records),
        "playlist_unique_tvg_ids": len(wanted_ids),
        "xmltv_channel_ids": len(channel_ids),
        "xmltv_channel_ids_with_programmes": len(programme_ids),
        "playlist_tvg_ids_with_programmes": len(covered_ids),
        "playlist_entries_with_programmes": playlist_entry_coverage,
        "unique_channel_id_coverage_percent": pct,
        "guide_days": 2,
        "guide_url": EPG_URL,
        "match_method": "Exact tvg-id to XMLTV channel/programme ID matching only.",
        "limitations": "Only channels with a matching public guide source and programme data are covered; unmatched channels remain without EPG.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if REPORT.is_file():
        try:
            existing = json.loads(REPORT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing["epg"] = report
    REPORT.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = PLAYLIST.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith("#EXTM3U")]
    header = f'#EXTM3U x-tvg-url="{EPG_URL}" url-tvg="{EPG_URL}"'
    PLAYLIST.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    (EPG_DIR / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))

def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"prepare", "finalize"}:
        print("Usage: python scripts/build_epg.py prepare <path-to-iptv-org-epg> | finalize", file=sys.stderr)
        return 2
    if sys.argv[1] == "prepare":
        if len(sys.argv) < 3:
            raise RuntimeError("prepare requires the cloned iptv-org/epg repository path")
        prepare(Path(sys.argv[2]))
    else:
        finalize()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
