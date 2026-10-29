# Yandex-image-downloader

A semi-automated Playwright downloader for Yandex Images that:

- Lets you solve Yandex's SmartCaptcha manually (once per session, or whenever it appears),
- Captures every loaded result image at original resolution **as it appears**,
  so a mid-session CAPTCHA no longer discards earlier batches, and
- Optionally crawls each result's **source page** ("depth 1") to harvest
  additional images that Yandex itself does not index.

> Built because Yandex's image search is heavily client-rendered and CAPTCHA-gated,
> which breaks pure-`requests` scrapers and headless-only crawlers.

## Why this project exists

Yandex Images does **not** serve image URLs in static HTML. Its result grid is
rendered client-side, and direct API calls are blocked without a `tmpl_version`
token that rotates frequently. Fully headless scrapers get CAPTCHA-served
immediately.

This tool takes a pragmatic hybrid approach:

1. Opens a **real (non-headless) browser** so you can solve the CAPTCHA yourself.
2. Injects a MutationObserver that captures every `img_url` result link as
   Yandex renders it — **before** you press Enter.
3. Persists captured URLs to `localStorage` per SERP page, so a CAPTCHA
   mid-session does not lose earlier batches.
4. Optionally visits each result's **source page** and harvests images from there.
5. Downloads everything with `requests`, validating each file with PIL and
   using unique filenames per URL.

## Requirements

- Python 3.9+
- `requests`
- `Pillow`
- `playwright`

Install dependencies:

```bash
pip install requests Pillow playwright
playwright install chromium
```
## Usage

```bash
python yandex.py --search "cats wearing hats"
```

A Chromium window opens with Yandex Images results. Click **Show more** to load additional results. Image URLs are captured automatically as results appear. If a CAPTCHA appears, solve it in the browser to continue. Return to the terminal and press Enter when you're finished.

### Example run

```console
PS C:\Users\1\Desktop> python yandex.py --search "sports car" --count 8 --depth 1
Navigating to: https://yandex.com/images/search?text=sports+car&p=0

============================================================
MANUAL BROWSER STEP
1. Click 'Show more' to load additional image results.
2. Image URLs are captured automatically as results appear.
3. Repeat as needed: click 'Show more' to load more results
   and solve any CAPTCHA that appears to continue browsing.
4. Once you've collected enough images, return to this
   console and press Enter to start downloading.
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
| `--count` | Maximum images to download in each phase | `100` |
| `--output` | Output folder name under `yandex_images/` | Search query |
| `--depth` | `0` = Yandex results only; `1` = also crawl source pages | `1` |

## Examples

```bash
# Download up to 100 Yandex results without crawling source pages
python yandex.py --search "vintage cars" --count 100 --depth 0

# Download up to 500 Yandex results and enable source-page crawling
python yandex.py --search "botanical illustrations" --count 500 --depth 1

# Search a different Yandex results page
python yandex.py --search "cyberpunk city" --page 3 --output cyberpunk
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

Known limitations

    Manual CAPTCHA step: unavoidable for reliable Yandex access. Residential
    proxies + stealth plugins can bypass it, but reliability varies.

    Depth 1 relevance: Images collected from source pages are not guaranteed to 
    match the original search query. Source pages may contain unrelated or only 
    loosely related images, such as recommendations, thumbnails, and other page content.

    YouTube sources are skipped: the source-page crawler filters YouTube
    hosts out.

    Lazy-loading: source pages that lazy-load below the fold may yield fewer
    images than a live browser shows. A scroll-loop can be added inside
    get_images_from_source_page to improve recall.

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
Use only for personal research, respect robots.txt and copyright, and do not
redistribute scraped content. I take no responsibility for misuse.
