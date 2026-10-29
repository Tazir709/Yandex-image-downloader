import argparse
from io import BytesIO
import json
import os
import re
import threading
import time
import uuid
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import requests
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import Error as PlaywrightError, sync_playwright


class YandexImagesDownloader:
    def __init__(self, output_dir="yandex_images"):
        self.output_dir = output_dir
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    @staticmethod
    def _safe_directory_name(name):
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        return safe_name or "images"

    @staticmethod
    def _unique_image_path(directory, image_url, extension=None):
        filename = PurePosixPath(unquote(urlparse(image_url).path)).name
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
        if not filename:
            filename = "image"

        stem, url_extension = os.path.splitext(filename)
        stem = stem or "image"
        extension = extension or url_extension
        filename = f"{stem}{extension}"
        candidate = os.path.join(directory, filename)
        suffix = 2

        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{stem}_{suffix}{extension}")
            suffix += 1

        return candidate

    @classmethod
    def _save_valid_image(cls, directory, image_url, response):
        if response.status_code < 200 or response.status_code >= 300:
            return None, f"HTTP {response.status_code}"

        if not response.content:
            return None, "Empty response"

        try:
            with Image.open(BytesIO(response.content)) as image:
                image_format = image.format
                image.load()
        except (
            Image.DecompressionBombError,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as error:
            return None, f"Invalid or unreadable image: {error}"

        format_extensions = {
            "AVIF": ".avif",
            "BMP": ".bmp",
            "GIF": ".gif",
            "ICO": ".ico",
            "JPEG": ".jpg",
            "PNG": ".png",
            "TIFF": ".tiff",
            "WEBP": ".webp",
        }
        extension = format_extensions.get(image_format)
        if not extension:
            return None, f"Unsupported image format: {image_format}"

        filepath = cls._unique_image_path(directory, image_url, extension)
        with open(filepath, "wb") as image_file:
            image_file.write(response.content)

        return filepath, None

    
    def get_images_from_source_page(self, page, source_url):
        """Collect image URLs using an existing Playwright page."""
        try:
            response = page.goto(
                source_url,
                wait_until="domcontentloaded",
                timeout=6000
            )

            if not response or response.status >= 400:
                print(f"Could not load source page: {source_url}")
                return []

            page.wait_for_timeout(6000)

            image_urls = page.evaluate(r"""
                
                () => {
                    const images = new Map();

                    function canonicalUrl(rawUrl) {
                        try {
                            const url = new URL(rawUrl);

                            // Remove query parameters used for resizing/tracking.
                            url.search = "";
                            url.hash = "";

                            // Pinterest image variants often look like:
                            // /236x/...jpg, /474x/...jpg, /736x/...jpg
                            // /originals/...jpg
                            url.pathname = url.pathname.replace(
                                /\/(?:\d+x|originals)\//,
                                "/IMAGE/"
                            );

                            return url.href;
                        } catch {
                            return rawUrl;
                        }
                    }

                    function addImage(rawUrl, width = 0, height = 0, declaredResolution = 0) {
                        if (!rawUrl || !rawUrl.startsWith("http")) return;

                        // Ignore obvious UI assets.
                        if (/avatar|profile|logo|icon/i.test(rawUrl)) return;

                        const key = canonicalUrl(rawUrl);
                        const pinterestSize = rawUrl.match(/\/(\d+)x\//i);
                        const resolution = /\/originals\//i.test(rawUrl)
                            ? Number.MAX_SAFE_INTEGER
                            : Math.max(
                                width,
                                height,
                                declaredResolution,
                                pinterestSize ? Number(pinterestSize[1]) : 0
                            );
                        const existing = images.get(key);

                        if (!existing || resolution > existing.resolution) {
                            images.set(key, {
                                url: rawUrl,
                                resolution
                            });
                        }
                    }

                    document.querySelectorAll("img").forEach(img => {
                        const naturalWidth = img.naturalWidth || img.width;
                        const naturalHeight = img.naturalHeight || img.height;
                        addImage(
                            img.currentSrc || img.dataset.src || img.src,
                            naturalWidth,
                            naturalHeight
                        );

                        // Collect srcset candidates and retain the largest declared size.
                        const srcset = img.getAttribute("srcset");

                        if (srcset) {
                            srcset.split(",").forEach(entry => {
                                const parts = entry.trim().split(/\s+/);
                                const url = parts[0];
                                const descriptor = parts[1] || "";
                                const match = descriptor.match(/^(\d+(?:\.\d+)?)(w|x)$/);
                                const declaredResolution = match
                                    ? Number(match[1]) * (
                                        match[2] === "x" ? naturalWidth || 1 : 1
                                    )
                                    : 0;

                                addImage(url, 0, 0, declaredResolution);
                            });
                        }
                    });

                    document.querySelectorAll(
                        'meta[property="og:image"], meta[name="twitter:image"]'
                    ).forEach(meta => {
                        addImage(meta.content);
                    });

                    return [...images.values()].map(img => img.url);
                }
            """)

            print(f"Found {len(image_urls)} candidate images on {source_url}")
            return image_urls

        except Exception as e:
            print(f"Source-page error ({source_url}): {e}")
            return []


    
    def crawl_source_pages(self, page, entities, max_source_pages=10):
        """Collect additional images from Yandex results' source pages."""
        source_pages = []
        seen_pages = set()

        for entity in entities:
            viewer = entity.get("viewerData", {})
            snippet = viewer.get("snippet", {})
            source_url = snippet.get("url")

            if not source_url or not source_url.startswith("https://"):
                continue

            hostname = urlparse(source_url).hostname or ""
            if (
                hostname == "youtu.be"
                or hostname == "youtube.com"
                or hostname.endswith(".youtube.com")
                or hostname == "youtube-nocookie.com"
                or hostname.endswith(".youtube-nocookie.com")
            ):
                print(f"Skipping YouTube source page: {source_url}")
                continue

            # Avoid visiting the same source page repeatedly.
            if source_url not in seen_pages:
                seen_pages.add(source_url)
                source_pages.append(source_url)

        discovered = []
        seen_images = set()

        # Avoid redownloading images already represented by Yandex results.
        for entity in entities:
            original = entity.get("origUrl")
            if original:
                seen_images.add(original)

        for source_url in source_pages[:max_source_pages]:
            print(f"Depth 1: Visiting {source_url}")

            try:
                images = self.get_images_from_source_page(page, source_url)

                for image_url in images:
                    if image_url not in seen_images:
                        seen_images.add(image_url)
                        discovered.append({
                            "url": image_url,
                            "source_page": source_url,
                            "depth": 1,
                        })

            except Exception as e:
                print(f"Failed to crawl {source_url}: {e}")

        print(f"Discovered {len(discovered)} additional image URLs.")
        return discovered


    def download_images_by_page(
        self,
        keyword,
        page,
        imgs_count,
        sub_directory=None,
        depth=1
    ):
        """
        Open Yandex Images for manual result loading, then download its images.
        """
        img_url_results = []
        search_directory = self._safe_directory_name(sub_directory or keyword)
        download_dir = os.path.join(self.output_dir, search_directory)
        os.makedirs(download_dir, exist_ok=True)

        def report_progress(label, completed, total, succeeded, skipped):
            failed = completed - succeeded - skipped
            print(
                f"\r{label}: {completed}/{total} "
                f"({succeeded} saved, {skipped} skipped, {failed} failed)",
                end="\n" if completed == total else "",
                flush=True
            )
        
        # Construct URL with pagination
        from urllib.parse import urlencode

        base_url = "https://yandex.com/images/search"
        params = {
            "text": keyword,
            "p": page,  # Page number, starting at 0
        }

        url = f"{base_url}?{urlencode(params)}"
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page_obj = context.new_page()
            capture_storage_key = f"yandex_image_urls_{uuid.uuid4().hex}"
            capture_script = r"""
                (() => {
                    if (window.__yandexCapturedImageUrls) return;

                    const storageKey = __CAPTURE_STORAGE_KEY__;
                    let storedUrlsByPage = {};
                    try {
                        storedUrlsByPage = JSON.parse(
                            localStorage.getItem(storageKey) || "[]"
                        );
                    } catch {
                        storedUrlsByPage = {};
                    }

                    if (
                        !storedUrlsByPage ||
                        typeof storedUrlsByPage !== "object" ||
                        Array.isArray(storedUrlsByPage)
                    ) {
                        storedUrlsByPage = {};
                    }

                    const urlsByPage = new Map(
                        Object.entries(storedUrlsByPage).map(
                            ([page, urls]) => [page, new Set(urls)]
                        )
                    );
                    const urls = new Set(
                        [...urlsByPage.values()].flatMap(pageUrls => [...pageUrls])
                    );
                    window.__yandexCapturedImageUrls = urls;
                    window.__yandexCapturedImageUrlsByPage = urlsByPage;

                    function persistUrls() {
                        try {
                            localStorage.setItem(
                                storageKey,
                                JSON.stringify(
                                    Object.fromEntries(
                                        [...urlsByPage].map(
                                            ([page, pageUrls]) => [
                                                page,
                                                [...pageUrls]
                                            ]
                                        )
                                    )
                                )
                            );
                        } catch {
                            // Keep the in-memory collection if storage is unavailable.
                        }
                    }

                    function captureLinks(root = document) {
                        if (/\/showcaptcha\/?$/i.test(location.pathname)) {
                            return;
                        }

                        const links = [];
                        let added = false;
                        const currentPageNumber = new URL(location.href)
                            .searchParams.get("p") || "unknown";
                        if (
                            root instanceof Element &&
                            root.matches('a[href*="img_url="]')
                        ) {
                            links.push(root);
                        }
                        if (root.querySelectorAll) {
                            links.push(
                                ...root.querySelectorAll('a[href*="img_url="]')
                            );
                        }

                        for (const link of links) {
                            try {
                                const resultUrl = new URL(link.href);
                                const imageUrl = resultUrl.searchParams.get("img_url");
                                const pageNumber = currentPageNumber;

                                if (
                                    imageUrl &&
                                    /^https?:\/\//i.test(imageUrl)
                                ) {
                                    if (!urlsByPage.has(pageNumber)) {
                                        urlsByPage.set(pageNumber, new Set());
                                    }
                                    const pageUrls = urlsByPage.get(pageNumber);

                                    if (!urls.has(imageUrl)) {
                                        added = true;
                                    }
                                    urls.add(imageUrl);
                                    if (!pageUrls.has(imageUrl)) {
                                        added = true;
                                        pageUrls.add(imageUrl);
                                    }
                                }
                            } catch {
                                // Ignore malformed result links.
                            }
                        }

                        if (added) {
                            persistUrls();
                        }
                    }

                    const observer = new MutationObserver(mutations => {
                        for (const mutation of mutations) {
                            if (mutation.type === "attributes") {
                                captureLinks(mutation.target);
                            } else {
                                for (const node of mutation.addedNodes) {
                                    if (node instanceof Element) {
                                        captureLinks(node);
                                    }
                                }
                            }
                        }
                    });

                    function startCapture() {
                        captureLinks();
                        observer.observe(document.documentElement, {
                            attributes: true,
                            attributeFilter: ["href"],
                            childList: true,
                            subtree: true
                        });
                        window.setInterval(captureLinks, 300);
                    }

                    if (document.documentElement) {
                        startCapture();
                    } else {
                        document.addEventListener(
                            "DOMContentLoaded",
                            startCapture,
                            { once: true }
                        );
                    }
                })();
            """.replace(
                "__CAPTURE_STORAGE_KEY__",
                json.dumps(capture_storage_key)
            )
            page_obj.add_init_script(capture_script)
            source_browser = None
            
            try:
                print(f"Navigating to: {url}")
                page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                import html

                def read_state():
                    try:
                        state_locator = page_obj.locator(
                            '[id^="ImagesApp-"][data-state]'
                        ).first

                        raw = state_locator.get_attribute("data-state", timeout=500)

                        if not raw:
                            return None

                        return json.loads(html.unescape(raw))
                    except (PlaywrightError, json.JSONDecodeError):
                        return None

                def get_entities(state):
                    if not isinstance(state, dict):
                        return []

                    initial = state.get("initialState", state)
                    if not isinstance(initial, dict):
                        return []

                    serp_list = initial.get("serpList", {})
                    if not isinstance(serp_list, dict):
                        return []

                    items = serp_list.get("items", {})
                    if not isinstance(items, dict):
                        return []

                    entities = items.get("entities", {})
                    keys = items.get("keys", [])

                    if not isinstance(entities, dict) or not isinstance(keys, list):
                        return []

                    return [
                        entities[key]
                        for key in keys
                        if key in entities and isinstance(entities[key], dict)
                    ]

                
                print("\n" + "=" * 60)
                print("""MANUAL BROWSER STEPS
                1. Click 'Show more' to load additional image results.
                2. Image URLs are captured automatically as results appear.
                3. Repeat as needed: click 'Show more' to load more results
                and solve any CAPTCHA that appears to continue browsing.
                4. Once you've collected enough images, return to this
                console and press Enter to start downloading.
                ============================================================
                Waiting for you to finish loading images...
                """)
                print("=" * 60)

                collected_urls = set()
                page_image_counts = {}
                reported_pages = set()
                entity_list = get_entities(read_state())
                input_finished = threading.Event()

                def process_captured_urls(captures):
                    by_page = captures.get("byPage", {})
                    for page_number, page_urls in by_page.items():
                        if page_number not in page_image_counts:
                            page_image_counts[page_number] = 0

                        page_count = page_image_counts.setdefault(page_number, 0)
                        for image_url in page_urls:
                            if image_url not in collected_urls:
                                collected_urls.add(image_url)
                                page_count += 1
                        page_image_counts[page_number] = page_count

                def report_page(page_number):
                    if page_number in reported_pages:
                        return
                    reported_pages.add(page_number)
                    try:
                        last_page = int(page_number)
                    except (TypeError, ValueError):
                        last_page = None

                    total_urls = sum(
                        count
                        for page, count in page_image_counts.items()
                        if last_page is None
                        or (
                            page.lstrip("-").isdigit()
                            and int(page) <= last_page
                        )
                    )
                    print(
                        f"Loaded {page_image_counts.get(page_number, 0)} new "
                        "images from page "
                        f"({total_urls} unique URLs total)."
                    )

                def wait_for_enter():
                    try:
                        input("Waiting for you to finish loading images... ")
                    except EOFError:
                        print("Terminal input closed; continuing with captured URLs.")
                    finally:
                        input_finished.set()

                threading.Thread(target=wait_for_enter, daemon=True).start()
                read_urls_script = r"""
                    () => {
                        const byPage = {};
                        const currentUrl = new URL(location.href);
                        const isCaptcha = /\/showcaptcha\/?$/i.test(
                            currentUrl.pathname
                        );
                        const currentPage = isCaptcha
                            ? null
                            : currentUrl.searchParams.get("p") || "unknown";

                        for (
                            const [page, urls] of
                            (window.__yandexCapturedImageUrlsByPage || new Map())
                        ) {
                            byPage[page] = [...urls];
                        }

                        try {
                            const storedByPage = JSON.parse(
                                localStorage.getItem(__CAPTURE_STORAGE_KEY__)
                                || "{}"
                            );
                            for (const [page, urls] of Object.entries(storedByPage)) {
                                byPage[page] = [
                                    ...new Set([...(byPage[page] || []), ...urls])
                                ];
                            }
                        } catch {}

                        if (currentPage !== null) {
                            if (!byPage[currentPage]) byPage[currentPage] = [];
                            const currentUrls = new Set(byPage[currentPage]);
                            for (const link of document.querySelectorAll(
                                'a[href*="img_url="]'
                            )) {
                                try {
                                    const resultUrl = new URL(link.href);
                                    const url = resultUrl.searchParams.get("img_url");
                                    if (url && /^https?:\/\//i.test(url)) {
                                        currentUrls.add(url);
                                    }
                                } catch {}
                            }

                            byPage[currentPage] = [...currentUrls];
                        }

                        return { byPage, currentPage, isCaptcha };
                    }
                """.replace(
                    "__CAPTURE_STORAGE_KEY__",
                    json.dumps(capture_storage_key)
                )

                last_new_entity_read = 0.0
                last_page_number = None
                captcha_active = False
                while not input_finished.is_set() and not page_obj.is_closed():
                    try:
                        captures = page_obj.evaluate(read_urls_script)
                        current_page_number = captures.get("currentPage")
                        captcha_detected = captures.get("isCaptcha", False)

                        process_captured_urls(captures)
                        if (
                            last_page_number is not None
                            and current_page_number != last_page_number
                        ):
                            report_page(last_page_number)
                        last_page_number = current_page_number

                        if captcha_detected and not captcha_active:
                            print(
                                "CAPTCHA detected. Solve it to scan the next "
                                "page, or press Enter to finish."
                            )
                        elif not captcha_detected and captcha_active:
                            print("CAPTCHA cleared. Continue loading results.")
                        captcha_active = captcha_detected

                        now = time.monotonic()
                        if now - last_new_entity_read >= 1:
                            state = read_state()
                            current_entities = get_entities(state)
                            if current_entities:
                                entity_list = current_entities
                            last_new_entity_read = now
                    except PlaywrightError:
                        # Navigation can briefly destroy the execution context.
                        current_url = page_obj.url
                        captcha_detected = (
                            urlparse(current_url).path.rstrip("/").lower()
                            == "/showcaptcha"
                        )
                        if captcha_detected and not captcha_active:
                            if last_page_number is not None:
                                report_page(last_page_number)
                                last_page_number = None
                            print(
                                "CAPTCHA detected. Solve it to scan the next "
                                "page, or press Enter to finish."
                            )
                        captcha_active = captcha_detected

                    time.sleep(0.25)

                # Make one final attempt to collect links still present in the page.
                if not page_obj.is_closed():
                    try:
                        captures = page_obj.evaluate(read_urls_script)
                        process_captured_urls(captures)
                        current_page_number = captures.get("currentPage")
                        if (
                            last_page_number is not None
                            and current_page_number != last_page_number
                        ):
                            report_page(last_page_number)
                        last_page_number = current_page_number
                        state = read_state()
                        current_entities = get_entities(state)
                        if current_entities:
                            entity_list = current_entities
                    except PlaywrightError:
                        pass

                if last_page_number is not None:
                    report_page(last_page_number)
                for page_number in sorted(
                    page_image_counts,
                    key=lambda value: (
                        not value.isdigit(),
                        int(value) if value.isdigit() else value
                    )
                ):
                    report_page(page_number)
                urls = list(collected_urls)

                print(f"Entities in data-state: {len(entity_list)}")

                if not urls:
                    print("No Yandex image URLs were captured.")
                    return []

                unique_urls = list(dict.fromkeys(urls))
                print(
                    f"Captured {len(unique_urls)} unique image URLs "
                    "while browsing."
                )
                
                # Download up to requested count
                search_urls = unique_urls[:imgs_count]
                search_succeeded = 0
                search_skipped = 0
                for completed, img_url in enumerate(search_urls, start=1):
                    try:
                        response = requests.get(img_url, timeout=10)
                        filepath, error = self._save_valid_image(
                            download_dir,
                            img_url,
                            response
                        )
                        if filepath:
                            img_url_results.append({
                                "status": "success",
                                "url": img_url,
                                "path": filepath
                            })
                            search_succeeded += 1
                        else:
                            img_url_results.append({
                                "status": "skipped",
                                "url": img_url,
                                "error": error
                            })
                            search_skipped += 1
                    except Exception as e:
                        img_url_results.append({
                            "status": "fail",
                            "url": img_url,
                            "error": str(e)
                        })

                    report_progress(
                        "Search image downloads",
                        completed,
                        len(search_urls),
                        search_succeeded,
                        search_skipped
                    )
                    
                    # Small delay to avoid rate limiting
                    time.sleep(0.5)
                if depth >= 1:
                    # Depth 1: visit source pages for the loaded search results once.
                    selected_entities = [
                        entity
                        for entity in entity_list
                        if entity.get("origUrl")
                    ][:imgs_count]

                    source_browser = p.chromium.launch(headless=True)
                    source_context = source_browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                        )
                    )
                    source_page = source_context.new_page()
                    extra_images = self.crawl_source_pages(
                        source_page,
                        selected_entities,
                        max_source_pages=1000
                    )

                    depth1_dir = os.path.join(download_dir, "depth_1")
                    os.makedirs(depth1_dir, exist_ok=True)

                    depth1_images = extra_images[:imgs_count]
                    depth1_succeeded = 0
                    depth1_skipped = 0
                    for completed, item in enumerate(depth1_images, start=1):
                        img_url = item["url"]

                        try:
                            response = requests.get(
                                img_url,
                                timeout=15,
                                headers={
                                    "User-Agent": (
                                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 Chrome/120.0.0.0 "
                                        "Safari/537.36"
                                    )
                                }
                            )
                            filepath, error = self._save_valid_image(
                                depth1_dir,
                                img_url,
                                response
                            )
                            if filepath:
                                img_url_results.append({
                                    "status": "success",
                                    "url": img_url,
                                    "path": filepath,
                                    "source_page": item["source_page"],
                                    "depth": 1,
                                })
                                depth1_succeeded += 1
                            else:
                                img_url_results.append({
                                    "status": "skipped",
                                    "url": img_url,
                                    "error": error,
                                    "source_page": item["source_page"],
                                    "depth": 1,
                                })
                                depth1_skipped += 1

                        except Exception as e:
                            img_url_results.append({
                                "status": "fail",
                                "url": img_url,
                                "error": str(e),
                                "source_page": item["source_page"],
                                "depth": 1,
                            })

                        report_progress(
                            "Depth-1 downloads",
                            completed,
                            len(depth1_images),
                            depth1_succeeded,
                            depth1_skipped
                        )

                        time.sleep(0.5)


            except Exception as e:
                print(f"Error during scraping: {e}")
            finally:
                if source_browser and source_browser.is_connected():
                    source_browser.close()
                if browser.is_connected():
                    browser.close()
                
        return img_url_results

def main():
    parser = argparse.ArgumentParser(
        description="Download Yandex Images results and optionally crawl source pages."
    )
    parser.add_argument(
        "--search",
        required=True,
        help="Search query to use on Yandex Images."
    )
    parser.add_argument(
        "--page",
        type=int,
        default=0,
        help="Yandex result page number (default: 0)."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="Maximum number of search result images to download (default: 100)."
    )
    parser.add_argument(
        "--output",
        help="Output folder name under yandex_images (defaults to the search query)."
    )
    parser.add_argument(
        "--depth",
        type=int,
        choices=(0, 1),
        default=1,
        help="Source-page crawl depth: 0 disables it, 1 enables it (default: 1)."
    )
    args = parser.parse_args()

    downloader = YandexImagesDownloader(output_dir="yandex_images")
    results = downloader.download_images_by_page(
        keyword=args.search,
        page=args.page,
        imgs_count=args.count,
        sub_directory=args.output,
        depth=args.depth
    )

    succeeded = sum(result["status"] == "success" for result in results)
    failed = sum(result["status"] == "fail" for result in results)
    skipped = sum(result["status"] == "skipped" for result in results)
    print(
        f"Finished: {succeeded} images saved, "
        f"{skipped} skipped, {failed} failed."
    )


if __name__ == "__main__":
    main()
