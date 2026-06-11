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
