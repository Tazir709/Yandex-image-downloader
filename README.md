# Yandex-image-downloader

A semi-automated Playwright downloader for Yandex Images that:

- Lets you solve Yandex SmartCaptcha manually whenever it appears.
- Captures image URLs from loaded search results as they appear, preserving the original image URLs where available.
- Optionally crawls each result's **source page** ("depth 1") to harvest
  additional images that Yandex itself does not index.

> Built because Yandex's image search is heavily client-rendered and CAPTCHA-gated,
> which breaks pure-`requests` scrapers and headless-only crawlers.

## Why this project exists

Yandex Images does **not** serve image URLs in static HTML. Its result grid is
rendered client-side, and direct API calls are blocked without a `tmpl_version`
token that rotates frequently. Headless scraping can trigger CAPTCHA challenges, 
making fully automated approaches unreliable.

This tool takes a pragmatic hybrid approach:

1. Opens a **real (non-headless) browser** so you can solve the CAPTCHA yourself.
2. Injects a MutationObserver that captures every `img_url` result link as
   Yandex renders it — **before** you press Enter.
3. Persists captured URLs to `localStorage` per SERP page, so a CAPTCHA
   mid-session does not lose earlier batches.
4. Optionally visits each result's **source page** and harvests images from there.
5. Downloads everything with `requests`, validating each file with PIL and
   using unique filenames per URL.

## Preview
<img width="1278" height="798" alt="example" src="https://github.com/user-attachments/assets/6f9d980a-efaf-4a74-9bc7-98e037d84df8" />


## Live browser capture

The downloader includes a floating control panel directly inside the Yandex browser window.

- **Live counters:** See how many unique, qualifying and filtered images have been collected.
- **Automatic capture:** Image URLs are collected as results appear, without needing to return to the terminal.
- **CAPTCHA recovery:** Previously captured images are preserved if Yandex interrupts the search with a CAPTCHA.
- **Finish button:** Start downloading directly from the browser when you've collected enough results.

You can continue loading results for as long as needed before finishing the collection.

## Requirements

- Python 3.9+
- `requests`
- `Pillow`
- `playwright`

Install dependencies:

```bash
py -m pip install requests Pillow playwright
py -m playwright install chromium
```
## Usage

```bash
python yandex.py --search "cats wearing hats"
```
A Chromium window opens with Yandex Images results. Click **Show more** to load
results, solve any CAPTCHA that appears, and keep clicking **Show more** until
you've loaded enough. A floating panel in the bottom-right shows live capture
counts (unique, qualifying, filtered, batches). When you're ready:

- Click **Finish and download** in the panel, or
- Return to the terminal and press **Enter**.

Both exit paths are equivalent. Earlier captures are preserved across CAPTCHAs,
so you don't need to restart if one interrupts the session.

### Example run

```console
PS C:\Users\1\Desktop> python yandex.py --search "sports car" --count 8 --depth 1
Navigating to: https://yandex.com/images/search?text=sports+car&p=0

============================================================
MANUAL BROWSER STEPS
1. Click 'Show more' to load additional image results.
2. Image URLs are captured automatically as results appear.
3. Repeat as needed: click 'Show more' to load more results
   and solve any CAPTCHA that appears to continue browsing.
4. Click 'Finish and download' in the floating panel, or
   return to this console and press Enter.
============================================================
Waiting for you to finish loading images...
Captured 30 unique image URLs.
Entities in data-state: 30

Search image downloads: 8/8 (8 saved, 0 skipped, 0 failed)
Skipping YouTube source page: https://www.youtube.com/watch?v=AoLVi5xJuN4
Skipping YouTube source page: https://www.youtube.com/watch?v=uRc_wSScbvU
Skipping YouTube source page: https://www.youtube.com/watch?v=AmQsGVWqiQc
Depth 1: Visiting https://www.pinterest.com/jaxon20280/luxury-cars/
Found 7 candidate images on https://www.pinterest.com/jaxon20280/luxury-cars/
Depth 1: Visiting https://ca.pinterest.com/ideas/white-modified-sports-car-at-car-show/937043171544/
Found 12 candidate images on https://ca.pinterest.com/ideas/white-modified-sports-car-at-car-show/937043171544/
...

Discovered 62 additional image URLs.
Depth-1 downloads: 8/8 (8 saved, 0 skipped, 0 failed)
Finished: 16 images saved, 0 skipped, 0 failed.
```

In this example, `--count 8` downloads up to 8 images from Yandex results and up to 8 additional images from source pages when `--depth 1` is enabled. The actual number of images saved depends on availability, duplicates, and download failures.

## Options

| Flag | Description | Default |
|---|---|---|
| `--search` | Search query (required) | — |
| `--page` | Starting Yandex results page | `0` |
| `--count` | Maximum *qualifying* images to save per phase | `100` |
| `--min-size` | Only keep images at least `WIDTHxHEIGHT` px, e.g. `800x600` | (no filter) |
| `--output` | Output folder name under `yandex_images/` | Search query |
| `--depth` | `0` = Yandex results only; `1` = also crawl source pages | `1` |

> **`--count` counts qualifying images, not attempts.** With `--min-size` set,
> every candidate URL is downloaded and inspected, and only images meeting the
> size threshold count toward `--count`. A run with `--min-size 800x800
> --count 100` may fetch several hundred URLs to find 100 qualifying images,
> which takes noticeably longer than an unfiltered run.

## Examples

```bash
# Download up to 100 Yandex results without crawling source pages
python yandex.py --search "vintage cars" --count 100 --depth 0

# Download up to 500 Yandex results and enable source-page crawling
python yandex.py --search "botanical illustrations" --count 500 --depth 1

# Search a different Yandex results page
python yandex.py --search "cyberpunk city" --page 3 --output cyberpunk

# Only keep images at least 1200×800 (both phases)
python yandex.py --search "wallpapers" --min-size 1200x800 --count 50
```

The number of images actually downloaded depends on the results loaded, available source pages, duplicate URLs, and download failures. Source-page crawling is also limited by the configured maximum number of source pages.

## Output structure

```text
yandex_images/
└── cats_with_hats/
    ├── 0f3a2b1c...jpg
    ├── a1b2c3d4...png
    └── depth_1/
        ├── x9y8z7w6...jpg
        └── ...
```

- **Main folder:** Images collected directly from Yandex results (depth 0).
- **`depth_1/`:** Additional images harvested from the results' source pages.

Filenames are derived from the image URLs. If two URLs produce the same filename, a numeric suffix is added (`image.jpg`, `image_2.jpg`, etc.). Existing files are never overwritten.

## How it works (internals)

### Depth 0 — Yandex results (live capture)

    An init script injects a MutationObserver into every Yandex page before
    any HTML loads. It watches for a[href*="img_url="] links appearing or
    changing, extracts the img_url parameter, and stores results per SERP page
    (?p=N) in localStorage under a per-run key.

    A polling loop in Python reads those captures every 250ms and merges them
    into a persistent set. Every ~1s it also re-reads
    [id^="ImagesApp-"][data-state] for entity metadata (used by depth 1).

    Because URLs are captured at render time, images loaded before a CAPTCHA
    appeared are already saved. A CAPTCHA on page 2 does not lose page 0's URLs.

    On CAPTCHA, the loop detects location.pathname === "/showcaptcha" and
    reports it. On solve, capture resumes automatically.

### Depth 1 — source-page crawl

    For each Yandex result entity, viewerData.snippet.url points to the host page.

    YouTube and YouTube-nocookie hosts are skipped (no useful images).

    Source URLs are deduplicated (exact match) before visiting.

    Each source page is loaded headless, then <img>, srcset, og:image, and
    twitter:image are collected.

    URL canonicalization collapses CDN variants (/236x/, /474x/,
    /originals/ → /IMAGE/) so the highest-resolution version is kept.

    Common UI assets (such as avatars, logos, icons, and profile images) 
    are filtered out where possible, but some may remain.

## Known limitations

    Manual CAPTCHA
    Yandex may occasionally require manual CAPTCHA solving.
    The browser remains open so you can complete the challenge
    and continue collecting results.

    Depth-1 relevance
    Images collected from source pages are not guaranteed to
    match the original search query. Source pages may contain
    recommendations, thumbnails, and unrelated page content.

    Lazy loading
    Some source pages load images only when scrolled into view.
    These images may not be collected unless the page loads them
    automatically.

    YouTube sources are skipped: the source-page crawler filters YouTube
    hosts out.

    Duplicate source URLs: many Yandex results point at the same host page
    (Pinterest boards, article galleries, product pages). Depth-1 deduplicates
    these — expect fewer harvested images than count × avg_per_page.

    Download 403s: some hosts hotlink-protect. If you hit these, swap
    requests.get for context.request.get inside the same Playwright context
    to inherit session cookies and TLS fingerprint.

    How many results you get depends on you, not on --count. --count caps downloads; 
    it does not tell Yandex to load more. To get more depth-0 candidates, click Show 
    more in the browser before pressing Enter. Yandex's initial render is ~30 results; 
    each Show More click adds another ~30.

    Rate limits: 0.5s delay between downloads. Increase if you see failures.

## Legal & ethical note

Scraping Yandex Images and third-party sites may violate their Terms of Service.
Use only for personal research, respect the source websites' terms and applicable 
copyright rules and do not redistribute scraped content. I take no responsibility for misuse.
