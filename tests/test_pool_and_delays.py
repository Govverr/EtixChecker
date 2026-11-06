"""Unit tests verifying pool enhancements, concurrency detection, delay validation, and URL handling."""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_validate_worker_connection_proxy_failure_immediate(self):
        """Verify immediate rejection when start.adspower.net displays Proxy failure."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        pool = CDPBrowserPool(config=AppConfig(), client=AsyncMock(), profile_manager=MagicMock())

        mock_page = MagicMock()
        mock_page.url = "http://start.adspower.net/?id=k1f743cw&host=127.0.0.1:20725"
        mock_page.inner_text = AsyncMock(return_value="Proxy failure. Please check if your network meets the proxy service provider's conditions. 1. If the proxy didn't passed the IP checker...")

        worker = BrowserWorker(
            profile=AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="G1"),
            browser=AsyncMock(),
            context=MagicMock(),
            page=mock_page,
            worker_index=1,
        )

        is_healthy, reason = await pool.validate_worker_connection(worker, timeout_s=3.0)
        self.assertFalse(is_healthy)
        self.assertIn("Proxy failure", reason)

    async def test_validate_worker_connection_valid_ip_immediate(self):
        """Verify immediate success when start.adspower.net resolves a real IPv4 address."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        pool = CDPBrowserPool(config=AppConfig(), client=AsyncMock(), profile_manager=MagicMock())

        mock_page = MagicMock()
        mock_page.url = "http://start.adspower.net/?id=k1f743cw&host=127.0.0.1:20725"
        mock_page.inner_text = AsyncMock(return_value="Account Information\nName: Profile #01\nIP: 142.250.190.46\nUS / Los Angeles")

        worker = BrowserWorker(
            profile=AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="G1"),
            browser=AsyncMock(),
            context=MagicMock(),
            page=mock_page,
            worker_index=1,
        )

        is_healthy, reason = await pool.validate_worker_connection(worker, timeout_s=3.0)
        self.assertTrue(is_healthy)
        self.assertIn("142.250.190.46", reason)

    async def test_validate_worker_connection_timeout_stub_ip(self):
        """Verify failure when start.adspower.net stays on stub IP ---.---.---.--- after timeout."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        pool = CDPBrowserPool(config=AppConfig(), client=AsyncMock(), profile_manager=MagicMock())

        mock_page = MagicMock()
        mock_page.url = "http://start.adspower.net/?id=k1f743cw&host=127.0.0.1:20725"
        mock_page.inner_text = AsyncMock(return_value="Account Information\nName: Profile #01\n---.---.---.---\n-- / --")

        worker = BrowserWorker(
            profile=AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="G1"),
            browser=AsyncMock(),
            context=MagicMock(),
            page=mock_page,
            worker_index=1,
        )

        is_healthy, reason = await pool.validate_worker_connection(worker, timeout_s=0.5)
        self.assertFalse(is_healthy)
        self.assertIn("timed out", reason.lower())

    async def test_validate_worker_connection_chrome_network_error(self):
        """Verify failure when URL is chrome-error:// or page has ERR_PROXY_CONNECTION_FAILED."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        pool = CDPBrowserPool(config=AppConfig(), client=AsyncMock(), profile_manager=MagicMock())

        mock_page = MagicMock()
        mock_page.url = "chrome-error://chromewebdata/"
        mock_page.inner_text = AsyncMock(return_value="This site can't be reached. net::ERR_PROXY_CONNECTION_FAILED")

        worker = BrowserWorker(
            profile=AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="G1"),
            browser=AsyncMock(),
            context=MagicMock(),
            page=mock_page,
            worker_index=1,
        )

        is_healthy, reason = await pool.validate_worker_connection(worker, timeout_s=3.0)
        self.assertFalse(is_healthy)

    async def test_initialize_slot_replacements_and_no_audit_logging(self):
        """Verify slot replenishment up to 3 times, closing failing browsers, and NO bad proxy file logging."""
        import tempfile
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_bad = Path(tmp_dir) / "bad_proxies.txt"
            tmp_blocked = Path(tmp_dir) / "blocked_profiles.txt"

            mock_client = AsyncMock(spec=AdsPowerClient)
            mock_client.stop_browser.return_value = True

            manager = AdsPowerProfileManager(
                client=mock_client,
                bad_proxies_file=tmp_bad,
                blocked_profiles_file=tmp_blocked,
            )

            p1 = AdsPowerProfile(user_id="u1", name="Profile #01", serial_number="101", group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)", proxy_host="1.1.1.1", proxy_port="8080")
            p2 = AdsPowerProfile(user_id="u2", name="Profile #02", serial_number="102", group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)", proxy_host="2.2.2.2", proxy_port="8080")
            p3 = AdsPowerProfile(user_id="u3", name="Profile #03", serial_number="103", group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)", proxy_host="3.3.3.3", proxy_port="8080")
            p1.role = ProfileRole.IN_USE
            p2.role = ProfileRole.RESERVE
            p3.role = ProfileRole.RESERVE
            manager.profiles = [p1, p2, p3]

            pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=manager)

            call_count = 0
            async def mock_connect(prof, worker_index):
                nonlocal call_count
                call_count += 1
                b = AsyncMock()
                p = MagicMock()
                p.url = "http://start.adspower.net/?id=test"
                if prof.user_id in ("u1", "u2"):
                    p.inner_text = AsyncMock(return_value="Proxy failure. Please check if your network meets the proxy service provider's conditions.")
                else:
                    p.inner_text = AsyncMock(return_value="Account Information\nIP: 50.60.70.80\nUS / New York")
                return BrowserWorker(profile=prof, browser=b, context=MagicMock(), page=p, worker_index=worker_index)

            pool._connect_profile = mock_connect

            with patch("src.adspower.profile_manager.random.choice", side_effect=[p2, p3]):
                final_worker = await pool._initialize_slot(slot_idx=1, initial_profile=p1, max_swaps=3)

            # Must succeed on p3 (attempt 3)
            self.assertIsNotNone(final_worker)
            self.assertEqual(final_worker.profile.user_id, "u3")
            self.assertEqual(call_count, 3)

            # p1 and p2 must be marked FAILED
            self.assertEqual(p1.role, ProfileRole.FAILED)
            self.assertEqual(p2.role, ProfileRole.FAILED)

            # p1 and p2 browsers must be stopped in AdsPower
            self.assertEqual(mock_client.stop_browser.await_count, 2)
            mock_client.stop_browser.assert_any_await("u1")
            mock_client.stop_browser.assert_any_await("u2")

            # Crucial: NO startup proxy logging to bad_proxies.txt or blocked_profiles.txt
            self.assertFalse(tmp_bad.exists(), "bad_proxies.txt must NOT be created/written to during startup check")
            self.assertFalse(tmp_blocked.exists(), "blocked_profiles.txt must NOT be created/written to during startup check")

    async def test_initialize_slot_exhaustion_max_swaps(self):
        """Verify that after 3 failed replacements (4 attempts total), slot returns None and closes all."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker

        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True
        manager = MagicMock(spec=AdsPowerProfileManager)

        profs = [
            AdsPowerProfile(user_id=f"u{i}", name=f"Profile #{i}", serial_number=str(100+i), group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)")
            for i in range(1, 5)
        ]
        reserve_iter = iter(profs[1:])
        manager.get_next_available_reserve.side_effect = lambda: next(reserve_iter, None)

        pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=manager)

        async def mock_connect(prof, worker_index):
            b = AsyncMock()
            p = MagicMock()
            p.url = "http://start.adspower.net/?id=test"
            p.inner_text = AsyncMock(return_value="Proxy failure")
            return BrowserWorker(profile=prof, browser=b, context=MagicMock(), page=p, worker_index=worker_index)

        pool._connect_profile = mock_connect

        result = await pool._initialize_slot(slot_idx=1, initial_profile=profs[0], max_swaps=3)
        self.assertIsNone(result)
        self.assertEqual(mock_client.stop_browser.await_count, 4)

    async def test_replace_worker_with_reserve_strict_immutability(self):
        """Verify Hot-Swap preserves native profile proxy and never calls update_profile_proxy."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True

        manager = MagicMock(spec=AdsPowerProfileManager)
        failing_p = AdsPowerProfile(user_id="u_fail", name="Failing Worker", serial_number="101", group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)", proxy_host="10.0.0.1", proxy_port="8080")
        reserve_p = AdsPowerProfile(user_id="u_res", name="Reserve Worker", serial_number="102", group_id="g1", group_name="Inventory Etix (DO NOT TOUCH)", proxy_host="10.0.0.2", proxy_port="9090")

        manager.get_next_available_reserve.return_value = reserve_p

        pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=manager)

        failing_worker = BrowserWorker(
            profile=failing_p,
            browser=AsyncMock(),
            context=MagicMock(),
            page=MagicMock(),
            worker_index=1,
        )
        pool.workers.append(failing_worker)

        # Mock connect for reserve
        mock_res_page = MagicMock()
        mock_res_page.url = "http://start.adspower.net/?id=res"
        mock_res_page.inner_text = AsyncMock(return_value="Account Information\nIP: 85.90.95.100\nUS")
        new_w = BrowserWorker(profile=reserve_p, browser=AsyncMock(), context=MagicMock(), page=mock_res_page, worker_index=1)

        pool._connect_profile = AsyncMock(return_value=new_w)
        pool.validate_worker_connection = AsyncMock(return_value=(True, "Verified IP: 85.90.95.100"))

        result = await pool.replace_worker_with_reserve(failing_worker, reason="DataDome block")

        self.assertIsNotNone(result)
        self.assertEqual(result.profile.user_id, "u_res")
        # Native proxy must be completely untouched
        self.assertEqual(result.profile.proxy_host, "10.0.0.2")
        self.assertEqual(result.profile.proxy_port, "9090")
        # AdsPower API update_profile_proxy must NEVER be called
        mock_client.update_profile_proxy.assert_not_called()

    async def test_client_update_profile_proxy_success(self):
        """Verify client.update_profile_proxy sends POST /api/v1/user/update with correct payload."""
        client = AdsPowerClient()
        client._request = AsyncMock(return_value={"code": 0, "msg": "success"})

        cfg = {"proxy_soft": "other", "proxy_type": "http", "proxy_host": "1.1.1.1", "proxy_port": "8080"}
        res = await client.update_profile_proxy("u123", cfg)
        self.assertTrue(res)
        client._request.assert_called_once_with(
            "POST",
            "/api/v1/user/update",
            json_data={"user_id": "u123", "user_proxy_config": cfg},
        )

    async def test_replace_worker_with_reserve_retries_on_launch_failure(self):
        """Verify Hot-Swap retries when the first reserve candidate fails to launch (e.g. SunBrowser error)."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True

        manager = MagicMock(spec=AdsPowerProfileManager)
        manager.profiles = []
        failing_p = AdsPowerProfile(user_id="u_fail", name="Failing Worker", serial_number="101", group_id="g1", group_name="TestGroup")
        bad_reserve = AdsPowerProfile(user_id="u_bad", name="Bad Reserve", serial_number="102", group_id="g1", group_name="TestGroup")
        good_reserve = AdsPowerProfile(user_id="u_good", name="Good Reserve", serial_number="103", group_id="g1", group_name="TestGroup")

        # First returns bad_reserve, then good_reserve
        manager.get_next_available_reserve.side_effect = [bad_reserve, good_reserve]

        pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=manager)
        failing_worker = BrowserWorker(
            profile=failing_p,
            browser=AsyncMock(),
            context=MagicMock(),
            page=MagicMock(),
            worker_index=1,
        )
        pool.workers.append(failing_worker)

        # Mock _connect_profile: returns None for u_bad, returns valid worker for u_good
        good_w = BrowserWorker(profile=good_reserve, browser=AsyncMock(), context=MagicMock(), page=MagicMock(), worker_index=1)
        async def mock_connect(prof, worker_index):
            if prof.user_id == "u_bad":
                return None
            return good_w

        pool._connect_profile = AsyncMock(side_effect=mock_connect)
        pool.validate_worker_connection = AsyncMock(return_value=(True, "Verified IP: 1.2.3.4"))

        result = await pool.replace_worker_with_reserve(failing_worker, reason="DataDome block")

        self.assertIsNotNone(result)
        self.assertEqual(result.profile.user_id, "u_good")
        self.assertEqual(pool.workers[0].profile.user_id, "u_good")
        # u_bad must have been marked failed
        manager.mark_profile_failed.assert_any_call(bad_reserve)

    async def test_replace_two_workers_concurrently_both_get_replacements(self):
        """Verify that when 2 workers are blocked simultaneously, both get working reserve replacements."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True

        manager = MagicMock(spec=AdsPowerProfileManager)
        manager.profiles = []

        w1_p = AdsPowerProfile(user_id="w1", name="Worker 1", serial_number="1", group_id="g1", group_name="Grp")
        w2_p = AdsPowerProfile(user_id="w2", name="Worker 2", serial_number="2", group_id="g1", group_name="Grp")

        # Reserve pool: r1 (fails launch), r2 (succeeds for w1), r3 (succeeds for w2)
        r1 = AdsPowerProfile(user_id="r1", name="Reserve 1 (fail)", serial_number="11", group_id="g1", group_name="Grp")
        r2 = AdsPowerProfile(user_id="r2", name="Reserve 2 (good)", serial_number="12", group_id="g1", group_name="Grp")
        r3 = AdsPowerProfile(user_id="r3", name="Reserve 3 (good)", serial_number="13", group_id="g1", group_name="Grp")

        manager.get_next_available_reserve.side_effect = [r1, r2, r3]

        pool = CDPBrowserPool(config=AppConfig(), client=mock_client, profile_manager=manager)
        worker1 = BrowserWorker(profile=w1_p, browser=AsyncMock(), context=MagicMock(), page=MagicMock(), worker_index=1)
        worker2 = BrowserWorker(profile=w2_p, browser=AsyncMock(), context=MagicMock(), page=MagicMock(), worker_index=2)
        pool.workers = [worker1, worker2]

        async def mock_connect(prof, worker_index):
            if prof.user_id == "r1":
                return None
            return BrowserWorker(profile=prof, browser=AsyncMock(), context=MagicMock(), page=MagicMock(), worker_index=worker_index)

        pool._connect_profile = AsyncMock(side_effect=mock_connect)
        pool.validate_worker_connection = AsyncMock(return_value=(True, "Verified IP: 100.100.100.100"))

        res1, res2 = await asyncio.gather(
            pool.replace_worker_with_reserve(worker1, reason="DataDome block w1"),
            pool.replace_worker_with_reserve(worker2, reason="DataDome block w2"),
        )

        self.assertIsNotNone(res1)
        self.assertIsNotNone(res2)
        self.assertEqual(res1.profile.user_id, "r2")
        self.assertEqual(res2.profile.user_id, "r3")
        self.assertEqual(len(pool.workers), 2)
        self.assertEqual({w.profile.user_id for w in pool.workers}, {"r2", "r3"})

    def test_get_next_available_reserve_all_groups(self):
        """Verify get_next_available_reserve correctly selects profile when group is 'Все группы'."""
        client = MagicMock(spec=AdsPowerClient)
        manager = AdsPowerProfileManager(client=client)
        p1 = AdsPowerProfile(user_id="u1", name="P1", serial_number="1", group_id="g1", group_name="Group Alpha", role=ProfileRole.RESERVE)
        p2 = AdsPowerProfile(user_id="u2", name="P2", serial_number="2", group_id="g2", group_name="Group Beta", role=ProfileRole.RESERVE)
        manager.profiles = [p1, p2]

        # Case 1: 'Все группы'
        manager.group_name = "Все группы"
        chosen = manager.get_next_available_reserve()
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.role, ProfileRole.IN_USE)

        # Case 2: Specific group match (case insensitive)
        manager.profiles = [
            AdsPowerProfile(user_id="u3", name="P3", serial_number="3", group_id="g1", group_name="Group Alpha", role=ProfileRole.RESERVE),
            AdsPowerProfile(user_id="u4", name="P4", serial_number="4", group_id="g2", group_name="Group Beta", role=ProfileRole.RESERVE),
        ]
        manager.group_name = "group beta"
        chosen_beta = manager.get_next_available_reserve()
        self.assertIsNotNone(chosen_beta)
        self.assertEqual(chosen_beta.user_id, "u4")



    def test_circuit_breaker_logic(self):
        """Verify circuit breaker trips strictly after 8 consecutive bv verdicts and resets on success."""
        from src.browser.cdp_pool import CDPBrowserPool
        pool = CDPBrowserPool(
            config=AppConfig(),
            client=MagicMock(spec=AdsPowerClient),
            profile_manager=MagicMock(spec=AdsPowerProfileManager),
        )
        self.assertFalse(pool.is_circuit_breaker_tripped())

        # 1 to 7 consecutive bv verdicts should not trip breaker
        for i in range(1, 8):
            tripped = pool.record_hard_verdict_bv()
            self.assertFalse(tripped)
            self.assertFalse(pool.is_circuit_breaker_tripped())
            self.assertEqual(pool.consecutive_bv_count, i)

        # 8th consecutive bv verdict trips the breaker
        tripped = pool.record_hard_verdict_bv()
        self.assertTrue(tripped)
        self.assertTrue(pool.is_circuit_breaker_tripped())
        self.assertEqual(pool.consecutive_bv_count, 8)

        # Reset circuit breaker
        pool.reset_circuit_breaker()
        self.assertFalse(pool.is_circuit_breaker_tripped())
        self.assertEqual(pool.consecutive_bv_count, 0)

    async def test_in_place_proxy_swap_cascade_level1_success(self):
        """Verify In-Place Hot-Swap v2 updates proxy on existing profile via API when clean proxy is available."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True
        mock_client.update_profile_proxy.return_value = True

        mock_proxy_mgr = MagicMock()
        mock_proxy_mgr.get_next_available_proxy.return_value = ("192.168.1.100:8080:myuser:mypass", "good")
        mock_proxy_mgr.in_use_proxies = set()

        manager = MagicMock(spec=AdsPowerProfileManager)
        profile = AdsPowerProfile(
            user_id="u_inplace",
            name="In-Place Profile",
            serial_number="201",
            group_id="g1",
            group_name="Inventory Etix",
            proxy_host="10.0.0.1",
            proxy_port="8000",
        )

        pool = CDPBrowserPool(
            config=AppConfig(),
            client=mock_client,
            profile_manager=manager,
            proxy_manager=mock_proxy_mgr,
        )

        mock_browser = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        worker = BrowserWorker(
            profile=profile,
            browser=mock_browser,
            context=MagicMock(),
            page=mock_page,
            worker_index=1,
        )
        pool.workers.append(worker)

        reconnected_worker = BrowserWorker(
            profile=profile,
            browser=AsyncMock(),
            context=MagicMock(),
            page=MagicMock(),
            worker_index=1,
        )
        pool._connect_profile = AsyncMock(return_value=reconnected_worker)
        pool.validate_worker_connection = AsyncMock(return_value=(True, "Verified IP: 192.168.1.100"))

        result = await pool.in_place_proxy_swap(worker, reason="DataDome block")

        self.assertIsNotNone(result)
        self.assertEqual(result.profile.user_id, "u_inplace")
        self.assertEqual(result.profile.proxy_host, "192.168.1.100")
        self.assertEqual(result.profile.proxy_port, "8080")
        self.assertEqual(result.profile.proxy_user, "myuser")
        self.assertEqual(result.profile.proxy_password, "mypass")

        mock_client.update_profile_proxy.assert_called_once()
        mock_proxy_mgr.mark_proxy_bad.assert_called_with("10.0.0.1:8000", reason="DataDome block")

    async def test_in_place_proxy_swap_cascade_level2_fallback_when_exhausted(self):
        """Verify In-Place Hot-Swap smoothly cascades to Reserve profile when external proxy pool is exhausted."""
        from src.browser.cdp_pool import CDPBrowserPool, BrowserWorker
        mock_client = AsyncMock(spec=AdsPowerClient)
        mock_client.stop_browser.return_value = True

        mock_proxy_mgr = MagicMock()
        mock_proxy_mgr.get_next_available_proxy.return_value = None  # EXHAUSTED!
        mock_proxy_mgr.in_use_proxies = set()

        manager = MagicMock(spec=AdsPowerProfileManager)
        reserve_p = AdsPowerProfile(
            user_id="u_reserve_cascade",
            name="Reserve Cascade",
            serial_number="202",
            group_id="g1",
            group_name="Inventory Etix",
            proxy_host="10.0.0.50",
            proxy_port="9000",
        )
        manager.get_next_available_reserve.return_value = reserve_p

        failing_p = AdsPowerProfile(
            user_id="u_fail_exhausted",
            name="Exhausted Worker",
            serial_number="201",
            group_id="g1",
            group_name="Inventory Etix",
            proxy_host="10.0.0.1",
            proxy_port="8000",
        )

        pool = CDPBrowserPool(
            config=AppConfig(),
            client=mock_client,
            profile_manager=manager,
            proxy_manager=mock_proxy_mgr,
        )

        worker = BrowserWorker(
            profile=failing_p,
            browser=AsyncMock(),
            context=MagicMock(),
            page=MagicMock(),
            worker_index=1,
        )
        pool.workers.append(worker)

        reserve_worker = BrowserWorker(
            profile=reserve_p,
            browser=AsyncMock(),
            context=MagicMock(),
            page=MagicMock(),
            worker_index=1,
        )
        pool._connect_profile = AsyncMock(return_value=reserve_worker)
        pool.validate_worker_connection = AsyncMock(return_value=(True, "Verified IP: 10.0.0.50"))

        result = await pool.in_place_proxy_swap(worker, reason="External proxy exhausted")

        self.assertIsNotNone(result)
        self.assertEqual(result.profile.user_id, "u_reserve_cascade")
        # In-Place update must not be called since external pool was empty
        mock_client.update_profile_proxy.assert_not_called()
        self.assertEqual(result.profile.proxy_host, "10.0.0.50")

if __name__ == "__main__":
    unittest.main()


