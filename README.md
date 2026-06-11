# deemix-bulk

Two-phase bulk music fetcher for a local
[deemix-gui](https://gitlab.com/RemixDev/deemix-gui) server:

1. **`deemix_dl.py`** — read a list of artists, resolve each on Deezer, and
   queue all their albums / EPs / singles to the deemix-gui server.
2. **`deemix_sync.py`** — watch the download queue and, as soon as every release
   for a band is finished (or errored/stuck), move that band's folder to your
   NAS over SMB.

Folder structure, file naming, tagging and download quality come from your
deemix-gui config — these scripts only choose what to queue and where to move
the results. Standard library only — **nothing to `pip install`.**

> Use this only for content you're legally allowed to download with your own
> Deezer account. You are responsible for how you use it.

## Requirements

- **Python 3.8+** (stdlib only)
- A running **deemix-gui** server (the headless build) reachable over HTTP.
- A **Deezer ARL** for an account that can stream the quality you want
  (FLAC needs Deezer HiFi).
- For phase 2: a reachable **SMB share** on your NAS (Windows host assumed —
  uses `net use`).

## Setup

```sh
cp .env.example .env          # then fill it in (ARL + NAS creds)
cp artists.example.txt artists.txt
```

`.env` keys:

| Key | Purpose |
|-----|---------|
| `DEEMIX_ARL` | Deezer ARL cookie (secret). See `.env.example` for how to grab it. |
| `DEEMIX_SERVER` | deemix-gui base URL (default `http://127.0.0.1:6595`). |
| `DEEMIX_QUALITY` | `flac` or `mp3`, used when no `--flac`/`--mp3` flag is passed. |
| `NAS_HOST` / `NAS_SHARE` | SMB target — `\\HOST\SHARE` (e.g. `data`). |
| `NAS_USER` / `NAS_PASS` | SMB credentials. |
| `NAS_MUSIC_PATH` | Path under the share, e.g. `media/music`. |
| `DEEMIX_DOWNLOAD_DIR` | *(optional)* override; otherwise read from the server. |

## Phase 1 — queue (`deemix_dl.py`)

```sh
python deemix_dl.py --albums --eps --flac          # albums + EPs in FLAC
python deemix_dl.py --albums --eps --singles --mp3 # everything in MP3 320
python deemix_dl.py --singles --dry-run            # preview, queue nothing
```

| Flag | Description |
|------|-------------|
| `--albums` `--eps` `--singles` | Types to include. If none given, all three. |
| `--flac` / `--mp3` | Quality (FLAC=9, MP3 320=3). Default from `DEEMIX_QUALITY`. |
| `--compilations` | Also include releases tagged "compilation". |
| `-a`, `--artists PATH` | Artist list (default `artists.txt`). |
| `--server URL` / `--arl VALUE` | Overrides for `DEEMIX_SERVER` / `DEEMIX_ARL`. |
| `--dry-run` | Resolve & list only. |

An artist line can be a name, a Deezer artist id, or a Deezer artist URL.

## Phase 2 — move finished bands to the NAS (`deemix_sync.py`)

```sh
python deemix_sync.py --dry-run        # report only; connect/move nothing
python deemix_sync.py                  # watch loop: move each band as it finishes
python deemix_sync.py --once           # single pass, then exit
python deemix_sync.py --copy           # copy to NAS, keep local copies
```

It polls `GET /api/getQueue`, groups items by **artist**, and treats a band as
**done** when every one of its releases is in a terminal state
(`completed` / `withErrors` / `failed`). For each done band it takes the local
folder straight from the queue item's `artistPath` and moves it to
`\\NAS_HOST\NAS_SHARE\NAS_MUSIC_PATH\<Artist>\` (merging into an existing artist
folder, overwriting same-named files). Already-moved bands are recorded in
`state/synced.json` so re-runs skip them.

| Flag | Description |
|------|-------------|
| `--dry-run` | Report what would move; no SMB connection, no changes. |
| `--once` | One pass instead of the watch loop. |
| `--copy` | Copy instead of move (keep local). |
| `--interval N` | Poll seconds (default 30). |
| `--stuck-timeout N` | Seconds of no queue progress before logging `STUCK` (default 1800). |
| `--server` / `--arl` / `--download-dir` | Overrides. |

**Errored / stuck:** failed and partial (`withErrors`) releases are logged per
band with the first error message; if the whole queue stops making progress for
`--stuck-timeout` seconds the active item is flagged `STUCK`.

## Logs

Every run writes a timestamped log to `logs/` (e.g.
`logs/deemix_sync_20260611_004500.log`) in addition to the console. `logs/` and
`state/` are gitignored.

## Typical workflow

```sh
python deemix_dl.py --albums --eps --singles --flac   # queue everything
python deemix_sync.py                                  # leave running; bands land on the NAS as they finish
```

## Notes on deemix-gui's API (reverse-engineered, build 2022.12.14)

- **Login:** `POST /api/loginArl` `{"arl": "..."}` — **bound to the session
  cookie** (`connect.sid`), so the tools keep one session and log in each run.
- **Queue:** `POST /api/addToQueue` `{"url": "<deezer url>", "bitrate": N}` —
  `url` is a **string** (a JSON array crashes the server with
  `url.split is not a function`). Bitrate: `9`=FLAC, `3`=MP3 320, `1`=MP3 128.
- **Queue items** expose `status`, `size`, `downloaded`, `failed`, `errors[]`,
  and (once downloaded) `artistPath` / `albumPath` / `files[].path`.
- **`POST /api/saveSettings` is broken** (HTTP 500) in this build — the UI saves
  settings over socket.io. Change settings in `config.json` and restart instead.

## Troubleshooting

- **Lots of `failed` / `wrongBitrateNoAlternative`** — the track has no FLAC and
  bitrate fallback is off, so deemix fails it instead of grabbing a lower
  quality. Enable it by setting `"fallbackBitrate": true` (and
  `"fallbackSearch": true`) in the deemix config
  (`%APPDATA%\deemix\config.json`) and **restarting the server** — the REST
  `saveSettings` endpoint is broken in this build, so the config file + restart
  is the reliable way. `deemix_dl.py` warns at startup whenever the running
  server still has fallback disabled (`--skip-fallback-check` to silence).
- **`Cannot reach deemix server`** — server not running / wrong `DEEMIX_SERVER`.
- **`Could not log in`** — bad/expired ARL (they last ~3 months; refresh it).
- **`SMB connect failed`** — wrong `NAS_*` creds, share name, or the host is
  unreachable. Test with `net use \\HOST\SHARE /user:USER PASS`.
- **Wrong artist matched** — name search takes Deezer's top hit (a `~ resolved
  to '...'` warning is logged). Use the Deezer id/URL to be exact.

## Files

| File | Purpose |
|------|---------|
| `deemix_dl.py` | Phase 1 — queue discographies. |
| `deemix_sync.py` | Phase 2 — move finished bands to the NAS. |
| `deemix_common.py` | Shared helpers (.env, logging, API client). |
| `.env.example` / `artists.example.txt` | Templates — copy and fill in. |
| `.gitignore` | Keeps secrets, binaries, logs, state and your personal list out of git. |
