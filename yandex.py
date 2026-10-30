import argparse
from io import BytesIO
import json
import os
import re
import threading
import time
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

import requests
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import Error as PlaywrightError, sync_playwright


def _parse_min_size(value):
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError(
            "minimum size must use WIDTHxHEIGHT format, for example 400x400"
        )

    width, height = (int(dimension) for dimension in match.groups())
    if width < 1 or height < 1:
        raise argparse.ArgumentTypeError(
            "minimum width and height must be greater than zero"
        )
    return width, height


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

    @staticmethod
    def _inspect_image(response):
        if response.status_code < 200 or response.status_code >= 300:
            return None, f"HTTP {response.status_code}"

        if not response.content:
            return None, "Empty response"

        try:
            with Image.open(BytesIO(response.content)) as image:
                image_format = image.format
                dimensions = image.size
                image.load()
        except (
            Image.DecompressionBombError,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as error:
            return None, f"Invalid or unreadable image: {error}"

        if not image_format:
            return None, "Image format could not be determined"

        return (image_format, dimensions), None

    @classmethod
    def _save_valid_image(
        cls,
        directory,
        image_url,
        response,
        image_info=None
    ):
        if response.status_code < 200 or response.status_code >= 300:
            return None, f"HTTP {response.status_code}"

        if not response.content:
            return None, "Empty response"

        if image_info is None:
            image_info, error = cls._inspect_image(response)
            if error:
                return None, error

        image_format = image_info[0]
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


    
    def crawl_source_pages(
        self,
        page,
        entities,
        max_source_pages=10,
        progress_callback=None,
        image_callback=None,
        existing_image_urls=None
    ):
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
        seen_images = set(existing_image_urls or ())

        # Avoid redownloading images already represented by Yandex results.
        for entity in entities:
            original = entity.get("origUrl")
            if original:
                seen_images.add(original)

        pages_to_scan = source_pages[:max_source_pages]
        if progress_callback:
            progress_callback(
                0,
                len(pages_to_scan),
                "Preparing source-page scan"
            )

        for completed, source_url in enumerate(pages_to_scan, start=1):
            print(f"Depth 1: Visiting {source_url}")

            try:
                images = self.get_images_from_source_page(page, source_url)
                page_discovered = []

                for image_url in images:
                    if image_url not in seen_images:
                        seen_images.add(image_url)
                        image = {
                            "url": image_url,
                            "source_page": source_url,
                            "depth": 1,
                        }
                        discovered.append(image)
                        page_discovered.append(image_url)
                if image_callback:
                    image_callback(page_discovered)

            except Exception as e:
                print(f"Failed to crawl {source_url}: {e}")
            finally:
                if progress_callback:
                    progress_callback(
                        completed,
                        len(pages_to_scan),
                        f"Scanned source page {completed} of {len(pages_to_scan)}"
                    )

        print(f"Discovered {len(discovered)} additional image URLs.")
        return discovered


    def download_images_by_page(
        self,
        keyword,
        page,
        imgs_count,
        sub_directory=None,
        depth=1,
        min_size=None
    ):
        """
        Open Yandex Images for manual result loading, then download its images.
        """
        img_url_results = []
        search_directory = self._safe_directory_name(sub_directory or keyword)
        download_dir = os.path.join(self.output_dir, search_directory)
        os.makedirs(download_dir, exist_ok=True)

        def report_progress(
            label,
            completed,
            total,
            succeeded,
            skipped,
            progress_detail=None
        ):
            failed = completed - succeeded - skipped
            print(
                f"\r{label}: {completed}/{total} "
                f"({succeeded} saved, {skipped} skipped, {failed} failed)",
                end="\n" if completed == total else "",
                flush=True
            )
            update_panel_progress(
                label,
                completed,
                total,
                progress_detail
                or f"{succeeded} saved, {skipped} skipped, {failed} failed"
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
            collected_urls = set()
            page_image_counts = {}
            seen_batches = set()
            qualifying_count = 0
            filtered_count = 0
            image_dimension_cache = {}
            input_finished = threading.Event()
            captcha_active = False

            def update_panel_progress(label, completed, total, detail):
                if page_obj.is_closed():
                    return

                try:
                    page_obj.evaluate(
                        """progress => {
                            if (typeof window.__yandexCollectorRender === "function") {
                                window.__yandexCollectorRender(progress);
                            }
                        }""",
                        {
                            "progress": {
                                "stage": label,
                                "completed": completed,
                                "total": total,
                                "detail": detail,
                            }
                        }
                    )
                except PlaywrightError as error:
                    print(f"Could not update progress panel: {error}")

            def register_image_urls(image_urls):
                new_urls = []
                for image_url in image_urls:
                    if (
                        isinstance(image_url, str)
                        and urlparse(image_url).scheme in {"http", "https"}
                        and urlparse(image_url).netloc
                        and image_url not in collected_urls
                    ):
                        collected_urls.add(image_url)
                        new_urls.append(image_url)
                return new_urls

            def update_panel_unique_count():
                if page_obj.is_closed():
                    return

                try:
                    page_obj.evaluate(
                        """counts => {
                            if (typeof window.__yandexCollectorRender === "function") {
                                window.__yandexCollectorRender(counts);
                            }
                        }""",
                        {
                            "uniqueCount": len(collected_urls),
                            "qualifyingCount": qualifying_count,
                            "filteredCount": filtered_count,
                        }
                    )
                except PlaywrightError as error:
                    print(f"Could not update unique image count: {error}")

            def report_capture(_source, payload):
                nonlocal qualifying_count, filtered_count

                if not isinstance(payload, dict):
                    raise ValueError("Invalid Yandex capture payload.")

                page_number = str(payload.get("pageNumber") or "unknown")
                new_urls = register_image_urls(payload.get("urls", []))

                if new_urls:
                    page_image_counts[page_number] = (
                        page_image_counts.get(page_number, 0) + len(new_urls)
                    )
                    batch_id = payload.get("batchId")
                    if isinstance(batch_id, str) and batch_id:
                        seen_batches.add(batch_id)

                return {
                    "uniqueCount": len(collected_urls),
                    "qualifyingCount": qualifying_count,
                    "filteredCount": filtered_count,
                    "batchCount": len(seen_batches),
                    "lastUpdate": "Just now",
                }

            def finish_capture(_source):
                input_finished.set()

            def download_candidates(
                candidates,
                limit,
                directory,
                stage,
                timeout,
                headers=None
            ):
                nonlocal qualifying_count, filtered_count

                target_count = max(0, limit)
                candidates_to_check = (
                    candidates
                    if min_size is not None
                    else candidates[:target_count]
                )
                os.makedirs(directory, exist_ok=True)
                saved = 0
                skipped = 0
                failed = 0
                filtered_in_stage = 0
                processed = 0

                for item in candidates_to_check:
                    if saved >= target_count:
                        break

                    image_url = item["url"] if isinstance(item, dict) else item
                    metadata = (
                        {
                            "source_page": item["source_page"],
                            "depth": item.get("depth", 1),
                        }
                        if isinstance(item, dict)
                        else {}
                    )

                    if image_url in image_dimension_cache:
                        continue

                    processed += 1
                    try:
                        response = requests.get(
                            image_url,
                            timeout=timeout,
                            headers=headers
                        )
                        image_info, inspect_error = self._inspect_image(response)
                        image_dimension_cache[image_url] = (
                            image_info,
                            inspect_error
                        )

                        if inspect_error:
                            img_url_results.append({
                                "status": "skipped",
                                "url": image_url,
                                "error": inspect_error,
                                **metadata,
                            })
                            skipped += 1
                        else:
                            image_width, image_height = image_info[1]
                            below_minimum = (
                                min_size is not None
                                and (
                                    image_width < min_size[0]
                                    or image_height < min_size[1]
                                )
                            )
                            if below_minimum:
                                filtered_count += 1
                                filtered_in_stage += 1
                                skipped += 1
                                img_url_results.append({
                                    "status": "skipped",
                                    "url": image_url,
                                    "error": (
                                        f"Image is {image_width}x{image_height}; "
                                        f"minimum is {min_size[0]}x{min_size[1]}"
                                    ),
                                    **metadata,
                                })
                            else:
                                qualifying_count += 1
                                filepath, save_error = self._save_valid_image(
                                    directory,
                                    image_url,
                                    response,
                                    image_info=image_info
                                )
                                if filepath:
                                    img_url_results.append({
                                        "status": "success",
                                        "url": image_url,
                                        "path": filepath,
                                        **metadata,
                                    })
                                    saved += 1
                                else:
                                    img_url_results.append({
                                        "status": "skipped",
                                        "url": image_url,
                                        "error": save_error,
                                        **metadata,
                                    })
                                    skipped += 1

                    except Exception as error:
                        img_url_results.append({
                            "status": "fail",
                            "url": image_url,
                            "error": str(error),
                            **metadata,
                        })
                        failed += 1

                    report_progress(
                        stage,
                        processed,
                        len(candidates_to_check),
                        saved,
                        skipped,
                        progress_detail=(
                            f"{saved} saved, {filtered_in_stage} filtered, "
                            f"{skipped - filtered_in_stage} skipped, "
                            f"{failed} failed"
                        )
                    )

                    update_panel_unique_count()
                    time.sleep(0.5)

                if processed != len(candidates_to_check):
                    report_progress(
                        stage,
                        processed,
                        processed,
                        saved,
                        skipped,
                        progress_detail=(
                            f"{saved} saved, {filtered_in_stage} filtered, "
                            f"{skipped - filtered_in_stage} skipped, "
                            f"{failed} failed"
                        )
                    )

            page_obj.expose_binding("__yandexReportCapture", report_capture)
            page_obj.expose_binding("__finishYandexCapture", finish_capture)
            capture_script = r"""
                (() => {
                    if (window.top !== window || window.__yandexCollectorPanel) return;
                    window.__yandexCollectorPanel = true;

                    const seenInDocument = new Set();
                    const pendingUrls = new Set();
                    let flushTimer = null;
                    let lastCaptchaState = null;

                    function isCaptcha() {
                        return /\/showcaptcha\/?$/i.test(location.pathname);
                    }

                    function mountPanel() {
                        const host = document.createElement("div");
                        host.id = "yandex-image-collector";
                        host.style.cssText = [
                            "all:initial",
                            "position:fixed",
                            "right:20px",
                            "bottom:20px",
                            "z-index:2147483647"
                        ].join(";");
                        const shadow = host.attachShadow({ mode: "open" });
                        shadow.innerHTML = `
                            <style>
                                :host { all: initial; }
                                .panel {
                                    box-sizing: border-box;
                                    width: 310px;
                                    padding: 18px;
                                    border: 1px solid #e5e7eb;
                                    border-radius: 14px;
                                    background: #fff;
                                    box-shadow: 0 12px 36px #0003;
                                    color: #111827;
                                    font: 14px/1.45 Arial, sans-serif;
                                }
                                .header, .status, .metric {
                                    display: flex;
                                    align-items: center;
                                    justify-content: space-between;
                                }
                                .title { font-size: 16px; font-weight: 700; }
                                .status { justify-content: flex-start; gap: 7px; margin-top: 12px; }
                                .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; }
                                .dot.captcha { background: #f59e0b; }
                                .dot.progress { background: #2563eb; }
                                .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px; }
                                .metric {
                                    align-items: flex-start;
                                    flex-direction: column;
                                    padding: 11px;
                                    border-radius: 9px;
                                    background: #f3f4f6;
                                }
                                .value { font-size: 22px; font-weight: 700; }
                                .label, .last-update, .hint { color: #6b7280; font-size: 12px; }
                                .last-update { margin-top: 13px; }
                                .hint { min-height: 32px; margin: 8px 0 14px; }
                                .progress-section { margin-top: 15px; }
                                .progress-meta {
                                    display: flex;
                                    justify-content: space-between;
                                    gap: 8px;
                                    margin-bottom: 7px;
                                    font-size: 12px;
                                    font-weight: 700;
                                }
                                .progress-track {
                                    height: 8px;
                                    overflow: hidden;
                                    border-radius: 999px;
                                    background: #e5e7eb;
                                }
                                .progress-fill {
                                    width: 0;
                                    height: 100%;
                                    border-radius: inherit;
                                    background: #2563eb;
                                    transition: width 180ms ease;
                                }
                                .progress-detail {
                                    margin-top: 6px;
                                    color: #6b7280;
                                    font-size: 11px;
                                }
                                button {
                                    width: 100%;
                                    padding: 10px 12px;
                                    border: 0;
                                    border-radius: 8px;
                                    background: #2563eb;
                                    color: white;
                                    cursor: pointer;
                                    font: inherit;
                                    font-weight: 700;
                                }
                                button:disabled { cursor: wait; opacity: .7; }
                            </style>
                            <section class="panel" aria-label="Yandex Image Downloader">
                                <div class="header">
                                    <span class="title">Yandex Image Downloader</span>
                                </div>
                                <div class="status" role="status">
                                    <span class="dot"></span>
                                    <strong class="status-text">Capturing</strong>
                                </div>
                                <div class="metrics">
                                    <div class="metric">
                                        <span class="value unique-count">0</span>
                                        <span class="label">Unique images</span>
                                    </div>
                                    <div class="metric">
                                        <span class="value qualifying-count">0</span>
                                        <span class="label">Qualifying</span>
                                    </div>
                                    <div class="metric">
                                        <span class="value filtered-count">0</span>
                                        <span class="label">Filtered out</span>
                                    </div>
                                    <div class="metric">
                                        <span class="value batch-count">0</span>
                                        <span class="label">Result batches</span>
                                    </div>
                                </div>
                                <div class="last-update">Last update: <span>Just now</span></div>
                                <p class="hint">Click Show more on Yandex to collect additional results.</p>
                                <div class="progress-section" hidden>
                                    <div class="progress-meta">
                                        <span class="progress-title"></span>
                                        <span class="progress-count"></span>
                                    </div>
                                    <div
                                        class="progress-track"
                                        role="progressbar"
                                        aria-valuemin="0"
                                        aria-valuemax="100"
                                        aria-valuenow="0"
                                    >
                                        <div class="progress-fill"></div>
                                    </div>
                                    <div class="progress-detail"></div>
                                </div>
                                <button type="button">Finish and download</button>
                            </section>
                        `;

                        const statusText = shadow.querySelector(".status-text");
                        const dot = shadow.querySelector(".dot");
                        const uniqueCount = shadow.querySelector(".unique-count");
                        const qualifyingCount = shadow.querySelector(".qualifying-count");
                        const filteredCount = shadow.querySelector(".filtered-count");
                        const batchCount = shadow.querySelector(".batch-count");
                        const lastUpdate = shadow.querySelector(".last-update span");
                        const hint = shadow.querySelector(".hint");
                        const progressSection = shadow.querySelector(".progress-section");
                        const progressTitle = shadow.querySelector(".progress-title");
                        const progressCount = shadow.querySelector(".progress-count");
                        const progressTrack = shadow.querySelector(".progress-track");
                        const progressFill = shadow.querySelector(".progress-fill");
                        const progressDetail = shadow.querySelector(".progress-detail");
                        const finishButton = shadow.querySelector("button");
                        let currentProgress = null;

                        function render(snapshot = {}) {
                            if (snapshot.progress) {
                                currentProgress = snapshot.progress;
                            }
                            const captcha = isCaptcha();
                            dot.classList.toggle("captcha", captcha);
                            dot.classList.toggle("progress", Boolean(currentProgress));
                            if (Number.isFinite(snapshot.uniqueCount)) {
                                uniqueCount.textContent = snapshot.uniqueCount;
                            }
                            if (Number.isFinite(snapshot.qualifyingCount)) {
                                qualifyingCount.textContent = snapshot.qualifyingCount;
                            }
                            if (Number.isFinite(snapshot.filteredCount)) {
                                filteredCount.textContent = snapshot.filteredCount;
                            }
                            if (Number.isFinite(snapshot.batchCount)) {
                                batchCount.textContent = snapshot.batchCount;
                            }
                            if (snapshot.lastUpdate) {
                                lastUpdate.textContent = snapshot.lastUpdate;
                            }

                            if (currentProgress) {
                                const completed = Math.max(
                                    0,
                                    Number(currentProgress.completed) || 0
                                );
                                const total = Math.max(
                                    0,
                                    Number(currentProgress.total) || 0
                                );
                                const percent = total
                                    ? Math.min(100, Math.round(completed / total * 100))
                                    : 100;
                                progressSection.hidden = false;
                                progressTitle.textContent = currentProgress.stage;
                                progressCount.textContent = `${completed} / ${total}`;
                                progressTrack.setAttribute("aria-valuenow", percent);
                                progressFill.style.width = `${percent}%`;
                                progressDetail.textContent = currentProgress.detail || "";
                                statusText.textContent = currentProgress.stage;
                                hint.textContent = currentProgress.detail || "Working...";
                            } else {
                                statusText.textContent = captcha
                                    ? "CAPTCHA challenge"
                                    : "Capturing";
                                hint.textContent = captcha
                                    ? "Solve the CAPTCHA in the browser, then continue collecting."
                                    : "Click Show more on Yandex to collect additional results.";
                            }
                        }

                        finishButton.addEventListener("click", async () => {
                            finishButton.disabled = true;
                            finishButton.textContent = "Preparing download...";
                            hint.textContent = "Finishing collection and preparing downloads.";
                            try {
                                await window.__finishYandexCapture();
                            } catch (error) {
                                finishButton.disabled = false;
                                finishButton.textContent = "Finish and download";
                                hint.textContent = `Could not finish: ${error.message}`;
                            }
                        });

                        document.documentElement.appendChild(host);
                        render();
                        window.__yandexCollectorRender = render;
                    }

                    function report(urls, batchId = null) {
                        const pageNumber = new URL(location.href)
                            .searchParams.get("p") || "unknown";
                        return window.__yandexReportCapture({
                            urls,
                            pageNumber,
                            batchId,
                            isCaptcha: isCaptcha()
                        }).then(snapshot => {
                            window.__yandexCollectorRender?.(snapshot);
                        });
                    }

                    function createBatchId() {
                        if (typeof crypto.randomUUID === "function") {
                            return crypto.randomUUID();
                        }
                        return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
                    }

                    function flushPendingUrls() {
                        flushTimer = null;
                        if (!pendingUrls.size || isCaptcha()) return;
                        const urls = [...pendingUrls];
                        pendingUrls.clear();
                        report(urls, createBatchId()).catch(error => {
                            urls.forEach(url => pendingUrls.add(url));
                            const hint = document.querySelector(
                                "#yandex-image-collector"
                            )?.shadowRoot?.querySelector(".hint");
                            if (hint) {
                                hint.textContent = `Capture connection error: ${error.message}`;
                            }
                            flushTimer = window.setTimeout(flushPendingUrls, 1500);
                        });
                    }

                    function captureLinks(root = document) {
                        if (isCaptcha()) {
                            window.__yandexCollectorRender?.();
                            return;
                        }

                        const links = [];
                        if (
                            root instanceof Element &&
                            root.matches('a[href*="img_url="]')
                        ) {
                            links.push(root);
                        }
                        if (root.querySelectorAll) {
                            links.push(...root.querySelectorAll('a[href*="img_url="]'));
                        }

                        for (const link of links) {
                            try {
                                const resultUrl = new URL(link.href);
                                const imageUrl = resultUrl.searchParams.get("img_url");
                                if (
                                    imageUrl &&
                                    /^https?:\/\//i.test(imageUrl) &&
                                    !seenInDocument.has(imageUrl)
                                ) {
                                    seenInDocument.add(imageUrl);
                                    pendingUrls.add(imageUrl);
                                }
                            } catch {
                                // Ignore malformed result links.
                            }
                        }

                        if (pendingUrls.size && !flushTimer) {
                            flushTimer = window.setTimeout(flushPendingUrls, 700);
                        }
                    }

                    function startCapture() {
                        mountPanel();
                        report([]).catch(error => {
                            const hint = document.querySelector(
                                "#yandex-image-collector"
                            )?.shadowRoot?.querySelector(".hint");
                            if (hint) {
                                hint.textContent = `Capture connection error: ${error.message}`;
                            }
                        });
                        captureLinks();
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
                        observer.observe(document.documentElement, {
                            attributes: true,
                            attributeFilter: ["href"],
                            childList: true,
                            subtree: true
                        });
                        window.setInterval(() => {
                            const captcha = isCaptcha();
                            if (captcha !== lastCaptchaState) {
                                lastCaptchaState = captcha;
                                window.__yandexCollectorRender?.();
                                report([]).catch(error => {
                                    const hint = document.querySelector(
                                        "#yandex-image-collector"
                                    )?.shadowRoot?.querySelector(".hint");
                                    if (hint) {
                                        hint.textContent = `Capture connection error: ${error.message}`;
                                    }
                                });
                            }
                            captureLinks();
                        }, 500);
                    }

                    if (document.documentElement) {
                        startCapture();
                    } else {
                        document.addEventListener("DOMContentLoaded", startCapture, {
                            once: true
                        });
                    }
                })();
            """
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
                print(
                    "MANUAL BROWSER STEPS\n"
                    "1. Click 'Show more' to load additional image results.\n"
                    "2. Image URLs are captured automatically as results appear.\n"
                    "3. Repeat as needed: click 'Show more' to load more results\n"
                    "   and solve any CAPTCHA that appears to continue browsing.\n"
                    "4. Click 'Finish and download' in the floating panel, or\n"
                    "   return to this console and press Enter."
                )
                print("=" * 60)

                reported_pages = set()
                entity_list = get_entities(read_state())

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

                last_new_entity_read = 0.0
                last_page_number = None
                while not input_finished.is_set() and not page_obj.is_closed():
                    current_url = page_obj.url
                    parsed_url = urlparse(current_url)
                    captcha_detected = (
                        parsed_url.path.rstrip("/").lower() == "/showcaptcha"
                    )
                    current_page_number = (
                        None
                        if captcha_detected
                        else parse_qs(parsed_url.query).get("p", ["unknown"])[0]
                    )
                    if (
                        last_page_number is not None
                        and current_page_number != last_page_number
                    ):
                        report_page(last_page_number)
                    last_page_number = current_page_number

                    if captcha_detected and not captcha_active:
                        if last_page_number is not None:
                            report_page(last_page_number)
                            last_page_number = None
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

                    time.sleep(0.25)

                if not page_obj.is_closed():
                    state = read_state()
                    current_entities = get_entities(state)
                    if current_entities:
                        entity_list = current_entities

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
                
                download_candidates(
                    unique_urls,
                    imgs_count,
                    download_dir,
                    "Saving base images",
                    timeout=10
                )
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

                    def report_source_scan_progress(completed, total, detail):
                        update_panel_progress(
                            "Scanning source pages",
                            completed,
                            total,
                            detail
                        )

                    def report_source_candidates(image_urls):
                        if register_image_urls(image_urls):
                            update_panel_unique_count()

                    extra_images = self.crawl_source_pages(
                        source_page,
                        selected_entities,
                        max_source_pages=1000,
                        progress_callback=report_source_scan_progress,
                        image_callback=report_source_candidates,
                        existing_image_urls=collected_urls
                    )

                    depth1_dir = os.path.join(download_dir, "depth_1")
                    os.makedirs(depth1_dir, exist_ok=True)

                    download_candidates(
                        extra_images,
                        imgs_count,
                        depth1_dir,
                        "Saving source images",
                        timeout=15,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0.0.0 "
                                "Safari/537.36"
                            )
                        }
                    )


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
        "--min-size",
        type=_parse_min_size,
        metavar="WIDTHxHEIGHT",
        help=(
            "Only save images at least this wide and high, for example "
            "400x400. Without this option, all image sizes are eligible."
        )
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
        depth=args.depth,
        min_size=args.min_size
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
