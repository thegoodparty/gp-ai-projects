"""Fetch a JS-rendered web page using Playwright. Saves HTML to a file and prints the path."""
import sys
import os
import hashlib
from datetime import datetime

from playwright.sync_api import sync_playwright

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def fetch(
    url: str,
    output_dir: str = "/tmp",
    wait_until: str = "networkidle",
    timeout: int = 30000,
    selector: str | None = None,
    delay: int = 0,
) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until=wait_until, timeout=timeout)
        if selector:
            try:
                page.wait_for_selector(selector, timeout=timeout)
            except Exception:
                pass
        if delay:
            page.wait_for_timeout(delay)
        html = page.content()
        browser.close()

    slug = hashlib.md5(url.encode()).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{slug}.html"
    filepath = os.path.join(output_dir, filename)

    os.makedirs(output_dir, exist_ok=True)
    with open(filepath, "w") as f:
        f.write(html)

    return filepath


def _parse_arg(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    if flag in args:
        idx = args.index(flag)
        val = args[idx + 1]
        return val, args[:idx] + args[idx + 2:]
    return None, args


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run fetch_js_page.py <url> [options]")
        print("  --dir PATH        Save directory (default: /tmp)")
        print("  --wait MODE       networkidle (default), domcontentloaded, load")
        print("  --selector CSS    Wait for CSS selector after page load (for AJAX content)")
        print("  --delay MS        Extra wait in ms after page load (for slow AJAX)")
        sys.exit(1)

    args = sys.argv[1:]

    output_dir, args = _parse_arg(args, "--dir")
    wait, args = _parse_arg(args, "--wait")
    selector, args = _parse_arg(args, "--selector")
    delay_str, args = _parse_arg(args, "--delay")

    path = fetch(
        url=args[0],
        output_dir=output_dir or "/tmp",
        wait_until=wait or "networkidle",
        selector=selector,
        delay=int(delay_str) if delay_str else 0,
    )
    print(path)
