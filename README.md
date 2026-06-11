# deemix-bulk

Bulk-queue an artist's whole discography to a running
[deemix-gui](https://gitlab.com/RemixDev/deemix-gui) server.

Feed it a text file of artists (one per line). For each artist it resolves the
Deezer artist via the public Deezer API, enumerates every release, filters by
type (**albums / EPs / singles**), and queues them to your local deemix-gui
server at the quality you choose. **Folder structure, file naming and tagging
come from your deemix-gui config** — this script only picks the URL and bitrate.

> Use this only for content you're legally allowed to download (e.g. public
> domain) with your own Deezer account. You are responsible for how you use it.

## Requirements

- **Python 3.8+** (standard library only — nothing to `pip install`)
- A running **deemix-gui** server (the headless `win-x64-latest` / `linux` /
  `mac` build) reachable over HTTP, with your download folder structure and
  quality already configured in its settings.
- A **Deezer ARL** for an account that can stream the quality you want
  (FLAC requires Deezer HiFi).

## Setup

1. **Get deemix-gui** and run the server build. It listens on
   `http://127.0.0.1:6595` by default. (The binary is intentionally **not**
   included in this repo.)
2. **Configure deemix-gui** (download location, album/track templates, max
   bitrate, etc.) via its web UI — the script respects all of it.
3. **Create your `.env`:**
   ```sh
   cp .env.example .env
   ```
   Then put your ARL in it (see `.env.example` for how to find the cookie):
   ```ini
   DEEMIX_ARL=your_arl_cookie_here
   DEEMIX_SERVER=http://127.0.0.1:6595
   DEEMIX_QUALITY=flac
   ```
4. **Make your artist list:**
   ```sh
   cp artists.example.txt artists.txt
   ```
   Add one artist per line. A line can be a name, a Deezer artist id, or a
   Deezer artist URL.

## Usage

```sh
# Albums + EPs in FLAC
python deemix_dl.py --albums --eps --flac

# Everything (albums + eps + singles) in MP3 320
python deemix_dl.py --albums --eps --singles --mp3

# Preview what would be queued, download nothing (no login required)
python deemix_dl.py --singles --dry-run
```

### Flags

| Flag | Description |
|------|-------------|
| `--albums` `--eps` `--singles` | Release types to include. If **none** given, all three are used. |
| `--flac` / `--mp3` | Quality. FLAC = bitrate 9, MP3 320 = bitrate 3. Default from `DEEMIX_QUALITY`, else FLAC. |
| `--compilations` | Also include releases Deezer tags as "compilation". |
| `-a`, `--artists PATH` | Artist list file (default `artists.txt`). |
| `--server URL` | deemix-gui base URL (else `DEEMIX_SERVER`, else `http://127.0.0.1:6595`). |
| `--arl VALUE` | ARL override (else `DEEMIX_ARL`, else `%APPDATA%\deemix\.arl`). |
| `--dry-run` | Resolve and list only; queue nothing. |

ARL resolution order: `--arl` → `DEEMIX_ARL` (incl. `.env`) → the deemix
config file `.arl`.

## How it works / notes on deemix-gui's API

This was reverse-engineered against deemix-gui build `2022.12.14` (deemix-lib
`3.6.14`). Useful things to know if you hack on it:

- **Login is `POST /api/loginArl`** with `{"arl": "...", "force": false,
  "child": 0}` and is **bound to the HTTP session cookie** (`connect.sid`), so
  the script keeps one cookie jar and logs in every run.
- **`POST /api/addToQueue`** takes `{"url": "<deezer url>", "bitrate": N}` where
  **`url` is a string, not an array** (multiple links are joined with `;`).
  Passing a JSON array triggers an unhandled `url.split is not a function` and
  **crashes the whole server process** — so this script sends one string per
  call.
- **Bitrate codes:** `9` = FLAC, `3` = MP3 320, `1` = MP3 128.
- Artist/album metadata comes from the **public Deezer API**
  (`api.deezer.com`), which needs no auth. `record_type` is one of
  `album`, `single`, `ep`, `compilation`.

## Troubleshooting

- **`Cannot reach deemix server`** — the server isn't running, or `DEEMIX_SERVER`
  is wrong.
- **`Could not log in`** — bad/expired ARL. ARLs last ~3 months; refresh it.
- **Server crashes / `NotLoggedIn` mid-run** — almost always login state; the
  script aborts cleanly and tells you what to do. Re-run after refreshing the
  ARL.
- **Wrong artist matched** — name search takes Deezer's top hit; the script
  prints a `~ resolved to '...'` warning when the match isn't exact. Use the
  Deezer id or URL to be unambiguous.

## Files

| File | Purpose |
|------|---------|
| `deemix_dl.py` | The downloader. |
| `.env.example` | Template config; copy to `.env`. |
| `artists.example.txt` | Template artist list; copy to `artists.txt`. |
| `.gitignore` | Keeps `.env`, `*.exe`, and your personal `artists.txt` out of git. |
