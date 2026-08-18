"""
Standalone diagnostic: launches the browser exactly like discover.py
does, takes one screenshot, and reports whether the screenshot's
pixel dimensions match the requested viewport. No LLM calls -- free
and fast to run repeatedly while debugging the coordinate mismatch.

Usage:
    python3 -m agent.diagnose_coords
"""

from PIL import Image
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5000"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=1,
        )
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")

        path = "/tmp/coord_diagnostic.png"
        page.screenshot(path=path)
        img = Image.open(path)

        viewport = page.viewport_size
        device_pixel_ratio = page.evaluate("window.devicePixelRatio")

        print("=" * 60)
        print(f"Requested viewport:      {viewport}")
        print(f"Screenshot pixel size:   {img.width} x {img.height}")
        print(f"window.devicePixelRatio: {device_pixel_ratio}")
        print("=" * 60)

        if (img.width, img.height) == (viewport["width"], viewport["height"]):
            print("MATCH -- screenshot pixels == viewport CSS pixels. "
                  "Coordinates should map 1:1. The bug is elsewhere.")
        else:
            ratio_w = img.width / viewport["width"]
            ratio_h = img.height / viewport["height"]
            print(f"MISMATCH -- screenshot is {ratio_w:.2f}x wider and {ratio_h:.2f}x taller "
                  f"than the viewport. This confirms the coordinate bug: the model sees a "
                  f"{img.width}x{img.height} image but clicks are interpreted in "
                  f"{viewport['width']}x{viewport['height']} space.")

        # Also report where the Member ID input actually is, in CSS px,
        # so we know the ground truth to compare against model guesses.
        box = page.locator("input[name='member_id']").bounding_box()
        print(f"\nActual Member ID input bounding box (CSS px): {box}")

        input("\nBrowser is open -- press Enter here to close it.")
        browser.close()


if __name__ == "__main__":
    main()