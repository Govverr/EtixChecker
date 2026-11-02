"""Service for collecting and packaging logs, reports, and error dumps into a single ZIP bundle."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import List, Optional
import zipfile

from src.utils.logger import LOGGER


class DiagnosticsCollector:
    """Collects and compresses diagnostic files into a desktop ZIP bundle."""

    @staticmethod
    def get_desktop_dir() -> Path:
        """Find the active user's Desktop directory reliably across Windows environments."""
        candidates = [
            Path(os.environ.get("USERPROFILE", "")) / "Desktop",
            Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop",
            Path.home() / "Desktop",
            Path.home() / "OneDrive" / "Desktop",
            Path("."),
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                return c
        return Path(".")

    @classmethod
    def collect_bundle(cls, output_dir: Optional[Path] = None) -> Path:
        """
        Package logs, reports, blocked profile audits, and recent error dumps into a ZIP bundle.
        Strictly excludes sensitive secrets (.env, proxy credentials).
        Falls back to local workspace directory if Desktop has write permission restrictions.
        """
        out_dir = output_dir or cls.get_desktop_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        bundle_name = f"Etix_Logs_{now_str}.zip"
        zip_path = out_dir / bundle_name

        files_to_pack: List[tuple[Path, str]] = []

        # 1. Logs directory: app.log and other recent logs
        logs_dir = Path("logs")
        if logs_dir.exists():
            for log_file in logs_dir.glob("*.log"):
                if log_file.is_file() and log_file.stat().st_size > 0:
                    files_to_pack.append((log_file, f"logs/{log_file.name}"))

        # 2. Report and rerun files
        for rep in [Path("report.csv"), Path("data/shows_rerun.csv")]:
            if rep.exists() and rep.is_file():
                files_to_pack.append((rep, rep.name))

        # 3. Blocked profiles and bad proxies history (sanitized audit files)
        for audit_file in [Path("data/blocked_profiles.txt"), Path("data/bad_proxies.txt")]:
            if audit_file.exists() and audit_file.is_file():
                files_to_pack.append((audit_file, f"data/{audit_file.name}"))

        # 4. Recent failure dumps and screenshots from screens/ (created in last 48 hours, up to 30 latest)
        screens_dir = Path("screens")
        if screens_dir.exists():
            cutoff_time = datetime.now().timestamp() - (48 * 3600)
            screen_files = [
                f
                for f in screens_dir.iterdir()
                if f.is_file()
                and f.suffix.lower() in (".png", ".html", ".htm")
                and f.stat().st_mtime >= cutoff_time
            ]
            screen_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            for sf in screen_files[:30]:
                files_to_pack.append((sf, f"screens/{sf.name}"))

        # 5. Generate sanitized system_info.txt
        sys_info_content = cls._generate_system_info()

        def _write_zip(target: Path):
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("system_info.txt", sys_info_content)
                for src_path, arc_name in files_to_pack:
                    try:
                        zf.write(src_path, arcname=arc_name)
                    except Exception as pack_err:
                        LOGGER.debug(f"Could not pack {src_path}: {pack_err}")

        try:
            _write_zip(zip_path)
        except (PermissionError, OSError) as perm_err:
            LOGGER.warning(
                f"Could not write diagnostic bundle to {out_dir}: {perm_err}. "
                f"Falling back to local workspace directory..."
            )
            fallback_dir = Path("runs") if Path("runs").exists() else Path(".")
            zip_path = fallback_dir / bundle_name
            _write_zip(zip_path)

        size_kb = zip_path.stat().st_size / 1024.0
        LOGGER.info(
            f"Diagnostic bundle created successfully: {zip_path} ({size_kb:.1f} KB, {len(files_to_pack) + 1} items)"
        )
        return zip_path

    @staticmethod
    def _generate_system_info() -> str:
        """Create sanitized environment telemetry summary without secrets."""
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        python_ver = sys.version.split()[0]
        arch = platform.architecture()[0]
        os_name = f"{platform.system()} {platform.release()}"
        win_build = platform.version()

        shows_count = 0
        shows_file = Path("data/shows.csv")
        if shows_file.exists():
            try:
                with open(shows_file, "r", encoding="utf-8", errors="ignore") as f:
                    shows_count = max(0, sum(1 for line in f if line.strip()) - 1)
            except Exception:
                pass

        info = [
            "=" * 60,
            "ETIX CHECKER - DIAGNOSTIC SYSTEM INFO",
            "=" * 60,
            f"Generated: {now_ts}",
            f"OS: {os_name} (Architecture: {platform.machine()})",
            f"Windows Build: {win_build}",
            f"Python: {python_ver} ({arch})",
            f"Working Directory: {Path('.').resolve()}",
            f"Configured Shows Count: {shows_count}",
            "",
            "SECURITY AUDIT NOTICE:",
            "  * Sensitive environment variables (.env) are strictly EXCLUDED.",
            "  * Private proxy credentials (good_proxies.txt) are strictly EXCLUDED.",
            "=" * 60,
        ]
        return "\n".join(info)

    @classmethod
    def open_in_explorer(cls, file_path: Path) -> None:
        """Reveal file in Windows Explorer with selection."""
        try:
            resolved = str(file_path.resolve())
            if sys.platform == "win32":
                subprocess.Popen(["explorer", f"/select,{resolved}"])
            else:
                subprocess.Popen(["open", str(file_path.parent)])
        except Exception as exc:
            LOGGER.warning(f"Could not open explorer for {file_path}: {exc}")


if __name__ == "__main__":
    bundle_path = DiagnosticsCollector.collect_bundle()
    DiagnosticsCollector.open_in_explorer(bundle_path)
    print(f"\n[+] Diagnostic bundle created successfully: {bundle_path}")
