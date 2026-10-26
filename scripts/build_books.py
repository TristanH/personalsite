# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""Turn a Readwise Books `books_list_books` export into data/books.json and covers/*.webp.

Usage: uv run scripts/build_books.py <export.json> <download-cache-dir>
"""

import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
COVER_DIR = ROOT / "covers"
DATA_FILE = ROOT / "data" / "books.json"
COVER_HEIGHT = 360
# Library book id -> Readwise Books page id (readwise.io/books/<id>/). The MCP doesn't
# return this id, so it was resolved once from each book's ISBN or a title search.
READWISE_IDS = json.loads((Path(__file__).parent / "readwise_book_ids.json").read_text())

# Catalog mismatches in the export.
AUTHOR_FIXES = {"The Lies of Locke Lamora": "Scott Lynch"}
# Fan fiction with no published edition, or no usable scan; these keep a generated cover.
NO_LOOKUP = {"Harry Potter and the Methods of Rationality", "Significant Digits", "Do the Work"}
# Open Library cover ids picked by hand where search returned a translation or a placeholder.
COVER_OVERRIDES = {
    "Foundation": 374166,
    "Mistborn": 14658160,
    "Consider Phlebas": 15160165,
    "Assassin's Apprentice": 15133628,
    "The Sunlit Man": 15266704,
    "Dawnshard": 15244613,
    "The Mad Ship": 368046,
    "The Score Takes Care of Itself": 9522341,
}


def clean_title(raw):
    title = re.sub(r"\s+", " ", raw).strip()
    title = re.sub(r"\s*\((?:[^)]*#\d+|Penguin Classics[^)]*|Aristillus[^)]*)\)", "", title)
    title = re.sub(r"^Farseer \d - ", "", title)
    title = re.sub(r" - (Book Two in the Gentlemen Bastards Sequence|Revised 3rd Edition|Second Edition)$", "", title)
    title = re.sub(r": (A Novel|The Illustrated Edition|Second Edition)$", "", title)
    main, _, sub = title.partition(": ")
    return main[0].upper() + main[1:], sub


def clean_author(raw, title):
    if title in AUTHOR_FIXES:
        return AUTHOR_FIXES[title]
    if raw == "Iain M. Banks, Iain Banks":
        return "Iain M. Banks"
    parts = raw.split(", ")
    # "Heinlein, Robert A." -> "Robert A. Heinlein"; leave real author lists alone.
    if len(parts) == 2 and " " not in parts[0]:
        return f"{parts[1]} {parts[0]}"
    return raw


def dedupe_key(title):
    key = title.lower()
    key = re.sub(r"^the ", "", key)
    return re.sub(r"[^a-z0-9]", "", key)


def score(book):
    """Prefer the copy of a duplicate that carries the most information."""
    product = book.get("product") or {}
    return sum(bool(x) for x in (book.get("rating"), book.get("finished_reading_at"), product.get("cover_url"), product.get("page_count")))


def hex_color(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def palette(img):
    small = img.convert("RGB").resize((64, 96))
    quant = small.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()
    counts = sorted(quant.getcolors(), reverse=True)
    colors = [tuple(pal[i * 3:i * 3 + 3]) for _, i in counts]
    return colors


def download(url, dest):
    if dest.exists():
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "personalsite-build (tristanhomsi.com)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def openlibrary(title, author, cache):
    """Find a cover and page count for books Bookwise has no catalog match for."""
    query = urllib.parse.urlencode({
        "title": title,
        "author": author.split(",")[0],
        "limit": 5,
        "fields": "title,author_name,cover_i,number_of_pages_median",
    })
    dest = cache / f"ol-{dedupe_key(title)}.json"
    try:
        docs = json.loads(download(f"https://openlibrary.org/search.json?{query}", dest).read_text())["docs"]
    except Exception as err:
        print(f"open library lookup failed for {title}: {err}", file=sys.stderr)
        return None, None
    with_cover = [d for d in docs if d.get("cover_i")]
    if not with_cover:
        return None, None
    doc = with_cover[0]
    pages = next((d["number_of_pages_median"] for d in docs if d.get("number_of_pages_median")), None)
    return f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg", pages


def main(export_path, cache_dir):
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(exist_ok=True)
    DATA_FILE.parent.mkdir(exist_ok=True)

    raw = json.loads(Path(export_path).read_text())
    rows = raw["results"] if isinstance(raw, dict) else raw
    finished = [b for b in rows if b["status"] == "finished"]

    best = {}
    for book in finished:
        title, _ = clean_title(book["title"])
        key = dedupe_key(title)
        if key not in best or score(book) > score(best[key]):
            best[key] = book

    out = []
    for book in best.values():
        product = book.get("product") or {}
        title, subtitle = clean_title(book["title"])
        entry = {
            "id": book["id"],
            "t": title,
            "a": clean_author(book["author"] or product.get("author") or "", title),
            "r": book.get("rating"),
            "d": (book.get("finished_reading_at") or "")[:10] or None,
            "p": product.get("page_count"),
            "u": f"https://readwise.io/books/{READWISE_IDS[str(book['id'])]}/" if str(book["id"]) in READWISE_IDS else None,
        }
        if subtitle:
            entry["s"] = subtitle
        review = re.sub(r"\n{3,}", "\n\n", (book.get("review") or "").replace("\r\n", "\n")).strip()
        if review:
            entry["v"] = review

        cover_url = product.get("cover_url")
        if title in COVER_OVERRIDES:
            cover_url = f"https://covers.openlibrary.org/b/id/{COVER_OVERRIDES[title]}-L.jpg"
        elif not cover_url and title not in NO_LOOKUP:
            cover_url, pages = openlibrary(title, entry["a"], cache)
            entry["p"] = entry["p"] or pages
        if cover_url:
            try:
                src = download(cover_url, cache / f"{book['id']}.img")
                with Image.open(src) as img:
                    colors = palette(img)
                    w, h = img.size
                    resized = img.convert("RGB").resize((round(w * COVER_HEIGHT / h), COVER_HEIGHT), Image.Resampling.LANCZOS)
                    resized.save(COVER_DIR / f"{book['id']}.webp", "WEBP", quality=72, method=6)
                entry["c"] = [hex_color(c) for c in colors[:3]]
                entry["ar"] = round(w / h, 3)
            except Exception as err:  # a bad cover shouldn't sink the build
                print(f"cover failed for {title}: {err}", file=sys.stderr)
        out.append(entry)

    out.sort(key=lambda b: (b["d"] or "0000", b["id"]), reverse=True)
    payload = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    DATA_FILE.write_text(payload)
    # Stamp the page's data URL with a content hash so browsers never serve a stale copy.
    version = hashlib.sha1(payload.encode()).hexdigest()[:10]
    index = ROOT / "index.html"
    index.write_text(re.sub(r"data/books\.json(\?v=[0-9a-f]+)?", f"data/books.json?v={version}", index.read_text()))
    print(f"{len(out)} books, {sum('c' in b for b in out)} with covers -> {DATA_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
