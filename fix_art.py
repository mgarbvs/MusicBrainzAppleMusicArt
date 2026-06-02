#!/usr/bin/env python3
"""Fetch album art from iTunes Search API and apply via AppleScript.

Usage:
  fix_art.py [--playlist "name"] [--dry-run] [--test] [--verbose]

Defaults to playlist "broken album covers " (note trailing space).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

ART_DIR = "/tmp/album_art"

NOISE_PATTERNS = [
    r"\[[^\]]*\]",
    r"\([^)]*\)",
    r"\bcd\s*\d+\b",
    r"\bdisc\s*\d+\b",
    r"\bdemos?\b",
    r"\bremaster(ed)?\b",
    r"\bdeluxe( edition)?\b",
    r"\bbonus( tracks?)?\b",
    r"[-_]+$",
    r"^[-_\s]+",
]

def clean(s, aggressive=False):
    out = s
    patterns = NOISE_PATTERNS if aggressive else NOISE_PATTERNS[:5]
    for p in patterns:
        out = re.sub(p, " ", out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()

def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())

def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()

def get_album_groups(playlist):
    script = f'''
tell application "Music"
  set out to ""
  repeat with t in (every track of playlist "{playlist}")
    set out to out & (database ID of t) & tab & (album artist of t) & tab & (artist of t) & tab & (album of t) & linefeed
  end repeat
  return out
end tell
'''
    raw = osa(script)
    groups = {}
    for line in raw.split("\n"):
        if not line.strip(): continue
        parts = line.split("\t")
        if len(parts) < 4: continue
        tid, aartist, artist, album = parts[0], parts[1], parts[2], parts[3]
        key = (aartist or artist, album)
        groups.setdefault(key, []).append(tid)
    return groups

def itunes_search(term, limit=10):
    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}&entity=album&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r).get("results", [])
    except Exception:
        return []

def pick_match(results, artist, album):
    """Return (result, score). Score >= 50 means confident match."""
    if not results:
        return None, 0
    a_norm = norm(album)
    artist_norm = norm(artist)
    best = None
    best_score = 0
    for r in results:
        r_album = norm(r.get("collectionName",""))
        r_artist = norm(r.get("artistName",""))
        score = 0
        if a_norm and (a_norm in r_album or r_album in a_norm):
            overlap = min(len(a_norm), len(r_album)) / max(len(a_norm), len(r_album))
            score += overlap * 70
        elif a_norm:
            score -= 50
        if artist_norm:
            # Artist matches either the matched artist OR appears in the matched album name
            # (handles classical "Composer: Work" cases)
            if (artist_norm in r_artist or r_artist in artist_norm) and len(artist_norm) >= 3:
                score += 30
            elif artist_norm in r_album:
                score += 20
            else:
                score -= 30
        if score > best_score:
            best_score = score
            best = r
    return best, best_score

def fetch_art(album_artist, album, verbose=False):
    queries = []
    clean_artist = clean(album_artist)
    clean_album = clean(album)
    if clean_artist and clean_album:
        queries.append(f"{clean_artist} {clean_album}")
    if clean_album:
        queries.append(clean_album)
    agg_album = clean(album, aggressive=True)
    if agg_album and agg_album != clean_album:
        if clean_artist:
            queries.append(f"{clean_artist} {agg_album}")
        queries.append(agg_album)

    for q in queries:
        if verbose: print(f"    query: {q!r}")
        results = itunes_search(q)
        best, score = pick_match(results, album_artist, album)
        if best and score >= 50:
            art_url = best.get("artworkUrl100","").replace("100x100bb.jpg","1200x1200bb.jpg")
            if not art_url: continue
            safe = "".join(c if c.isalnum() else "_" for c in f"{album_artist}_{album}")[:80]
            path = os.path.join(ART_DIR, f"{safe}.jpg")
            try:
                urllib.request.urlretrieve(art_url, path)
            except Exception as e:
                return None, f"download failed: {e}"
            return path, f"matched [{score:.0f}]: {best.get('artistName')} - {best.get('collectionName')}"
    return None, "no confident match"

def apply_art(track_ids, art_path):
    ids_list = ", ".join(track_ids)
    posix = art_path.replace('"', '\\"')
    script = f'''
tell application "Music"
  set artData to (read (POSIX file "{posix}") as picture)
  repeat with tid in {{{ids_list}}}
    try
      set t to (some track of library playlist 1 whose database ID is tid)
      tell t
        try
          delete artworks
        end try
        set data of artwork 1 to artData
      end tell
    end try
  end repeat
end tell
'''
    osa(script)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default="broken album covers ")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    os.makedirs(ART_DIR, exist_ok=True)
    groups = get_album_groups(args.playlist)
    print(f"Found {len(groups)} unique albums, {sum(len(v) for v in groups.values())} tracks in {args.playlist!r}")
    if args.dry_run: print("(dry run — no changes will be made)")

    ok = fail = 0
    failures = []
    for i, ((aartist, album), tids) in enumerate(groups.items(), 1):
        label = f"[{i}/{len(groups)}] {aartist or '?'} - {album}"
        path, msg = fetch_art(aartist, album, verbose=args.verbose)
        if not path:
            print(f"  FAIL {label}: {msg}")
            failures.append((aartist, album))
            fail += 1
            continue
        if args.dry_run:
            print(f"  DRY  {label}: {msg}")
            ok += 1
        else:
            try:
                apply_art(tids, path)
                print(f"  OK   {label}: {msg} ({len(tids)} tracks)")
                ok += 1
            except Exception as e:
                print(f"  FAIL {label}: apply error: {e}")
                failures.append((aartist, album))
                fail += 1
        if args.test and ok:
            break

    print(f"\nDone. OK: {ok}, Fail: {fail}")
    if failures:
        print("\nUnmatched (likely mixtapes/demos/bootlegs not in Apple's catalog):")
        for a, al in failures:
            print(f"  - {a or '?'} - {al}")

if __name__ == "__main__":
    main()
