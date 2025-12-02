import os
import re
import hashlib
import time
import argparse
from typing import Set, Dict, Optional
from urllib.parse import urlparse, urljoin, urlunparse
import urllib.robotparser
import signal
import random
import json
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


IGNORED_EXTENSIONS = re.compile(r".*\.(css|js|json|zip|rar|exe|tar|gz|mp3|mp4|avi|mov)$", re.I)

# extensions we consider 'assets' (images/docs) and will download when encountered on pages
ASSET_EXTENSIONS = re.compile(r".*\.(jpg|jpeg|png|gif|svg|webp|bmp|pdf|doc|docx|xls|xlsx|ppt|pptx)$", re.I)


def _is_valid_href(href: Optional[str]) -> bool:
    if not href:
        return False
    href = href.strip()
    if href.startswith("#"):
        return False
    if href.startswith("javascript:"):
        return False
    if href.startswith("mailto:") or href.startswith("tel:"):
        return False
    if IGNORED_EXTENSIONS.match(href):
        return False
    return True


def _safe_filename_from_url(base_url: str, target_url: str) -> str:
    """Create a safe file path for a given target_url relative to base_url.

    Examples:
      https://example.com/ -> index.md
      https://example.com/about -> about.md
      https://example.com/docs/intro -> docs/intro.md
      https://example.com/?q=query -> index_q=query.md  (query encoded simply)
    """
    u_base = urlparse(base_url)
    u_target = urlparse(target_url)

    # Use path; default to index
    path = u_target.path or "/"
    if path.endswith("/"):
        path = path + "index"

    # sanitize path
    safe_path = path.lstrip("/")
    if not safe_path:
        safe_path = "index"

    # include query if present (simple replacement of special chars)
    if u_target.query:
        q = re.sub(r"[^0-9A-Za-z\-_]", "_", u_target.query)
        safe_path = f"{safe_path}__{q}"

    # append .md
    if not safe_path.endswith(".md"):
        safe_path = safe_path + ".md"

    # Ensure filename length for the last path component doesn't exceed OS limits
    # Most filesystems restrict a single filename component to 255 chars; we'll pick
    # a conservative cap (200) for safety. If the last part is too long, truncate and
    # append a short sha1 suffix to keep names unique.
    max_component_len = 200
    # split into directory and last component
    dirpart, last = os.path.split(safe_path)
    if len(last) > max_component_len:
        name, ext = os.path.splitext(last)
        h = hashlib.sha1(target_url.encode("utf-8")).hexdigest()[:10]
        allowed = max_component_len - len(ext) - 3 - len(h)  # 3 for '__'
        if allowed < 16:
            allowed = 16
        name_trunc = name[:allowed]
        last = f"{name_trunc}__{h}{ext}"
        safe_path = os.path.join(dirpart, last)

    return safe_path


def crawl_site_to_markdown(start_url: str, output_dir: str = "site_markdown", max_pages: int = 500,
                           respect_robots: bool = True, user_agent: str = "web-crawling-bot/1.0",
                           delay: float = 0.2, jitter: float = 0.0,
                           checkpoint_file: Optional[str] = None, resume: bool = False, save_every: int = 10,
                           include_frontmatter: bool = True):
    """Crawl the site starting from start_url, find all internal pages (same netloc)
    and save each page's content as a markdown file under output_dir.

    Also creates an index.md listing page titles and their paths.
    """
    parsed = urlparse(start_url)
    base_netloc = parsed.netloc
    base_scheme = parsed.scheme
    robots_parser = None
    if respect_robots:
        robots_parser = urllib.robotparser.RobotFileParser()
        robots_url = urlunparse((base_scheme, base_netloc, '/robots.txt', '', '', ''))
        try:
            robots_parser.set_url(robots_url)
            robots_parser.read()
        except Exception:
            # If robots cannot be read, fall back to allowing everything
            robots_parser = None

    session = requests.Session()
    to_visit = [start_url]
    visited: Set[str] = set()
    discovered_titles: Dict[str, str] = {}

    os.makedirs(output_dir, exist_ok=True)
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    if not checkpoint_file:
        checkpoint_file = os.path.join(output_dir, ".crawl_state.json")

    # resume from checkpoint if requested
    if resume and os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as fh:
                state = json.load(fh)
            to_visit = state.get('to_visit', to_visit)
            visited = set(state.get('visited', []))
            discovered_titles.update(state.get('discovered_titles', {}))
            print(f"🔁 Resumed crawl state from {checkpoint_file} — queue {len(to_visit)}, visited {len(visited)}")
        except Exception as e:
            print(f"⚠️ Could not restore state from {checkpoint_file}: {e}")

    # signal handler for graceful shutdown (save state)
    shutdown = {'flag': False}

    def _sigint_handler(signum, frame):
        print("\n⚠️  Received interrupt — saving state and exiting gracefully")
        shutdown['flag'] = True

    signal.signal(signal.SIGINT, _sigint_handler)

    pages_done = 0
    def _save_state():
        try:
            data = {
                'to_visit': to_visit,
                'visited': list(visited),
                'discovered_titles': discovered_titles,
                'last_saved': datetime.utcnow().isoformat() + 'Z'
            }
            with open(checkpoint_file, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            print(f"💾 Saved checkpoint to {checkpoint_file}")
        except Exception as e:
            print(f"⚠️ Failed to save checkpoint: {e}")

    while to_visit and len(visited) < max_pages and not shutdown['flag']:
        url = to_visit.pop(0)
        if url in visited:
            continue
        try:
            print(f"Fetching: {url}")
            # robots check for the URL
            if robots_parser and not robots_parser.can_fetch(user_agent, url):
                print(f"⛔ Skipping {url} per robots.txt rules")
                visited.add(url)
                continue
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"⚠️  Failed to fetch {url}: {e}")
            visited.add(url)
            continue

        visited.add(url)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Save title
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else url
        discovered_titles[url] = title_text

        # find and download assets (images, pdfs, office docs)
        def _is_asset_link(link: str) -> bool:
            if not link:
                return False
            return ASSET_EXTENSIONS.match(link)

        def _safe_asset_path(base: str, resource_url: str) -> str:
            # create an assets path under assets_dir. Keep domain + path structure
            u = urlparse(resource_url)
            asset_path = u.path.lstrip("/") or "root"
            if u.query:
                q = re.sub(r"[^0-9A-Za-z\-_]", "_", u.query)
                asset_path = f"{asset_path}__{q}"
            # if no extension, add bin
            if not os.path.splitext(asset_path)[1]:
                asset_path = asset_path + ".bin"

            # make sure the last path component isn't too long
            max_component_len = 200
            dirpart, last = os.path.split(asset_path)
            if len(last) > max_component_len:
                name, ext = os.path.splitext(last)
                h = hashlib.sha1(resource_url.encode("utf-8")).hexdigest()[:10]
                allowed = max_component_len - len(ext) - 3 - len(h)
                if allowed < 8:
                    allowed = 8
                last = f"{name[:allowed]}__{h}{ext}"
                asset_path = os.path.join(dirpart, last)

            return os.path.join(assets_dir, u.netloc, asset_path)

        def _download_asset(session: requests.Session, asset_url: str) -> Optional[str]:
            try:
                abs_url = urljoin(url, asset_url)
                # allow data: URIs to pass through
                if abs_url.startswith("data:"):
                    return None
                resp = session.get(abs_url, timeout=20)
                resp.raise_for_status()
            except Exception as e:
                print(f"⚠️  Failed to download asset {asset_url} (page {url}): {e}")
                return None

            local_path = _safe_asset_path(url, abs_url)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            try:
                with open(local_path, "wb") as outf:
                    outf.write(resp.content)
            except Exception as e:
                print(f"⚠️  Could not write asset {local_path}: {e}")
                return None

            # return path relative to output_dir so markdown can reference it
            rel = os.path.relpath(local_path, start=output_dir)
            return rel.replace(os.path.sep, "/")

        # download <img src=> assets
        for img in soup.find_all("img", src=True):
            src = img.get("src")
            if not src:
                continue
            if _is_asset_link(src) or src.startswith("data:"):
                rel = _download_asset(session, src)
                if rel:
                    img["src"] = rel

        # download linked assets like PDFs or docs
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not href:
                continue
            if _is_asset_link(href):
                rel = _download_asset(session, href)
                if rel:
                    a["href"] = rel

        # Convert body to markdown and write file
        body = soup.body or soup
        markdown_text = md(str(body), heading_style="ATX")

        target_path = _safe_filename_from_url(start_url, url)
        full_path = os.path.join(output_dir, target_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        crawled_at = datetime.utcnow().isoformat() + 'Z'
        with open(full_path, "w", encoding="utf-8") as f:
            if include_frontmatter:
                # write YAML frontmatter
                f.write("---\n")
                f.write(f"title: {json.dumps(title_text)}\n")
                f.write(f"url: {json.dumps(url)}\n")
                f.write(f"crawled_at: {json.dumps(crawled_at)}\n")
                f.write("---\n\n")
            else:
                f.write(f"<!-- Source: {url} -->\n\n")
                f.write(f"# {title_text}\n\n")
            f.write(markdown_text)

        # find links
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not _is_valid_href(href):
                continue
            new_url = urljoin(url, href)
            parsed_new = urlparse(new_url)
            # only same domain / netloc
            if parsed_new.netloc != base_netloc:
                continue
            # robots check for link target
            normalized_for_check = urlunparse((parsed_new.scheme or base_scheme,
                                              parsed_new.netloc,
                                              parsed_new.path or '/',
                                              '', parsed_new.query, ''))
            if robots_parser and not robots_parser.can_fetch(user_agent, normalized_for_check):
                # don't add disallowed urls to the queue
                continue
            # don't enqueue asset files (images, pdfs, docs)
            if ASSET_EXTENSIONS.match(parsed_new.path or parsed_new.geturl()):
                continue
            # normalize scheme and path
            normalized = urlunparse((parsed_new.scheme or base_scheme,
                                     parsed_new.netloc,
                                     parsed_new.path or "/",
                                     '', parsed_new.query, ''))
            if normalized not in visited and normalized not in to_visit:
                to_visit.append(normalized)

        # polite delay (configurable) with optional jitter
        if delay and delay > 0:
            sleep_time = delay
            if jitter and jitter > 0:
                sleep_time = delay + random.uniform(-jitter, jitter)
                if sleep_time < 0:
                    sleep_time = 0
            time.sleep(sleep_time)

        pages_done += 1
        # periodic save checkpoint
        if pages_done % save_every == 0:
            _save_state()

    # write index
    index_path = os.path.join(output_dir, "index.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(f"# Crawled site index for {start_url}\n\n")
        f.write("| Title | URL | File |\n|---|---|---|\n")
        for url in sorted(discovered_titles.keys()):
            title = discovered_titles[url]
            file_path = _safe_filename_from_url(start_url, url)
            f.write(f"| {title} | {url} | {file_path} |\n")

    print(f"✅ Crawled {len(visited)} pages from {start_url} and saved to {output_dir}")

    # finished — save final state (clear or keep preserved)
    try:
        if os.path.exists(checkpoint_file):
            # write final state
            _save_state()
    except Exception:
        pass

# 사용 예시
def _cli():
    parser = argparse.ArgumentParser(description="Crawl a website and export pages to markdown files.")
    parser.add_argument("url", help="start URL to crawl")
    parser.add_argument("-o", "--output-dir", default="site_markdown", help="output directory")
    parser.add_argument("-m", "--max-pages", type=int, default=200, help="max number of pages to crawl")
    parser.add_argument("--respect-robots", dest="respect_robots", action="store_true", default=True,
                        help="Respect robots.txt (default: enabled)")
    parser.add_argument("--no-respect-robots", dest="respect_robots", action="store_false",
                        help="Do not respect robots.txt")
    parser.add_argument("--user-agent", dest="user_agent", default="web-crawling-bot/1.0",
                        help="User-Agent string to use when fetching pages and evaluating robots.txt")
    parser.add_argument("--delay", dest="delay", type=float, default=0.2,
                        help="Base delay (seconds) between requests — default 0.2")
    parser.add_argument("--jitter", dest="jitter", type=float, default=0.0,
                        help="Max jitter (seconds) to add/substract from delay randomly — default 0.0")
    parser.add_argument("--checkpoint-file", dest="checkpoint_file", default=None,
                        help="Path for saving crawler checkpoint state (default: <output>/.crawl_state.json)")
    parser.add_argument("--resume", dest="resume", action='store_true', default=False,
                        help="Resume crawl from an existing checkpoint file (default: false)")
    parser.add_argument("--save-every", dest="save_every", type=int, default=10,
                        help="Save checkpoint state every N pages (default: 10)")
    parser.add_argument("--no-frontmatter", dest="include_frontmatter", action="store_false", default=True,
                        help="Disable YAML frontmatter in generated markdown files")
    args = parser.parse_args()
    crawl_site_to_markdown(args.url, args.output_dir, args.max_pages, respect_robots=args.respect_robots,
                           user_agent=args.user_agent, delay=args.delay, jitter=args.jitter,
                           checkpoint_file=args.checkpoint_file, resume=args.resume, save_every=args.save_every,
                           include_frontmatter=args.include_frontmatter)


if __name__ == "__main__":
    _cli()