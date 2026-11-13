"""
Build a matchup dataset for a format that has no community sheets, from the
Videre Project's public MTGO API (https://api.videreproject.com).

    python build_videre.py vintage              # past 3 months -> site/data-vintage.json
    python build_videre.py vintage --days 180

MTGO only publishes the top 32 decklists of a challenge, so only those players
get a deck label. Everyone else is written with an empty deck, which the page
treats as "Unknown" and leaves out of the matrix by default: the matrix is then
top 32 vs top 32.

Videre labels each deck twice: a deck name (e.g. "Esper Tinker") and an
archetype (e.g. "Blue Tinker"). These become the page's Deck and Archetype group.
When Videre's classifier is unsure it falls back to colours ("WU", "UBR");
classify.json lets you relabel those by hand (see the file for the rules).

Output uses the same packed format as build_data.py (see that file).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://api.videreproject.com"
CACHE = ROOT / "cache" / "videre"
CLASSIFY = ROOT / "classify.json"
FRESH_DAYS = 4                      # events this recent are re-fetched (late imports, relabels)
KINDS = ("Challenge", "Showcase")
COLOURS_ONLY = re.compile(r"^(?:[WUBRG]{1,5}|[2345]c|Unknown)$", re.I)


def get(path: str) -> dict:
    """GET an API route; an empty result comes back as 400 'No results found.'"""
    url = API + path
    for wait in (0, 3, 10, 30):
        time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": "legacy-challenge-matrix (github.com/kaisengg/legacy-challenge-matrix)"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 400 and b"No results found" in body:
                return {"data": [], "meta": {"has_more": False}}
            if e.code < 500 and e.code != 429:
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
    raise RuntimeError(f"giving up on {url}")


def get_all(path: str) -> list[dict]:
    sep = "&" if "?" in path else "?"
    rows, offset = [], 0
    while True:
        d = get(f"{path}{sep}limit=500&offset={offset}")
        rows += d["data"]
        if not d["meta"].get("has_more"):
            return rows
        offset = d["meta"]["next_offset"]


def event_raw(fmt: str, ev: dict) -> dict:
    """Matches, standings and decklists of one event, cached on disk once it has settled."""
    path = CACHE / fmt / f"{ev['id']}.json"
    settled = date.fromisoformat(ev["date"][:10]) < date.today() - timedelta(days=FRESH_DAYS)
    if settled and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raw = {
        "matches": get_all(f"/matches/{fmt}?event_id={ev['id']}"),
        "standings": get_all(f"/standings/{fmt}?event_id={ev['id']}"),
        "decks": [{"player": d["player"], "main": {c["name"]: c["quantity"] for c in d.get("mainboard") or []},
                   "side": {c["name"]: c["quantity"] for c in d.get("sideboard") or []}}
                  for d in get_all(f"/decks/{fmt}?event_id={ev['id']}")],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return raw


# ---------- manual classification ----------
def load_rules(fmt: str) -> dict:
    if not CLASSIFY.exists():
        return {}
    cfg = json.loads(CLASSIFY.read_text(encoding="utf-8"))
    return cfg.get(fmt, {})


def classify(rules: dict, deck: str, group: str, cards: dict | None, deck_key: str) -> tuple[str, str]:
    """Apply classify.json: per-deck overrides, then card rules, then plain renames."""
    by_deck = rules.get("decks", {})
    if deck_key in by_deck:
        v = by_deck[deck_key]
        return (v, v) if isinstance(v, str) else (v["deck"], v.get("group", v["deck"]))
    if cards is not None:
        for r in rules.get("rules", []):
            if not r.get("override") and not COLOURS_ONLY.match(deck or "Unknown"):
                continue
            if all(cards.get(c, 0) > 0 for c in r.get("all", [])) and \
               (not r.get("any") or any(cards.get(c, 0) > 0 for c in r["any"])) and \
               not any(cards.get(c, 0) > 0 for c in r.get("none", [])):
                return r["deck"], r.get("group", r["deck"])
    ren = rules.get("rename", {})
    return ren.get(deck, deck), ren.get(group, group)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("format", help="MTGO format, e.g. vintage, modern, pauper")
    ap.add_argument("--days", type=int, default=92)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    fmt = args.format.lower()
    t0 = time.time()
    until = date.today()
    since = until - timedelta(days=args.days)

    events = [e for e in get_all(f"/events/{fmt}?min_date={since}&max_date={until}") if e["kind"] in KINDS]
    events.sort(key=lambda e: (e["date"], e["id"]))
    print(f"{len(events)} {fmt} challenges since {since} ({time.time() - t0:.0f}s)", flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        raws = list(pool.map(lambda e: event_raw(fmt, e), events))
    print(f"  downloaded ({time.time() - t0:.0f}s)", flush=True)

    rules = load_rules(fmt)
    tables = {k: {} for k in ("decks", "groups", "names", "kinds")}
    ix = lambda t, v: tables[t].setdefault(v, len(tables[t]))  # noqa: E731
    packed, relabelled, colour_only = [], 0, 0
    seen_slug: dict[str, int] = {}
    for ev, raw in zip(events, raws):
        ms = [m for m in raw["matches"]]
        if not ms:
            continue
        swiss = ev["rounds"]
        decks = {d["player"]: d for d in raw["decks"]}
        names = sorted({s["player"] for s in raw["standings"]} | {m["player"] for m in ms} | {m["opponent"] for m in ms if m["opponent"]})
        pidx = {n: i for i, n in enumerate(names)}
        label = {s["player"]: (s.get("deck_name") or s.get("archetype") or "", s.get("archetype") or s.get("deck_name") or "", s.get("deck_id"))
                 for s in raw["standings"]}

        # one row per pairing; Videre returns each pairing from both sides
        matches, seen = [], set()
        last_round, w, l = {}, {n: 0 for n in names}, {n: 0 for n in names}
        for m in sorted(ms, key=lambda m: m["round"]):
            a = m["player"]
            last_round[a] = max(last_round.get(a, 0), m["round"])
            if m["isbye"] or not m["opponent"]:
                if m["round"] <= swiss and m["result"] == "win":
                    key = (m["round"], a, "")
                    if key not in seen:
                        seen.add(key); w[a] += 1
                continue
            b = m["opponent"]
            key = (m["round"],) + tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            ga, gb, _ = (int(x) for x in m["record"].split("-"))
            matches += [m["round"], pidx[a], pidx[b], ga, gb]
            if m["round"] <= swiss:
                if ga > gb: w[a] += 1; l[b] += 1
                elif gb > ga: w[b] += 1; l[a] += 1
            last_round[b] = max(last_round.get(b, 0), m["round"])

        players = []
        for n in names:
            deck, group, deck_id = label.get(n, ("", "", None))
            if deck:
                d = decks.get(n)
                cards = None if d is None else {**d["side"], **d["main"]}
                nd, ng = classify(rules, deck, group, cards, str(deck_id))
                if (nd, ng) != (deck, group):
                    relabelled += 1
                deck, group = nd, ng
                if COLOURS_ONLY.match(deck):
                    colour_only += 1
            flags = (1 if last_round.get(n, 0) < swiss else 0) | (2 if last_round.get(n, 0) > swiss else 0)
            players += [ix("names", n), ix("decks", deck), ix("groups", group), w[n], l[n], flags]

        day = ev["date"][:10]
        base = re.sub(r"[^a-z0-9]+", "-", ev["name"].lower()).strip("-") + "-" + day
        k = seen_slug.get(base, 0)
        seen_slug[base] = k + 1
        slug = base if k == 0 else f"{base}-{k}"   # MTGGoldfish: second same-day event gets "-1"
        packed.append([str(ev["id"]), ix("kinds", ev["name"]), day, swiss, players, matches, slug])

    data = {
        "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "format": fmt.capitalize(),
        "provider": "videre",
        "source": "https://videreproject.com",
        "first": packed[0][2] if packed else None,
        "last": packed[-1][2] if packed else None,
        **{k: list(v) for k, v in tables.items()},
        "events": packed,
    }
    out = ROOT / "site" / f"data-{fmt}.json"
    out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    n_matches = sum(len(e[5]) // 5 for e in packed)
    print(f"\nWrote {out} ({out.stat().st_size / 1e3:.0f} kB): {len(packed)} events, {n_matches:,} matches, "
          f"{len(tables['decks'])} deck labels, {data['first']} .. {data['last']}")
    print(f"classify.json relabelled {relabelled} decklists; {colour_only} top-32 decks still have colour-only labels")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
