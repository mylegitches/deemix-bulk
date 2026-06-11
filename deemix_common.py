"""Shared helpers for the deemix tools (deemix_dl.py / deemix_sync.py).

Standard library only -- no third-party dependencies.
"""

import http.cookiejar
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env"):
    """Minimal .env loader. Real environment variables take precedence."""
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


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def setup_logging(name, logdir="logs", level=logging.INFO):
    """Console + timestamped file logger.  Returns (logger, logfile_path)."""
    os.makedirs(logdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = os.path.join(logdir, f"{name}_{ts}.log")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.info("log file: %s", logfile)
    return logger, logfile


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #
def server_url(cli=None):
    return (cli or os.environ.get("DEEMIX_SERVER")
            or "http://127.0.0.1:6595").rstrip("/")


def find_arl(cli=None):
    """ARL from --arl, then DEEMIX_ARL, then the deemix .arl config file."""
    if cli:
        return cli.strip()
    if os.environ.get("DEEMIX_ARL"):
        return os.environ["DEEMIX_ARL"].strip()
    candidates = []
    if os.environ.get("APPDATA"):
        candidates.append(os.path.join(os.environ["APPDATA"], "deemix", ".arl"))
    candidates.append(os.path.expanduser("~/.config/deemix/.arl"))
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                arl = f.read().strip()
            if arl:
                return arl
    return None


# --------------------------------------------------------------------------- #
# Ollama (cloud) -- optional AI disambiguation of ambiguous artist names
# --------------------------------------------------------------------------- #
def ai_enabled():
    return bool(os.environ.get("OLLAMA_API_KEY"))


def ollama_chat(messages, model=None, timeout=60):
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        raise RuntimeError("OLLAMA_API_KEY not set")
    # Use OLLAMA_URL, not OLLAMA_HOST -- the latter is commonly set to a bind
    # address (e.g. 0.0.0.0) by a local Ollama install and isn't a client URL.
    base = os.environ.get("OLLAMA_URL", "https://ollama.com").rstrip("/")
    if not base.startswith("http"):
        base = "https://ollama.com"
    model = model or os.environ.get("OLLAMA_MODEL", "gemini-3-flash-preview")
    body = json.dumps({"model": model, "messages": messages,
                       "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/chat", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


def ai_pick_artist(query, candidates, model=None):
    """Pick the best Deezer artist for `query` from `candidates` (dicts with
    id/name/nb_fan). Returns the chosen id (int) or None. Best-effort: any
    failure returns None so the caller can fall back to the heuristic."""
    if not candidates:
        return None
    lines = [f'{i+1}) id={c["id"]} name="{c["name"]}" fans={c.get("nb_fan", 0)}'
             for i, c in enumerate(candidates)]
    prompt = (
        "Match a user's music-artist name to the correct Deezer artist.\n"
        f'Query: "{query}"\n'
        "Candidates:\n" + "\n".join(lines) + "\n\n"
        "Choose the candidate the user most likely means (the well-known "
        "recording artist with that name; use fan counts as a tie-breaker). "
        "Reply with ONLY the numeric Deezer id, or NONE.")
    try:
        out = ollama_chat([{"role": "user", "content": prompt}], model=model).strip()
    except Exception:  # noqa: BLE001
        return None
    ids = {str(c["id"]): c["id"] for c in candidates}
    # exact id substring match (ids are long enough to be unambiguous)
    for sid, real in ids.items():
        if sid in out:
            return real
    return None


# --------------------------------------------------------------------------- #
# manifest -- maps each queued artist back to its original artists.txt line,
# so phase 2 can remove the line after the band is moved (handles id/URL lines)
# --------------------------------------------------------------------------- #
STATE_DIR = "state"
MANIFEST_FILE = os.path.join(STATE_DIR, "manifest.json")


def load_manifest():
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"artists_file": None, "entries": []}


def save_manifest(m):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# deemix-gui client
# --------------------------------------------------------------------------- #
class ServerDown(Exception):
    """The deemix server can't be reached (crashed / not running)."""


class Client:
    """Thin client for the deemix-gui REST API. Keeps a session cookie so the
    ARL login (which is bound to connect.sid) persists across calls."""

    def __init__(self, server):
        self.server = server.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, path, timeout=30):
        req = urllib.request.Request(self.server + path,
                                     headers={"User-Agent": "deemix-tools/1.0"})
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path, body, timeout=60):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.server + path, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "deemix-tools/1.0"})
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def up(self):
        try:
            self.get("/api/getSettings", timeout=8)
            return True
        except Exception:  # noqa: BLE001
            return False

    def login(self, arl):
        resp = self.post("/api/loginArl",
                         {"arl": arl, "force": False, "child": 0})
        if resp.get("status") == 1:
            return (resp.get("user") or {}).get("name", "?")
        raise RuntimeError(f"login failed (status {resp.get('status')}, "
                           f"errid {resp.get('errid')})")

    def settings(self):
        return self.get("/api/getSettings").get("settings", {})

    def get_queue(self):
        return self.get("/api/getQueue")

    def add_to_queue(self, url, bitrate):
        """url MUST be a string (deemix joins multiple with ';'; a list crashes
        the server). Returns ('ok', None) | ('error', errid); raises ServerDown."""
        try:
            resp = self.post("/api/addToQueue", {"url": url, "bitrate": bitrate})
        except urllib.error.HTTPError as e:
            return ("error", f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 connection-level => server gone
            raise ServerDown(str(e))
        if resp.get("result") is False:
            return ("error", resp.get("errid"))
        return ("ok", None)
