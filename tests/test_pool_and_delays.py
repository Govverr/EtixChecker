"""Unit tests verifying pool enhancements, concurrency detection, delay validation, and URL handling."""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adspower.client import AdsPowerClient
from src.adspower.profile_manager import AdsPowerProfileManager
from src.config.settings import (
    MIN_SAFE_DELAYS,
    validate_and_clamp_delay,
    estimate_check_duration_seconds,
    AppConfig,
)
from src.domain.enums import ProfileRole, ShowStatus
from src.domain.models import AdsPowerProfile, CheckResult, Show
from src.etix.checker import extract_performance_id
from src.etix.detector import EtixDetector


class TestPoolAndDelays(unittest.IsolatedAsyncioTestCase):
    """Test suite for new architectural enhancements."""

    def test_extract_performance_id(self):
        """Verify performance_id extraction from various Etix URL formats."""
        test_cases = [
            ("https://www.etix.com/ticket/p/67711700/waylon-wyatt-dustpiles-world-tour-eugene-mcdonald-theatre", "67711700"),
            ("https://www.etix.com/ticket/p/41746075/sawyer-hill-carrboro-cats-cradle", "41746075"),
            ("https://www.etix.com/ticket/p/99385382/kevin-atwater-blush-red-tourwith-jessie-mazin-carrboro-cats-cradle", "99385382"),
            ("https://www.etix.com/ticket/p/94701607/julia-jacklinwith-jordan-patterson-carrboro-cats-cradle", "94701607"),
            ("https://www.etix.com/ticket/online/performanceSale.do?performance_id=35196855&method=restoreToken", "35196855"),
            ("https://www.etix.com/ticket/p/87677793/gasolina-party?partner_id=100&cobrand=test", "87677793"),
            ("https://www.etix.com/ticket/e/1043831/rock-festival", "1043831"),
        ]
        for url, expected_id in test_cases:
            extracted = extract_performance_id(url)
            self.assertEqual(extracted, expected_id, f"Failed on URL: {url}")

    def test_delay_validation_and_clamping(self):
        """Verify hardware-level delay thresholds and safety buffers."""
        # 1. Below threshold -> must clamp to MIN_SAFE_DELAYS
        self.assertEqual(validate_and_clamp_delay("batch_nav_delay_ms", 100.0), MIN_SAFE_DELAYS["batch_nav_delay_ms"])
        self.assertEqual(validate_and_clamp_delay("after_click_sleep_ms", 200.0), MIN_SAFE_DELAYS["after_click_sleep_ms"])
        self.assertEqual(validate_and_clamp_delay("add_sequential_delay_ms", 300.0), MIN_SAFE_DELAYS["add_sequential_delay_ms"])
        self.assertEqual(validate_and_clamp_delay("delay_before_clear_carts_s", 1.0), MIN_SAFE_DELAYS["delay_before_clear_carts_s"])
        self.assertEqual(validate_and_clamp_delay("clear_cart_stagger_ms", 150.0), MIN_SAFE_DELAYS["clear_cart_stagger_ms"])
        self.assertEqual(validate_and_clamp_delay("nav_timeout", 5000.0), MIN_SAFE_DELAYS["nav_timeout"])

        # 2. Above threshold -> must preserve user setting
        self.assertEqual(validate_and_clamp_delay("batch_nav_delay_ms", 800.0), 800.0)
        self.assertEqual(validate_and_clamp_delay("delay_before_clear_carts_s", 5.0), 5.0)

        # 3. Minimum safety buffers are enforced
        self.assertGreaterEqual(MIN_SAFE_DELAYS["batch_nav_delay_ms"], 350.0)
        self.assertGreaterEqual(MIN_SAFE_DELAYS["after_click_sleep_ms"], 500.0)
        self.assertGreaterEqual(MIN_SAFE_DELAYS["add_sequential_delay_ms"], 900.0)
        self.assertGreaterEqual(MIN_SAFE_DELAYS["clear_cart_stagger_ms"], 400.0)

    def test_estimate_duration(self):
        """Verify duration estimator returns realistic values."""
        cfg = AppConfig()
        est = estimate_check_duration_seconds(12, cfg)
        self.assertGreater(est, 30.0)
        self.assertLess(est, 65.0)

    async def test_dynamic_pool_and_concurrency_detection(self):
        """Verify dynamic pool sizing, multi-user concurrency locks, and strict hot-swap."""
        mock_client = AsyncMock(spec=AdsPowerClient)

        # Mock 20 profiles in AdsPower group "Inventory Etix (DO NOT TOUCH)"
        mock_profiles_data = [
            {
                "user_id": f"prof_{i}",
                "name": f"Profile #{i:02d}",
                "serial_number": str(100 + i),
                "group_id": "grp_etix_001",
                "group_name": "Inventory Etix (DO NOT TOUCH)",
                "user_proxy_config": {
                    "proxy_host": "1.2.3.4",
                    "proxy_port": "8080",
                    "proxy_user": "u",
                    "proxy_password": "p",
                    "proxy_type": "http",
                },
            }
            for i in range(1, 21)
        ]
        mock_client.get_profiles_by_group.return_value = mock_profiles_data

        # Mock 3 profiles opened externally by another user
        mock_client.get_active_browsers.return_value = ["prof_2", "prof_5", "prof_11"]

        mock_backup = MagicMock()
        manager = AdsPowerProfileManager(
            client=mock_client,
            backup_service=mock_backup,
            good_proxies_file=Path("data/test_good.txt"),
            bad_proxies_file=Path("data/test_bad.txt"),
        )

        loaded = await manager.load_and_organize_profiles(group_name="Inventory Etix (DO NOT TOUCH)")
        self.assertEqual(len(loaded), 20)

        # Check concurrency detection
        busy = manager.get_busy_external_profiles()
        self.assertEqual(len(busy), 3)
        busy_uids = {p.user_id for p in busy}
        self.assertEqual(busy_uids, {"prof_2", "prof_5", "prof_11"})

        free = manager.get_available_free_profiles()
        self.assertEqual(len(free), 17)
        for p in free:
            self.assertNotIn(p.user_id, busy_uids)
            self.assertEqual(p.role, ProfileRole.RESERVE)

        # Random sample allocation
        allocated = manager.allocate_random_profiles(5)
        self.assertEqual(len(allocated), 5)
        for p in allocated:
            self.assertEqual(p.role, ProfileRole.IN_USE)

        # Remaining free
        remaining_free = manager.get_available_free_profiles()
        self.assertEqual(len(remaining_free), 12)

        # Strict Hot-Swap inside target group
        reserve = manager.get_next_available_reserve()
        self.assertIsNotNone(reserve)
        self.assertEqual(reserve.group_name, "Inventory Etix (DO NOT TOUCH)")
        self.assertEqual(reserve.role, ProfileRole.IN_USE)
        self.assertNotIn(reserve.user_id, busy_uids)

    def _create_mock_page(self, body_text: str = "", visible_selectors: list = None, has_controls: bool = False):
        page = AsyncMock()
        visible_selectors = visible_selectors or []
        page.inner_text = AsyncMock(return_value=body_text)

        controls_locator = MagicMock()
        if has_controls:
            controls_locator.count = AsyncMock(return_value=1)
            ctrl = AsyncMock()
            ctrl.is_visible = AsyncMock(return_value=True)
            ctrl.is_disabled = AsyncMock(return_value=False)
            ctrl.get_attribute = AsyncMock(return_value="ticket_qty_1")
            ctrl.evaluate = AsyncMock(return_value="select")
            opt_loc = MagicMock()
            opt_loc.all_inner_texts = AsyncMock(return_value=["0", "1", "2", "3", "4"])
            ctrl.locator = MagicMock(return_value=opt_loc)
            controls_locator.nth = MagicMock(return_value=ctrl)
        else:
            controls_locator.count = AsyncMock(return_value=0)

        def mock_locator(sel):
            if any(k in sel for k in [".smoketest-ticket-quantity", "[role='combobox']", ".MuiSelect-select", "select"]):
                return controls_locator
            loc = MagicMock()
            first_loc = AsyncMock()
            is_vis = any(vs in sel for vs in visible_selectors)
            first_loc.is_visible = AsyncMock(return_value=is_vis)
            loc.first = first_loc
            return loc

        page.locator = MagicMock(side_effect=mock_locator)
        return page

    async def test_sold_out_detection_artist_bio_not_sold_out(self):
        """Artist biography mentioning 'sold out' must NOT trigger Sold Out status."""
        config = AppConfig()
        detector = EtixDetector(config)

        bio_text = (
            "Kevin Atwater - Blush Red Tour. "
            "In 2025, Kevin released his debut album Achilles, which garnered more than 10 million streams "
            "across all DSPs, sold out his debut headline North American tour, and premiered a short film..."
        )
        page = self._create_mock_page(body_text=bio_text, visible_selectors=[], has_controls=True)

        is_sold_out = await detector.is_soldout_page(page)
        self.assertFalse(is_sold_out, "Page with bio mentioning 'sold out' should NOT be marked sold out!")

    async def test_sold_out_detection_real_sold_out(self):
        """Page with official 'This performance is sold out' and no controls must be detected as Sold Out."""
        config = AppConfig()
        detector = EtixDetector(config)

        page = self._create_mock_page(
            body_text="This performance is sold out.",
            visible_selectors=[".alert:has-text('This performance is sold out')"],
            has_controls=False,
        )

        is_sold_out = await detector.is_soldout_page(page)
        self.assertTrue(is_sold_out, "Real sold out page must be detected as Sold Out!")

    async def test_sold_out_detection_off_sale_online(self):
        """Page with 'Off Sale Online' banner and no controls must be detected as Sold Out/Off Sale."""
        config = AppConfig()
        detector = EtixDetector(config)

        page = self._create_mock_page(
            body_text="Off Sale Online. This performance is sold out.",
            visible_selectors=[".alert:has-text('Off Sale Online')"],
            has_controls=False,
        )

        is_sold_out = await detector.is_soldout_page(page)
        self.assertTrue(is_sold_out, "Page with 'Off Sale Online' banner must be detected as Sold Out!")

    async def test_sold_out_detection_waitlist_set_alert(self):
        """Page with waitlist 'Set Alert' button and no controls must be detected as Sold Out."""
        config = AppConfig()
        detector = EtixDetector(config)

        page = self._create_mock_page(
            body_text="We'll send you an email if tickets become available.",
            visible_selectors=["button:has-text('Set Alert')"],
            has_controls=False,
        )

        is_sold_out = await detector.is_soldout_page(page)
        self.assertTrue(is_sold_out, "Waitlist page with 'Set Alert' must be detected as Sold Out!")

    async def test_sold_out_detection_controls_override_safeguard(self):
        """If active ticket controls exist, page must NOT be marked sold out even if text/alert matches."""
        config = AppConfig()
        detector = EtixDetector(config)

        page = self._create_mock_page(
            body_text="This performance is sold out.",
            visible_selectors=[".alert:has-text('This performance is sold out')"],
            has_controls=True,
        )

        is_sold_out = await detector.is_soldout_page(page)
        self.assertFalse(is_sold_out, "Active ticket controls must override any sold out text/alert!")

    async def test_blocked_profile_tracking_and_persistence(self):
        """Verify recording, deduplication, file persistence, and clearing of blocked profiles."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_blocked = Path(tmp_dir) / "blocked_test.txt"
            mock_client = AsyncMock(spec=AdsPowerClient)
            manager = AdsPowerProfileManager(
                client=mock_client,
                blocked_profiles_file=tmp_blocked,
            )

            p1 = AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="G1", proxy_host="1.1.1.1", proxy_port="8000")
            p2 = AdsPowerProfile(user_id="u2", name="Profile #02", serial_number="102", group_id="g1", group_name="G1", proxy_host="2.2.2.2", proxy_port="8000")

            manager.record_blocked_profile(p1, reason="DataDome Blocked")
            manager.record_blocked_profile(p2, reason="Access Temporarily Blocked")
            # Duplicate call for p1
            manager.record_blocked_profile(p1, reason="DataDome Blocked")

            session_blocked = manager.get_session_blocked_profiles()
            self.assertEqual(len(session_blocked), 2, "Duplicate profile record must be deduplicated in session list.")
            self.assertEqual(session_blocked[0]["user_id"], "u1")
            self.assertEqual(session_blocked[0]["name"], "Profile #01")
            self.assertEqual(session_blocked[1]["user_id"], "u2")

            # Check file persistence
            self.assertTrue(tmp_blocked.exists(), "Blocked profiles file must be created on disk.")
            content = tmp_blocked.read_text(encoding="utf-8")
            self.assertIn("Profile #01 (ID: u1)", content)
            self.assertIn("Profile #02 (ID: u2)", content)

            # Check session clearing
            manager.clear_session_blocked_profiles()
            self.assertEqual(len(manager.get_session_blocked_profiles()), 0, "Session list must be cleared.")
            # File on disk remains intact
            self.assertTrue(tmp_blocked.exists())

    async def test_cdp_pool_stop_and_remove_worker(self):
        """Verify stop_and_remove_worker cleanly closes browser, marks failed, and unregisters worker."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_manager = MagicMock(spec=AdsPowerProfileManager)

        pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=mock_manager)

        p = AdsPowerProfile(user_id="u99", name="Profile #99", serial_number="199", group_id="g1", group_name="Test")
        p.is_open = True
        mock_browser = AsyncMock()
        mock_page = AsyncMock()
        mock_ctx = AsyncMock()

        worker = BrowserWorker(
            profile=p,
            browser=mock_browser,
            context=mock_ctx,
            page=mock_page,
            worker_index=1,
        )
        pool.workers.append(worker)

        await pool.stop_and_remove_worker(worker, reason="Max profile swaps reached")

        mock_browser.close.assert_awaited_once()
        mock_client.stop_browser.assert_awaited_once_with("u99")
        mock_manager.mark_profile_failed.assert_called_once_with(p)
        self.assertFalse(p.is_open)
        self.assertNotIn(worker, pool.workers, "Worker must be cleanly removed from active pool.")

    async def test_ensure_worker_ready_for_show_block_and_swap_limit(self):
        """Verify 1 reload attempt, recording to blocked profiles, and swap limit exhaustion."""
        from src.etix.checker import EtixCheckEngine
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker

        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_manager = MagicMock(spec=AdsPowerProfileManager)

        cfg = AppConfig()
        engine = EtixCheckEngine(config=cfg, client=mock_client, profile_manager=mock_manager)
        mock_pool = AsyncMock(spec=CDPBrowserPool)
        engine.cdp_pool = mock_pool

        # Mock worker whose page is blocked
        p = AdsPowerProfile(user_id="u_fail", name="Blocked Worker", serial_number="999", group_id="g1", group_name="G1")
        mock_page = MagicMock()
        mock_page.url = "https://www.etix.com/ticket/p/12345/test-event"
        mock_page.is_closed.return_value = False
        mock_page.bring_to_front = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.reload = AsyncMock()
        mock_page.set_default_navigation_timeout = MagicMock()
        mock_page.set_default_timeout = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.clear_cookies = AsyncMock()
        mock_ctx.new_page = AsyncMock(return_value=mock_page)

        worker = BrowserWorker(
            profile=p,
            browser=AsyncMock(),
            context=mock_ctx,
            page=mock_page,
            worker_index=1,
        )

        # Detector always reports blocked
        engine.detector.is_bad_proxy_page = AsyncMock(return_value=False)
        engine.detector.is_slider_captcha = AsyncMock(return_value=False)
        engine.detector.is_blocked_page = AsyncMock(return_value=True)

        # Mock pool replace_worker_with_reserve returning None (exhausted)
        mock_pool.replace_worker_with_reserve.return_value = None

        show = Show(show_id="s1", name="Test Show", url="https://www.etix.com/ticket/p/12345/test-event", target_total=4)

        result = await engine._ensure_worker_ready_for_show(worker, show, max_profile_swaps=1)

        # Must return None when swaps exhausted
        self.assertIsNone(result)
        # Must record blocked profile
        mock_manager.record_blocked_profile.assert_called()
        # Must call stop_and_remove_worker to close the browser cleanly
        mock_pool.stop_and_remove_worker.assert_awaited()


if __name__ == "__main__":
    unittest.main()


