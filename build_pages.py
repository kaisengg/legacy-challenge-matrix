"""
Turn site/ into a standalone static site for GitHub Pages.

site/index.html is written for claude.ai Artifacts, which add the <!doctype>,
<head> and <body> wrapper at publish time. GitHub Pages serves files as-is,
so this wraps the page in the same skeleton and copies the data file.

    python build_pages.py        ->  _site/index.html, _site/data.json
"""

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "site"
OUT = ROOT / "_site"

SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
  :root { color-scheme: light; padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }
  body { margin: 0; font-size: 14px; font-family: system-ui, sans-serif; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
{page}
</body>
</html>
"""


def main():
    OUT.mkdir(exist_ok=True)
    page = (SRC / "index.html").read_text(encoding="utf-8")
    (OUT / "index.html").write_text(SKELETON.replace("{page}", page), encoding="utf-8")
    shutil.copy2(SRC / "data.json", OUT / "data.json")
    for extra in SRC.glob("data-*.json"):          # other formats (Videre-sourced)
        shutil.copy2(extra, OUT / extra.name)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
