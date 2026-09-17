"""Playwright CDP connection pool over AdsPower browsers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from src.adspower.client import AdsPowerClient
from src.adspower.profile_manager import AdsPowerProfileManager
from src.config.settings import AppConfig
from src.domain.models import AdsPowerProfile
from src.etix.detector import EtixDetector
from src.utils.logger import LOGGER


@dataclass
class BrowserWorker:
    """Worker encapsulating an AdsPower profile and its Playwright CDP connection."""
    profile: AdsPowerProfile
    browser: Browser
    context: BrowserContext
    page: Page
    worker_index: int


class CDPBrowserPool:
    """Manages concurrent Playwright browser sessions connected to AdsPower."""

    def __init__(
        self,
        config: AppConfig,
        client: AdsPowerClient,
        profile_manager: AdsPowerProfileManager,
        detector: Optional[EtixDetector] = None,
    ) -> None:
        self.config = config
        self.client = client
        self.profile_manager = profile_manager
        self.detector = detector or EtixDetector(config)
        self.playwright: Optional[Playwright] = None
        self.workers: List[BrowserWorker] = []
        self._lock = asyncio.Lock()

    async def validate_worker_connection(
        self,
        worker: BrowserWorker,
        timeout_s: float = 3.0,
    ) -> tuple[bool, str]:
        """
        Validate that the worker's browser has a working proxy and active internet connection.
        Inspects start.adspower.net with up to timeout_s (3.0s) polling:
          - If Proxy failure or Chrome error -> immediate (False, reason)
          - If valid public IPv4 resolved -> immediate (True, ip)
          - If timeout expires with '---.---.---.---' -> (False, "IP detection timed out")
        If start page is blank or not start.adspower.net, runs a lightweight probe ping.
        """
        page = worker.page
        try:
            curr_url = (page.url or "").lower()
            if "start.adspower.net" in curr_url:
                start_time = asyncio.get_running_loop().time()
                while (asyncio.get_running_loop().time() - start_time) < timeout_s:
                    # 1. Check for immediate proxy failure or Chrome error
                    is_failure, reason = await self.detector.is_adspower_proxy_failure(page)
                    if is_failure:
                        return False, reason

                    # 2. Check for resolved public IP
                    resolved_ip = await self.detector.extract_resolved_ip_from_start_page(page)
                    if resolved_ip:
                        return True, f"Verified IP: {resolved_ip}"

                    await asyncio.sleep(0.25)

                # Final check after timeout
                is_failure, reason = await self.detector.is_adspower_proxy_failure(page)
                if is_failure:
                    return False, reason

                resolved_ip = await self.detector.extract_resolved_ip_from_start_page(page)
                if resolved_ip:
                    return True, f"Verified IP: {resolved_ip}"

                return False, "Proxy timed out (IP stayed '---.---.---.---' on start.adspower.net)"

            # Fallback probe if start.adspower.net was not opened (e.g. blank page or custom URL)
            if "chrome-error://" in curr_url or "about:error" in curr_url:
                return False, f"Chrome network error: {curr_url}"

            try:
                probe_resp = await page.request.get("http://cp.cloudflare.com/generate_204", timeout=2500)
                if probe_resp.status in (200, 204):
                    return True, "Probe 204 successful"
                return False, f"Probe returned unexpected status {probe_resp.status}"
            except Exception as probe_err:
                return False, f"Probe connection failed: {probe_err}"

        except Exception as exc:
            return False, f"Health check exception: {exc}"

    async def _initialize_slot(
        self,
        slot_idx: int,
        initial_profile: AdsPowerProfile,
        max_swaps: int = 3,
    ) -> Optional[BrowserWorker]:
        """
        Initialize and validate a single worker slot with up to max_swaps replacements.
        If proxy fails, cleanly closes browser, marks profile failed (without writing to audit files),
        and hot-swaps from group 'Inventory Etix (DO NOT TOUCH)'.
        """
        swaps_done = 0
        current_profile: Optional[AdsPowerProfile] = initial_profile

        while swaps_done <= max_swaps:
            if not current_profile:
                async with self._lock:
                    current_profile = self.profile_manager.get_next_available_reserve()
                if not current_profile:
                    LOGGER.warning(
                        f"[Slot #{slot_idx}] No reserve profiles available in group for replacement."
                    )
                    return None

            LOGGER.info(
                f"[Slot #{slot_idx}] Launching profile '{current_profile.name}' (ID: {current_profile.user_id}, "
                f"attempt {swaps_done + 1}/{max_swaps + 1})..."
            )
            worker = await self._connect_profile(current_profile, worker_index=slot_idx)
            if not worker:
                LOGGER.warning(
                    f"[Slot #{slot_idx}] Failed to connect profile '{current_profile.name}'."
                )
                self.profile_manager.mark_profile_failed(current_profile)
                swaps_done += 1
                current_profile = None
                continue

            # Validate connection (Pre-flight Health Check with 3s threshold)
            is_healthy, status_msg = await self.validate_worker_connection(worker, timeout_s=3.0)
            if is_healthy:
                LOGGER.info(
                    f"[Slot #{slot_idx}] Profile '{current_profile.name}' VERIFIED HEALTHY ({status_msg})."
                )
                return worker

            # Health check failed -> cleanly close browser immediately
            LOGGER.warning(
                f"[Slot #{slot_idx}] Profile '{current_profile.name}' (ID: {current_profile.user_id}) "
                f"FAILED health check: {status_msg}. Closing browser immediately..."
            )
            try:
                await worker.browser.close()
            except Exception:
                pass
            try:
                await self.client.stop_browser(current_profile.user_id)
            except Exception:
                pass
            current_profile.is_open = False
            self.profile_manager.mark_profile_failed(current_profile)
            # NOTE: Do NOT log to bad_proxies.txt or blocked_profiles.txt on startup (user requirement)

            swaps_done += 1
            if swaps_done <= max_swaps:
                async with self._lock:
                    current_profile = self.profile_manager.get_next_available_reserve()
                if current_profile:
                    LOGGER.info(
                        f"[Slot #{slot_idx}] Hot-Swapping with reserve profile '{current_profile.name}' "
                        f"(ID: {current_profile.user_id}, replacement {swaps_done}/{max_swaps})..."
                    )
                else:
                    LOGGER.error(
                        f"[Slot #{slot_idx}] No more reserve profiles available in group for hot-swap."
                    )
                    return None
            else:
                LOGGER.error(
                    f"[Slot #{slot_idx}] Exceeded max replacements ({max_swaps}) for this slot."
                )
                return None

        return None

    async def initialize(self, count_needed: Optional[int] = None) -> List[BrowserWorker]:
        """
        Launch active profiles and establish verified CDP connections in parallel batches.
        Performs Pre-flight Health Check on every profile (3.0s threshold).
        Failing profiles are cleanly closed and replaced up to 3 times per slot
        with fresh reserve profiles from group 'Inventory Etix (DO NOT TOUCH)'.
        """
        if count_needed is not None and count_needed > 0:
            target_count = count_needed
        else:
            free_profiles = self.profile_manager.get_available_free_profiles()
            target_count = len(free_profiles)

        selected_profiles = self.profile_manager.allocate_random_profiles(target_count)
        LOGGER.info(
            f"Allocated {len(selected_profiles)} initial profiles for browser pool (target: {target_count})."
        )

        if not selected_profiles:
            LOGGER.error("No available free profiles in AdsPower group to initialize!")
            return []

        if not self.playwright:
            self.playwright = await async_playwright().start()

        # Launch and validate slots in batches of 3 to avoid overwhelming local system
        batch_size = 3
        verified_workers: List[BrowserWorker] = []

        for i in range(0, len(selected_profiles), batch_size):
            batch = selected_profiles[i : i + batch_size]
            tasks = [
                self._initialize_slot(
                    slot_idx=i + offset + 1,
                    initial_profile=profile,
                    max_swaps=3,
                )
                for offset, profile in enumerate(batch)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, BrowserWorker) and res is not None:
                    verified_workers.append(res)
                elif isinstance(res, Exception):
                    LOGGER.error(f"Slot initialization raised exception: {res}")
            await asyncio.sleep(0.5)

        # Normalize worker indices
        for idx, w in enumerate(verified_workers, 1):
            w.worker_index = idx

        self.workers = verified_workers
        LOGGER.info(
            f"CDP Browser Pool initialization complete: {len(self.workers)}/{target_count} "
            f"confirmed healthy workers ready for checking."
        )
        return self.workers

    async def _connect_profile(
        self,
        profile: AdsPowerProfile,
        worker_index: int,
    ) -> Optional[BrowserWorker]:
        """Start single AdsPower profile and connect via CDP."""
        try:
            ws_url = await self.client.start_browser(
                user_id=profile.user_id,
                open_tabs=1,
                headless=self.config.headless,
            )
            if not ws_url:
                raise RuntimeError(f"AdsPower failed to start profile {profile.user_id}")

            profile.ws_endpoint = ws_url
            profile.is_open = True

            assert self.playwright is not None
            browser = await self.playwright.chromium.connect_over_cdp(
                ws_url,
                slow_mo=self.config.slowmo_ms,
                timeout=self.config.nav_timeout,
            )

            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            
            # Tab sanitization: ensure single active clean page
            pages = context.pages
            if not pages:
                page = await context.new_page()
            else:
                page = pages[0]
                # If AdsPower opened extra blank or start tabs, close them gracefully
                if len(pages) > 1:
                    for extra_page in pages[1:]:
                        try:
                            await extra_page.close()
                        except Exception:
                            pass

            try:
                await page.bring_to_front()
            except Exception:
                pass

            page.set_default_navigation_timeout(self.config.nav_timeout)
            page.set_default_timeout(self.config.click_timeout)

            LOGGER.info(
                f"[Worker #{worker_index}] Connected profile '{profile.name}' (ID: {profile.user_id}) "
                f"via proxy {profile.proxy_key}"
            )

            return BrowserWorker(
                profile=profile,
                browser=browser,
                context=context,
                page=page,
                worker_index=worker_index,
            )
        except Exception as exc:
            LOGGER.error(f"Error connecting profile {profile.user_id}: {exc}")
            return None

    async def replace_worker_with_reserve(
        self,
        failing_worker: BrowserWorker,
        reason: str = "DataDome block",
    ) -> Optional[BrowserWorker]:
        """
        Close a failing worker, mark its proxy bad for this session,
        allocate a reserve profile with a clean good proxy, and start a new worker.
        """
        async with self._lock:
            LOGGER.warning(
                f"Initiating Hot-Swap for Worker #{failing_worker.worker_index} ({failing_worker.profile.user_id}). Reason: {reason}"
            )
            # 1. Record failing proxy as session bad, record blocked profile, and mark profile failed
            if failing_worker.profile.proxy_key:
                self.profile_manager.record_bad_proxy(failing_worker.profile.proxy_key, reason)
            self.profile_manager.record_blocked_profile(failing_worker.profile, reason)
            self.profile_manager.mark_profile_failed(failing_worker.profile)

            # 2. Close failing browser
            try:
                await failing_worker.browser.close()
            except Exception:
                pass
            try:
                await self.client.stop_browser(failing_worker.profile.user_id)
            except Exception:
                pass
            failing_worker.profile.is_open = False

            # 3. Find next reserve profile strictly from target group
            reserve_prof = self.profile_manager.get_next_available_reserve()
            if not reserve_prof:
                LOGGER.error("No reserve profiles available in target group for hot-swap!")
                if failing_worker in self.workers:
                    self.workers.remove(failing_worker)
                return None

            # 4. Connect new reserve profile directly with native AdsPower settings (Strict Immutability)
            LOGGER.info(
                f"Connecting reserve profile '{reserve_prof.name}' ({reserve_prof.user_id}) with native proxy..."
            )
            new_worker = await self._connect_profile(reserve_prof, worker_index=failing_worker.worker_index)
            if not new_worker:
                LOGGER.error(f"Failed to connect reserve profile {reserve_prof.user_id}")
                if failing_worker in self.workers:
                    self.workers.remove(failing_worker)
                return None

            # 5. Pre-flight health check on new reserve worker
            is_healthy, status_msg = await self.validate_worker_connection(new_worker, timeout_s=3.0)
            if not is_healthy:
                LOGGER.warning(
                    f"Reserve profile '{reserve_prof.name}' failed health check ({status_msg}). Closing..."
                )
                try:
                    await new_worker.browser.close()
                except Exception:
                    pass
                try:
                    await self.client.stop_browser(reserve_prof.user_id)
                except Exception:
                    pass
                reserve_prof.is_open = False
                self.profile_manager.mark_profile_failed(reserve_prof)
                if failing_worker in self.workers:
                    self.workers.remove(failing_worker)
                return None

            # 6. Update workers list
            if failing_worker in self.workers:
                idx = self.workers.index(failing_worker)
                self.workers[idx] = new_worker

            LOGGER.info(
                f"Hot-swap complete! Worker #{failing_worker.worker_index} is now profile '{reserve_prof.name}' ({reserve_prof.user_id})"
            )
            return new_worker

    async def stop_and_remove_worker(self, worker: BrowserWorker, reason: str = "") -> None:
        """
        Safely close browser, stop AdsPower instance, mark profile failed,
        and remove worker from active pool.
        """
        async with self._lock:
            LOGGER.warning(
                f"Stopping and removing worker #{worker.worker_index} ({worker.profile.name}, {worker.profile.user_id}). Reason: {reason}"
            )
            try:
                await worker.browser.close()
            except Exception:
                pass
            try:
                await self.client.stop_browser(worker.profile.user_id)
            except Exception:
                pass
            worker.profile.is_open = False
            self.profile_manager.mark_profile_failed(worker.profile)
            if worker in self.workers:
                self.workers.remove(worker)

    async def close_all(self) -> None:
        """
        Gracefully close all browser connections and stop ONLY the AdsPower instances
        opened by this session (Strict Multi-User Concurrency ownership).
        """
        LOGGER.info(f"Closing {len(self.workers)} CDP Browser workers owned by this session...")
        for worker in self.workers:
            try:
                await worker.browser.close()
            except Exception:
                pass
            try:
                await self.client.stop_browser(worker.profile.user_id)
            except Exception:
                pass
            worker.profile.is_open = False
            self.profile_manager.release_profile(worker.profile)

        self.workers.clear()

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        LOGGER.info("All session browser workers closed successfully.")
