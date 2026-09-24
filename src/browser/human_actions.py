"""Human interaction helpers, dialog dismissers, and DataDome slider solvers."""

from __future__ import annotations

import asyncio
import math
import random
from typing import List, Optional, Tuple
from playwright.async_api import Frame, Page, Locator

from src.utils.logger import LOGGER


async def human_sleep(delay_range_ms: Tuple[int, int]) -> None:
    """Sleep for a randomized duration within delay_range_ms."""
    lo, hi = delay_range_ms
    if hi < lo:
        lo, hi = hi, lo
    delay_ms = random.randint(lo, hi)
    await asyncio.sleep(delay_ms / 1000.0)


async def accept_cookies_if_present(page: Page, timeout_ms: int = 1500) -> None:
    """Detect and click common cookie consent buttons, including Etix OK banner."""
    selectors = [
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept Cookies')",
        "button:has-text('I Accept')",
        "button:has-text('Allow All')",
        "button:has-text('Принять')",
        "button:has-text('Согласен')",
        # Etix Cookie Consent Overlay
        "div[class*='cookie'] button:has-text('OK')",
        "div[class*='cookie'] button.btn-primary",
        "div[class*='cookie'] button",
        "#cookie-consent button",
        ".cookie-banner button",
        "button:text-is('OK')",
        "button:has-text('OK')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=timeout_ms):
                await btn.click(timeout=1000)
                await asyncio.sleep(0.3)
                LOGGER.debug(f"Accepted cookies via {sel}")
                return
        except Exception:
            continue


async def close_blocking_popups(page: Page, timeout_ms: int = 1500) -> None:
    """Close age verification, newsletter popups, idle modals, and venue disclaimer dialogues."""
    popup_selectors = [
        "button[aria-label='Close']",
        "button.close",
        ".modal-header button.close",
        ".modal-footer button:has-text('I am 21 or older')",
        "button:has-text('Continue to Event')",
        "button:has-text('Close')",
        "button:has-text('Закрыть')",
        "button:has-text('Yes, I am over 21')",
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "button:has-text('I agree')",
        "button:has-text('Accept')",
        "button:has-text('Got it')",
        "button:has-text('Understood')",
        "button:has-text('Enter Site')",
        "button:has-text('Enter')",
        "button:has-text('Continue')",
        "button:has-text('Proceed')",
        "button:has-text('OK')",
        "button[data-dismiss='modal']",
        "button[data-bs-dismiss='modal']",
        "a[data-dismiss='modal']",
        ".modal-close",
    ]
    for sel in popup_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=timeout_ms):
                await btn.click(timeout=1000)
                await asyncio.sleep(0.3)
        except Exception:
            continue

    # Extra safety: remove blocking backdrop if present and inactive
    try:
        backdrop = page.locator(".modal-backdrop")
        if await backdrop.is_visible(timeout=300):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
    except Exception:
        pass


def _generate_bezier_points(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    steps: int = 32,
) -> List[Tuple[float, float]]:
    """
    Generate humanized drag points using a cubic Bezier curve with 3-phase kinematics:
    - Ease-In acceleration phase
    - Cruise phase with realistic micro-jitter on Y-axis (±1 - 3px)
    - Ease-Out deceleration phase near the target edge
    """
    points = []
    dx = end_x - start_x
    dy = end_y - start_y

    # Control points with slight natural curvature
    ctrl1_x = start_x + dx * random.uniform(0.22, 0.38)
    ctrl1_y = start_y + random.uniform(-4.0, 4.0)
    ctrl2_x = start_x + dx * random.uniform(0.65, 0.85)
    ctrl2_y = end_y + random.uniform(-3.0, 3.0)

    for i in range(1, steps + 1):
        raw_t = i / steps
        # Smoothstep / cubic easing for natural human acceleration and deceleration
        eased_t = (3 * (raw_t ** 2) - 2 * (raw_t ** 3)) if i < steps else 1.0

        inv = 1.0 - eased_t
        x = (inv ** 3) * start_x + 3 * (inv ** 2) * eased_t * ctrl1_x + 3 * inv * (eased_t ** 2) * ctrl2_x + (eased_t ** 3) * end_x
        y = (inv ** 3) * start_y + 3 * (inv ** 2) * eased_t * ctrl1_y + 3 * inv * (eased_t ** 2) * ctrl2_y + (eased_t ** 3) * end_y

        # Micro-tremor along Y axis: larger in the middle, dampened at start and end
        if 0.15 <= raw_t <= 0.88:
            jitter_y = random.uniform(-2.2, 2.2)
        elif raw_t < 0.15:
            jitter_y = random.uniform(-0.8, 0.8)
        else:
            jitter_y = random.uniform(-0.4, 0.4) if i < steps else 0.0

        points.append((x, y + jitter_y))

    return points


async def solve_datadome_slider(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Detects and smoothly solves the DataDome slider challenge across all frames (main and iframes)
    using humanized Bezier mouse motion with bio-realistic hesitation pauses.
    Performs up to 2 attempts with alternate trajectories before giving up.
    Returns True if solved and verified, False otherwise.
    """
    LOGGER.info("Attempting humanized Drag & Drop on DataDome slider across all frames (up to 2 attempts)...")

    slider_targets = [
        "[role='slider']",
        ".slider-button",
        "#sec-slider",
        ".sec-slider-btn",
        "#sec-slider-button",
        ".slider",
        ".geetest_slider_button",
        "#sec-slider-btn",
        ".captcha-slider-btn",
        "div.sliderBtn",
        "#slider",
        ".tc-slider-normal",
        "div[class*='slider']",
        "div[class*='handle']",
        "button[class*='slider']",
    ]

    track_targets = [
        "#sec-slider-track",
        ".sliderContainer",
        ".slider-track",
        ".track",
        ".sliderWrapper",
        "div[class*='track']",
        "#track",
        ".slider-bg",
        "#sec-cpt-content",
    ]

    for attempt in range(1, 3):
        LOGGER.info(f"Executing DataDome slider solve attempt {attempt}/2...")

        target_handle = None
        target_frame: Optional[Frame] = None

        # Step 1: Scan frames prioritizing captcha-delivery / datadome iframes
        frames_to_check: List[Optional[Frame]] = []
        for frame in page.frames:
            f_url = (frame.url or "").lower()
            if "captcha" in f_url or "datadome" in f_url:
                frames_to_check.insert(0, frame)
            else:
                frames_to_check.append(frame)

        scopes = frames_to_check + [None]

        for scope in scopes:
            context = scope if scope is not None else page
            for sel in slider_targets:
                try:
                    loc = context.locator(sel).first
                    if await loc.is_visible(timeout=300):
                        target_handle = loc
                        target_frame = scope
                        break
                except Exception:
                    continue
            if target_handle:
                break

        # Check explicit iframe if needed
        if not target_handle:
            try:
                iframe_loc = page.locator("iframe[src*='captcha-delivery'], iframe[src*='datadome']").first
                if await iframe_loc.is_visible(timeout=500):
                    c_frame = await iframe_loc.content_frame()
                    if c_frame:
                        for sel in slider_targets:
                            try:
                                loc = c_frame.locator(sel).first
                                if await loc.is_visible(timeout=300):
                                    target_handle = loc
                                    target_frame = c_frame
                                    break
                            except Exception:
                                continue
            except Exception:
                pass

        if not target_handle:
            LOGGER.warning(f"Could not find DataDome slider handle in any page/frames (attempt {attempt}/2).")
            if attempt < 2:
                await asyncio.sleep(0.8)
                continue
            return False

        try:
            try:
                await target_handle.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass

            box_handle = await target_handle.bounding_box()
            if not box_handle or box_handle.get("width", 0) <= 0:
                LOGGER.warning("Found slider handle but bounding box is invalid.")
                if attempt < 2:
                    await asyncio.sleep(0.6)
                    continue
                return False

            # Find track
            search_context = target_frame if target_frame is not None else page
            box_track = None
            for t_sel in track_targets:
                try:
                    t_loc = search_context.locator(t_sel).first
                    if await t_loc.is_visible(timeout=200):
                        candidate_box = await t_loc.bounding_box()
                        if candidate_box and candidate_box.get("width", 0) > box_handle["width"]:
                            box_track = candidate_box
                            break
                except Exception:
                    continue

            start_x = box_handle["x"] + box_handle["width"] / 2.0
            start_y = box_handle["y"] + box_handle["height"] / 2.0

            if box_track:
                # Extra bumper push on attempt 2 to guarantee trigger
                push = random.uniform(3.5, 6.0) if attempt == 2 else random.uniform(2.0, 4.5)
                end_x = (box_track["x"] + box_track["width"]) - (box_handle["width"] / 2.0) + push
            else:
                try:
                    p_width = await target_handle.evaluate(
                        "el => el.parentElement ? el.parentElement.getBoundingClientRect().width : 0"
                    )
                    if p_width and p_width > box_handle["width"] * 1.5:
                        push = random.uniform(3.0, 5.0) if attempt == 2 else random.uniform(2.0, 4.0)
                        end_x = box_handle["x"] + p_width - (box_handle["width"] / 2.0) + push
                    else:
                        distance = random.uniform(315.0, 340.0)
                        end_x = start_x + distance
                except Exception:
                    distance = random.uniform(315.0, 340.0)
                    end_x = start_x + distance

            vp = page.viewport_size or {"width": 1280, "height": 800}
            end_x = min(end_x, vp["width"] - 6)
            end_y = start_y + random.uniform(-1.0, 1.0)

            LOGGER.info(
                f"[Attempt {attempt}/2] Slider drag plan: start=({start_x:.1f}, {start_y:.1f}) -> end=({end_x:.1f}, {end_y:.1f}) "
                f"[Distance: {end_x - start_x:.1f}px]"
            )

            # Ensure focus on handle / iframe
            try:
                await target_handle.focus()
            except Exception:
                pass

            # Hover to handle
            try:
                await target_handle.hover(timeout=1500)
            except Exception:
                await page.mouse.move(start_x, start_y)

            await asyncio.sleep(random.uniform(0.15, 0.28))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.06, 0.14))

            # Bezier points: slightly faster on attempt 2
            steps = random.randint(24, 32) if attempt == 2 else random.randint(28, 38)
            points = _generate_bezier_points(start_x, start_y, end_x, end_y, steps=steps)
            pause_milestone = random.randint(int(steps * 0.4), int(steps * 0.7))

            for idx, (px, py) in enumerate(points):
                await page.mouse.move(px, py)
                if idx == pause_milestone and attempt == 1:
                    await asyncio.sleep(random.uniform(0.012, 0.025))
                else:
                    phase_ratio = idx / steps
                    if phase_ratio < 0.25 or phase_ratio > 0.8:
                        await asyncio.sleep(random.uniform(0.008, 0.018))
                    else:
                        await asyncio.sleep(random.uniform(0.005, 0.012))

            # Lock-in phase
            await page.mouse.move(end_x + random.uniform(1.0, 3.0), end_y)
            await asyncio.sleep(random.uniform(0.06, 0.12))
            await page.mouse.move(end_x, end_y)
            await asyncio.sleep(random.uniform(0.12, 0.22))

            await page.mouse.up()
            LOGGER.info(f"[Attempt {attempt}/2] Completed slider drag motion. Checking validation...")

            # Verification loop
            for _ in range(6):
                await asyncio.sleep(0.5)
                try:
                    if not await target_handle.is_visible(timeout=300):
                        LOGGER.info("DataDome slider cleared successfully!")
                        return True
                except Exception:
                    LOGGER.info("DataDome slider handle detached / solved!")
                    return True

                try:
                    cookies = await page.context.cookies()
                    dd_cookies = [c for c in cookies if "datadome" in c.get("name", "").lower()]
                    if dd_cookies and any(len(c.get("value", "")) > 20 for c in dd_cookies):
                        LOGGER.info(f"Found active DataDome session cookie: {dd_cookies[0]['name']}")
                        return True
                except Exception:
                    pass

            # If not solved and attempt == 1, pause before retry
            if attempt < 2:
                LOGGER.info("Slider not cleared after first attempt. Retrying with alternate curve in 0.6s...")
                await asyncio.sleep(0.6)

        except Exception as exc:
            LOGGER.warning(f"Failed to execute slider drag on attempt {attempt}: {exc}")
            if attempt < 2:
                await asyncio.sleep(0.6)

    # Final check after all attempts
    try:
        if target_handle and not await target_handle.is_visible(timeout=500):
            LOGGER.info("DataDome slider solved on final check!")
            return True
    except Exception:
        return True

    return False
