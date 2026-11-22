"""界面截图：用于改完 CSS 后的视觉自查。

用法：
    .venv/bin/python tools/shot_ui.py            # 默认 1080 宽
    .venv/bin/python tools/shot_ui.py 1440 1280  # 指定多个宽度

前提：另起 .venv/bin/python -m pp6kit.app --headless --port 8899
输出：out/界面预览-8112/v2-<宽度>-<页面>.png
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = Path(__file__).resolve().parent.parent / "out" / "界面预览-8112"


def shot(page, name: str) -> None:
    page.wait_for_timeout(320)
    page.screenshot(path=str(OUT / name), full_page=False)
    print("  ->", name)


def main(widths: list[int]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for w in widths:
            ctx = browser.new_context(viewport={"width": w, "height": 760},
                                      device_scale_factor=2)
            page = ctx.new_page()
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(600)

            shot(page, f"v2-{w}-01-首屏.png")

            # 结果页：载入示例 → 第 3 步
            try:
                page.click("#btnDemo", timeout=4000)
                page.wait_for_selector("#result:not([hidden])", timeout=20000)
                page.wait_for_timeout(800)
                shot(page, f"v2-{w}-02-结果页.png")
            except Exception as exc:  # noqa: BLE001
                print("  结果页截图跳过：", exc)

            # 查询页
            page.click('.mode[data-mode="query"]')
            page.wait_for_timeout(500)
            shot(page, f"v2-{w}-03-数据查询.png")

            # 设置页
            page.click('.mode[data-mode="settings"]')
            page.wait_for_timeout(400)
            shot(page, f"v2-{w}-04-系统设置.png")

            ctx.close()
        browser.close()


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]] or [1080]
    main(args)
