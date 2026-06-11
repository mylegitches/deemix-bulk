#!/usr/bin/env python3
r"""
Phase 2 -- watch the deemix-gui queue and move finished bands to the NAS.

Polls the deemix-gui download queue, groups items by artist, and as soon as
EVERY release for a band has reached a terminal state (completed / withErrors /
failed) it moves that band's folder from the local download location to the NAS
over SMB. Errored/stuck downloads are logged so you can retry them.

The local artist folder path comes straight from the completed queue items
(`artistPath`), so no folder-name guessing is involved.

NAS config (see .env / .env.example):
    NAS_HOST        SMB host, e.g. 192.168.1.50
    NAS_SHARE       SMB share name, e.g. data   (\\HOST\SHARE)
    NAS_USER        SMB username
    NAS_PASS        SMB password
    NAS_MUSIC_PATH  path under the share, e.g. media/music
Server/ARL config is shared with deemix_dl.py (DEEMIX_SERVER / DEEMIX_ARL).

Examples:
    python deemix_sync.py --dry-run          # report only, move nothing
    python deemix_sync.py                     # watch loop, move bands as they finish
    python deemix_sync.py --once              # single pass then exit
    python deemix_sync.py --copy              # copy to NAS, keep local copies
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import deemix_common as dc

TERMINAL = {"completed", "withErrors", "failed"}
STATE_DIR = "state"
SYNCED_STATE = os.path.join(STATE_DIR, "synced.json")


# --------------------------------------------------------------------------- #
# state (which bands we've already moved)
# --------------------------------------------------------------------------- #
def load_synced():
    try:
        with open(SYNCED_STATE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_synced(synced):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SYNCED_STATE, "w", encoding="utf-8") as f:
        json.dump(sorted(synced), f, indent=1)


# --------------------------------------------------------------------------- #
# SMB
# --------------------------------------------------------------------------- #
def smb_connect(host, share, user, password, log):
    unc = rf"\\{host}\{share}"
    # drop any stale mapping, then (re)connect with creds
    try:
        subprocess.run(["net", "use", unc, "/delete", "/y"],
                       capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        pass  # stale cleanup timed out — proceed anyway
    try:
        r = subprocess.run(["net", "use", unc, password, f"/user:{user}"],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SMB connect timed out for {unc} (NAS unreachable?)")
    if r.returncode != 0:
        raise RuntimeError(f"SMB connect failed for {unc}: "
                           f"{(r.stderr or r.stdout).strip()}")
    log.info("SMB connected: %s", unc)
    return unc


def merge_move(src, dst, copy_only=False):
    """Move (or copy) src into dst, merging into existing folders and
    overwriting existing files. Removes emptied source dirs when moving.

    Idempotent: if a file was already moved in a prior partial attempt
    (src missing but dst exists), it is treated as success so a retry
    after a partial failure completes cleanly.
    Retries up to 3x on WinError 32 (file in use) with a short sleep."""
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            merge_move(os.path.join(src, name), os.path.join(dst, name), copy_only)
        if not copy_only:
            try:
                os.rmdir(src)
            except OSError:
                pass
    else:
        if not os.path.exists(src):
            if os.path.exists(dst):
                return  # already moved in a prior partial attempt — skip
            raise FileNotFoundError(f"not at source or destination: {src}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        for attempt in range(3):
            try:
                if copy_only:
                    shutil.copy2(src, dst)
                else:
                    shutil.move(src, dst)
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(3)  # WinError 32: deemix still writing, retry
                else:
                    raise


# --------------------------------------------------------------------------- #
def prune_artist_lines(artists_file, targets, log):
    """Remove non-comment lines whose text (lowercased) is in `targets`.
    Writes a one-time .bak backup. Returns the number of lines removed."""
    if not artists_file or not os.path.isfile(artists_file):
        log.warning("    cannot prune - artists file not found: %s", artists_file)
        return 0
    with open(artists_file, encoding="utf-8") as f:
        lines = f.readlines()
    out, removed = [], 0
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and s.lower() in targets:
            removed += 1
            continue
        out.append(ln)
    if removed:
        bak = artists_file + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(artists_file, bak)
        with open(artists_file, "w", encoding="utf-8") as f:
            f.writelines(out)
    return removed


def artist_folder_of(items):
    """Best local artist-folder path for a band, from its terminal items."""
    for it in items:
        p = it.get("artistPath")
        if p:
            return os.path.normpath(p)
    for it in items:
        ep = it.get("extrasPath")
        if ep:
            return os.path.normpath(os.path.dirname(ep))  # album dir -> artist dir
    return None


def classify(items):
    completed = sum(1 for i in items if i.get("status") == "completed")
    witherr = sum(1 for i in items if i.get("status") == "withErrors")
    failed = sum(1 for i in items if i.get("status") == "failed")
    return completed, witherr, failed


def queue_signature(queue):
    """A value that changes whenever the queue makes progress (for stuck detection)."""
    total_done = sum((i.get("downloaded") or 0) for i in queue.values())
    statuses = tuple(sorted((u, v.get("status")) for u, v in queue.items()))
    return (total_done, hash(statuses))


def parse_args():
    p = argparse.ArgumentParser(
        description="Phase 2: move finished bands from the deemix queue to the NAS.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=None, help="deemix-gui URL (else DEEMIX_SERVER)")
    p.add_argument("--arl", default=None, help="Deezer ARL (else DEEMIX_ARL / .arl)")
    p.add_argument("--interval", type=int, default=30, help="poll seconds (default 30)")
    p.add_argument("--stuck-timeout", type=int, default=1800,
                   help="seconds of no queue progress before flagging STUCK (default 1800)")
    p.add_argument("--once", action="store_true", help="single pass, then exit")
    p.add_argument("--copy", action="store_true",
                   help="copy to NAS and keep local copies (default: move)")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="also move bands that finished with failed/partial releases "
                        "(default: only move bands where every release completed)")
    p.add_argument("--prune-artists", action="store_true",
                   help="after a band is moved, remove its line from the artists "
                        "file (a .bak backup is written). File comes from the phase-1 "
                        "manifest unless --artists-file is given.")
    p.add_argument("--artists-file", default=None,
                   help="artists list to prune (default: from the manifest)")
    p.add_argument("--dry-run", action="store_true",
                   help="report only; do not connect SMB or move anything")
    p.add_argument("--download-dir", default=None,
                   help="override local download dir (else from server settings)")
    return p.parse_args()


def main():
    args = parse_args()
    dc.load_dotenv()
    log, _ = dc.setup_logging("deemix_sync")

    server = dc.server_url(args.server)
    client = dc.Client(server)
    if not client.up():
        sys.exit(f"Cannot reach deemix server at {server}. Start it first.")
    arl = dc.find_arl(args.arl)
    if arl:
        try:
            log.info("logged in as: %s", client.login(arl))
        except Exception as e:  # noqa: BLE001
            log.warning("login failed (%s) - continuing read-only", e)

    settings = client.settings()
    download_dir = os.path.normpath(
        args.download_dir or os.environ.get("DEEMIX_DOWNLOAD_DIR")
        or settings.get("downloadLocation", ""))
    log.info("download dir: %s", download_dir)

    # NAS target
    host = os.environ.get("NAS_HOST")
    share = os.environ.get("NAS_SHARE")
    user = os.environ.get("NAS_USER")
    password = os.environ.get("NAS_PASS")
    music_rel = (os.environ.get("NAS_MUSIC_PATH", "")).replace("/", "\\").strip("\\")
    dest_base = None
    if args.dry_run:
        log.info("DRY RUN - no SMB connection, nothing will be moved")
    else:
        missing = [k for k, v in {"NAS_HOST": host, "NAS_SHARE": share,
                                  "NAS_USER": user, "NAS_PASS": password}.items() if not v]
        if missing:
            sys.exit(f"Missing NAS config in .env: {', '.join(missing)}")
        dest_base = os.path.join(rf"\\{host}\{share}", music_rel)
        try:
            smb_connect(host, share, user, password, log)
            log.info("NAS destination: %s  (%s)", dest_base,
                     "copy" if args.copy else "move")
        except RuntimeError as e:
            log.warning("NAS not reachable at startup (%s) - will retry when a band finishes", e)
            log.info("NAS destination (pending): %s", dest_base)

    synced = load_synced()
    reported_incomplete = set()
    move_failed = set()  # bands where a move was attempted and failed this session
    manifest = dc.load_manifest()
    artists_file = args.artists_file or manifest.get("artists_file")
    if args.prune_artists:
        log.info("prune: will remove moved bands from %s", artists_file or "(none found)")
    last_sig = None
    last_change = time.time()
    stuck_reported = False
    # deemix-gui removes completed items from the queue immediately after
    # download finishes, so we may never observe status="completed" in a poll.
    # Track the last-known state of each item so that when an active item
    # disappears we can inject it back as completed for this pass.
    seen_items = {}

    while True:
        try:
            q = client.get_queue().get("queue", {})
        except Exception as e:  # noqa: BLE001
            log.error("could not read queue: %s", e)
            if args.once:
                break
            time.sleep(args.interval)
            continue

        # Inject items that vanished while still active as "completed" so
        # they get moved/pruned in this pass.
        for uuid, snap in list(seen_items.items()):
            if uuid not in q and snap.get("status") not in TERMINAL:
                snap = dict(snap)
                snap["status"] = "completed"
                q[uuid] = snap
        for uuid, item in q.items():
            seen_items[uuid] = dict(item)

        # ---- stuck detection ---------------------------------------------- #
        active = [v for v in q.values() if v.get("status") not in TERMINAL]
        sig = queue_signature(q)
        if sig != last_sig:
            last_sig, last_change, stuck_reported = sig, time.time(), False
        elif active and not stuck_reported and (time.time() - last_change) > args.stuck_timeout:
            cur = next((v for v in q.values() if v.get("status") == "downloading"), None)
            log.warning("STUCK: no queue progress for %ds (current: %s)",
                        args.stuck_timeout, (cur or {}).get("title", "?"))
            stuck_reported = True

        # ---- group by band ------------------------------------------------ #
        bands = {}
        for it in q.values():
            bands.setdefault(it.get("artist") or "Unknown", []).append(it)

        n_wait = sum(1 for v in q.values() if v.get("status") == "inQueue")
        n_dl = sum(1 for v in q.values() if v.get("status") == "downloading")
        done_bands = [b for b, its in bands.items()
                      if its and all(i.get("status") in TERMINAL for i in its)]
        log.info("queue: %d items | %d waiting, %d downloading | %d/%d bands terminal",
                 len(q), n_wait, n_dl, len(done_bands), len(bands))

        # ---- move finished bands (only fully-completed ones) -------------- #
        for band in done_bands:
            items = bands[band]
            comp, werr, fail = classify(items)
            src = artist_folder_of(items)
            key = os.path.basename(src) if src else band
            if key in synced:
                continue

            fully_completed = (werr == 0 and fail == 0)
            if not fully_completed and not args.allow_incomplete:
                # finished, but with failures -> leave local for retry, don't move
                if key not in reported_incomplete:
                    log.warning("BAND INCOMPLETE: %s  [%d completed, %d withErrors, %d failed]"
                                " - NOT moving (fix/retry, then re-run)", band, comp, werr, fail)
                    for it in items:
                        if it.get("status") in ("failed", "withErrors"):
                            errs = it.get("errors") or []
                            msg = errs[0].get("message") if errs else ""
                            log.warning("    %s: %s %s", it.get("status"), it.get("title"), msg)
                    reported_incomplete.add(key)
                continue

            tag = "BAND DONE" if fully_completed else "BAND DONE (incomplete, forced)"
            if key not in move_failed:
                log.info("%s: %s  [%d completed, %d withErrors, %d failed]",
                         tag, band, comp, werr, fail)
            else:
                log.info("retrying move: %s", band)

            if not src or not os.path.isdir(src):
                log.warning("    local folder not found, skipping: %s", src)
                continue

            if args.dry_run:
                log.info("    [dry-run] would move %s -> %s\\%s",
                         src, dest_base or "<NAS>", os.path.basename(src))
                continue

            # Ensure SMB is connected before attempting a move (it may have
            # been unavailable at startup or dropped since).
            try:
                smb_connect(host, share, user, password, log)
            except RuntimeError as e:
                log.warning("    NAS still unreachable, skipping %s for now: %s", band, e)
                move_failed.add(key)
                continue

            dst = os.path.join(dest_base, os.path.basename(src))
            moved = False
            for attempt in range(2):
                try:
                    merge_move(src, dst, copy_only=args.copy)
                    log.info("    %s -> %s", "copied" if args.copy else "moved", dst)
                    synced.add(key)
                    save_synced(synced)
                    move_failed.discard(key)
                    moved = True
                    break
                except OSError as e:  # noqa: BLE001
                    winerr = getattr(e, "winerror", 0)
                    if attempt == 0 and winerr in (53, 59) and not args.dry_run:
                        log.warning("    SMB error %d — reconnecting and retrying…", winerr)
                        try:
                            smb_connect(host, share, user, password, log)
                        except Exception as ce:  # noqa: BLE001
                            log.error("    SMB reconnect failed: %s", ce)
                            log.error("    move FAILED for %s: %s", band, e)
                            move_failed.add(key)
                            break
                    else:
                        log.error("    move FAILED for %s: %s", band, e)
                        move_failed.add(key)
                        break
            if not moved:
                continue

            if args.prune_artists and not args.copy:
                targets = {band.strip().lower()}
                for e in manifest.get("entries", []):
                    if e.get("name", "").strip().lower() == band.strip().lower():
                        targets.add(str(e.get("line", "")).strip().lower())
                n = prune_artist_lines(artists_file, targets, log)
                if n:
                    log.info("    pruned %d line(s) from %s", n, artists_file)
                    manifest["entries"] = [e for e in manifest.get("entries", [])
                                           if e.get("name", "").strip().lower()
                                           != band.strip().lower()]
                    dc.save_manifest(manifest)

        # ---- loop control ------------------------------------------------- #
        if args.once:
            break
        if q and not active:
            log.info("all downloads terminal - final pass done, exiting")
            break
        if not q:
            log.info("queue empty - exiting")
            break
        time.sleep(args.interval)

    log.info("sync finished. %d band(s) recorded as synced.", len(synced))


if __name__ == "__main__":
    main()
