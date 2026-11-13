"""
Build a matchup dataset for a format that has no community sheets, from the
Videre Project's public MTGO API (https://api.videreproject.com).

    python build_videre.py vintage                     # 2024-01-01 .. today -> site/data-vintage.json
    python build_videre.py vintage --since 2025-06-01

Videre lists events back to 2015, but round-by-round matches only exist from
January 2024, so that is the default start.

MTGO only publishes the top 32 decklists of a challenge, so only those players
get a deck label. Everyone else is written with an empty deck, which the page
treats as "Unknown" and leaves out of the matrix by default: the matrix is then
top 32 vs top 32.

Videre labels each deck twice: a deck name (e.g. "Esper Tinker") and an
archetype (e.g. "Blue Tinker"). These become the page's Deck and Archetype group.
When Videre's classifier is unsure it falls back to colours ("WU", "UBR");
classify.json lets you relabel those by hand (see the file for the rules).

Output uses the same packed format as build_data.py (see that file), plus an 8th
field per event: each player's final placing (0 = unknown), in player order.
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
FIRST_MATCHES = "2024-01-01"        # Videre has no round data before this
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


# ---------- MTGO dates and MTGGoldfish links ----------
MTGO = "https://www.mtgo.com"
MTGO_LINK = re.compile(r"/decklist/([a-z0-9-]+?)-(\d{4}-\d{2}-\d{2})(\d{6,})")


def mtgo_get(url: str) -> str:
    for wait in (0, 5, 20, 60):
        time.sleep(wait or 0.5)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (legacy-challenge-matrix)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError):
            pass
    raise RuntimeError(f"giving up on {url}")


def mtgo_month(y: int, m: int) -> dict[str, tuple[str, str]]:
    """MTGO event id -> (event slug, date) from mtgo.com's monthly decklist index."""
    path = CACHE / "mtgo-index" / f"{y}-{m:02d}.json"
    done = date(y, m, 1) < (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)   # older than last month
    if done and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    for wait in (0, 30, 90):   # mtgo.com sometimes serves a page without the list; retry, never cache it
        time.sleep(wait)
        out = {i: (name, d) for name, d, i in MTGO_LINK.findall(mtgo_get(f"{MTGO}/decklists/{y}/{m:02d}"))}
        if out:
            break
    else:
        raise RuntimeError("index page had no decklist links")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")
    return out


def mtgo_links(events: list[dict], previous: dict[str, tuple[str, str]]) -> tuple[dict, dict]:
    """
    Videre stores a Pacific-time date, so evening events land a day before MTGO's own date, which is
    what MTGGoldfish uses. Take each event's date and name from mtgo.com's monthly index instead, and
    order same-name same-day events by MTGO event id (ids are handed out in start-time order;
    checked against the start times of 56 same-day pairs): the first keeps the plain MTGGoldfish slug, the
    second gets "-1", and so on. If mtgo.com is unreachable, events keep the date and link of the
    previous build (`previous`); only events never seen there fall back to Videre's date and name.
    """
    months = sorted({(int(e["date"][:4]), int(e["date"][5:7])) for e in events})
    months = sorted(set(months) | {(y + (m == 12), m % 12 + 1) for y, m in months})   # next month too
    index: dict[str, tuple[str, str]] = {}
    for y, m in months:
        if date(y, m, 1) > date.today():
            continue
        try:
            index.update(mtgo_month(y, m))
        except Exception as e:  # noqa: BLE001
            print(f"  mtgo.com index {y}-{m:02d} unavailable ({e}); using Videre dates there", flush=True)
    day_of, base_of, found = {}, {}, 0
    for e in events:
        hit = index.get(str(e["id"]))
        if not hit and str(e["id"]) in previous:
            day_of[e["id"]] = previous[str(e["id"])][0]
            continue
        if hit:
            found += 1
            name, day = hit
        else:
            name, day = re.sub(r"[^a-z0-9]+", "-", e["name"].lower()).strip("-"), e["date"][:10]
        day_of[e["id"]], base_of[e["id"]] = day, (name, day)
    groups: dict[tuple, list] = {}
    for e in events:
        if e["id"] in base_of:
            groups.setdefault(base_of[e["id"]], []).append(e["id"])
    slug_of = {e["id"]: previous[str(e["id"])][1] for e in events if e["id"] not in base_of}
    for (name, day), ids in groups.items():
        for k, eid in enumerate(sorted(ids, key=int)):
            slug_of[eid] = f"{name}-{day}" + (f"-{k}" if k else "")
    print(f"  mtgo.com dates found for {found}/{len(events)} events; "
          f"{sum(1 for v in groups.values() if len(v) > 1)} same-day duplicates", flush=True)
    return slug_of, day_of


# ---------- decklists without a Videre label ----------
GUESS_DAYS = 45      # compare with labelled decklists from this many days either side
GUESS_MIN = 0.5      # minimum share of the 60 that must match the nearest labelled deck


def guess_labels(pool: list, unlabelled: list, ix) -> int:
    """
    Videre sometimes never labels an event's decklists (about 2% of events). Give each such decklist
    the label of the most similar labelled decklist from nearby dates, measured as the share of
    mainboard cards in common (counting copies). Decks with nothing similar enough stay Unknown.
    """
    by_day: dict[str, list] = {}
    for day, main, deck, group in pool:
        by_day.setdefault(day, []).append((frozenset(main), main, deck, group))
    done = 0
    for day, main, players, at in unlabelled:
        d0, names = date.fromisoformat(day), frozenset(main)
        near = [c for k in range(-GUESS_DAYS, GUESS_DAYS + 1)
                for c in by_day.get((d0 + timedelta(days=k)).isoformat(), ())]
        near.sort(key=lambda c: len(names & c[0]) / (len(names | c[0]) or 1), reverse=True)
        best, score = None, 0.0
        for _, other, deck, group in near[:20]:
            both = sum(min(q, other.get(c, 0)) for c, q in main.items())
            sim = both / (max(sum(main.values()), sum(other.values())) or 1)
            if sim > score:
                best, score = (deck, group), sim
        if best and score >= GUESS_MIN:
            players[at], players[at + 1] = ix("decks", best[0]), ix("groups", best[1])
            done += 1
    return done


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
            if r.get("from") and deck not in r["from"]:
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
    ap.add_argument("--since", default=FIRST_MATCHES, help="first event date, YYYY-MM-DD")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    fmt = args.format.lower()
    t0 = time.time()
    until = date.today()
    since = date.fromisoformat(args.since)

    events = [e for e in get_all(f"/events/{fmt}?min_date={since}&max_date={until}") if e["kind"] in KINDS]
    events.sort(key=lambda e: (e["date"], e["id"]))
    print(f"{len(events)} {fmt} challenges since {since} ({time.time() - t0:.0f}s)", flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        raws = list(pool.map(lambda e: event_raw(fmt, e), events))
    print(f"  downloaded ({time.time() - t0:.0f}s)", flush=True)

    rules = load_rules(fmt)
    tables = {k: {} for k in ("decks", "groups", "names", "kinds")}
    ix = lambda t, v: tables[t].setdefault(v, len(tables[t]))  # noqa: E731
    packed, relabelled, colour_only = [], 0, 0
    pool, unlabelled = [], []   # labelled decklists (for guessing) and decklists Videre has not labelled yet
    out = ROOT / "site" / f"data-{fmt}.json"
    previous = {}
    if out.exists():
        try:
            previous = {e[0]: (e[2], e[6]) for e in json.loads(out.read_text(encoding="utf-8"))["events"] if len(e) > 7}
        except (ValueError, KeyError):
            pass
    slug_of, day_of = mtgo_links(events, previous)
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

        rank = {s["player"]: s.get("rank") or 0 for s in raw["standings"]}
        # The official standings record is authoritative: Videre's match rows occasionally miss a round.
        # Top-8 players keep the Swiss-only record counted above (standings include their top-8 rounds).
        played = {}
        for s_ in raw["standings"]:
            n = s_["player"]
            try:
                sw, sl, sd = (int(x) for x in (s_.get("record") or "").split("-"))
            except ValueError:
                continue
            if n in w and last_round.get(n, 0) <= swiss:
                w[n], l[n] = sw, sl
                played[n] = sw + sl + sd
        players, ranks = [], []
        for n in names:
            deck, group, deck_id = label.get(n, ("", "", None))
            if not deck and n in decks:
                unlabelled.append((ev["date"][:10], decks[n]["main"], players, len(players) + 1))
            if deck:
                d = decks.get(n)
                cards = None if d is None else {**d["side"], **d["main"]}
                nd, ng = classify(rules, deck, group, cards, str(deck_id))
                if (nd, ng) != (deck, group):
                    relabelled += 1
                deck, group = nd, ng
                if COLOURS_ONLY.match(deck):
                    colour_only += 1
                elif d is not None:
                    pool.append((ev["date"][:10], d["main"], deck, group))
            dropped = played[n] < swiss if n in played else last_round.get(n, 0) < swiss
            flags = (1 if dropped else 0) | (2 if last_round.get(n, 0) > swiss else 0)
            players += [ix("names", n), ix("decks", deck), ix("groups", group), w[n], l[n], flags]
            ranks.append(rank.get(n, 0))

        day = day_of[ev["id"]]
        packed.append([str(ev["id"]), ix("kinds", ev["name"]), day, swiss, players, matches, slug_of[ev["id"]], ranks])

    guessed = guess_labels(pool, unlabelled, ix)

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
    out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    n_matches = sum(len(e[5]) // 5 for e in packed)
    print(f"\nWrote {out} ({out.stat().st_size / 1e3:.0f} kB): {len(packed)} events, {n_matches:,} matches, "
          f"{len(tables['decks'])} deck labels, {data['first']} .. {data['last']}")
    print(f"{guessed}/{len(unlabelled)} decklists Videre had not labelled were matched to a similar labelled deck")
    print(f"classify.json relabelled {relabelled} decklists; {colour_only} top-32 decks still have colour-only labels")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
