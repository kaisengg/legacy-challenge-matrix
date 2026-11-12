"""
Download every Legacy Challenge sheet from the public Drive folder and pack the
players + matches into one compact file for the shareable page.

    python build_data.py                 # 2023-01-01 .. today  ->  site/data.json
    python build_data.py --since 2026-10-01

Sheets already in ./cache are reused, so re-running only fetches new ones
(and refreshes sheets from the last few weeks, which may still be edited).

Packed format (site/data.json):
  decks/groups/names/kinds : string tables
  events[] = [sheet_id, kind_idx, "YYYY-MM-DD", swiss_rounds, players, matches, mtggoldfish_slug]
     players = flat ints, 6 per player: name_idx, deck_idx, group_idx, wins, losses, flags
               (flags: 1 = dropped before the last Swiss round, 2 = made top 8)
     matches = flat ints, 5 per match:  round, player_a, player_b, games_a, games_b
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import server

OUT = Path(__file__).resolve().parent / "site" / "data.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f"{server.FIRST_YEAR}-01-01")
    ap.add_argument("--until", default=date.today().isoformat())
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    t0 = time.time()
    events = server.events_between(date.fromisoformat(args.since), date.fromisoformat(args.until))
    print(f"{len(events)} challenge sheets listed ({time.time() - t0:.0f}s)", flush=True)

    raws: dict[str, dict] = {}
    failed: list[dict] = []

    def fetch(ev):
        return ev, server.raw_event(ev)

    todo = list(events)
    for attempt in (1, 2):
        failed = []
        with ThreadPoolExecutor(max_workers=server.MAX_PARALLEL) as pool:
            futs = [pool.submit(fetch, ev) for ev in todo]
            for n, fut in enumerate(as_completed(futs), 1):
                try:
                    ev, raw = fut.result()
                    raws[ev["id"]] = raw
                except Exception as e:  # noqa: BLE001
                    failed.append({"ev": None, "err": str(e)})
                if n % 25 == 0 or n == len(futs):
                    print(f"  pass {attempt}: {n}/{len(futs)} sheets ({time.time() - t0:.0f}s)", flush=True)
        todo = [ev for ev in events if ev["id"] not in raws]
        if not todo:
            break
        print(f"  {len(todo)} sheets failed, retrying once more", flush=True)
    missing = [ev for ev in events if ev["id"] not in raws]

    aliases = server.load_aliases()
    tables = {k: {} for k in ("decks", "groups", "names", "kinds")}

    def ix(table: str, value: str) -> int:
        t = tables[table]
        if value not in t:
            t[value] = len(t)
        return t[value]

    packed, empty = [], 0
    for ev in events:
        if ev["id"] not in raws:
            continue
        p = server.parse_event(ev, raws[ev["id"]], aliases)
        if not p["has_data"]:
            empty += 1
            continue
        players = []
        for pl in p["players"]:
            flags = (1 if pl["dropped"] else 0) | (2 if pl["top8"] else 0)
            players += [ix("names", pl["name"]), ix("decks", pl["deck"]), ix("groups", pl["group"]), pl["w"], pl["l"], flags]
        matches = [v for m in p["matches"] for v in m]
        packed.append([ev["id"], ix("kinds", ev["kind"]), ev["date"], p["swiss_rounds"], players, matches, ev["slug"]])

    data = {
        "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "source": f"https://drive.google.com/drive/folders/{server.DRIVE_ROOT}",
        "first": packed[0][2] if packed else None,
        "last": packed[-1][2] if packed else None,
        **{k: list(v) for k, v in tables.items()},
        "events": packed,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    n_matches = sum(len(e[5]) // 5 for e in packed)
    print(f"\nWrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB): {len(packed)} events, {n_matches:,} matches, "
          f"{len(tables['decks'])} decks, {data['first']} .. {data['last']}")
    if empty:
        print(f"{empty} sheets had no match data yet (skipped)")
    if missing:
        print(f"{len(missing)} sheets could not be downloaded:")
        for ev in missing:
            print(f"   {ev['date']} {ev['name']}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
