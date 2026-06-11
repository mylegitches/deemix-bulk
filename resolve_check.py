#!/usr/bin/env python3
"""
Audit an artists list against Deezer BEFORE a big run.

For each line it shows how the name resolves and flags the risky ones:
  WRONG?  the top result's name doesn't match what you typed (likely the wrong
          artist -- e.g. "Unida" -> "Unidad de Musica de la Guardia Real")
  AMBIG   more than one artist has that exact name (a coin-flip without an id)
  MISS    no Deezer result at all

Fix flagged lines by replacing the name with a Deezer id or URL (100% exact).
Resolution here matches deemix_dl.py: among results, an exact (case-insensitive)
name match with the most fans wins; otherwise the top search hit is used.

    python resolve_check.py                 # audit artists.txt, show flagged only
    python resolve_check.py --all           # show every artist
    python resolve_check.py --ids           # print paste-ready "id  # name" list
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import deemix_common as dc

DEEZER = "https://api.deezer.com"


def get(url):
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deemix-dl/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.4 * (i + 1))
    return {}


def choose(results, query):
    """Mirror deemix_dl.resolve_artist: prefer exact name match by fans."""
    exact = [d for d in results if d["name"].strip().lower() == query.lower()]
    if exact:
        return max(exact, key=lambda d: d.get("nb_fan", 0)), len(exact)
    return (results[0] if results else None), 0


def main():
    ap = argparse.ArgumentParser(description="Audit an artists list against Deezer.")
    ap.add_argument("-a", "--artists", default="artists.txt")
    ap.add_argument("--all", action="store_true", help="show every artist, not just flagged")
    ap.add_argument("--ids", action="store_true", help="print paste-ready 'id  # name' list")
    ap.add_argument("--ai", action="store_true",
                    help="use the LLM (OLLAMA_API_KEY) to pick the right artist for flagged names")
    args = ap.parse_args()
    dc.load_dotenv()
    if args.ai and not dc.ai_enabled():
        print("note: --ai given but OLLAMA_API_KEY is not set; running heuristic-only.\n")
        args.ai = False

    lines = [ln.strip() for ln in open(args.artists, encoding="utf-8")]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    flagged = 0
    idmap = []
    for entry in lines:
        if entry.isdigit() or ("deezer.com" in entry and "/artist/" in entry):
            print(f"OK     {entry:38} -> explicit id/url")
            continue
        res = get(f"{DEEZER}/search/artist?q={urllib.parse.quote(entry)}&limit=10").get("data", [])
        if not res:
            print(f"MISS   {entry:38} -> no Deezer result")
            flagged += 1
            continue
        chosen, n_exact = choose(res, entry)
        nonexact = (n_exact == 0)
        ambiguous = (n_exact > 1)

        ai_pick = None
        if (nonexact or ambiguous) and args.ai:
            aid = dc.ai_pick_artist(entry, res[:6])
            if aid:
                ai_pick = next((c for c in res if c["id"] == aid), None)

        final = ai_pick or chosen
        idmap.append((entry, final["id"]))
        if nonexact or ambiguous:
            flagged += 1
            tag = "WRONG?" if nonexact else "AMBIG "
            ai_tag = " [AI]" if ai_pick else ""
            alts = "; ".join(f"{d['name']} (id {d['id']}, {d.get('nb_fan',0)} fans)"
                             for d in res[:3])
            print(f"{tag} {entry:38} -> {final['name']} "
                  f"(id {final['id']}, {final.get('nb_fan',0)} fans){ai_tag} | top3: {alts}")
        elif args.all:
            print(f"OK     {entry:38} -> {chosen['name']} "
                  f"(id {chosen['id']}, {chosen.get('nb_fan',0)} fans)")
        time.sleep(0.05)

    print(f"\n{flagged}/{len(lines)} flagged for review.")
    if args.ids:
        print("\n# paste-ready (swap ambiguous names for ids):")
        for line, i in idmap:
            print(f"{i}\t# {line}")


if __name__ == "__main__":
    main()
