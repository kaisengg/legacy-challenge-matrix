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

## How it updates

`.github/workflows/refresh.yml` runs every day at 06:00 UTC (and on demand from the Actions tab):

1. restores previously downloaded sheets from the Actions cache,
2. `python build_data.py` downloads only new sheets and rewrites `site/data.json`,
3. commits the refreshed `site/data.json`,
4. `python build_pages.py` wraps the page into `_site/` and deploys it to GitHub Pages.

## Files

| File | Purpose |
| --- | --- |
| `site/index.html` | The page (written as a claude.ai Artifact; `build_pages.py` adds the HTML wrapper for Pages) |
| `site/data.json` | Packed players and matches for every challenge (format described in `build_data.py`) |
| `build_data.py` | Lists the Drive folders, downloads the two tabs of each sheet, packs `site/data.json` |
| `build_pages.py` | Builds the standalone `_site/` for GitHub Pages |
| `server.py` | Drive and sheet access shared by the build; also runs a local live version (`python server.py`, then http://localhost:8765) |
| `index.html` | The page for the local live version |
| `aliases.json` | Merge deck names that changed over time (e.g. `"Omni-Tell": "Omni-Show"`) |

## Run locally

```bash
python build_data.py      # first run downloads ~650 sheets (a few minutes), later runs only new ones
python build_pages.py
python -m http.server 8770 --directory _site
```

Standard library only; Python 3.10+.
