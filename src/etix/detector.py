"""Detectors for Etix page states, sold out banners, DataDome challenges, and dead proxy errors."""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from playwright.async_api import Page

from src.config.settings import AppConfig
from src.utils.logger import LOGGER


class EtixDetector:
    """Detects Sold Out, Sales Ended, DataDome Blocked, Bad Proxy, and Inventory Error states."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def is_bad_proxy_error(self, exc: Exception) -> bool:
        """Check if exception was caused by expired, dead, or timed out proxy."""
        err_msg = str(exc).lower()
        bad_patterns = [
            "net::err_proxy_connection_failed",
            "net::err_connection_timed_out",
            "net::err_tunnel_connection_failed",
            "net::err_proxy_auth_requested",
            "net::err_timed_out",
            "net::err_connection_reset",
            "net::err_connection_refused",
            "net::err_connection_closed",
            "net::err_name_not_resolved",
            "net::err_empty_response",
            "timeout",
            "timeouterror",
            "proxy connection failed",
            "proxy authentication required",
            "connection closed",
            "connection reset",
        ]
        return any(p in err_msg for p in bad_patterns)

    async def is_adspower_proxy_failure(self, page: Page) -> tuple[bool, str]:
        """Check if AdsPower start page or browser tab displays proxy failure or network error."""
        try:
            raw_url = page.url if isinstance(getattr(page, "url", None), str) else ""
            url = raw_url.lower()
            if "chrome-error://" in url or "about:error" in url:
                return True, "Chrome network error URL"

            body_text = await page.inner_text("body", timeout=1000)
            body_lower = body_text.lower()

            failure_phrases = [
                "proxy failure",
                "didn't passed the ip checker",
                "didn't pass the ip checker",
                "meets the proxy service provider's conditions",
                "err_proxy_connection_failed",
                "err_connection_timed_out",
                "err_tunnel_connection_failed",
                "this site can't be reached",
                "страница недоступна",
                "нет подключения к интернету",
                "no internet",
            ]
            for phrase in failure_phrases:
                if phrase in body_lower:
                    return True, f"Proxy failure detected ({phrase})"

            return False, ""
        except Exception as exc:
            return False, str(exc)

    async def extract_resolved_ip_from_start_page(self, page: Page) -> Optional[str]:
        """Extract confirmed resolved public IPv4 address from start.adspower.net."""
        try:
            body_text = await page.inner_text("body", timeout=1000)
            # Find all IPv4 addresses
            ips = re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", body_text)
            for ip in ips:
                # Exclude localhost, zero, and private/internal subnets if applicable
                parts = [int(p) for p in ip.split(".")]
                if all(0 <= p <= 255 for p in parts):
                    if ip not in ("127.0.0.1", "0.0.0.0") and not ip.startswith("127."):
                        return ip
            return None
        except Exception:
            return None

    async def is_bad_proxy_page(self, page: Page) -> bool:
        """Check if current page loaded Chrome network error or connection failure."""
        try:
            raw_url = page.url if isinstance(getattr(page, "url", None), str) else ""
            url = raw_url.lower()
            if "chrome-error://" in url or "about:error" in url:
                return True

            body_text = await page.inner_text("body", timeout=1000)
            error_phrases = [
                "proxy failure",
                "didn't passed the ip checker",
                "didn't pass the ip checker",
                "err_proxy_connection_failed",
                "err_connection_timed_out",
                "err_tunnel_connection_failed",
                "this site can't be reached",
                "страница недоступна",
                "нет подключения к интернету",
                "no internet",
                "proxy error",
                "bad gateway",
                "502 bad gateway",
                "504 gateway time-out",
            ]
            return any(phrase in body_text.lower() for phrase in error_phrases)
        except Exception:
            return False

    async def switch_to_price_level_if_seating_chart(self, page: Page, wait_timeout_ms: int = 7000) -> bool:
        """
        Check if the page has both 'Seating Chart' and 'Price Level' tabs.
        If present and not yet active, switch to 'Price Level' tab to reveal ticket selectors.
        Waits for tab panel to render ticket controls.
        """
        try:
            # Fast pre-check: if page is blocked, network broken, or in tax scheme conflict, do not wait for tabs
            if await self.is_blocked_page(page) or await self.is_bad_proxy_page(page) or await self.is_tax_scheme_conflict(page):
                return False

            # If the page already has visible ticket selectors and no tabs, return immediately
            has_direct_controls = await page.locator(
                ".smoketest-ticket-quantity [role='combobox'], select[name*='quantity']"
            ).first.is_visible()
            if has_direct_controls and not await page.locator(
                "a.ui-tabs-anchor:has-text('Price Level'), a.ui-tabs-anchor:has-text('Best Available')"
            ).first.is_visible():
                return False

            pl_loc = page.locator(
                "a.ui-tabs-anchor:has-text('Price Level'), a:has-text('Price Level'), "
                "button:has-text('Price Level'), [role='tab']:has-text('Price Level'), "
                "li:has-text('Price Level'), "
                "a.ui-tabs-anchor:has-text('Best Available'), a:has-text('Best Available'), "
                "button:has-text('Best Available'), [role='tab']:has-text('Best Available'), "
                "li:has-text('Best Available')"
            ).first
            sc_loc = page.locator(
                "a.ui-tabs-anchor:has-text('Seating Chart'), a:has-text('Seating Chart'), "
                "button:has-text('Seating Chart'), [role='tab']:has-text('Seating Chart'), "
                "li:has-text('Seating Chart')"
            ).first

            # Robust wait for tabs to mount in DOM
            tabs_found = False
            try:
                if hasattr(pl_loc, "wait_for"):
                    await pl_loc.wait_for(state="visible", timeout=wait_timeout_ms)
                if hasattr(sc_loc, "wait_for"):
                    await sc_loc.wait_for(state="visible", timeout=1500)
                tabs_found = True
            except Exception:
                pass

            if not tabs_found:
                try:
                    if await pl_loc.is_visible() and await sc_loc.is_visible():
                        tabs_found = True
                except Exception:
                    pass

            if not tabs_found:
                return False

            is_active = await pl_loc.evaluate("""el => {
                const li = el.closest('li') || el.parentElement || el;
                return li.classList.contains('ui-state-active') ||
                       li.classList.contains('ui-tabs-selected') ||
                       li.classList.contains('active') ||
                       li.classList.contains('selected') ||
                       el.getAttribute('aria-selected') === 'true';
            }""")
            if not is_active:
                LOGGER.info("Detected Seating Chart / Price Level tabs. Switching to 'Price Level' view...")
                try:
                    await pl_loc.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                await pl_loc.click()

                # Wait for tab panel to render ticket controls
                try:
                    await page.wait_for_selector(
                        ".smoketest-ticket-quantity, [role='combobox'], .MuiSelect-select, select, button:has-text('Add Tickets')",
                        timeout=2500,
                    )
                except Exception:
                    await asyncio.sleep(0.5)

                LOGGER.info("Switched to 'Price Level' tab successfully.")
                return True
            else:
                return True
        except Exception as exc:
            LOGGER.debug(f"Price Level tab check: {exc}")
        return False

    async def is_recaptcha_challenge_present(self, page: Page) -> bool:
        """Check whether a Google reCAPTCHA v2 challenge popup is currently active."""
        try:
            from src.browser.human_actions import is_recaptcha_challenge_visible
            return await is_recaptcha_challenge_visible(page)
        except Exception:
            return False

    async def has_active_ticket_controls(self, page: Page) -> bool:
        """Check whether page has visible, active ticket quantity selectors."""
        try:
            # Switch to Price Level first if page has Seating Chart tabs
            await self.switch_to_price_level_if_seating_chart(page)

            controls = page.locator(
                ".smoketest-ticket-quantity [role='combobox'], "
                ".smoketest-ticket-quantity .MuiSelect-select, "
                "[role='combobox'], "
                ".MuiSelect-select, "
                "select"
            )
            count = await controls.count()
            for i in range(count):
                ctrl = controls.nth(i)
                try:
                    if not await ctrl.is_visible(timeout=200):
                        continue
                    if await ctrl.is_disabled():
                        continue

                    # Filter out price/section selection dropdowns
                    el_id = (await ctrl.get_attribute("id") or "").strip().lower()
                    el_name = (await ctrl.get_attribute("name") or "").strip().lower()
                    if "selection" in el_id or any(k in el_name for k in ["priceselection", "price_level", "pricecode"]):
                        continue

                    tag = await ctrl.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        opts = await ctrl.locator("option").all_inner_texts()
                        has_positive_qty = any(o.strip().isdigit() and int(o.strip()) > 0 for o in opts)
                        if has_positive_qty:
                            return True
                    else:
                        # Material-UI combobox
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def is_soldout_page(self, page: Page) -> bool:
        """Check whether the page indicates the event is Sold Out."""
        # Edge Case Safeguard: if active ticket controls are visible on the page, the show is available
        if await self.has_active_ticket_controls(page):
            return False

        for selector in self.config.sold_out_banner_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue

        try:
            body_text = await page.inner_text("body", timeout=1500)
            for pattern in self.config.sold_out_text_patterns:
                if re.search(pattern, body_text, flags=re.I):
                    return True
        except Exception:
            pass

        return False

    async def is_event_ended_page(self, page: Page) -> bool:
        """Check whether sales for this event have ended."""
        # Edge Case Safeguard: if active ticket controls are visible, sales have not ended
        if await self.has_active_ticket_controls(page):
            return False

        for selector in self.config.ended_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue

        try:
            body_text = await page.inner_text("body", timeout=1500)
            for pattern in self.config.ended_text_patterns:
                if re.search(pattern, body_text, flags=re.I):
                    return True
        except Exception:
            pass

        return False

    async def is_blocked_page(self, page: Page) -> bool:
        """
        Check whether DataDome returned 'Access Temporarily Blocked' in main page or any iframe.
        Safety Invariant: If an interactive slider captcha is active or solved, it is NOT an un-solvable hard block.
        """
        # 0. Safety Invariant: Check if interactive slider captcha is present first!
        if await self.is_slider_captcha(page):
            return False

        # If DataDome success state is currently visible, page is unlocking, NOT blocked
        try:
            for frame in page.frames:
                if await frame.locator(".slider-success, .captcha-success").first.is_visible(timeout=50):
                    return False
            if await page.locator(".slider-success, .captcha-success").first.is_visible(timeout=50):
                return False
        except Exception:
            pass


        try:
            # Check page title first (strict check - avoid matching generic words)
            title = await page.title()
            for pattern in self.config.blocked_text_patterns:
                if re.search(pattern, title, flags=re.I):
                    return True
        except Exception:
            pass

        # Fast selector check for block headers in DOM (<10ms)
        try:
            block_hdr = page.locator(
                "h1:has-text('Access Temporarily Blocked'), h2:has-text('Access Temporarily Blocked'), "
                "p:has-text('Access Temporarily Blocked'), title:has-text('Access Temporarily Blocked'), "
                "text='Access Temporarily Blocked', "
                "text=/To help protect our platform from automated traffic/i, "
                "text=/our security systems have temporarily restricted access/i"
            ).first
            if await block_hdr.is_visible(timeout=150):
                return True
        except Exception:
            pass

        # Check all frames (main page + iframes, excluding captcha challenge frames)
        for frame in page.frames:
            try:
                f_url = (frame.url or "").lower()
                if "captcha-delivery.com" in f_url or "datadome.co" in f_url:
                    continue
                body_text = await frame.inner_text("body", timeout=400)
                for pattern in self.config.blocked_text_patterns:
                    if re.search(pattern, body_text, flags=re.I):
                        return True
            except Exception:
                continue
        return False

    async def is_slider_captcha(self, page: Page) -> bool:
        """
        Check whether DataDome is actively displaying an UNSOLVED slider challenge in the main frame,
        nested iframes (e.g. captcha-delivery.com), or shadow roots.
        Returns False if the slider was already solved (.slider-success / .captcha-success)
        or if no interactive slider handle/container is currently active.
        """
        # 0. Early exit: If challenge was already solved, it is NOT an active challenge!
        try:
            for frame in page.frames:
                try:
                    if await frame.locator(".slider-success, .captcha-success").first.is_visible(timeout=50):
                        return False
                except Exception:
                    continue
            if await page.locator(".slider-success, .captcha-success").first.is_visible(timeout=50):
                return False
        except Exception:
            pass

        slider_union = (
            "[role='slider'], .slider-button, #sec-slider, .sec-slider-btn, "
            ".slider, .geetest_slider_button, #sec-slider-btn, .captcha-slider-btn, "
            "div.sliderBtn, #slider, .tc-slider-normal, .sliderContainer, #ddv1-captcha-container"
        )

        # 1. Check DataDome captcha frames in page.frames: must have an active handle or sliderContainer
        for frame in page.frames:
            try:
                f_url = (frame.url or "").lower()
                if "captcha-delivery.com" in f_url or "datadome.co" in f_url:
                    # Verify if frame contains an active handle or container
                    f_loc = frame.locator(slider_union).first
                    if await f_loc.is_visible(timeout=200):
                        return True
                    # Check text patterns in frame body
                    body_text = await frame.inner_text("body", timeout=200)
                    for pattern in self.config.slider_captcha_patterns:
                        if re.search(pattern, body_text, flags=re.I):
                            return True
            except Exception:
                continue

        # 2. Check for explicit DataDome iframe element in DOM
        try:
            cpt_iframe = page.locator("iframe[src*='captcha-delivery.com'], iframe[src*='datadome'], iframe[title*='DataDome']").first
            if await cpt_iframe.is_visible(timeout=150):
                c_frame = await cpt_iframe.content_frame()
                if c_frame:
                    if await c_frame.locator(slider_union).first.is_visible(timeout=150):
                        return True
                    body_t = await c_frame.inner_text("body", timeout=150)
                    for pattern in self.config.slider_captcha_patterns:
                        if any(re.search(pat, body_t, flags=re.I) for pat in self.config.slider_captcha_patterns):
                            return True
        except Exception:
            pass

        # 3. Check for visible slider handles across main page and frames
        try:
            loc = page.locator(slider_union).first
            if await loc.is_visible(timeout=150):
                return True
        except Exception:
            pass

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                f_loc = frame.locator(slider_union).first
                if await f_loc.is_visible(timeout=150):
                    return True
            except Exception:
                continue

        # 4. Text patterns check across all frames
        for frame in page.frames:
            try:
                body_text = await frame.inner_text("body", timeout=200)
                for pattern in self.config.slider_captcha_patterns:
                    if re.search(pattern, body_text, flags=re.I):
                        return True
            except Exception:
                continue

        return False

    def is_inventory_message(self, text: str) -> bool:
        """Check whether an alert text indicates inventory exhaustion / insufficient tickets."""
        for pattern in self.config.inventory_error_patterns:
            if re.search(pattern, text, flags=re.I):
                return True
        return False

    async def is_cart_page(self, page: Page) -> bool:
        """Check whether page navigated to Shopping Cart / Review step."""
        url = (page.url or "").lower()
        cart_keywords = ["cart", "shoppingcart", "viewshoppingcart", "checkout", "review", "basket"]
        if any(k in url for k in cart_keywords):
            return True
        try:
            cart_selectors = (
                ".cart-item, #cart-container, .order-summary, table.cart, #shopping-cart, .shoppingCart, "
                "[class*='cart-item'], .cart-table, .cart-ticket-item, .cart-header, "
                "a:has-text('Clear Shopping Cart'), button:has-text('Clear Shopping Cart'), "
                "a:has-text('Empty Shopping Cart'), button:has-text('Empty Shopping Cart'), "
                "a:has-text('Clear Cart'), button:has-text('Clear Cart'), "
                "a:has-text('Checkout'), button:has-text('Checkout'), input[value*='Checkout'], "
                "h1:has-text('Shopping Cart'), h2:has-text('Shopping Cart'), div:has-text('Shopping Cart')"
            )
            has_cart_elem = await page.locator(cart_selectors).first.is_visible(timeout=250)
            return bool(has_cart_elem)
        except Exception:
            return False

    async def is_tax_scheme_conflict(self, page: Page) -> bool:
        """Check whether page displays Etix tax scheme conflict / empty cart error."""
        try:
            loc = page.locator(
                "a:has-text('Empty Shopping Cart'), "
                "text=/different tax scheme/i, "
                "text=/tax scheme with the venue in cart/i"
            ).first
            return await loc.is_visible(timeout=250)
        except Exception:
            return False

    async def is_captcha_required_error(self, page: Page) -> bool:
        """
        Check whether page displays Etix captcha error:
        - SYS-BS-004: 'Response to CAPTCHA is required. Please go back and try again.'
        - SYS-BS-003: 'The CAPTCHA has expired. Please click the button and try a new CAPTCHA.'
        """
        try:
            err_loc = page.locator(
                "text=/Response to CAPTCHA is required/i, "
                "text=/The CAPTCHA has expired/i, "
                "text=/try a new CAPTCHA/i, "
                "text=/SYS-BS-004/i, "
                "text=/SYS-BS-003/i, "
                "text=/Status Code:\\s*SYS-BS-(003|004)/i"
            ).first
            if await err_loc.is_visible(timeout=200):
                return True
        except Exception:
            pass

        try:
            body_text = await page.inner_text("body", timeout=300)
            if any(k in body_text for k in [
                "SYS-BS-004", "SYS-BS-003",
                "Response to CAPTCHA is required",
                "The CAPTCHA has expired",
                "try a new CAPTCHA",
            ]):
                return True
        except Exception:
            pass

        return False

