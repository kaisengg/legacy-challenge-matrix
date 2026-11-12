"""
Legacy Challenge matchup explorer - local server.

Reads the public "Public Facing Sheets" Google Drive folder:
    <year> / <n - Month> / "Legacy Challenge 32 10/30/2026" (Google Sheet)
From each sheet it exports two tabs as CSV:
    Input           - every player: wins, losses, archetype group, deck, byes
    Match Up Input  - every player's opponent and game score for each round
Everything is cached under ./cache so each sheet is downloaded once.

Run:   python server.py        then open http://localhost:8765
Standard library only.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
ALIASES_FILE = ROOT / "aliases.json"

DRIVE_ROOT = "1eX3hYT_iou09-xgrhScFvdz2ShIlS3EJ"     # "Public Facing Sheets"
INPUT_GID = "0"                                       # tab gids are identical in every sheet (one template)
MATCHUP_GID = "1528213059"
FIRST_YEAR = 2023
PORT = 8765
USER_AGENT = "Mozilla/5.0 (compatible; legacy-matchup-explorer/1.0; personal use)"
MAX_PARALLEL = 4                                      # simultaneous requests to Google
RECENT_DAYS = 21                                      # sheets this new may still be edited -> refresh
RECENT_TTL = 6 * 3600

_google = threading.Semaphore(MAX_PARALLEL)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------- #
# Google access
# --------------------------------------------------------------------------- #
def http_get(url: str) -> str:
    last = None
    for wait in (0, 5, 20, 45):
        time.sleep(wait)
        try:
            with _google:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    raise RuntimeError(f"Google did not respond ({last})")


def cached(path: Path, ttl: float | None, produce):
    """Return JSON from `path` if fresh enough, otherwise produce() and save it."""
    with _lock_for(str(path)):
        if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
            return json.loads(path.read_text(encoding="utf-8"))
        data = produce()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data


def list_folder(folder_id: str, ttl: float | None) -> list[dict]:
    """Children of a public Drive folder, parsed from the folder page's embedded data."""
    def produce():
        html = http_get(f"https://drive.google.com/drive/folders/{folder_id}")
        m = re.search(r"window\['_DRIVE_ivd'\]\s*=\s*'(.*?)';", html, re.S)
        if not m:
            raise RuntimeError(f"could not read Drive folder {folder_id} (is it still public?)")
        raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), m.group(1))
        raw = raw.replace("\\/", "/").replace("\\'", "'")
        items = json.loads(raw)[0] or []
        return [{"id": it[0], "name": it[2], "kind": it[3].rsplit(".", 1)[-1]} for it in items]
    return cached(CACHE / "folders" / f"{folder_id}.json", ttl, produce)


def sheet_csv(sheet_id: str, gid: str) -> list[list[str]]:
    text = http_get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}")
    return [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]


# --------------------------------------------------------------------------- #
# Event index
# --------------------------------------------------------------------------- #
SKIP_SUBFOLDER_RE = re.compile(r"prelim|premier|last chance|qualifier|paper", re.I)
NAME_RE = re.compile(r"^(?P<kind>.*?challenge(?:\s+\d+)?)\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?", re.I)


def _month_ttl(year: int, month: int) -> float | None:
    today = date.today()
    months_ago = (today.year - year) * 12 + today.month - month
    return 3600 if months_ago <= 1 else None   # recent months may still get new sheets


def events_between(start: date, end: date) -> list[dict]:
    years = {f["name"].strip(): f["id"] for f in list_folder(DRIVE_ROOT, 6 * 3600) if f["kind"] == "folder"}
    out, seen = [], set()
    for year in range(start.year, end.year + 1):
        if str(year) not in years:
            continue
        months = {}
        for f in list_folder(years[str(year)], 6 * 3600 if year >= date.today().year else None):
            m = re.match(r"\s*(\d{1,2})", f["name"])
            if f["kind"] == "folder" and m:
                months[int(m.group(1))] = f["id"]
        for month, fid in sorted(months.items()):
            if (year, month) < (start.year, start.month) or (year, month) > (end.year, end.month):
                continue
            ttl = _month_ttl(year, month)
            files = list_folder(fid, ttl)
            # Some months keep challenges in a subfolder (Jan-Jul 2024: "Challenges", Nov 2025: "Pre Ban").
            for sub in [f for f in files if f["kind"] == "folder" and not SKIP_SUBFOLDER_RE.search(f["name"])]:
                files = files + list_folder(sub["id"], ttl)
            for f in files:
                if f["kind"] != "spreadsheet" or f["id"] in seen:
                    continue
                seen.add(f["id"])
                m = NAME_RE.match(f["name"].strip())
                if not m:
                    continue
                y = int(m["y"]) if m["y"] else year
                y = y + 2000 if y < 100 else y
                try:
                    d = date(y, int(m["m"]), int(m["d"]))
                except ValueError:
                    continue
                if start <= d <= end:
                    kind = re.sub(r"\s+", " ", m["kind"]).strip().title()
                    # MTGGoldfish tournament slug, from the event's own name: legacy-challenge-32-2026-10-30
                    slug_kind = kind
                    if kind == "Legacy Challenge" and d >= date(2024, 6, 1):
                        slug_kind = "Legacy Challenge 32"   # sizes are in MTGO names since spring 2024; a few sheets omit it
                    slug = re.sub(r"[^a-z0-9]+", "-", slug_kind.lower()).strip("-") + "-" + d.isoformat()
                    if kind == "Legacy Challenge":
                        kind = "Legacy Challenge 32"   # 2023-era sheets have no size in the name
                    out.append({"id": f["id"], "name": f["name"].strip(), "kind": kind, "date": d.isoformat(), "slug": slug})
    out.sort(key=lambda e: (e["date"], e["name"]))
    return out


# --------------------------------------------------------------------------- #
# Event parsing
# --------------------------------------------------------------------------- #
SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*\d+)?\s*$")


def load_aliases() -> dict[str, str]:
    if not ALIASES_FILE.exists():
        return {}
    data = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    return {k.strip().lower(): v for k, v in (data.get("aliases") or {}).items()}


def raw_event(ev: dict) -> dict:
    """Both tabs of one sheet as CSV rows (cached; recent sheets are refreshed)."""
    age = (date.today() - date.fromisoformat(ev["date"])).days
    ttl = RECENT_TTL if age <= RECENT_DAYS else None
    return cached(CACHE / "sheets" / f"{ev['id']}.json", ttl, lambda: {
        "input": sheet_csv(ev["id"], INPUT_GID),
        "matchups": sheet_csv(ev["id"], MATCHUP_GID),
    })


def parse_event(ev: dict, raw: dict, aliases: dict[str, str]) -> dict:
    def deck_name(d: str) -> str:
        d = (d or "").strip()
        return aliases.get(d.lower(), d) or "Unknown"

    # ---- Input tab: player -> deck / group
    players: dict[str, dict] = {}
    rows = raw["input"]
    hdr_i = next((i for i, r in enumerate(rows) if "Name" in r and "Deck" in r), None)
    if hdr_i is not None:
        h = rows[hdr_i]
        ci = {k: h.index(k) for k in ("Name", "Archetype", "Deck") if k in h}
        for r in rows[hdr_i + 1:]:
            name = r[ci["Name"]].strip() if len(r) > ci["Name"] else ""
            if not name:
                continue
            players[name.lower()] = {
                "name": name,
                "deck": deck_name(r[ci["Deck"]] if len(r) > ci.get("Deck", 99) else ""),
                "group": (r[ci["Archetype"]].strip() if "Archetype" in ci and len(r) > ci["Archetype"] else "") or "Unknown",
            }

    # ---- Match Up Input tab: rounds
    rows = raw["matchups"]
    hdr_m = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Player"), None)
    games: dict[tuple, tuple] = {}      # (round, a, b) with a<b  ->  (games a, games b)
    rounds_played: dict[str, int] = {}
    byes: dict[str, int] = {}
    conflicts = 0
    if hdr_m is not None:
        h = rows[hdr_m]
        round_cols = [(int(re.sub(r"\D", "", c) or 0), i) for i, c in enumerate(h) if c.strip().lower().startswith("round")]
        for r in rows[hdr_m + 1:]:
            me = r[0].strip()
            if not me:
                continue
            key_me = me.lower()
            if key_me not in players:
                players[key_me] = {"name": me, "deck": deck_name(r[1] if len(r) > 1 else ""), "group": "Unknown"}
            for rnd, col in round_cols:
                opp = r[col].strip() if col < len(r) else ""
                score = r[col + 1].strip() if col + 1 < len(r) else ""
                if not opp and not score:
                    continue
                rounds_played[key_me] = max(rounds_played.get(key_me, 0), rnd)
                if score.lower() == "bye" or opp.lower() == "bye":
                    byes[key_me] = byes.get(key_me, 0) + 1
                    continue
                m = SCORE_RE.match(score)
                if not opp or not m:
                    continue
                key_opp = opp.lower()
                if key_opp not in players:
                    players[key_opp] = {"name": opp, "deck": "Unknown", "group": "Unknown"}
                rounds_played[key_opp] = max(rounds_played.get(key_opp, 0), rnd)
                g_me, g_opp = int(m.group(1)), int(m.group(2))
                a, b = sorted((key_me, key_opp))
                val = (g_me, g_opp) if a == key_me else (g_opp, g_me)
                k = (rnd, a, b)
                if k in games and games[k] != val:
                    conflicts += 1
                games.setdefault(k, val)

    # ---- Swiss vs top 8, records, drops
    max_round = max(rounds_played.values(), default=0)
    swiss_rounds = max_round - 3 if max_round >= 6 else max_round
    rec = {k: [byes.get(k, 0), 0] for k in players}
    for (rnd, a, b), (ga, gb) in games.items():
        if rnd > swiss_rounds:
            continue
        if ga > gb:
            rec[a][0] += 1; rec[b][1] += 1
        elif gb > ga:
            rec[b][0] += 1; rec[a][1] += 1

    keys = sorted(players)
    idx = {k: i for i, k in enumerate(keys)}
    out_players = []
    for k in keys:
        p = players[k]
        last = rounds_played.get(k, 0)
        out_players.append({
            "name": p["name"], "deck": p["deck"], "group": p["group"],
            "w": rec[k][0], "l": rec[k][1],
            "dropped": last < swiss_rounds,
            "top8": last > swiss_rounds,
        })
    matches = [[rnd, idx[a], idx[b], ga, gb] for (rnd, a, b), (ga, gb) in sorted(games.items())]
    return {**ev, "swiss_rounds": swiss_rounds, "players": out_players, "matches": matches,
            "conflicts": conflicts, "has_data": bool(matches)}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_event_index: dict[str, dict] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/event?" not in (args[0] if args else ""):
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/events":
                start = datetime.strptime(q["start"], "%Y-%m-%d").date()
                end = datetime.strptime(q["end"], "%Y-%m-%d").date()
                events = events_between(max(start, date(FIRST_YEAR, 1, 1)), end)
                for e in events:
                    _event_index[e["id"]] = e
                    e["cached"] = (CACHE / "sheets" / f"{e['id']}.json").exists()
                self._json({"events": events, "first_year": FIRST_YEAR})
            elif url.path == "/api/event":
                ev = _event_index.get(q.get("id", ""))
                if not ev:
                    raise KeyError("unknown event id; list events first")
                self._json(parse_event(ev, raw_event(ev), load_aliases()))
            else:
                self._send(404, b"not found", "text/plain")
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    print(f"Legacy Challenge matchup explorer: http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
