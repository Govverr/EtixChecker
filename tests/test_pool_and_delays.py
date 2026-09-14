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


if __name__ == "__main__":
    unittest.main()
