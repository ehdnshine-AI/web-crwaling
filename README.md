# Web crawling -> Markdown (dnshine.co.kr sample)

This folder contains `web-crewling.py`, a small site crawler that:

- Crawls a site (same-domain links only)
- Downloads images and common document types (pdf, docx, xlsx, etc.) into `assets/`
- Converts HTML body to Markdown and rewrites image/document links to local `assets/` files

Quick usage (from workspace root):

1. Activate your Python environment:

```bash
source /home/dnshine/venv312/bin/activate
```

2. Install dependencies (recommended inside the venv):

```bash
pip install -r web-crawling/requirements.txt
```

3. Run the crawler (example: crawl dnshine main domain, limit to 10 pages):

```bash
python web-crawling/web-crawling.py https://www.dnshine.co.kr/ -o python-files/output_dnshine -m 10
```

Backward compatibility: an older file name `web-crewling.py` may still exist in this folder. Both names will work; `web-crawling.py` is the up-to-date script.

Output:
- Markdown files will be created under the output directory (default `site_markdown`) with `assets/` subfolder containing downloaded files.

Notes & tips:
- This is a lightweight tool for simple site exports; it is not a full web-archiver.
- Keep `--max-pages` conservative when crawling external sites.

New features and CLI options:

- --respect-robots / --no-respect-robots (default: enabled) — follow robots.txt rules
- --user-agent "string" — user-agent used for requests and robots checks
- --delay FLOAT — base number of seconds to wait between requests (default 0.2)
- --jitter FLOAT — add/subtract up to this many seconds randomly to the delay (default 0.0)
- --checkpoint-file PATH — path to save state (default: <output>/.crawl_state.json)
- --resume — resume from existing checkpoint file if present
- --save-every N — save checkpoint every N pages (default 10)
- --no-frontmatter — disable YAML frontmatter in each generated markdown file

Graceful shutdown and resume
-- If you interrupt the crawler (Ctrl+C) it will save a checkpoint to the checkpoint file (`<output>/.crawl_state.json` by default).
-- Restart the crawler with `--resume` and the same `-o <output>` (or `--checkpoint-file <path>`) to continue where it left off.
