#!/usr/bin/env python3
"""
Phase 1 -- bulk-queue an artist's discography to a running deemix-gui server.

Reads a list of artists (one per line) from a text file, resolves each to a
Deezer artist via the public Deezer API, enumerates their releases, filters by
type (albums / EPs / singles) and queues them to your local deemix-gui server.
Folder structure, naming and tagging come from the SERVER's own config -- this
script only chooses the URL and the quality (bitrate).

Config (see .env / .env.example):
    DEEMIX_ARL      Deezer ARL (also --arl, or the deemix .arl file)
    DEEMIX_SERVER   server URL (also --server; default http://127.0.0.1:6595)
    DEEMIX_QUALITY  flac | mp3  (default when neither --flac/--mp3 given)

A line in the artist file may be a name, a numeric Deezer artist id, or a
Deezer artist URL. Blank lines and lines starting with '#' are ignored.

Examples:
    python deemix_dl.py --albums --flac
    python deemix_dl.py --albums --eps --singles --mp3
    python deemix_dl.py --singles --dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import deemix_common as dc

BITRATE_FLAC = 9
BITRATE_MP3_320 = 3
DEEZER_API = "https://api.deezer.com"


# --------------------------------------------------------------------------- #
# Deezer metadata (public API, no auth)
# --------------------------------------------------------------------------- #
def deezer(path):
    url = path if path.startswith("http") else f"{DEEZER_API}{path}"
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deemix-dl/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode("utf-8"))
            if isinstance(res, dict) and res.get("error"):
                last = res["error"]
                time.sleep(0.4 * (i + 1))
                continue
            return res
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"Deezer API failed: {url} ({last})")


def resolve_artist(entry):
    entry = entry.strip()
    if entry.isdigit():
        a = deezer(f"/artist/{entry}")
        return {"id": a["id"], "name": a["name"], "matched": True} if a.get("id") else None
    if "deezer.com" in entry and "/artist/" in entry:
        aid = "".join(c for c in entry.split("/artist/", 1)[1] if c.isdigit())
        if aid:
            a = deezer(f"/artist/{aid}")
            return {"id": a["id"], "name": a["name"], "matched": True} if a.get("id") else None
        return None
    q = urllib.parse.quote(entry)
    data = (deezer(f"/search/artist?q={q}&limit=10") or {}).get("data") or []
    if not data:
        return None
    # Prefer an exact (case-insensitive) name match, most-followed first; this
    # avoids Deezer's relevance ranking handing back the wrong same-ish artist
    # (e.g. "Unida" -> "Unidad de Musica de la Guardia Real"). Falls back to the
    # top search hit (flagged as unmatched) when nothing matches exactly.
    exact = [d for d in data if d["name"].strip().lower() == entry.lower()]
    if exact:
        top = max(exact, key=lambda d: d.get("nb_fan", 0))
        return {"id": top["id"], "name": top["name"], "matched": True}
    top = data[0]
    return {"id": top["id"], "name": top["name"], "matched": False}


def get_all_albums(artist_id):
    albums, url = [], f"{DEEZER_API}/artist/{artist_id}/albums?limit=100"
    while url:
        page = deezer(url)
        albums.extend(page.get("data") or [])
        url = page.get("next")
        if url:
            time.sleep(0.15)
    return albums


# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(
        description="Phase 1: queue artists' discographies to a deemix-gui server.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--artists", default="artists.txt",
                   help="path to artist list (default: artists.txt)")
    p.add_argument("--albums", action="store_true")
    p.add_argument("--eps", action="store_true")
    p.add_argument("--singles", action="store_true")
    p.add_argument("--compilations", action="store_true",
                   help='also include releases tagged "compilation"')
    q = p.add_mutually_exclusive_group()
    q.add_argument("--flac", action="store_true", help="FLAC (default)")
    q.add_argument("--mp3", action="store_true", help="MP3 320")
    p.add_argument("--server", default=None,
                   help="deemix-gui base URL (else DEEMIX_SERVER)")
    p.add_argument("--arl", default=None,
                   help="Deezer ARL (else DEEMIX_ARL, else deemix .arl file)")
    p.add_argument("--skip-fallback-check", action="store_true",
                   help="don't warn when the server has bitrate fallback disabled")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be queued; queue nothing (no login)")
    return p.parse_args()


def main():
    args = parse_args()
    dc.load_dotenv()
    log, _ = dc.setup_logging("deemix_dl")

    server = dc.server_url(args.server)
    if args.mp3:
        quality = "mp3"
    elif args.flac:
        quality = "flac"
    else:
        quality = os.environ.get("DEEMIX_QUALITY", "flac").lower()
    bitrate, quality_name = ((BITRATE_MP3_320, "MP3 320") if quality == "mp3"
                             else (BITRATE_FLAC, "FLAC"))

    want = set()
    if not (args.albums or args.eps or args.singles):
        want = {"album", "ep", "single"}
    else:
        if args.albums:
            want.add("album")
        if args.eps:
            want.add("ep")
        if args.singles:
            want.add("single")
    if args.compilations:
        want.add("compilation")

    try:
        with open(args.artists, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
    except OSError as e:
        sys.exit(f"Artist file not found: {args.artists} ({e})")
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        sys.exit(f"No artists found in {args.artists}")

    log.info("server=%s  quality=%s (bitrate %d)  types=%s  artists=%d  dry_run=%s",
             server, quality_name, bitrate, ",".join(sorted(want)), len(lines),
             args.dry_run)

    client = dc.Client(server)
    if not args.dry_run:
        if not client.up():
            sys.exit(f"Cannot reach deemix server at {server}. Start it first.")
        arl = dc.find_arl(args.arl)
        if not arl:
            sys.exit("No ARL found. Set DEEMIX_ARL in .env, pass --arl, or use "
                     "the deemix .arl file.")
        try:
            user = client.login(arl)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Could not log in: {e}")
        log.info("logged in as: %s", user)

        if not args.skip_fallback_check:
            try:
                if not client.settings().get("fallbackBitrate"):
                    log.warning("server has bitrate fallback DISABLED - tracks without "
                                "%s will FAIL instead of downloading a lower quality. "
                                "Set fallbackBitrate=true in the deemix config and restart "
                                "the server.", quality_name)
            except Exception:  # noqa: BLE001
                pass

    total_queued = grand_total = 0
    for entry in lines:
        log.info("artist: %s", entry)
        try:
            artist = resolve_artist(entry)
        except Exception as e:  # noqa: BLE001
            log.error("  lookup failed: %s", e)
            continue
        if not artist:
            log.error("  no Deezer artist found")
            continue
        if artist["matched"]:
            log.info("  = %s (id %s)", artist["name"], artist["id"])
        else:
            log.warning("  ~ resolved to '%s' (id %s) - verify", artist["name"], artist["id"])

        try:
            all_albums = get_all_albums(artist["id"])
        except Exception as e:  # noqa: BLE001
            log.error("  album lookup failed: %s", e)
            continue

        seen, picked = set(), []
        for al in all_albums:
            if str(al.get("record_type", "")).lower() not in want:
                continue
            if al["id"] in seen:
                continue
            seen.add(al["id"])
            picked.append(al)

        if not picked:
            log.info("  (nothing matching selected types)")
            continue

        picked.sort(key=lambda x: (x.get("record_type", ""), x.get("title", "")))
        for al in picked:
            log.info("    [%-11s] %s", al.get("record_type", ""), al.get("title", ""))
        log.info("  %d release(s) selected", len(picked))
        grand_total += len(picked)
        if args.dry_run:
            continue

        queued_here = 0
        for al in picked:
            url = f"https://www.deezer.com/album/{al['id']}"
            try:
                status, errid = client.add_to_queue(url, bitrate)
            except dc.ServerDown as e:
                log.error("ABORTED: lost connection to deemix server (%s). Restart and re-run.", e)
                sys.exit(1)
            if status == "ok":
                total_queued += 1
                queued_here += 1
            elif errid == "NotLoggedIn":
                log.error("ABORTED: server reports NotLoggedIn. Re-run; refresh ARL if it persists.")
                sys.exit(1)
            else:
                log.warning("    rejected: %s (%s)", al.get("title", ""), errid)
            time.sleep(0.25)
        log.info("  queued %d/%d", queued_here, len(picked))

    if args.dry_run:
        log.info("DRY RUN complete: %d release(s) would be queued.", grand_total)
    else:
        log.info("Done. Queued %d release(s) to %s.", total_queued, server)
        log.info("Run deemix_sync.py to move finished bands to the NAS.")


if __name__ == "__main__":
    main()
