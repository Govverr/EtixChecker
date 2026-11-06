import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gui_app import BlockedProfilesModal, EtixGuiApp, parse_blocked_profiles


class TestGuiProxyFeatures(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_blocked_profiles_standard_and_fallback(self):
        """Verify parse_blocked_profiles parses standard formatted lines and legacy fallbacks."""
        f = self.temp_path / "blocked.txt"
        content = (
            "[2026-11-06 14:22:27] Profile: Profile #01 (ID: u1) | Proxy: 1.1.1.1:8000 | Reason: DataDome Blocked\n"
            "[2026-11-06 14:22:28] Profile: Profile #02 (ID: u2) | Proxy: 2.2.2.2:8000 | Reason: Access Temporarily Blocked\n"
            "Legacy line without full format (ID: u3) - timed out\n"
        )
        f.write_text(content, encoding="utf-8")

        records = parse_blocked_profiles(f)
        self.assertEqual(len(records), 3)

        self.assertEqual(records[0]["user_id"], "u1")
        self.assertEqual(records[0]["name"], "Profile #01")
        self.assertEqual(records[0]["proxy"], "1.1.1.1:8000")
        self.assertEqual(records[0]["reason"], "DataDome Blocked")

        self.assertEqual(records[1]["user_id"], "u2")
        self.assertEqual(records[1]["name"], "Profile #02")

        self.assertEqual(records[2]["user_id"], "u3")

    def test_parse_blocked_profiles_non_existent_file(self):
        """Verify parse_blocked_profiles returns empty list for non-existent file."""
        non_existent = self.temp_path / "does_not_exist.txt"
        self.assertEqual(parse_blocked_profiles(non_existent), [])

    @patch("gui_app.ctk.CTkFont")
    @patch("gui_app.ctk.CTkLabel")
    @patch("gui_app.ctk.CTkFrame")
    def test_blocked_profiles_modal_data_loading(self, mock_frame_cls, mock_label_cls, mock_font_cls):
        """Verify BlockedProfilesModal loads unique IDs into textbox and handles copy safely."""
        f = self.temp_path / "blocked.txt"
        f.write_text(
            "[2026-11-06 14:22:27] Profile: Profile #01 (ID: u1) | Proxy: 1.1.1.1:8000 | Reason: DataDome Blocked\n"
            "[2026-11-06 14:22:28] Profile: Profile #01 (ID: u1) | Proxy: 1.1.1.1:8000 | Reason: Repeated Block\n"
            "[2026-11-06 14:22:29] Profile: Profile #02 (ID: u2) | Proxy: 2.2.2.2:8000 | Reason: DataDome Blocked\n",
            encoding="utf-8",
        )

        modal = BlockedProfilesModal.__new__(BlockedProfilesModal)
        modal.file_path = f
        modal.on_clear_callback = MagicMock()
        modal.unique_ids = []

        modal.txt_ids = MagicMock()
        modal.btn_copy = MagicMock()
        modal.scroll_details = MagicMock()
        modal.scroll_details.winfo_children.return_value = []

        modal._load_data()

        # Should deduplicate u1 (2 entries -> 1 unique) -> u1, u2
        self.assertEqual(modal.unique_ids, ["u1", "u2"])
        modal.txt_ids.insert.assert_called_with("1.0", "u1\nu2")
        modal.btn_copy.configure.assert_called_with(state="normal", text="📋 Скопировать все ID (2)")

        # Test copy action
        modal.clipboard_clear = MagicMock()
        modal.clipboard_append = MagicMock()
        modal.after = MagicMock()

        modal._on_copy_clicked()
        modal.clipboard_clear.assert_called_once()
        modal.clipboard_append.assert_called_with("u1\nu2")

    def test_update_blocked_badge(self):
        """Verify _update_blocked_badge updates button text and styling based on blocked profiles."""
        app = EtixGuiApp.__new__(EtixGuiApp)
        app.btn_blocked = MagicMock()

        # Case 1: Empty / 0 blocked
        with patch("gui_app.parse_blocked_profiles", return_value=[]):
            app._update_blocked_badge()
            app.btn_blocked.configure.assert_called_with(
                text="🚫  Заблокированные (0)",
                fg_color="#1e293b",
                hover_color="#2a374f",
                text_color="#94a3b8",
            )

        # Case 2: 2 blocked profiles
        mock_records = [
            {"user_id": "u1", "name": "P1"},
            {"user_id": "u2", "name": "P2"},
        ]
        with patch("gui_app.parse_blocked_profiles", return_value=mock_records):
            app._update_blocked_badge()
            app.btn_blocked.configure.assert_called_with(
                text="🚫  Заблокированные (2)",
                fg_color="#3b1219",
                hover_color="#541b24",
                text_color="#f87171",
            )

    @patch("gui_app.ctk.CTkProgressBar")
    @patch("gui_app.ctk.CTkFont")
    @patch("gui_app.ctk.CTkLabel")
    @patch("gui_app.ctk.CTkFrame")
    def test_modern_dashboard_cards_proxies_and_formatting(
        self, mock_frame_cls, mock_label_cls, mock_font_cls, mock_progress_cls
    ):
        """Verify modern dashboard cards format values and updates matching mockup."""
        mock_label_cls.side_effect = lambda *args, **kwargs: MagicMock()
        mock_frame_cls.side_effect = lambda *args, **kwargs: MagicMock()
        mock_progress_cls.side_effect = lambda *args, **kwargs: MagicMock()
        app = EtixGuiApp.__new__(EtixGuiApp)
        parent_mock = MagicMock()

        # 1. Events card
        card_events = app._create_events_card(parent_mock)
        self.assertTrue(hasattr(card_events, "lbl_val"))
        card_events.lbl_val.configure(text="2 из 2")
        # proxy internal checks
        self.assertEqual(card_events.lbl_val._num.configure.call_args_list[-1][1]["text"], "2")
        self.assertEqual(card_events.lbl_val._suf.configure.call_args_list[-1][1]["text"], " / 2 Active")
        self.assertEqual(card_events.lbl_val._pill.configure.call_args_list[-1][1]["text"], "100% loaded")

        # 2. Tickets card
        card_tickets = app._create_tickets_card(parent_mock)
        self.assertTrue(hasattr(card_tickets, "lbl_val"))
        card_tickets.lbl_val.configure(text="12 / 48")
        self.assertEqual(card_tickets.lbl_val._val.configure.call_args_list[-1][1]["text"], "12 / 48")
        card_tickets.lbl_val._bar.set.assert_called_with(12 / 48)
        self.assertEqual(card_tickets.lbl_val._pct.configure.call_args_list[-1][1]["text"], "25%")

        # 3. Workers card
        card_workers = app._create_workers_card(parent_mock)
        self.assertTrue(hasattr(card_workers, "lbl_val"))
        card_workers.lbl_val.configure(text="12 профилей")
        self.assertEqual(card_workers.lbl_val._num.configure.call_args_list[-1][1]["text"], "12")
        self.assertEqual(card_workers.lbl_val._suf.configure.call_args_list[-1][1]["text"], " Profiles")

        # 4. Status card
        card_status = app._create_status_card(parent_mock)
        self.assertTrue(hasattr(card_status, "lbl_val"))
        card_status.lbl_val.configure(text="Выполняется...")
        self.assertEqual(card_status.lbl_val._status.configure.call_args_list[-1][1]["text"], "Running")

        # 5. Proxies card
        card_proxies = app._create_proxies_card(parent_mock)
        self.assertTrue(hasattr(card_proxies, "lbl_val"))
        card_proxies.lbl_val.configure(text="Good: 263 | Slow: 0")
        self.assertEqual(card_proxies.lbl_val._good.configure.call_args_list[-1][1]["text"], "Good: 263")
        self.assertEqual(card_proxies.lbl_val._slow.configure.call_args_list[-1][1]["text"], "Slow: 0")

    @patch("gui_app.ctk.CTkProgressBar")
    @patch("gui_app.ctk.CTkFont")
    @patch("gui_app.ctk.CTkLabel")
    @patch("gui_app.ctk.CTkFrame")
    def test_card_dimensions_prevent_clipping(
        self, mock_frame_cls, mock_label_cls, mock_font_cls, mock_progress_cls
    ):
        """Verify top cards have height=88 and width=165 to prevent bottom info clipping."""
        mock_label_cls.side_effect = lambda *args, **kwargs: MagicMock()
        mock_progress_cls.side_effect = lambda *args, **kwargs: MagicMock()

        created_frames = []
        def capture_frame(*args, **kwargs):
            m = MagicMock()
            m.kwargs = kwargs
            created_frames.append((m, kwargs))
            return m

        mock_frame_cls.side_effect = capture_frame
        app = EtixGuiApp.__new__(EtixGuiApp)
        parent_mock = MagicMock()

        app._create_events_card(parent_mock)
        events_card_kw = created_frames[0][1]
        self.assertEqual(events_card_kw.get("height"), 88)
        self.assertEqual(events_card_kw.get("width"), 165)

        created_frames.clear()
        app._create_tickets_card(parent_mock)
        tickets_card_kw = created_frames[0][1]
        self.assertEqual(tickets_card_kw.get("height"), 88)
        self.assertEqual(tickets_card_kw.get("width"), 165)

        created_frames.clear()
        app._create_workers_card(parent_mock)
        workers_card_kw = created_frames[0][1]
        self.assertEqual(workers_card_kw.get("height"), 88)
        self.assertEqual(workers_card_kw.get("width"), 165)

        created_frames.clear()
        app._create_status_card(parent_mock)
        status_card_kw = created_frames[0][1]
        self.assertEqual(status_card_kw.get("height"), 88)
        self.assertEqual(status_card_kw.get("width"), 165)

        created_frames.clear()
        app._create_proxies_card(parent_mock)
        proxies_card_kw = created_frames[0][1]
        self.assertEqual(proxies_card_kw.get("height"), 88)
        self.assertEqual(proxies_card_kw.get("width"), 165)

    def test_profile_manager_blocked_profiles_per_check_lifecycle(self):
        """Verify blocked_profiles.txt is reset per check, keeping only the latest check's blocked profiles."""
        from src.adspower.profile_manager import AdsPowerProfileManager
        from src.domain.models import AdsPowerProfile

        blocked_file = self.temp_path / "blocked_profiles.txt"
        pm = AdsPowerProfileManager.__new__(AdsPowerProfileManager)
        pm.blocked_profiles_file = blocked_file
        pm._session_blocked_profiles = []
        pm._session_bad_proxies = set()
        pm.profiles = []

        # Populate with a previous run's blocked profile
        blocked_file.write_text("[2026-11-06 10:00:00] Profile: Old (ID: old1) | Proxy: N/A | Reason: Blocked\n", encoding="utf-8")
        pm._session_blocked_profiles.append({"user_id": "old1", "name": "Old"})

        # Start of new check: clear session statuses
        pm.clear_session_blocked_profiles()
        self.assertEqual(len(pm.get_session_blocked_profiles()), 0)
        self.assertEqual(blocked_file.read_text(encoding="utf-8"), "")

        # A new profile gets blocked during this latest check
        p_new = AdsPowerProfile(user_id="new1", name="New Profile", serial_number="1", group_id="g1", group_name="Test")
        pm.record_blocked_profile(p_new, reason="DataDome: Hard Block")

        self.assertEqual(len(pm.get_session_blocked_profiles()), 1)
        content = blocked_file.read_text(encoding="utf-8")
        self.assertIn("New Profile", content)
        self.assertIn("ID: new1", content)
        self.assertNotIn("Old", content)

        # Crucial test: reset_session_statuses at the END of a check must NOT erase blocked profiles
        pm.reset_session_statuses()
        self.assertEqual(len(pm.get_session_blocked_profiles()), 1)
        content_after_reset = blocked_file.read_text(encoding="utf-8")
        self.assertIn("ID: new1", content_after_reset)

    def test_detector_is_blocked_page_detects_all_datadome_hard_blocks(self):
        """Verify is_blocked_page correctly identifies DataDome hard blocks (verdict=bv, iframe text, dd script)."""
        import asyncio
        from src.config.settings import AppConfig
        from src.etix.detector import EtixDetector

        cfg = AppConfig()
        detector = EtixDetector(config=cfg)

        class AsyncMockLoc:
            def __init__(self, visible=False):
                self._visible = visible
            def first(self):
                return self
            async def is_visible(self, timeout=None):
                return self._visible

        class AsyncMockFrame:
            def __init__(self, url="", body=""):
                self.url = url
                self._body = body
            def locator(self, *args, **kwargs):
                has_hdr = "Access Temporarily Blocked" in self._body
                return AsyncMockLoc(visible=has_hdr)
            async def inner_text(self, selector, timeout=None):
                return self._body
            async def evaluate(self, script):
                return None

        class AsyncMockPage:
            def __init__(self, frames, content="", title="etix.com"):
                self.frames = frames
                self._content = content
                self._title = title
            async def title(self):
                return self._title
            async def content(self):
                return self._content
            def locator(self, selector):
                is_vis = ("t=bv" in selector and "t=bv" in self._content)
                return AsyncMockLoc(visible=is_vis)

        # Case 1: geo.captcha-delivery.com frame has "Access Temporarily Blocked"
        frame_dd = AsyncMockFrame(
            url="https://geo.captcha-delivery.com/captcha/?initialCid=123&t=bv",
            body="Access Temporarily Blocked\nTo help protect our platform from automated traffic, our security systems have temporarily restricted access to this page."
        )
        page1 = AsyncMockPage(frames=[frame_dd], content="<html><iframe src='https://geo.captcha-delivery.com/captcha/?t=bv'></iframe></html>")

        is_blk = asyncio.run(detector.is_blocked_page(page1))
        self.assertTrue(is_blk, "Should detect DataDome hard block inside captcha-delivery iframe")

        # Case 2: DataDome script tag with t: 'bv' in main page content
        page2 = AsyncMockPage(
            frames=[],
            content="<script>var dd={'rt':'c','cid':'123','t':'bv','host':'geo.captcha-delivery.com'}</script>"
        )
        is_blk2 = asyncio.run(detector.is_blocked_page(page2))
        self.assertTrue(is_blk2, "Should detect DataDome hard block with verdict bv in page content")

        # Case 3: Solvable slider challenge (t: 'fe') - should NOT be treated as a hard block
        frame_slider = AsyncMockFrame(
            url="https://geo.captcha-delivery.com/captcha/?initialCid=123&t=fe",
            body="Slide right to secure your access"
        )
        page3 = AsyncMockPage(
            frames=[frame_slider],
            content="<script>var dd={'rt':'c','cid':'123','t':'fe'}</script>"
        )
        with patch.object(detector, "is_slider_captcha", return_value=True):
            is_blk3 = asyncio.run(detector.is_blocked_page(page3))
            self.assertFalse(is_blk3, "Solvable slider must NOT be classified as hard block")


if __name__ == "__main__":
    unittest.main()
