# Wonderful IPTV

A private project for building and maintaining a worldwide playlist of publicly available free-to-watch television streams.

## Project files
- `playlists/Wonderful.m3u` — current published playlist snapshot
- `playlists/Wonderful-report.json` — source counts, filtering and verification notes
- `scripts/build_wonderful.py` — collection, filtering, labelling and deduplication
- `.github/workflows/build-wonderful.yml` — scheduled and manual build workflow

## Build
Requires Python 3.12.

```bash
python scripts/build_wonderful.py
```

The GitHub Actions workflow is scheduled daily and can also be started manually.

## Stream status and usage
A catalogue listing is not proof that a stream is online, licensed for every territory, or playable in every IPTV app. Each entry should retain its source and status label. Geo-restrictions, service terms and stream availability can change.

This project is intended for publicly accessible free streams. It must not be used to access paid channels without authorization. The builder's 30,000-entry threshold is a safety check, not a guarantee that 30,000 unique and usable streams exist.
