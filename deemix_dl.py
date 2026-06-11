#!/usr/bin/env python3
"""
Bulk-queue an artist's discography to a running deemix-gui server.

Reads a list of artists (one per line) from a text file, resolves each to a
Deezer artist via the public Deezer API, enumerates their releases, filters by
type (albums / EPs / singles) and queues them to your local deemix-gui server.
Folder structure, naming and tagging come from the SERVER's own config -- this
script only chooses the URL and the quality (bitrate).

The script logs in to the server each run using your ARL (Deezer login is bound
to the HTTP session). The ARL is read from, in order:
    1. --arl <value>
    2. env var DEEMIX_ARL
    3. the deemix config file  %APPDATA%\\deemix\\.arl   (Linux/mac: ~/.config/deemix/.arl)

A line in the artist file may be:
    - an artist name              e.g.  Scott Joplin
    - a numeric Deezer artist id  e.g.  9140
    - a Deezer artist URL         e.g.  https://www.deezer.com/artist/9140
Blank lines and lines starting with '#' are ignored.

Examples:
    python deemix_dl.py --albums --flac
    python deemix_dl.py --albums --eps --singles --mp3
    python deemix_dl.py --singles --dry-run
"""

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Deezer download-format codes used by deemix
BITRATE_FLAC = 9
BITRATE_MP3_320 = 3

DEEZER_API = "https://api.deezer.com"


def load_dotenv(path=".env"):
    """Minimal .env loader (no dependency). Real env vars take precedence."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)

# One opener shared by every request so the deemix session cookie (connect.sid)
# persists across login + addToQueue calls. Login is session-bound; without this
# the queue calls would be unauthenticated.
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "deemix-dl/1.0"})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "deemix-dl/1.0"},
    )
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class ServerDown(Exception):
    """Raised when the deemix server can't be reached (crashed / not running)."""


def check_server(server):
    """True if the deemix server answers a harmless GET."""
    try:
        http_get_json(f"{server}/api/getSettings", timeout=8)
        return True
    except Exception:  # noqa: BLE001
        return False


def find_arl(cli_arl):
    """Resolve the ARL from --arl, env, or the deemix .arl config file."""
    if cli_arl:
        return cli_arl.strip()
    env = os.environ.get("DEEMIX_ARL")
    if env:
        return env.strip()
    appdata = os.environ.get("APPDATA")
    candidates = []
    if appdata:
        candidates.append(os.path.join(appdata, "deemix", ".arl"))
    candidates.append(os.path.expanduser("~/.config/deemix/.arl"))
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                arl = f.read().strip()
            if arl:
                return arl
    return None


def login(server, arl):
    """Log the current session in. Returns the Deezer user name, or raises."""
    resp = http_post_json(f"{server}/api/loginArl",
                          {"arl": arl, "force": False, "child": 0})
    if resp.get("status") == 1:
        return (resp.get("user") or {}).get("name", "?")
    raise RuntimeError(f"login failed (status {resp.get('status')}, "
                       f"errid {resp.get('errid')})")


def queue_one(server, url, bitrate):
    """
    Queue a single album URL.  NOTE: deemix expects `url` as a STRING (multiple
    links would be joined with ';'); passing a list crashes the server.
    Returns ("ok", None) | ("error", errid).
    Raises ServerDown on a connection-level failure.
    """
    try:
        resp = http_post_json(f"{server}/api/addToQueue",
                              {"url": url, "bitrate": bitrate})
    except urllib.error.HTTPError as e:
        return ("error", f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001  -- reset/refused/timeout => server gone
        raise ServerDown(str(e))
    if resp.get("result") is False:
        return ("error", resp.get("errid"))
    return ("ok", None)


# --------------------------------------------------------------------------- #
# Deezer metadata
# --------------------------------------------------------------------------- #
def deezer(path):
    """GET a Deezer API path with small backoff for rate limiting (~50 req/5s)."""
    url = path if path.startswith("http") else f"{DEEZER_API}{path}"
    last = None
    for i in range(4):
        try:
            res = http_get_json(url)
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
    """Return dict(id, name, matched) or None."""
    entry = entry.strip()

    if entry.isdigit():
        a = deezer(f"/artist/{entry}")
        if a.get("id"):
            return {"id": a["id"], "name": a["name"], "matched": True}
        return None

    if "deezer.com" in entry and "/artist/" in entry:
        tail = entry.split("/artist/", 1)[1]
        aid = "".join(c for c in tail if c.isdigit())
        if aid:
            a = deezer(f"/artist/{aid}")
            if a.get("id"):
                return {"id": a["id"], "name": a["name"], "matched": True}
        return None

    q = urllib.parse.quote(entry)
    res = deezer(f"/search/artist?q={q}&limit=5")
    data = res.get("data") or []
    if not data:
        return None
    top = data[0]
    matched = top["name"].strip().lower() == entry.lower()
    return {"id": top["id"], "name": top["name"], "matched": matched}


def get_all_albums(artist_id):
    albums = []
    url = f"{DEEZER_API}/artist/{artist_id}/albums?limit=100"
    while url:
        page = deezer(url)
        albums.extend(page.get("data") or [])
        url = page.get("next")
        if url:
            time.sleep(0.15)
    return albums


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(
        description="Bulk-queue artists' discographies to a deemix-gui server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-a", "--artists", default="artists.txt",
                   help="path to artist list (default: artists.txt)")
    p.add_argument("--albums", action="store_true", help="include albums")
    p.add_argument("--eps", action="store_true", help="include EPs")
    p.add_argument("--singles", action="store_true", help="include singles")
    p.add_argument("--compilations", action="store_true",
                   help='also include releases tagged "compilation"')
    q = p.add_mutually_exclusive_group()
    q.add_argument("--flac", action="store_true", help="FLAC quality (default)")
    q.add_argument("--mp3", action="store_true", help="MP3 320 quality")
    p.add_argument("--server", default=None,
                   help="deemix-gui base URL (else DEEMIX_SERVER, "
                        "else http://127.0.0.1:6595)")
    p.add_argument("--arl", default=None,
                   help="Deezer ARL (else env DEEMIX_ARL, else deemix .arl file)")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be queued; queue nothing (no login needed)")
    return p.parse_args()


def main():
    args = parse_args()
    load_dotenv()

    server = (args.server or os.environ.get("DEEMIX_SERVER")
              or "http://127.0.0.1:6595").rstrip("/")

    # quality: --mp3/--flac win; otherwise DEEMIX_QUALITY; default flac
    if args.mp3:
        quality = "mp3"
    elif args.flac:
        quality = "flac"
    else:
        quality = os.environ.get("DEEMIX_QUALITY", "flac").lower()
    if quality == "mp3":
        bitrate, quality_name = BITRATE_MP3_320, "MP3 320"
    else:
        bitrate, quality_name = BITRATE_FLAC, "FLAC"

    # Deezer record_type values: album, single, ep, compilation
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

    print()
    print("deemix bulk downloader")
    print(f"  Server   : {server}")
    print(f"  Quality  : {quality_name} (bitrate {bitrate})")
    print(f"  Types    : {', '.join(sorted(want))}")
    print(f"  Artists  : {len(lines)} from {args.artists}")
    if args.dry_run:
        print("  DRY RUN  : nothing will be queued")
    print()

    # ---- connect + login (skipped for dry runs) --------------------------- #
    if not args.dry_run:
        if not check_server(server):
            sys.exit(f"Cannot reach deemix server at {server}. "
                     "Start it (win-x64-latest.exe) first.")
        arl = find_arl(args.arl)
        if not arl:
            sys.exit("No ARL found. Pass --arl, set DEEMIX_ARL, or put it in "
                     "%APPDATA%\\deemix\\.arl")
        try:
            user = login(server, arl)
        except ServerDown as e:
            sys.exit(f"Server went away during login ({e}).")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Could not log in: {e}")
        print(f"  Logged in as: {user}")
        print()

    total_queued = 0
    grand_total = 0

    for entry in lines:
        print(f"-> {entry}")
        try:
            artist = resolve_artist(entry)
        except Exception as e:  # noqa: BLE001
            print(f"   ! lookup failed: {e}")
            continue
        if not artist:
            print("   ! no Deezer artist found")
            continue
        if artist["matched"]:
            print(f"   = {artist['name']} (id {artist['id']})")
        else:
            print(f"   ~ resolved to '{artist['name']}' (id {artist['id']}) "
                  f"- verify this is correct")

        all_albums = get_all_albums(artist["id"])

        seen = set()
        picked = []
        for al in all_albums:
            rt = str(al.get("record_type", "")).lower()
            if rt not in want:
                continue
            if al["id"] in seen:
                continue
            seen.add(al["id"])
            picked.append(al)

        if not picked:
            print("   (nothing matching selected types)")
            time.sleep(0.2)
            continue

        picked.sort(key=lambda x: (x.get("record_type", ""), x.get("title", "")))
        for al in picked:
            print(f"     [{al.get('record_type',''):<11}] {al.get('title','')}")
        print(f"   {len(picked)} release(s) selected")
        grand_total += len(picked)

        if args.dry_run:
            time.sleep(0.2)
            continue

        # Queue one album at a time (url MUST be a string, not a list).
        queued_here = 0
        for al in picked:
            url = f"https://www.deezer.com/album/{al['id']}"
            try:
                status, errid = queue_one(server, url, bitrate)
            except ServerDown as e:
                print()
                print(f"ABORTED: lost connection to the deemix server ({e}).")
                print("Restart it and re-run.")
                sys.exit(1)

            if status == "ok":
                total_queued += 1
                queued_here += 1
            elif errid == "NotLoggedIn":
                print()
                print("ABORTED: server reports NotLoggedIn (session expired or "
                      "ARL rejected). Re-run; if it persists, refresh your ARL.")
                sys.exit(1)
            else:
                print(f"   ! rejected: {al.get('title','')} ({errid})")
            time.sleep(0.25)

        print(f"   queued {queued_here}/{len(picked)}")

    print()
    if args.dry_run:
        print(f"DRY RUN complete: {grand_total} release(s) would be queued.")
    else:
        print(f"Done. Queued {total_queued} release(s) to {server}.")
        print("Watch progress in the deemix-gui web UI.")


if __name__ == "__main__":
    main()
