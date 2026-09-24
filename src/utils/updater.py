"""Update service for checking remote GitHub releases/commits and performing safe 1-click updates."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from src.utils.logger import LOGGER

GITHUB_REPO_OWNER = os.getenv("ETIX_GITHUB_REPO_OWNER", "Govverr")
GITHUB_REPO_NAME = os.getenv("ETIX_GITHUB_REPO_NAME", "EtixChecker")
GITHUB_COMMITS_API = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/commits/main"
GITHUB_ZIP_URL = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/archive/refs/heads/main.zip"

# Protected relative paths that must NEVER be overwritten or deleted during update
PROTECTED_PATHS: Set[str] = {
    ".env",
    "data/shows.csv",
    "data/good_proxies.txt",
    "data/bad_proxies.txt",
    "data/blocked_profiles.txt",
    "data/adspower_backup",
    "runs",
    "logs",
    "screens",
    "venv",
    "ms-playwright",
    ".git",
    "tests",
    "scratch",
    ".backup_prev_version",
}


@dataclass
class VersionInfo:
    sha: str
    short_sha: str
    message: str
    author: str
    date: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "sha": self.sha,
            "short_sha": self.short_sha,
            "message": self.message,
            "author": self.author,
            "date": self.date,
        }


class UpdateService:
    """Handles version checks against GitHub and safe local updates."""

    def __init__(self, root_dir: Optional[Path] = None) -> None:
        self.root_dir = root_dir or Path.cwd()
        self.version_file = self.root_dir / ".version"
        self.backup_dir = self.root_dir / ".backup_prev_version"
        self.backup_version_file = self.backup_dir / ".version_backup"

    def get_local_version(self) -> Optional[VersionInfo]:
        """Get local commit version from Git or .version metadata file."""
        # 1. Try via Git CLI
        if (self.root_dir / ".git").exists():
            try:
                res = subprocess.run(
                    ["git", "log", "-1", "--format=%H|%h|%s|%an|%cd"],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                output = res.stdout.strip()
                if output and "|" in output:
                    parts = output.split("|", 4)
                    return VersionInfo(
                        sha=parts[0],
                        short_sha=parts[1],
                        message=parts[2] if len(parts) > 2 else "",
                        author=parts[3] if len(parts) > 3 else "",
                        date=parts[4] if len(parts) > 4 else "",
                    )
            except Exception as exc:
                LOGGER.debug(f"Git log failed: {exc}")

        # 2. Try via .version file
        if self.version_file.exists():
            try:
                data = json.loads(self.version_file.read_text(encoding="utf-8"))
                return VersionInfo(
                    sha=data.get("sha", ""),
                    short_sha=data.get("short_sha", data.get("sha", "")[:7]),
                    message=data.get("message", ""),
                    author=data.get("author", ""),
                    date=data.get("date", ""),
                )
            except Exception as exc:
                LOGGER.debug(f"Read .version failed: {exc}")

        return None

    async def check_for_updates(self) -> Tuple[bool, Optional[VersionInfo], Optional[str]]:
        """
        Check GitHub API for latest commit on main branch.
        Returns: (has_update, remote_version_info, error_message)
        """
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "EtixChecker-Updater/2026",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(GITHUB_COMMITS_API, headers=headers)
                if resp.status_code != 200:
                    return False, None, f"GitHub API вернул статус {resp.status_code}: {resp.text[:150]}"

                data = resp.json()
                sha = data.get("sha", "")
                commit_dict = data.get("commit", {})
                message = commit_dict.get("message", "").split("\n")[0]
                author = commit_dict.get("author", {}).get("name", "Unknown")
                date = commit_dict.get("author", {}).get("date", "")

                remote_info = VersionInfo(
                    sha=sha,
                    short_sha=sha[:7] if sha else "",
                    message=message,
                    author=author,
                    date=date,
                )

                local_info = self.get_local_version()
                if local_info is None:
                    # No local version info -> suggest update
                    return True, remote_info, None

                # If local SHA is different from remote SHA -> update available
                has_update = (local_info.sha.lower() != remote_info.sha.lower())
                return has_update, remote_info, None

        except Exception as exc:
            LOGGER.error(f"Error checking GitHub for updates: {exc}")
            return False, None, f"Не удалось связаться с сервером GitHub: {exc}"

    def _is_path_protected(self, rel_path_str: str) -> bool:
        """Check if relative path matches any protected files/directories."""
        norm = rel_path_str.replace("\\", "/").strip("/")
        for prot in PROTECTED_PATHS:
            prot_norm = prot.replace("\\", "/").strip("/")
            if norm == prot_norm or norm.startswith(f"{prot_norm}/"):
                return True
        return False

    async def apply_update(self, remote_version: Optional[VersionInfo] = None) -> Tuple[bool, str]:
        """
        Download latest main.zip or perform git pull, safely updating files
        without touching protected configuration and data files.
        Automatically creates a rollback snapshot of previous version before updating.
        """
        # Create rollback snapshot of current working version
        self.create_rollback_snapshot()

        # 1. Strategy: If git is initialized and available
        if (self.root_dir / ".git").exists():
            try:
                LOGGER.info("Attempting update via Git...")
                # Stash changes to protected files if any
                subprocess.run(["git", "fetch", "origin", "main"], cwd=str(self.root_dir), capture_output=True, timeout=15)
                res = subprocess.run(
                    ["git", "pull", "--no-rebase", "origin", "main"],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                if res.returncode == 0:
                    LOGGER.info("Git pull completed successfully.")
                    self._update_dependencies_if_needed()
                    return True, "Файлы программы успешно обновлены через Git!"
            except Exception as exc:
                LOGGER.warning(f"Git pull failed, falling back to ZIP download: {exc}")

        # 2. Strategy: ZIP Download fallback
        LOGGER.info(f"Downloading update ZIP from {GITHUB_ZIP_URL}...")
        temp_dir = Path(tempfile.mkdtemp(prefix="etix_update_"))
        zip_path = temp_dir / "repo.zip"
        extract_dir = temp_dir / "extracted"

        try:
            async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
                resp = await client.get(GITHUB_ZIP_URL)
                if resp.status_code != 200:
                    return False, f"Не удалось скачать архив с GitHub (код {resp.status_code})"
                zip_path.write_bytes(resp.content)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            # Find root folder in zip (e.g. EtixChecker-main)
            subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
            source_dir = subdirs[0] if subdirs else extract_dir

            # Recursively copy updated files while skipping protected paths
            updated_count = 0
            for root, dirs, files in os.walk(source_dir):
                rel_root = Path(root).relative_to(source_dir)

                for f in files:
                    src_file = Path(root) / f
                    rel_file = rel_root / f
                    rel_str = str(rel_file).replace("\\", "/")

                    if self._is_path_protected(rel_str):
                        LOGGER.debug(f"Skipping protected file: {rel_str}")
                        continue

                    dest_file = self.root_dir / rel_file
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    updated_count += 1

            LOGGER.info(f"Successfully updated {updated_count} files.")

            # Save version info
            if remote_version:
                self.version_file.write_text(
                    json.dumps(remote_version.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Update dependencies if needed
            self._update_dependencies_if_needed()

            return True, f"Успешно обновлено {updated_count} файлов проекта!"

        except Exception as exc:
            LOGGER.error(f"Failed to apply update: {exc}")
            return False, f"Ошибка при установке обновления: {exc}"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _update_dependencies_if_needed(self) -> None:
        """Run pip install -r requirements.txt if venv is present."""
        venv_pip = self.root_dir / "venv" / "Scripts" / "pip.exe"
        req_file = self.root_dir / "requirements.txt"
        if venv_pip.exists() and req_file.exists():
            try:
                LOGGER.info("Updating Python dependencies in venv...")
                subprocess.run(
                    [str(venv_pip), "install", "-r", str(req_file), "-q"],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    timeout=60,
                )
            except Exception as exc:
                LOGGER.warning(f"Dependency update check failed: {exc}")

    def create_rollback_snapshot(self) -> bool:
        """
        Create a clean backup of executable application files in self.backup_dir before updating.
        Excludes protected user files (.env, shows.csv, proxies, runs, etc.).
        """
        try:
            if self.backup_dir.exists():
                shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            # Record current version
            local_ver = self.get_local_version()
            if local_ver:
                self.backup_version_file.write_text(
                    json.dumps(local_ver.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Copy directories and files that can be updated
            files_copied = 0
            for item in self.root_dir.iterdir():
                rel_str = item.name
                if self._is_path_protected(rel_str):
                    continue

                dest = self.backup_dir / rel_str
                if item.is_file():
                    shutil.copy2(item, dest)
                    files_copied += 1
                elif item.is_dir():
                    shutil.copytree(
                        item,
                        dest,
                        ignore=lambda src, names: [
                            n for n in names
                            if self._is_path_protected(f"{Path(src).relative_to(self.root_dir)}/{n}".replace("\\", "/"))
                        ],
                    )
                    files_copied += 1

            LOGGER.info(f"Created rollback snapshot ({files_copied} items backed up).")
            return True
        except Exception as exc:
            LOGGER.warning(f"Failed to create rollback snapshot: {exc}")
            return False

    def has_rollback_backup(self) -> bool:
        """Check if a valid rollback snapshot or git history exists."""
        if self.backup_dir.exists():
            items = [i for i in self.backup_dir.iterdir() if i.name != ".version_backup"]
            if len(items) > 0:
                return True
        if (self.root_dir / ".git").exists():
            return True
        return False

    def get_rollback_version(self) -> Optional[VersionInfo]:
        """Get version info of the rollback backup if available."""
        if self.backup_version_file.exists():
            try:
                data = json.loads(self.backup_version_file.read_text(encoding="utf-8"))
                return VersionInfo(
                    sha=data.get("sha", ""),
                    short_sha=data.get("short_sha", data.get("sha", "")[:7]),
                    message=data.get("message", ""),
                    author=data.get("author", ""),
                    date=data.get("date", ""),
                )
            except Exception:
                pass
        return None

    def rollback_to_backup(self) -> Tuple[bool, str]:
        """
        Restore executable files from .backup_prev_version snapshot (or git checkout fallback).
        Strictly preserves all protected user configuration and data files.
        """
        if not self.has_rollback_backup():
            return False, "Резервная копия предыдущей версии не найдена."

        # Strategy 1: Restore from local snapshot directory
        if self.backup_dir.exists() and any(i for i in self.backup_dir.iterdir() if i.name != ".version_backup"):
            try:
                LOGGER.info("Restoring files from rollback snapshot...")
                restored_count = 0
                for item in self.backup_dir.iterdir():
                    if item.name == ".version_backup":
                        continue
                    rel_str = item.name
                    if self._is_path_protected(rel_str):
                        continue

                    dest = self.root_dir / rel_str
                    if item.is_file():
                        shutil.copy2(item, dest)
                        restored_count += 1
                    elif item.is_dir():
                        for root, dirs, files in os.walk(item):
                            rel_root = Path(root).relative_to(self.backup_dir)
                            for f in files:
                                sub_rel = str(rel_root / f).replace("\\", "/")
                                if self._is_path_protected(sub_rel):
                                    continue
                                src_f = Path(root) / f
                                dest_f = self.root_dir / sub_rel
                                dest_f.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(src_f, dest_f)
                                restored_count += 1

                # Restore version file if available
                prev_ver = self.get_rollback_version()
                if prev_ver:
                    self.version_file.write_text(
                        json.dumps(prev_ver.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )

                self._update_dependencies_if_needed()
                LOGGER.info(f"Rollback complete: {restored_count} files restored from snapshot.")
                return True, "Программа успешно возвращена к предыдущей стабильной версии."
            except Exception as exc:
                LOGGER.error(f"Failed to rollback from snapshot: {exc}")
                return False, f"Ошибка при возврате к предыдущей версии: {exc}"

        # Strategy 2: Git rollback fallback
        if (self.root_dir / ".git").exists():
            try:
                LOGGER.info("Attempting git rollback to HEAD~1...")
                res = subprocess.run(
                    ["git", "checkout", "HEAD~1", "--", "src", "gui_app.py", "cli.py", "requirements.txt"],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    self._update_dependencies_if_needed()
                    return True, "Программа успешно возвращена к предыдущей стабильной версии через Git."
                return False, f"Не удалось выполнить откат через Git: {res.stderr}"
            except Exception as exc:
                return False, f"Ошибка при откате через Git: {exc}"

        return False, "Не найден источник для отката к предыдущей версии."
