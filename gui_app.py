"""Modern Graphical User Interface for Etix Checker using CustomTkinter with Slate-Indigo 3D Card theme."""

from __future__ import annotations

import asyncio
import math
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def _handle_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    import traceback
    from datetime import datetime
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    crash_log = Path("logs") / "gui_crash.log"
    try:
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] UNCAUGHT GUI EXCEPTION:\n{tb_str}\n")
    except Exception:
        pass

    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Произошла непредвиденная ошибка в работе Etix Checker:\n\n{exc_value}\n\nПодробный отчет записан в:\n{crash_log.resolve()}",
            "Etix Checker 2026 — Ошибка GUI",
            0x10 | 0x0,  # MB_ICONERROR | MB_OK
        )
    except Exception:
        pass


sys.excepthook = _handle_uncaught_exception

import customtkinter as ctk
import pandas as pd
from tkinter import messagebox

from src.adspower.backup_service import ProfileBackupService
from src.adspower.client import AdsPowerClient
from src.adspower.profile_manager import AdsPowerProfileManager
from src.config.settings import (
    AppConfig,
    CONFIG,
    MIN_SAFE_DELAYS,
    estimate_check_duration_seconds,
    reload_config,
    save_delays_to_dotenv,
    validate_and_clamp_delay,
)
from src.domain.enums import ShowStatus
from src.domain.models import CheckResult, Show
from src.etix.checker import EtixCheckEngine
from src.utils.logger import LOGGER
from src.utils.updater import UpdateService, VersionInfo

# Configure appearance
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Design Tokens (Slate-Navy / Indigo Gradient Palette)
FONT_FAMILY = "Segoe UI"
COLOR_BG = "#0b0f19"              # Main window background (Deep Space)
COLOR_CARD = "#121829"            # Primary card surface
COLOR_CARD_BORDER = "#1e293b"     # Card border outline
COLOR_CARD_INNER = "#182038"      # Inner nested item surface
COLOR_CARD_INNER_HOVER = "#202c4c"
COLOR_TEXT_PRIMARY = "#f8fafc"    # Bright text
COLOR_TEXT_MUTED = "#94a3b8"      # Muted subtext
COLOR_TEXT_ACCENT = "#818cf8"     # Indigo text

# Button Styling (Indigo Gradient Inspired)
COLOR_BTN_PRIMARY = "#4338ca"     # Deep Indigo
COLOR_BTN_PRIMARY_HOVER = "#4f46e5"
COLOR_BTN_SEC = "#1e293b"         # Dark slate
COLOR_BTN_SEC_HOVER = "#2a374f"
COLOR_BTN_SEC_BORDER = "#334155"

# Status Badges Colors (bg, text, border)
STATUS_COLORS = {
    ShowStatus.OK: {"bg": "#064e3b", "text": "#34d399", "border": "#059669"},
    ShowStatus.PARTIAL: {"bg": "#78350f", "text": "#fbbf24", "border": "#d97706"},
    ShowStatus.SOLD_OUT: {"bg": "#7f1d1d", "text": "#f87171", "border": "#dc2626"},
    ShowStatus.ENDED: {"bg": "#374151", "text": "#9ca3af", "border": "#4b5563"},
    ShowStatus.BLOCKED: {"bg": "#581c87", "text": "#c084fc", "border": "#9333ea"},
    ShowStatus.FAILED: {"bg": "#881337", "text": "#fb7185", "border": "#e11d48"},
    ShowStatus.IN_FLIGHT: {"bg": "#1e3a8a", "text": "#60a5fa", "border": "#2563eb"},
    ShowStatus.PENDING: {"bg": "#1e293b", "text": "#94a3b8", "border": "#334155"},
}


class ShowCardWidget(ctk.CTkFrame):
    """Modern card item representing a single monitored event with selection checkbox."""

    def __init__(
        self,
        master,
        show: Show,
        on_toggle: Optional[Callable[[], None]] = None,
        initial_checked: bool = True,
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color=COLOR_CARD_INNER,
            border_color="#263352",
            border_width=1,
            corner_radius=10,
            **kwargs,
        )
        self.show = show
        self.on_toggle = on_toggle
        self._is_checked = ctk.BooleanVar(value=initial_checked)
        self._build_card()
        self._update_appearance()

    @property
    def is_selected(self) -> bool:
        """Whether this show is marked for checking."""
        return bool(self._is_checked.get())

    def set_selected(self, val: bool, trigger_callback: bool = True) -> None:
        """Programmatically set checkbox state."""
        self._is_checked.set(val)
        self._update_appearance()
        if trigger_callback and self.on_toggle:
            self.on_toggle()

    def _on_check_changed(self) -> None:
        """Internal callback when user clicks checkbox."""
        self._update_appearance()
        if self.on_toggle:
            self.on_toggle()

    def _update_appearance(self) -> None:
        """Visually reflect enabled/disabled state of card."""
        if self.is_selected:
            self.configure(fg_color=COLOR_CARD_INNER, border_color="#263352")
            if hasattr(self, "lbl_name"):
                self.lbl_name.configure(text_color=COLOR_TEXT_PRIMARY)
        else:
            self.configure(fg_color="#0e1424", border_color="#182033")
            if hasattr(self, "lbl_name"):
                self.lbl_name.configure(text_color="#64748b")

    def _build_card(self):
        # Far-left section: Selection Checkbox
        self.chk_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.chk_frame.pack(side="left", padx=(14, 0), pady=10)

        self.chk_select = ctk.CTkCheckBox(
            self.chk_frame,
            text="",
            variable=self._is_checked,
            width=22,
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            border_width=2,
            border_color="#475569",
            fg_color=COLOR_BTN_PRIMARY,
            hover_color=COLOR_BTN_PRIMARY_HOVER,
            checkmark_color="#ffffff",
            command=self._on_check_changed,
        )
        self.chk_select.pack(anchor="center")

        # Left section: Icon + Event info
        self.left_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.left_frame.pack(side="left", fill="both", expand=True, padx=(8, 14), pady=10)

        title_frame = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        title_frame.pack(fill="x", anchor="w")

        self.lbl_icon = ctk.CTkLabel(
            title_frame,
            text="🎟️",
            font=ctk.CTkFont(size=14),
        )
        self.lbl_icon.pack(side="left", padx=(0, 6))

        self.lbl_name = ctk.CTkLabel(
            title_frame,
            text=self.show.name,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
            anchor="w",
        )
        self.lbl_name.pack(side="left", fill="x")

        # Subtitle details (Target & ticket index)
        sub_info = f"Цель: {self.show.target_total} шт. • Лимит: {self.show.max_per_order}"
        if self.show.ticket_index:
            sub_info += f" • Тип билета #{self.show.ticket_index}"

        self.lbl_sub = ctk.CTkLabel(
            self.left_frame,
            text=sub_info,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
            anchor="w",
        )
        self.lbl_sub.pack(fill="x", anchor="w", pady=(2, 0))

        # Center section: Progress Bar & Reserved Count
        self.center_frame = ctk.CTkFrame(self, fg_color="transparent", width=180)
        self.center_frame.pack(side="left", padx=12, pady=10)

        self.lbl_count = ctk.CTkLabel(
            self.center_frame,
            text=f"0 / {self.show.target_total}",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_TEXT_MUTED,
        )
        self.lbl_count.pack(anchor="center")

        self.progress_bar = ctk.CTkProgressBar(
            self.center_frame,
            width=160,
            height=6,
            corner_radius=3,
            fg_color="#0f172a",
            progress_color="#6366f1",
        )
        self.progress_bar.set(0.0)
        self.progress_bar.pack(anchor="center", pady=(4, 0))

        # Right section: Status Badge
        self.right_frame = ctk.CTkFrame(self, fg_color="transparent", width=140)
        self.right_frame.pack(side="right", padx=14, pady=10)

        self.lbl_status = ctk.CTkLabel(
            self.right_frame,
            text="PENDING",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color="#94a3b8",
            fg_color="#1e293b",
            corner_radius=6,
            width=100,
            height=26,
        )
        self.lbl_status.pack(anchor="e")

    def update_result(self, res: CheckResult):
        """Update card state with check outcome."""
        color_info = STATUS_COLORS.get(res.status, STATUS_COLORS[ShowStatus.PENDING])
        self.lbl_status.configure(
            text=res.status.value,
            text_color=color_info["text"],
            fg_color=color_info["bg"],
        )

        ratio = 0.0
        if res.target > 0:
            ratio = min(1.0, max(0.0, res.reserved / res.target))

        self.lbl_count.configure(
            text=f"{res.reserved} / {res.target}",
            text_color=COLOR_TEXT_PRIMARY if res.reserved > 0 else COLOR_TEXT_MUTED,
        )

        # Progress bar color logic
        if res.status == ShowStatus.OK:
            self.progress_bar.configure(progress_color="#10b981")
            self.progress_bar.set(1.0)
        elif res.status == ShowStatus.PARTIAL:
            self.progress_bar.configure(progress_color="#f59e0b")
            self.progress_bar.set(ratio)
        elif res.status in (ShowStatus.SOLD_OUT, ShowStatus.ENDED, ShowStatus.BLOCKED, ShowStatus.FAILED):
            self.progress_bar.configure(progress_color="#ef4444")
            self.progress_bar.set(ratio)
        else:
            self.progress_bar.set(ratio)

        if res.details:
            self.lbl_sub.configure(text=f"{res.details}")


class EtixGuiApp(ctk.CTk):
    """Modern Dark Card Dashboard for Etix Checker."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Etix Checker 2026 — AdsPower Edition")
        self.geometry("1020x760")
        self.minsize(900, 640)
        self.configure(fg_color=COLOR_BG)

        icon_file = Path("icons/etix_robot_round.ico")
        if icon_file.exists():
            try:
                self.iconbitmap(str(icon_file))
            except Exception:
                pass

        self.event_queue: queue.Queue = queue.Queue()
        self.is_running = False
        self.client = AdsPowerClient(base_url=CONFIG.adspower_api_url)
        self.profile_manager = AdsPowerProfileManager(client=self.client)
        self.backup_service = ProfileBackupService()
        self.update_service = UpdateService()
        self.show_cards: Dict[str, ShowCardWidget] = {}

        self._build_ui()
        self._load_shows_preview()
        self._check_adspower_status_async()
        self._poll_queue()

    def _build_ui(self) -> None:
        # 1. Top Header Card (Inspired by 3D Card Design)
        self.header_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD,
            border_color=COLOR_CARD_BORDER,
            border_width=1,
            corner_radius=14,
        )
        self.header_card.pack(fill="x", padx=20, pady=(16, 10))

        header_inner = ctk.CTkFrame(self.header_card, fg_color="transparent")
        header_inner.pack(fill="x", padx=18, pady=14)

        # Title & Subtitle with Icon
        title_left = ctk.CTkFrame(header_inner, fg_color="transparent")
        title_left.pack(side="left", fill="both", expand=True)

        title_row = ctk.CTkFrame(title_left, fg_color="transparent")
        title_row.pack(anchor="w")

        self.badge_tag = ctk.CTkLabel(
            title_row,
            text="✨ CDP 2026",
            font=ctk.CTkFont(family=FONT_FAMILY, size=10, weight="bold"),
            text_color="#818cf8",
            fg_color="#1e1b4b",
            corner_radius=6,
            padx=6,
            pady=2,
        )
        self.badge_tag.pack(side="left", padx=(0, 8))

        self.lbl_title = ctk.CTkLabel(
            title_row,
            text="Etix Checker — AdsPower CDP Edition",
            font=ctk.CTkFont(family=FONT_FAMILY, size=17, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
        )
        self.lbl_title.pack(side="left")

        # AdsPower Status indicator with glowing pill
        self.adspower_pill = ctk.CTkFrame(
            header_inner,
            fg_color="#182038",
            border_color="#263352",
            border_width=1,
            corner_radius=8,
        )
        self.adspower_pill.pack(side="right", padx=(10, 0))

        self.lbl_adspower_dot = ctk.CTkLabel(
            self.adspower_pill,
            text="●",
            font=ctk.CTkFont(size=14),
            text_color="#f59e0b",
        )
        self.lbl_adspower_dot.pack(side="left", padx=(10, 4), pady=6)

        self.lbl_adspower = ctk.CTkLabel(
            self.adspower_pill,
            text="AdsPower: Проверка...",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_TEXT_MUTED,
        )
        self.lbl_adspower.pack(side="left", padx=(0, 10), pady=6)

        # 2. Metric Stat Badges Bar
        self.stats_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.stats_bar.pack(fill="x", padx=20, pady=(0, 10))

        self.stat_shows = self._create_metric_chip(self.stats_bar, "📋 Событий", "0")
        self.stat_shows.pack(side="left", padx=(0, 8))

        self.stat_reserved = self._create_metric_chip(self.stats_bar, "📦 Набрано билетов", "0 / 0")
        self.stat_reserved.pack(side="left", padx=8)

        self.stat_workers = self._create_metric_chip(self.stats_bar, "⚡ Активных воркеров", "—")
        self.stat_workers.pack(side="left", padx=8)

        self.stat_status = self._create_metric_chip(self.stats_bar, "🚀 Статус", "Готов к запуску")
        self.stat_status.pack(side="left", padx=8)

        # 3. Action Toolbar (Styled Buttons)
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(fill="x", padx=20, pady=(0, 12))

        # Primary Start Button (Indigo gradient style)
        self.btn_start = ctk.CTkButton(
            self.btn_frame,
            text="▶  Запустить проверку",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            fg_color=COLOR_BTN_PRIMARY,
            hover_color=COLOR_BTN_PRIMARY_HOVER,
            text_color="#ffffff",
            height=38,
            corner_radius=8,
            command=self._on_start_clicked,
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        # Secondary Action Buttons
        self.btn_backup = self._create_action_btn("💾  Бэкап профилей", self._on_backup_clicked)
        self.btn_backup.pack(side="left", padx=6)

        self.btn_shows = self._create_action_btn("📄  shows.csv", lambda: self._open_file(CONFIG.shows_csv))
        self.btn_shows.pack(side="left", padx=6)

        self.btn_report = self._create_action_btn("📊  report.csv", lambda: self._open_file(Path("report.csv")))
        self.btn_report.pack(side="left", padx=6)

        self.btn_blocked = self._create_action_btn("🚫  Заблокированные", lambda: self._open_file(Path("data/blocked_profiles.txt")))
        self.btn_blocked.pack(side="left", padx=6)

        self.btn_logs = self._create_action_btn("📁  Логи", lambda: self._open_folder(CONFIG.logs_dir))
        self.btn_logs.pack(side="left", padx=6)

        self.btn_updates = self._create_action_btn("🔄  Проверить обновления", self._on_check_updates_clicked)
        self.btn_updates.pack(side="left", padx=6)

        # 4. Main Content Card with Tabs (Dashboard Cards vs Live Logs)
        self.main_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD,
            border_color=COLOR_CARD_BORDER,
            border_width=1,
            corner_radius=14,
        )
        self.main_card.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        # Tabview
        self.tabview = ctk.CTkTabview(
            self.main_card,
            fg_color="transparent",
            segmented_button_fg_color="#0f172a",
            segmented_button_selected_color=COLOR_BTN_PRIMARY,
            segmented_button_selected_hover_color=COLOR_BTN_PRIMARY_HOVER,
            segmented_button_unselected_color="#182038",
            segmented_button_unselected_hover_color="#202c4c",
            text_color=COLOR_TEXT_PRIMARY,
            corner_radius=10,
        )
        self.tabview.pack(fill="both", expand=True, padx=12, pady=8)

        self.tab_dashboard = self.tabview.add("📊  Панель мониторинга")
        self.tab_logs = self.tabview.add("📜  Журнал событий (Лог)")
        self.tab_settings = self.tabview.add("⚙️  Настройки задержек")

        # Quick Selection Bar
        self.selection_bar = ctk.CTkFrame(self.tab_dashboard, fg_color="transparent")
        self.selection_bar.pack(fill="x", padx=6, pady=(4, 6))

        self.btn_select_all = ctk.CTkButton(
            self.selection_bar,
            text="✓  Выбрать все",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            fg_color="#1e293b",
            hover_color="#334155",
            border_color="#334155",
            border_width=1,
            text_color="#cbd5e1",
            height=28,
            corner_radius=6,
            command=self._on_select_all_clicked,
        )
        self.btn_select_all.pack(side="left", padx=(0, 6))

        self.btn_deselect_all = ctk.CTkButton(
            self.selection_bar,
            text="✕  Снять все",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            fg_color="#1e293b",
            hover_color="#334155",
            border_color="#334155",
            border_width=1,
            text_color="#94a3b8",
            height=28,
            corner_radius=6,
            command=self._on_deselect_all_clicked,
        )
        self.btn_deselect_all.pack(side="left", padx=6)

        self.lbl_selected_counter = ctk.CTkLabel(
            self.selection_bar,
            text="Выбрано: 0 из 0 событий",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
        )
        self.lbl_selected_counter.pack(side="right", padx=6)

        # Tab 1: Scrollable Show Cards
        self.scroll_shows = ctk.CTkScrollableFrame(
            self.tab_dashboard,
            fg_color="transparent",
        )
        self.scroll_shows.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab 2: Terminal Logs View
        self.txt_log = ctk.CTkTextbox(
            self.tab_logs,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#0b0f19",
            text_color="#e2e8f0",
            border_color="#1e293b",
            border_width=1,
            corner_radius=8,
            wrap="none",
        )
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab 3: Delays Configuration
        self._build_delays_tab()

    def _build_delays_tab(self) -> None:
        """Build user configuration interface for pipeline delays with hardware-enforced minimums."""
        scroll = ctk.CTkScrollableFrame(self.tab_settings, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # 1. Info Banner Card
        banner = ctk.CTkFrame(
            scroll,
            fg_color=COLOR_CARD_INNER,
            border_color=COLOR_CARD_BORDER,
            border_width=1,
            corner_radius=10,
        )
        banner.pack(fill="x", pady=(0, 14), padx=4)

        b_inner = ctk.CTkFrame(banner, fg_color="transparent")
        b_inner.pack(fill="x", padx=16, pady=12)

        lbl_b_title = ctk.CTkLabel(
            b_inner,
            text="⚙️  Пользовательская калибровка задержек конвейера",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
        )
        lbl_b_title.pack(anchor="w")

        lbl_b_desc = ctk.CTkLabel(
            b_inner,
            text=(
                "Настройте интервалы навигации, паузы синхронизации React/MUI, сдвиги кликов и время удержания корзин.\n"
                "🛡️ Аппаратная защита: программа физически блокирует установку значений ниже безопасного порога (MIN_SAFE_DELAYS + 100 мс буфер)."
            ),
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
            justify="left",
        )
        lbl_b_desc.pack(anchor="w", pady=(4, 0))

        # 2. Input Fields Grid
        grid_card = ctk.CTkFrame(
            scroll,
            fg_color=COLOR_CARD_INNER,
            border_color=COLOR_CARD_BORDER,
            border_width=1,
            corner_radius=10,
        )
        grid_card.pack(fill="x", pady=(0, 14), padx=4)

        g_inner = ctk.CTkFrame(grid_card, fg_color="transparent")
        g_inner.pack(fill="x", padx=16, pady=14)

        self.delay_entries: Dict[str, Any] = {}

        # Parameter 1: Batch Nav Delay
        self._create_range_delay_row(
            g_inner,
            row=0,
            label="1. Интервал навигации между воркерами (мс):",
            sub="Разнос старта браузеров во времени. Аппаратный минимум: 350 мс",
            key="batch_nav",
            default_val=CONFIG.batch_nav_delay_ms,
            min_val=MIN_SAFE_DELAYS["batch_nav_delay_ms"],
        )

        # Parameter 2: After Click Sleep
        self._create_range_delay_row(
            g_inner,
            row=1,
            label="2. Пауза после выбора количества (мс):",
            sub="Время для обновления состояния React/MUI combobox. Аппаратный минимум: 500 мс",
            key="after_click",
            default_val=CONFIG.after_click_sleep_ms,
            min_val=MIN_SAFE_DELAYS["after_click_sleep_ms"],
        )

        # Parameter 3: Add Tickets Stagger
        self._create_range_delay_row(
            g_inner,
            row=2,
            label="3. Сдвиг клика «Add Tickets» между воркерами (мс):",
            sub="Интервал параллельного добавления билетов в корзины. Аппаратный минимум: 900 мс",
            key="add_stagger",
            default_val=CONFIG.add_sequential_delay_ms,
            min_val=MIN_SAFE_DELAYS["add_sequential_delay_ms"],
        )

        # Parameter 4: Hold Duration
        self._create_single_delay_row(
            g_inner,
            row=3,
            label="4. Время удержания брони перед очисткой (сек):",
            sub="Пауза удержания зарезервированных билетов перед Clear Cart. Аппаратный минимум: 2.2 с",
            key="hold_seconds",
            default_val=CONFIG.delay_before_clear_carts_s,
            min_val=MIN_SAFE_DELAYS["delay_before_clear_carts_s"],
            unit="сек",
        )

        # Parameter 5: Clear Cart Stagger
        self._create_range_delay_row(
            g_inner,
            row=4,
            label="5. Сдвиг очистки корзин между воркерами (мс):",
            sub="Интервал освобождения инвентаря в корзинах. Аппаратный минимум: 400 мс",
            key="clear_stagger",
            default_val=CONFIG.clear_cart_stagger_ms,
            min_val=MIN_SAFE_DELAYS["clear_cart_stagger_ms"],
        )

        # Parameter 6: Navigation Timeout
        self._create_single_delay_row(
            g_inner,
            row=5,
            label="6. Таймаут загрузки страницы / прокси (мс):",
            sub="Максимальное ожидание ответа страницы перед Hot-Swap заменой. Аппаратный минимум: 12000 мс",
            key="nav_timeout",
            default_val=CONFIG.nav_timeout,
            min_val=MIN_SAFE_DELAYS["nav_timeout"],
            unit="мс",
        )

        # 3. Live Duration Estimator Badge
        est_card = ctk.CTkFrame(
            scroll,
            fg_color="#182038",
            border_color="#312e81",
            border_width=1,
            corner_radius=10,
        )
        est_card.pack(fill="x", pady=(0, 14), padx=4)

        est_inner = ctk.CTkFrame(est_card, fg_color="transparent")
        est_inner.pack(fill="x", padx=16, pady=10)

        self.lbl_est_duration = ctk.CTkLabel(
            est_inner,
            text="⏱  Ориентировочная скорость проверки 1 шоу (на 12 профилей): ~45.0 сек",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color="#818cf8",
        )
        self.lbl_est_duration.pack(side="left")

        # 4. Action Buttons (Save and Reset)
        btn_box = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_box.pack(fill="x", padx=4, pady=6)

        btn_save = ctk.CTkButton(
            btn_box,
            text="💾  Сохранить настройки задержек",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            fg_color=COLOR_BTN_PRIMARY,
            hover_color=COLOR_BTN_PRIMARY_HOVER,
            text_color="#ffffff",
            height=38,
            corner_radius=8,
            command=self._on_save_delays_clicked,
        )
        btn_save.pack(side="left", padx=(0, 10))

        btn_reset = ctk.CTkButton(
            btn_box,
            text="🔄  Сбросить по умолчанию",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_BTN_SEC,
            hover_color=COLOR_BTN_SEC_HOVER,
            border_color=COLOR_BTN_SEC_BORDER,
            border_width=1,
            text_color="#cbd5e1",
            height=38,
            corner_radius=8,
            command=self._on_reset_delays_clicked,
        )
        btn_reset.pack(side="left")

    def _create_range_delay_row(
        self,
        parent,
        row: int,
        label: str,
        sub: str,
        key: str,
        default_val: Tuple[int, int],
        min_val: float,
    ) -> None:
        row_frame = ctk.CTkFrame(parent, fg_color="transparent")
        row_frame.pack(fill="x", pady=8)

        left = ctk.CTkFrame(row_frame, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)

        lbl = ctk.CTkLabel(
            left,
            text=label,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
        )
        lbl.pack(anchor="w")

        lbl_sub = ctk.CTkLabel(
            left,
            text=sub,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
            text_color=COLOR_TEXT_MUTED,
        )
        lbl_sub.pack(anchor="w")

        right = ctk.CTkFrame(row_frame, fg_color="transparent")
        right.pack(side="right")

        ent_min = ctk.CTkEntry(
            right,
            width=70,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#0b0f19",
            border_color="#334155",
            justify="center",
        )
        ent_min.insert(0, str(default_val[0]))
        ent_min.pack(side="left", padx=4)

        lbl_dash = ctk.CTkLabel(right, text="—", font=ctk.CTkFont(size=12), text_color=COLOR_TEXT_MUTED)
        lbl_dash.pack(side="left")

        ent_max = ctk.CTkEntry(
            right,
            width=70,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#0b0f19",
            border_color="#334155",
            justify="center",
        )
        ent_max.insert(0, str(default_val[1]))
        ent_max.pack(side="left", padx=4)

        lbl_unit = ctk.CTkLabel(right, text="мс", font=ctk.CTkFont(size=11), text_color=COLOR_TEXT_MUTED)
        lbl_unit.pack(side="left", padx=(0, 4))

        self.delay_entries[key] = {
            "type": "range",
            "min_ent": ent_min,
            "max_ent": ent_max,
            "min_bound": min_val,
        }

    def _create_single_delay_row(
        self,
        parent,
        row: int,
        label: str,
        sub: str,
        key: str,
        default_val: float,
        min_val: float,
        unit: str,
    ) -> None:
        row_frame = ctk.CTkFrame(parent, fg_color="transparent")
        row_frame.pack(fill="x", pady=8)

        left = ctk.CTkFrame(row_frame, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)

        lbl = ctk.CTkLabel(
            left,
            text=label,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
        )
        lbl.pack(anchor="w")

        lbl_sub = ctk.CTkLabel(
            left,
            text=sub,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
            text_color=COLOR_TEXT_MUTED,
        )
        lbl_sub.pack(anchor="w")

        right = ctk.CTkFrame(row_frame, fg_color="transparent")
        right.pack(side="right")

        ent = ctk.CTkEntry(
            right,
            width=90,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#0b0f19",
            border_color="#334155",
            justify="center",
        )
        ent.insert(0, str(default_val))
        ent.pack(side="left", padx=4)

        lbl_unit = ctk.CTkLabel(right, text=unit, font=ctk.CTkFont(size=11), text_color=COLOR_TEXT_MUTED)
        lbl_unit.pack(side="left", padx=(0, 4))

        self.delay_entries[key] = {
            "type": "single",
            "entry": ent,
            "min_bound": min_val,
        }

    def _on_save_delays_clicked(self) -> None:
        """Validate input values, clamp to MIN_SAFE_DELAYS, save to .env and reload CONFIG."""
        updates: Dict[str, Any] = {}
        had_auto_clamp = False

        try:
            # 1. batch_nav
            c = self.delay_entries["batch_nav"]
            lo = max(int(c["min_ent"].get().strip()), int(c["min_bound"]))
            hi = max(int(c["max_ent"].get().strip()), lo)
            if int(c["min_ent"].get().strip()) < c["min_bound"]:
                had_auto_clamp = True
            c["min_ent"].delete(0, "end")
            c["min_ent"].insert(0, str(lo))
            c["max_ent"].delete(0, "end")
            c["max_ent"].insert(0, str(hi))
            updates["ETIX_BATCH_NAV_DELAY_MS"] = f"{lo}-{hi}"

            # 2. after_click
            c = self.delay_entries["after_click"]
            lo = max(int(c["min_ent"].get().strip()), int(c["min_bound"]))
            hi = max(int(c["max_ent"].get().strip()), lo)
            if int(c["min_ent"].get().strip()) < c["min_bound"]:
                had_auto_clamp = True
            c["min_ent"].delete(0, "end")
            c["min_ent"].insert(0, str(lo))
            c["max_ent"].delete(0, "end")
            c["max_ent"].insert(0, str(hi))
            updates["ETIX_AFTER_CLICK_SLEEP_MS"] = f"{lo}-{hi}"

            # 3. add_stagger
            c = self.delay_entries["add_stagger"]
            lo = max(int(c["min_ent"].get().strip()), int(c["min_bound"]))
            hi = max(int(c["max_ent"].get().strip()), lo)
            if int(c["min_ent"].get().strip()) < c["min_bound"]:
                had_auto_clamp = True
            c["min_ent"].delete(0, "end")
            c["min_ent"].insert(0, str(lo))
            c["max_ent"].delete(0, "end")
            c["max_ent"].insert(0, str(hi))
            updates["ETIX_ADD_SEQUENTIAL_DELAY_MS"] = f"{lo}-{hi}"

            # 4. hold_seconds
            c = self.delay_entries["hold_seconds"]
            raw_s = float(c["entry"].get().strip())
            safe_s = max(raw_s, float(c["min_bound"]))
            if raw_s < c["min_bound"]:
                had_auto_clamp = True
            c["entry"].delete(0, "end")
            c["entry"].insert(0, f"{safe_s:.1f}")
            updates["ETIX_DELAY_BEFORE_CLEAR_CARTS_S"] = f"{safe_s:.1f}"

            # 5. clear_stagger
            c = self.delay_entries["clear_stagger"]
            lo = max(int(c["min_ent"].get().strip()), int(c["min_bound"]))
            hi = max(int(c["max_ent"].get().strip()), lo)
            if int(c["min_ent"].get().strip()) < c["min_bound"]:
                had_auto_clamp = True
            c["min_ent"].delete(0, "end")
            c["min_ent"].insert(0, str(lo))
            c["max_ent"].delete(0, "end")
            c["max_ent"].insert(0, str(hi))
            updates["ETIX_CLEAR_CART_STAGGER_MS"] = f"{lo}-{hi}"

            # 6. nav_timeout
            c = self.delay_entries["nav_timeout"]
            raw_t = int(c["entry"].get().strip())
            safe_t = max(raw_t, int(c["min_bound"]))
            if raw_t < c["min_bound"]:
                had_auto_clamp = True
            c["entry"].delete(0, "end")
            c["entry"].insert(0, str(safe_t))
            updates["ETIX_NAV_TIMEOUT"] = str(safe_t)

            # Persist and reload
            save_delays_to_dotenv(updates)
            new_cfg = reload_config()

            est = estimate_check_duration_seconds(12, new_cfg)
            self.lbl_est_duration.configure(
                text=f"⏱  Ориентировочная скорость проверки 1 шоу (на 12 профилей): ~{est} сек"
            )

            clamp_note = "\n\n⚠️ Некоторые значения были автоматически скорректированы до безопасного минимума." if had_auto_clamp else ""
            messagebox.showinfo(
                "Настройки задержек",
                f"Настройки задержек успешно сохранены в .env и применены!{clamp_note}\n\nРасчетное время: ~{est} сек на шоу (12 профилей)."
            )
            self._log(f"⚙️ Сохранены новые задержки: {updates}")

        except Exception as exc:
            messagebox.showerror("Ошибка валидации", f"Некорректный формат чисел: {exc}")

    def _on_reset_delays_clicked(self) -> None:
        """Reset delays in UI to safe human defaults."""
        defaults = {
            "batch_nav": (600, 1100),
            "after_click": (500, 900),
            "add_stagger": (1000, 1800),
            "hold_seconds": 4.0,
            "clear_stagger": (600, 1000),
            "nav_timeout": 18000,
        }
        for k, v in defaults.items():
            ent_info = self.delay_entries.get(k)
            if not ent_info:
                continue
            if ent_info["type"] == "range":
                ent_info["min_ent"].delete(0, "end")
                ent_info["min_ent"].insert(0, str(v[0]))
                ent_info["max_ent"].delete(0, "end")
                ent_info["max_ent"].insert(0, str(v[1]))
            else:
                ent_info["entry"].delete(0, "end")
                ent_info["entry"].insert(0, str(v))

        self.lbl_est_duration.configure(
            text="⏱  Ориентировочная скорость проверки 1 шоу (на 12 профилей): ~45.0 сек"
        )

    def _create_metric_chip(self, parent, label: str, value: str) -> ctk.CTkFrame:
        """Create a sleek metric badge chip."""
        chip = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD,
            border_color=COLOR_CARD_BORDER,
            border_width=1,
            corner_radius=8,
        )
        lbl_k = ctk.CTkLabel(
            chip,
            text=label,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
            text_color=COLOR_TEXT_MUTED,
        )
        lbl_k.pack(anchor="w", padx=10, pady=(6, 0))

        lbl_v = ctk.CTkLabel(
            chip,
            text=value,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_PRIMARY,
        )
        lbl_v.pack(anchor="w", padx=10, pady=(0, 6))
        chip.lbl_val = lbl_v
        return chip

    def _create_action_btn(self, text: str, command) -> ctk.CTkButton:
        """Create a styled secondary action button."""
        return ctk.CTkButton(
            self.btn_frame,
            text=text,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_BTN_SEC,
            hover_color=COLOR_BTN_SEC_HOVER,
            border_color=COLOR_BTN_SEC_BORDER,
            border_width=1,
            text_color=COLOR_TEXT_PRIMARY,
            height=38,
            corner_radius=8,
            command=command,
        )

    def _load_shows_preview(self) -> None:
        """Load shows from shows.csv and generate cards, preserving selection state."""
        existing_selection = {show_id: card.is_selected for show_id, card in self.show_cards.items()}

        for child in self.scroll_shows.winfo_children():
            child.destroy()
        self.show_cards.clear()

        engine = EtixCheckEngine(config=CONFIG)
        shows = engine.load_shows(CONFIG.shows_csv)

        if not shows:
            self.stat_shows.lbl_val.configure(text="0")
            self.stat_reserved.lbl_val.configure(text="0 / 0")
            if hasattr(self, "lbl_selected_counter"):
                self.lbl_selected_counter.configure(text="Выбрано: 0 из 0 событий")
            empty_lbl = ctk.CTkLabel(
                self.scroll_shows,
                text="Файл shows.csv пуст или не содержит событий. Нажмите 'shows.csv' для добавления ссылок.",
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                text_color=COLOR_TEXT_MUTED,
            )
            empty_lbl.pack(pady=40)
            return

        for s in shows:
            is_checked = existing_selection.get(s.show_id, True)
            card = ShowCardWidget(
                self.scroll_shows,
                show=s,
                on_toggle=self._update_selection_stats,
                initial_checked=is_checked,
            )
            card.pack(fill="x", pady=5)
            self.show_cards[s.show_id] = card

        self._update_selection_stats()

    def _update_selection_stats(self) -> None:
        """Recalculate and update top metric chips based on checked shows."""
        selected_cards = [c for c in self.show_cards.values() if c.is_selected]
        total_count = len(self.show_cards)
        selected_count = len(selected_cards)
        selected_target = sum(c.show.target_total for c in selected_cards)

        self.stat_shows.lbl_val.configure(text=f"{selected_count} из {total_count}")
        self.stat_reserved.lbl_val.configure(text=f"0 / {selected_target}")
        if hasattr(self, "lbl_selected_counter"):
            self.lbl_selected_counter.configure(
                text=f"Выбрано: {selected_count} из {total_count} событий"
            )

    def _on_select_all_clicked(self) -> None:
        """Select all shows."""
        for card in self.show_cards.values():
            card.set_selected(True, trigger_callback=False)
        self._update_selection_stats()

    def _on_deselect_all_clicked(self) -> None:
        """Deselect all shows."""
        for card in self.show_cards.values():
            card.set_selected(False, trigger_callback=False)
        self._update_selection_stats()

    def _log(self, text: str) -> None:
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")

    def _check_adspower_status_async(self) -> None:
        def worker():
            async def run():
                alive = await self.client.check_status()
                if alive:
                    profiles = await self.profile_manager.load_and_organize_profiles(
                        group_name=CONFIG.adspower_group_name,
                    )
                    free_count = len(self.profile_manager.get_available_free_profiles())
                    busy_count = len(self.profile_manager.get_busy_external_profiles())
                    total_count = len(profiles)
                    busy_tag = f" • {busy_count} занято" if busy_count > 0 else ""
                    self.event_queue.put(
                        (
                            "adspower_status",
                            True,
                            f"AdsPower: {free_count} своб. из {total_count}{busy_tag}",
                            free_count,
                        )
                    )
                else:
                    self.event_queue.put(
                        (
                            "adspower_status",
                            False,
                            "AdsPower: Офлайн (порт 50325)",
                            0,
                        )
                    )

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()

    def _on_backup_clicked(self) -> None:
        def worker():
            async def run():
                try:
                    profiles = await self.client.get_profiles_by_group(group_name=CONFIG.adspower_group_name)
                    if profiles:
                        backup_file = self.backup_service.backup_profiles(CONFIG.adspower_group_name, profiles)
                        self.event_queue.put(("backup_done", True, str(backup_file)))
                    else:
                        self.event_queue.put(("backup_done", False, "Профили не найдены"))
                except Exception as exc:
                    self.event_queue.put(("backup_done", False, str(exc)))

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_clicked(self) -> None:
        if self.is_running:
            return

        selected_cards = [c for c in self.show_cards.values() if c.is_selected]
        if not selected_cards:
            messagebox.showwarning(
                "Нет выбранных событий",
                "Пожалуйста, выберите хотя бы одно событие галочкой для запуска проверки.",
            )
            return

        selected_shows = [c.show for c in selected_cards]

        # Pre-flight Concurrency & Profile Sufficiency Check
        free_profiles = self.profile_manager.get_available_free_profiles()
        busy_profiles = self.profile_manager.get_busy_external_profiles()

        max_needed_workers = 1
        for s in selected_shows:
            limit = s.max_per_order if s.max_per_order and s.max_per_order > 0 else 1
            needed_for_show = math.ceil(s.target_total / limit)
            if needed_for_show > max_needed_workers:
                max_needed_workers = needed_for_show

        if free_profiles and len(free_profiles) < max_needed_workers:
            busy_ids = [p.user_id for p in busy_profiles]
            busy_details = (
                f"\n\n👥 Занято другими задачами/пользователями: {len(busy_profiles)} шт. (ID: {', '.join(busy_ids[:5])}{'...' if len(busy_ids) > 5 else ''})"
                if busy_profiles else ""
            )
            messagebox.showwarning(
                "Недостаточно свободных профилей",
                f"Для проверки выбранных событий требуется: {max_needed_workers} свободных профилей.\n"
                f"Доступно свободно в группе: {len(free_profiles)} профилей.{busy_details}\n\n"
                f"Пожалуйста, закройте открытые браузеры в AdsPower или добавьте новые профили в группу '{CONFIG.adspower_group_name}'."
            )
            return

        self.is_running = True
        self.btn_start.configure(state="disabled", text="⏳  Проверка выполняется...")
        self.stat_status.lbl_val.configure(text="Выполняется...", text_color="#f59e0b")
        self._log("=========================================")
        self._log(
            f"🚀 Запуск процесса проверки Etix (AdsPower CDP) для {len(selected_cards)} выбранных событий..."
        )

        # Reset state only for selected cards
        for card in selected_cards:
            card.lbl_status.configure(text="PENDING", text_color="#94a3b8", fg_color="#1e293b")
            card.lbl_count.configure(text=f"0 / {card.show.target_total}", text_color=COLOR_TEXT_MUTED)
            card.progress_bar.configure(progress_color="#6366f1")
            card.progress_bar.set(0.0)

        def worker():
            async def run():
                engine = EtixCheckEngine(
                    config=CONFIG,
                    client=self.client,
                    profile_manager=self.profile_manager,
                )

                def on_done(res: CheckResult, current: int, total: int):
                    self.event_queue.put(("show_done", res, current, total))

                try:
                    results = await engine.run(
                        shows_csv=CONFIG.shows_csv,
                        shows=selected_shows,
                        resume=True,
                        on_show_done=on_done,
                    )
                    self.event_queue.put(("check_completed", results))
                except Exception as exc:
                    self.event_queue.put(("check_failed", str(exc)))

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_updates_clicked(self) -> None:
        self.btn_updates.configure(state="disabled", text="⏳  Проверка...")
        self._log("🔍 Проверка наличия обновлений на GitHub...")

        def worker():
            async def run():
                has_update, version_info, err = await self.update_service.check_for_updates()
                self.event_queue.put(("update_check_result", has_update, version_info, err))

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()

    def _start_update_download(self, version_info: Optional[VersionInfo]) -> None:
        self.btn_updates.configure(state="disabled", text="⏳  Обновление...")
        self._log("⬇️ Скачивание и применение обновления...")

        def worker():
            async def run():
                ok, msg = await self.update_service.apply_update(remote_version=version_info)
                self.event_queue.put(("update_applied", ok, msg))

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.event_queue.get_nowait()
                kind = msg[0]

                if kind == "adspower_status":
                    ok, text, active_count = msg[1], msg[2], msg[3]
                    dot_color = "#10b981" if ok else "#ef4444"
                    txt_color = "#f8fafc" if ok else "#f87171"
                    self.lbl_adspower_dot.configure(text_color=dot_color)
                    self.lbl_adspower.configure(text=text, text_color=txt_color)
                    self.stat_workers.lbl_val.configure(text=f"{active_count} профилей" if ok else "Офлайн")

                elif kind == "backup_done":
                    ok, path_or_err = msg[1], msg[2]
                    if ok:
                        messagebox.showinfo("Бэкап профилей", f"Резервная копия успешно создана:\n{path_or_err}")
                        self._log(f"💾 Создан бэкап метаданных: {path_or_err}")
                    else:
                        messagebox.showerror("Ошибка бэкапа", f"Не удалось создать бэкап: {path_or_err}")

                elif kind == "update_check_result":
                    self.btn_updates.configure(state="normal", text="🔄  Проверить обновления")
                    has_update, version_info, err = msg[1], msg[2], msg[3]
                    if err:
                        self._log(f"❌ Ошибка проверки обновлений: {err}")
                        messagebox.showerror("Проверка обновлений", f"Не удалось проверить обновления:\n{err}")
                    elif not has_update:
                        local_ver = self.update_service.get_local_version()
                        ver_str = f"коммит {local_ver.short_sha}" if local_ver else "актуальная"
                        date_str = f" от {local_ver.date}" if local_ver and local_ver.date else ""
                        self._log(f"✅ У вас установлена актуальная версия ({ver_str}{date_str}).")
                        messagebox.showinfo("Обновления", f"У вас установлена самая свежая версия программы ({ver_str}{date_str})!")
                    else:
                        assert version_info is not None
                        self._log(f"🎉 Найдено обновление: {version_info.short_sha} — {version_info.message}")
                        update_prompt = (
                            f"🎉 Доступна новая версия программы!\n\n"
                            f"Коммит: {version_info.short_sha}\n"
                            f"Дата: {version_info.date}\n"
                            f"Описание: {version_info.message}\n\n"
                            f"⚠️ Пользовательские настройки (.env, shows.csv, прокси) будут сохранены.\n\n"
                            f"Обновить программу прямо сейчас в 1 клик?"
                        )
                        if messagebox.askyesno("Доступно обновление", update_prompt):
                            self._start_update_download(version_info)

                elif kind == "update_applied":
                    self.btn_updates.configure(state="normal", text="🔄  Проверить обновления")
                    ok, res_msg = msg[1], msg[2]
                    if ok:
                        self._log(f"🎉 {res_msg}")
                        messagebox.showinfo("Обновление завершено", f"{res_msg}\n\nПожалуйста, перезапустите программу для вступления изменений в силу.")
                    else:
                        self._log(f"❌ Ошибка обновления: {res_msg}")
                        messagebox.showerror("Ошибка обновления", res_msg)

                elif kind == "show_done":
                    res: CheckResult = msg[1]
                    current, total = msg[2], msg[3]
                    self.stat_status.lbl_val.configure(text=f"Прогресс: {current}/{total}")
                    if res.show_id in self.show_cards:
                        self.show_cards[res.show_id].update_result(res)
                    self._log(f"[{res.status.value}] {res.name} — Резерв: {res.reserved}/{res.target} ({res.details})")

                elif kind == "check_completed":
                    results: List[CheckResult] = msg[1]
                    self.is_running = False
                    self.btn_start.configure(state="normal", text="▶  Запустить проверку")
                    self.stat_status.lbl_val.configure(text="✅ Завершено", text_color="#10b981")
                    total_res = sum(r.reserved for r in results)
                    total_tgt = sum(r.target for r in results)
                    self.stat_reserved.lbl_val.configure(text=f"{total_res} / {total_tgt}")
                    self._log("🎉 Проверка успешно завершена. Отчет сохранен в report.csv.")

                    # Check for blocked profiles recorded in this session
                    blocked = self.profile_manager.get_session_blocked_profiles()
                    if blocked:
                        self._log("⚠️ =========================================")
                        self._log(f"⚠️ ВНИМАНИЕ: Во время проверки были заблокированы {len(blocked)} профилей:")
                        details_lines = []
                        for bp in blocked:
                            info = f"• {bp['name']} (ID: {bp['user_id']}) | Прокси: {bp.get('proxy', 'N/A')}"
                            self._log(f"   🚫 {info} — {bp.get('reason', 'Заблокирован')}")
                            details_lines.append(info)
                        self._log("📝 Полный список сохранен в 'data/blocked_profiles.txt'.")
                        self._log("⚠️ =========================================")

                        prompt_msg = (
                            f"Проверка завершена!\n\n"
                            f"⚠️ ОБНАРУЖЕНЫ БЛОКИРОВКИ НА {len(blocked)} ПРОФИЛЯХ:\n\n"
                            + "\n".join(details_lines[:8])
                            + (f"\n...и еще {len(blocked) - 8} шт." if len(blocked) > 8 else "")
                            + "\n\n💡 Список сохранен в файле: data/blocked_profiles.txt\n"
                            f"Рекомендуется сменить прокси или обновить цифровой отпечаток в AdsPower!"
                        )
                        messagebox.showwarning("Внимание: Заблокированные профили", prompt_msg)
                    else:
                        messagebox.showinfo("Готово", "Проверка завершена! Результаты сохранены в report.csv.")

                elif kind == "check_failed":
                    self.is_running = False
                    self.btn_start.configure(state="normal", text="▶  Запустить проверку")
                    self.stat_status.lbl_val.configure(text="❌ Ошибка", text_color="#ef4444")
                    err = msg[1]
                    self._log(f"❌ Ошибка: {err}")
                    messagebox.showerror("Ошибка проверки", err)

        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _open_file(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("Файл не найден", f"Файл {path} еще не создан.")
            return
        os.startfile(path)
        if path == CONFIG.shows_csv:
            self._load_shows_preview()

    def _open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)


if __name__ == "__main__":
    app = EtixGuiApp()
    app.mainloop()
