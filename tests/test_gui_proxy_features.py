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


if __name__ == "__main__":
    unittest.main()
