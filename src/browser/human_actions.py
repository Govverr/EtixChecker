"""Human interaction helpers, dialog dismissers, and DataDome slider solvers."""

from __future__ import annotations

import asyncio
import math
import random
import re
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

    # Extra check: Etix venue tax scheme cart conflict
    try:
        await handle_empty_shopping_cart_conflict(page, timeout_ms=250)
    except Exception:
        pass


def _extract_perf_id(url: str) -> Optional[str]:
    """Extract performance ID from URL."""
    m = re.search(r"/(?:p|e)/(\d+)", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"performance_id=(\d+)", url, re.I)
    if m:
        return m.group(1)
    return None


async def handle_empty_shopping_cart_conflict(
    page: Page,
    target_url: Optional[str] = None,
    timeout_ms: int = 1500,
) -> bool:
    """
    Detect and resolve Etix venue tax scheme conflict:
    'You requested venue has different tax scheme with the venue in cart.
     Please click the button to go back, or click the link of View Shopping Cart to view shopping cart info,
     or click the link of Empty Shopping Cart to remove the tickets from your cart and select another performance or package to buy tickets.'
    
    Clicks 'Empty Shopping Cart' link to clear the cart and proceed to the site.
    """
    try:
        empty_cart_union = (
            "a:has-text('Empty Shopping Cart'), "
            "a:has-text('Empty shopping cart'), "
            "button:has-text('Empty Shopping Cart'), "
            "a[href*='emptyShoppingCart'], "
            "a[href*='emptyCart']"
        )
        empty_cart_link = page.locator(empty_cart_union).first
        is_link_visible = False
        try:
            is_link_visible = await empty_cart_link.is_visible(timeout=timeout_ms)
        except Exception:
            pass

        tax_conflict_visible = False
        if not is_link_visible:
            try:
                tax_text = page.locator(
                    "text=/different tax scheme/i, text=/tax scheme with the venue in cart/i"
                ).first
                tax_conflict_visible = await tax_text.is_visible(timeout=300)
            except Exception:
                pass

        if not is_link_visible and not tax_conflict_visible:
            return False

        LOGGER.warning(
            "Detected Etix venue tax scheme / cart conflict ('different tax scheme with the venue in cart'). "
            "Clicking 'Empty Shopping Cart' to clear cart and enter site..."
        )

        # Auto-accept JavaScript confirmation dialogs if triggered by click
        def _handle_dialog(dialog):
            asyncio.create_task(dialog.accept())

        page.once("dialog", _handle_dialog)

        clicked = False
        if is_link_visible:
            try:
                await empty_cart_link.click(timeout=3000)
                clicked = True
            except Exception as click_err:
                LOGGER.debug(f"Direct click on 'Empty Shopping Cart' link failed: {click_err}")

        if not clicked:
            try:
                fallback_loc = page.locator("text='Empty Shopping Cart'").first
                if await fallback_loc.is_visible(timeout=1000):
                    await fallback_loc.click(timeout=3000)
                    clicked = True
            except Exception:
                pass

        if not clicked:
            try:
                href_loc = page.locator("a[href*='empty']").first
                if await href_loc.is_visible(timeout=1000):
                    await href_loc.click(timeout=3000)
                    clicked = True
            except Exception:
                pass

        if not clicked:
            LOGGER.error("Failed to click 'Empty Shopping Cart' link on tax scheme conflict page.")
            return False

        # Wait for page navigation and DOM hydration
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=7000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        # Edge-case safety: ensure page lands on target show URL
        if target_url:
            curr_url = page.url.lower()
            perf_id = _extract_perf_id(target_url)
            on_target = False
            if perf_id and (f"/p/{perf_id}" in curr_url or f"/{perf_id}" in curr_url or perf_id in curr_url):
                on_target = True
            elif target_url.lower() in curr_url:
                on_target = True

            if not on_target:
                try:
                    body_lower = (await page.inner_text("body", timeout=1000)).lower()
                    if "tax scheme" in body_lower or "cart" in curr_url or "empty" in curr_url:
                        LOGGER.info(f"Navigating to target event URL after emptying cart: {target_url}...")
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

        LOGGER.info("Cleared cart and entered site successfully via 'Empty Shopping Cart'.")
        return True
    except Exception as exc:
        LOGGER.warning(f"Exception while handling tax scheme cart conflict: {exc}")
        return False


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


async def find_recaptcha_challenge_frame(page: Page) -> Optional[Frame]:
    """Find the active Google reCAPTCHA v2 challenge bframe across page frames."""
    for frame in page.frames:
        f_url = (frame.url or "").lower()
        if "recaptcha" in f_url and ("bframe" in f_url or "challenge" in f_url):
            return frame
        try:
            reload_btn = frame.locator("#recaptcha-reload-button, .rc-button-reload").first
            if await reload_btn.is_visible(timeout=100):
                return frame
        except Exception:
            continue

    try:
        iframe_loc = page.locator(
            "iframe[src*='recaptcha/api2/bframe'], "
            "iframe[src*='recaptcha/enterprise/bframe'], "
            "iframe[title*='recaptcha challenge']"
        ).first
        if await iframe_loc.is_visible(timeout=300):
            c_frame = await iframe_loc.content_frame()
            if c_frame:
                return c_frame
    except Exception:
        pass
    return None


async def is_recaptcha_challenge_visible(page: Page) -> bool:
    """Check whether a Google reCAPTCHA image challenge popup is currently visible."""
    frame = await find_recaptcha_challenge_frame(page)
    if frame:
        try:
            img_chal = frame.locator(
                "#rc-imageselect, .rc-imageselect-desc-wrapper, #recaptcha-reload-button, button#solver-button"
            ).first
            if await img_chal.is_visible(timeout=300):
                return True
        except Exception:
            pass

    try:
        iframe_loc = page.locator(
            "iframe[src*='recaptcha/api2/bframe'], "
            "iframe[src*='recaptcha/enterprise/bframe'], "
            "iframe[title*='recaptcha challenge']"
        ).first
        if await iframe_loc.is_visible(timeout=300):
            return True
    except Exception:
        pass

    return False


async def solve_recaptcha_challenge(page: Page) -> Tuple[bool, str]:
    """
    Automated solver for Google reCAPTCHA v2 according to exact requirements:
    1. Click Reload ('↺') button strictly once inside challenge frame.
    2. Wait 2.0s to check if challenge resolves/disappears.
    3. If still open/changed: click the 'человечек' Buster solver button (#solver-button).
    4. Wait up to 10 seconds for resolution.
    Returns: (is_solved, detail_message)
    """
    frame = await find_recaptcha_challenge_frame(page)
    if not frame:
        if not await is_recaptcha_challenge_visible(page):
            return True, "no_recaptcha_present"
        await asyncio.sleep(0.5)
        frame = await find_recaptcha_challenge_frame(page)
        if not frame:
            return False, "challenge_frame_unreachable"

    LOGGER.info("Detected Google reCAPTCHA challenge. Step 1: Clicking Reload button ('↺') strictly once...")

    # Step 1: Reload button
    reload_selectors = [
        "#recaptcha-reload-button",
        "button#recaptcha-reload-button",
        ".rc-button-reload",
        "button[title*='new challenge' i]",
        "button[title*='challenge' i]",
        "button[id*='reload']",
    ]
    reload_clicked = False
    for sel in reload_selectors:
        try:
            r_btn = frame.locator(sel).first
            if await r_btn.is_visible(timeout=400):
                await r_btn.scroll_into_view_if_needed(timeout=1000)
                await r_btn.click(timeout=1500)
                reload_clicked = True
                LOGGER.info(f"Clicked reCAPTCHA reload button ('↺') via {sel}.")
                break
        except Exception:
            continue

    if not reload_clicked:
        LOGGER.warning("Could not find reCAPTCHA reload button in challenge frame.")

    # Wait 2.0s to check if challenge closed
    await asyncio.sleep(2.0)
    if not await is_recaptcha_challenge_visible(page):
        LOGGER.info("reCAPTCHA challenge cleared successfully after Reload button click!")
        return True, "cleared_via_reload"

    # Step 2: If captcha still visible -> Click 'человечек' (Buster solver button)
    LOGGER.info("Captcha still visible after Reload. Step 2: Clicking Buster solver button ('человечек')...")
    solver_selectors = [
        "#solver-button",
        "button#solver-button",
        ".help-button-holder button",
        "button[title*='Solve' i]",
        "button[title*='Buster' i]",
        "div.button-holder:nth-child(3) button",
        ".rc-controls div.button-holder:nth-child(3) button",
        "button.rc-button-default#solver-button",
    ]
    solver_clicked = False
    for sel in solver_selectors:
        try:
            s_btn = frame.locator(sel).first
            if await s_btn.is_visible(timeout=500):
                await s_btn.scroll_into_view_if_needed(timeout=1000)
                await s_btn.click(timeout=1500)
                solver_clicked = True
                LOGGER.info(f"Clicked solver button ('человечек') via {sel}.")
                break
        except Exception:
            continue

    if not solver_clicked:
        # Fallback by position in footer: Buster is typically the 3rd button
        try:
            footer_buttons = frame.locator(".rc-footer button, .rc-controls button, .button-holder button")
            count = await footer_buttons.count()
            if count >= 3:
                s_btn = footer_buttons.nth(2)
                await s_btn.click(timeout=1500)
                solver_clicked = True
                LOGGER.info("Clicked solver button ('человечек') by position index 2 in footer.")
        except Exception as exc:
            LOGGER.debug(f"Fallback solver click error: {exc}")

    if not solver_clicked:
        LOGGER.warning("Could not locate solver button ('человечек') in reCAPTCHA challenge frame.")

    # Wait up to 10 seconds for Buster to solve
    LOGGER.info("Awaiting reCAPTCHA Buster solver resolution (up to 10 seconds)...")
    for _ in range(20):  # 20 * 0.5s = 10.0s
        await asyncio.sleep(0.5)
        if not await is_recaptcha_challenge_visible(page):
            LOGGER.info("reCAPTCHA challenge cleared successfully after clicking solver ('человечек')!")
            return True, "cleared_via_solver"

    LOGGER.warning("reCAPTCHA challenge was not cleared after Reload and Solver ('человечек').")
    return False, "not_cleared_after_solver"

