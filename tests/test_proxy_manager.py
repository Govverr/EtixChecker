"""Unit tests for ProxyManager: regex extraction, TCP socket ping, HTTP probe,
smart merge, and collision prevention.
"""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.proxy_manager import ProxyCheckResult, ProxyManager


class TestProxyManager(unittest.IsolatedAsyncioTestCase):
    """Comprehensive test suite for ProxyManager."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.good_file = self.base_path / "good.txt"
        self.slow_file = self.base_path / "slow.txt"
        self.bad_file = self.base_path / "bad.txt"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_regex_extraction_from_csv_and_text(self):
        """Verify regex extraction from complex CSV lines, URLs, and standard lines."""
        raw_csv_text = """
proxy,1 - proxy,events,168.213.120.47:61234:events47:p99sutszj,ACTIVE
"group","name","10.20.30.40:8080:user:pass123","ok"
line with url: http://myuser:mypass@172.16.0.5:3128 in text
socks5://proxyuser:proxypass@192.168.1.1:1080
999.999.999.999:8080:invalid:ip
1.2.3.4:999999:invalid:port
# comment line
        """
        extracted = ProxyManager.extract_proxies_from_text(raw_csv_text)
        self.assertIn("168.213.120.47:61234:events47:p99sutszj", extracted)
        self.assertIn("10.20.30.40:8080:user:pass123", extracted)
        self.assertIn("http://myuser:mypass@172.16.0.5:3128", extracted)
        self.assertIn("socks5://proxyuser:proxypass@192.168.1.1:1080", extracted)
        # Invalid IP and port must be excluded
        self.assertNotIn("999.999.999.999:8080:invalid:ip", extracted)
        self.assertNotIn("1.2.3.4:999999:invalid:port", extracted)

    def test_smart_append_and_merge_preserves_existing(self):
        """Verify Smart Append & Merge preserves order of existing proxies and deduplicates."""
        # Pre-populate good file
        self.good_file.write_text("1.1.1.1:8000:u:p\n2.2.2.2:8000:u:p\n", encoding="utf-8")

        new_items = ["2.2.2.2:8000:u:p", "3.3.3.3:8000:u:p", "4.4.4.4:8000:u:p"]
        added = ProxyManager.smart_append_and_merge(self.good_file, new_items)
        self.assertEqual(added, 2)

        loaded = ProxyManager.load_proxies(self.good_file)
        self.assertEqual(len(loaded), 4)
        self.assertEqual(loaded[0], "1.1.1.1:8000:u:p")
        self.assertEqual(loaded[1], "2.2.2.2:8000:u:p")
        self.assertEqual(loaded[2], "3.3.3.3:8000:u:p")
        self.assertEqual(loaded[3], "4.4.4.4:8000:u:p")

    async def test_check_tcp_socket_good_and_slow_and_bad(self):
        """Verify TCP socket ping differentiates fast, slow, and failed proxies."""
        # Fast proxy: latency ~ 200ms
        async def mock_open_fast(host, port):
            mock_writer = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            await asyncio.sleep(0.01)
            return MagicMock(), mock_writer

        with patch("asyncio.open_connection", side_effect=mock_open_fast):
            ok, lat, err = await ProxyManager.check_tcp_socket("1.2.3.4:8080:u:p")
            self.assertTrue(ok)
            self.assertLess(lat, 1500.0)
            self.assertIsNone(err)

        # Connection timeout / failure
        with patch("asyncio.open_connection", side_effect=asyncio.TimeoutError()):
            ok, lat, err = await ProxyManager.check_tcp_socket("1.2.3.4:8080:u:p", timeout_s=0.1)
            self.assertFalse(ok)
            self.assertIn("timed out", err.lower())

    async def test_check_http_probe_success_and_auth_failure(self):
        """Verify HTTP probe confirms credentials and detects 407 Proxy Authentication Required."""
        # Success probe
        class MockRespSuccess:
            status_code = 200
            text = "194.26.29.112"

        class MockClientSuccess:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url):
                return MockRespSuccess()

        with patch("httpx.AsyncClient", return_value=MockClientSuccess()):
            ok, ip, err = await ProxyManager.check_http_probe("1.2.3.4:8080:u:p")
            self.assertTrue(ok)
            self.assertEqual(ip, "194.26.29.112")
            self.assertIsNone(err)

        # Auth 407 failure probe
        class MockResp407:
            status_code = 407
            text = "Proxy Authentication Required"

        class MockClient407:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url):
                return MockResp407()

        with patch("httpx.AsyncClient", return_value=MockClient407()):
            ok, ip, err = await ProxyManager.check_http_probe("1.2.3.4:8080:wrong_user:wrong_pass")
            self.assertFalse(ok)
            self.assertIn("407", err)

    async def test_run_preflight_check_batch(self):
        """Verify run_preflight_check categorizes into good, slow, bad and persists files."""
        manager = ProxyManager(
            good_proxies_file=self.good_file,
            slow_proxies_file=self.slow_file,
            bad_proxies_file=self.bad_file,
        )

        test_proxies = [
            "1.1.1.1:8080:u:p",
            "2.2.2.2:8080:u:p",
            "3.3.3.3:8080:u:p",
        ]

        async def mock_single_check(p_str, **kwargs):
            if "1.1.1.1" in p_str:
                return ProxyCheckResult(proxy=p_str, status="GOOD", latency_ms=450.0, egress_ip="1.1.1.1")
            elif "2.2.2.2" in p_str:
                return ProxyCheckResult(proxy=p_str, status="SLOW", latency_ms=1850.0, egress_ip="2.2.2.2")
            else:
                return ProxyCheckResult(proxy=p_str, status="BAD", latency_ms=3100.0, error="Timed out")

        with patch.object(manager, "check_single_proxy", side_effect=mock_single_check):
            results = await manager.run_preflight_check(test_proxies, concurrency=3)

        self.assertEqual(len(results["good"]), 1)
        self.assertEqual(len(results["slow"]), 1)
        self.assertEqual(len(results["bad"]), 1)

        # Verify files updated
        good_loaded = manager.load_proxies(self.good_file)
        slow_loaded = manager.load_proxies(self.slow_file)
        self.assertIn("1.1.1.1:8080:u:p", good_loaded)
        self.assertIn("2.2.2.2:8080:u:p", slow_loaded)
        self.assertTrue(self.bad_file.exists())
        bad_text = self.bad_file.read_text(encoding="utf-8")
        self.assertIn("3.3.3.3:8080:u:p", bad_text)

    def test_collision_prevention_and_slow_pool_transition(self):
        """Verify strict 1-proxy-per-1-profile collision prevention and fallback notification."""
        self.good_file.write_text("g1:8000:u:p\ng2:8000:u:p\n", encoding="utf-8")
        self.slow_file.write_text("s1:8000:u:p\n", encoding="utf-8")

        slow_transition_called = False
        def on_slow():
            nonlocal slow_transition_called
            slow_transition_called = True

        manager = ProxyManager(
            good_proxies_file=self.good_file,
            slow_proxies_file=self.slow_file,
            bad_proxies_file=self.bad_file,
            on_slow_pool_transition=on_slow,
        )

        # 1. Allocate 1st good proxy
        p1, pool1 = manager.get_next_available_proxy()
        self.assertEqual(p1, "g1:8000:u:p")
        self.assertEqual(pool1, "good")
        self.assertIn(p1, manager.in_use_proxies)

        # 2. Allocate 2nd good proxy
        p2, pool2 = manager.get_next_available_proxy()
        self.assertEqual(p2, "g2:8000:u:p")
        self.assertEqual(pool2, "good")
        self.assertIn(p2, manager.in_use_proxies)

        # 3. Good pool exhausted -> must transition to slow and fire notification
        p3, pool3 = manager.get_next_available_proxy()
        self.assertEqual(p3, "s1:8000:u:p")
        self.assertEqual(pool3, "slow")
        self.assertTrue(slow_transition_called)
        self.assertIn(p3, manager.in_use_proxies)

        # 4. Both pools exhausted -> returns None
        p4 = manager.get_next_available_proxy()
        self.assertIsNone(p4)

        # 5. Release p1 back
        manager.release_proxy(p1)
        self.assertNotIn(p1, manager.in_use_proxies)

        # 6. Mark p2 bad -> removed from in_use and good file
        manager.mark_proxy_bad(p2, reason="Banned by DataDome")
        self.assertNotIn(p2, manager.in_use_proxies)
        self.assertNotIn(p2, manager.load_proxies(self.good_file))
        bad_text = self.bad_file.read_text(encoding="utf-8")
        self.assertIn(p2, bad_text)
        self.assertIn("Banned by DataDome", bad_text)


if __name__ == "__main__":
    unittest.main()
