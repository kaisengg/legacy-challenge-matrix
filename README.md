# Legacy Matchup Matrix

Head-to-head win rates between archetypes in MTGO Legacy Challenges, from January 2023 to today.
Pick a time frame, exclude 0-2 drops, group by deck or archetype group, and focus on one deck (e.g. Doomsday) to see it against every archetype it faced.

## Data

All data comes from the community-maintained Legacy Challenge sheets in the
[Public Facing Sheets](https://drive.google.com/drive/folders/1eX3hYT_iou09-xgrhScFvdz2ShIlS3EJ) Google Drive folder:
`<year> / <month> / Legacy Challenge 32 10/30/2026`. From each sheet the build reads two tabs:

- **Input**: every player with record, archetype group and deck.
- **Match Up Input**: each player's opponent and game score for every round.

Credit for the archetype labels and match entry goes to the sheets' contributors.

### Modern and Vintage

Modern and Vintage have no such sheets, so their toggles use the [Videre Project](https://videreproject.com)'s
public MTGO API ([open source](https://github.com/videre-project)) for every challenge since January 2024
(Videre has no round-by-round data before that).
MTGO publishes only the top 32 decklists, so deck-vs-deck cells count matches where both players finished top 32;
"Overall" also counts their rounds against players outside the top 32 (shown with the opponent's placing and record).
Decks Videre can only label by colour (`WU`, `UBR`, ...) can be relabelled by hand in `classify.json`
(card rules, single-decklist fixes, renames; see the `_help` notes in the file). A few events' decklists were never
labelled by Videre; those get the label of the most similar labelled decklist from within six weeks (96% agreement
when tested on labelled decks). Event dates and MTGGoldfish links come from mtgo.com's monthly decklist index, since
Videre's dates are Pacific time; same-day events are ordered by event id (the second gets `-1`). Credit for the match data
and labels goes to the Videre Project.

## How it updates

`.github/workflows/refresh.yml` runs every day at 06:00 UTC (and on demand from the Actions tab):

1. restores previously downloaded sheets from the Actions cache,
2. `python build_data.py` downloads only new sheets and rewrites `site/data.json`,
3. `python build_videre.py vintage` and `... modern` rebuild `site/data-vintage.json` / `site/data-modern.json` from Videre,
4. commits the refreshed data files,
5. `python build_pages.py` wraps the page into `_site/` and deploys it to GitHub Pages.

## Files

| File | Purpose |
| --- | --- |
| `site/index.html` | The page (written as a claude.ai Artifact; `build_pages.py` adds the HTML wrapper for Pages) |
| `site/data.json` | Packed players and matches for every challenge (format described in `build_data.py`) |
| `build_data.py` | Lists the Drive folders, downloads the two tabs of each sheet, packs `site/data.json` |
| `build_pages.py` | Builds the standalone `_site/` for GitHub Pages |
| `server.py` | Drive and sheet access shared by the build; also runs a local live version (`python server.py`, then http://localhost:8765) |
| `index.html` | The page for the local live version |
| `site/data-vintage.json`, `site/data-modern.json` | Same packed format for Vintage / Modern (top-32 decklists only), plus final placings |
| `build_videre.py` | Downloads a format's challenges from the Videre API and packs `site/data-<format>.json` |
| `classify.json` | Manual labels for Videre decks that only got a colour label |
| `aliases.json` | Merge deck names that changed over time (e.g. `"Omni-Tell": "Omni-Show"`) |

## Run locally

```bash
python build_data.py      # first run downloads ~650 sheets (a few minutes), later runs only new ones
python build_videre.py vintage   # Vintage since 2024 from Videre (first run ~15 minutes)
python build_videre.py modern    # Modern since 2024 (first run ~40 minutes)
python build_pages.py
python -m http.server 8770 --directory _site
```

Standard library only; Python 3.10+.
