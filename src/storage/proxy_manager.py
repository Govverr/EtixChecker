"""Proxy Manager providing regex extraction, TCP socket pinging, lightweight HTTP probing,
and intelligent pool management (Good, Slow, Bad) with collision prevention.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from src.adspower.client import AdsPowerClient
from src.utils.logger import LOGGER

# Regular expression extractor for proxy strings: host:port:user:pass inside CSV or arbitrary text
PROXY_REGEX = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+:[^:,]+:[^:,\r\n\"]+)")

# Additional URL-style pattern (e.g., http://user:pass@host:port or socks5://user:pass@host:port)
PROXY_URL_REGEX = re.compile(r"((?:https?|socks5)://[^:@\s]+:[^@\s]+@\d{1,3}(?:\.\d{1,3}){3}:\d+)")

# Simple host:port pattern
PROXY_HOST_PORT_REGEX = re.compile(r"(\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b)")

# Neutral HTTP probe endpoints (NEVER poll etix.com before actual check)
HTTP_PROBE_URLS = [
    "https://api.ipify.org",
    "https://1.1.1.1/cdn-cgi/trace",
    "http://ip-api.com/json",
]


@dataclass
class ProxyCheckResult:
    """Detailed result of a proxy pre-flight check."""
    proxy: str
    status: str  # "GOOD", "SLOW", "BAD"
    latency_ms: float
    error: Optional[str] = None
    egress_ip: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proxy": self.proxy,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
            "egress_ip": self.egress_ip,
            "timestamp": self.timestamp,
        }


class ProxyManager:
    """
    Manages loading, parsing, health-checking, smart-merging, and real-time
    allocation of proxies with strict Collision Prevention.
    """

    def __init__(
        self,
        good_proxies_file: Path = Path("data/good_proxies.txt"),
        slow_proxies_file: Path = Path("data/slow_proxies.txt"),
        bad_proxies_file: Path = Path("data/bad_proxies.txt"),
        on_slow_pool_transition: Optional[Callable[[], None]] = None,
    ) -> None:
        self.good_proxies_file = good_proxies_file
        self.slow_proxies_file = slow_proxies_file
        self.bad_proxies_file = bad_proxies_file
        self.on_slow_pool_transition = on_slow_pool_transition

        # Collision prevention registry: proxies currently assigned to open profiles
        self.in_use_proxies: Set[str] = set()

        # In-memory queues of available proxies
        self.available_good_proxies: List[str] = []
        self.available_slow_proxies: List[str] = []
        self._notified_slow_transition: bool = False

        self.reload_pools()

    # -------------------------------------------------------------------------
    # 1. Regex Extraction & Parsing
    # -------------------------------------------------------------------------
    @staticmethod
    def is_valid_ipv4(ip: str) -> bool:
        """Validate that string is a valid IPv4 address."""
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        for p in parts:
            if not p.isdigit():
                return False
            val = int(p)
            if val < 0 or val > 255:
                return False
        return True

    @staticmethod
    def is_valid_port(port: str | int) -> bool:
        """Validate that port is in range 1-65535."""
        try:
            val = int(str(port).strip())
            return 1 <= val <= 65535
        except (ValueError, TypeError):
            return False

    @classmethod
    def extract_proxies_from_text(cls, text: str) -> List[str]:
        """
        Extract valid proxy configurations from arbitrary string/CSV data.
        Handles comma-separated columns, quoted fields, and standard formats:
          - host:port:user:pass
          - http://user:pass@host:port
          - socks5://user:pass@host:port
        """
        if not text:
            return []

        found: List[str] = []
        seen: Set[str] = set()

        # 1. Find standard host:port:user:pass format inside lines/columns
        for match in PROXY_REGEX.finditer(text):
            p = match.group(1).strip()
            # Validate IP and port
            parts = p.split(":")
            if len(parts) >= 2 and cls.is_valid_ipv4(parts[0]) and cls.is_valid_port(parts[1]):
                if p not in seen:
                    seen.add(p)
                    found.append(p)

        # 2. Find URL-style proxies
        for match in PROXY_URL_REGEX.finditer(text):
            p = match.group(1).strip()
            if p not in seen:
                seen.add(p)
                found.append(p)

        # 3. Line-by-line fallback for lines already in standard format (e.g. host:port)
        for raw_line in text.splitlines():
            line = raw_line.strip().split("#")[0].strip()
            if not line:
                continue
            # If line is comma-separated CSV, check columns
            if "," in line:
                cols = [c.strip().strip('"').strip("'") for c in line.split(",")]
                for col in cols:
                    if col and col not in seen:
                        parsed = AdsPowerClient.parse_proxy_string(col)
                        if parsed and cls.is_valid_ipv4(parsed["proxy_host"]) and cls.is_valid_port(parsed["proxy_port"]):
                            seen.add(col)
                            found.append(col)
            elif line not in seen:
                parsed = AdsPowerClient.parse_proxy_string(line)
                if parsed and cls.is_valid_ipv4(parsed["proxy_host"]) and cls.is_valid_port(parsed["proxy_port"]):
                    seen.add(line)
                    found.append(line)

        return found

    @classmethod
    def extract_proxies_from_file(cls, file_path: Path | str) -> List[str]:
        """Read and parse proxy strings from a file (CSV or TXT)."""
        p = Path(file_path)
        if not p.exists():
            LOGGER.warning(f"Proxy file not found: {p}")
            return []
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            return cls.extract_proxies_from_text(text)
        except Exception as exc:
            LOGGER.error(f"Error reading proxy file {p}: {exc}")
            return []

    # -------------------------------------------------------------------------
    # 2. Storage Operations (Smart Append & Deduplication)
    # -------------------------------------------------------------------------
    @staticmethod
    def load_proxies(file_path: Path) -> List[str]:
        """Read unique valid proxies from file while preserving order."""
        if not file_path.exists():
            return []
        try:
            proxies = []
            seen = set()
            for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                clean = line.strip().split("#")[0].strip()
                if clean and clean not in seen:
                    seen.add(clean)
                    proxies.append(clean)
            return proxies
        except Exception as exc:
            LOGGER.warning(f"Could not load proxies from {file_path}: {exc}")
            return []

    @staticmethod
    def save_proxies(file_path: Path, proxies: Iterable[str]) -> None:
        """Write unique proxies to file atomically."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        unique_proxies = []
        seen = set()
        for p in proxies:
            clean = p.strip()
            if clean and clean not in seen:
                seen.add(clean)
                unique_proxies.append(clean)

        tmp_path = file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for p in unique_proxies:
                    f.write(f"{p}\n")
            if os.name == "nt" and file_path.exists():
                file_path.unlink()
            tmp_path.rename(file_path)
        except Exception as exc:
            LOGGER.error(f"Failed to save proxies to {file_path}: {exc}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    @classmethod
    def smart_append_and_merge(cls, file_path: Path, new_proxies: Iterable[str]) -> int:
        """
        Appends new proxies to an existing file without modifying or reordering
        previously verified entries. Returns count of newly added proxies.
        """
        existing = cls.load_proxies(file_path)
        existing_set = set(existing)
        to_add = []

        for p in new_proxies:
            clean = p.strip()
            if clean and clean not in existing_set:
                existing_set.add(clean)
                to_add.append(clean)

        if not to_add:
            return 0

        merged = existing + to_add
        cls.save_proxies(file_path, merged)
        LOGGER.info(
            f"Smart merge into '{file_path.name}': added {len(to_add)} new proxies (total: {len(merged)})."
        )
        return len(to_add)

    def reload_pools(self) -> None:
        """Reload good and slow proxy pools from files while excluding in-use proxies."""
        good = self.load_proxies(self.good_proxies_file)
        slow = self.load_proxies(self.slow_proxies_file)

        self.available_good_proxies = [p for p in good if p not in self.in_use_proxies]
        self.available_slow_proxies = [p for p in slow if p not in self.in_use_proxies]
        self._notified_slow_transition = False

    # -------------------------------------------------------------------------
    # 3. Health Checks: TCP Socket Ping & HTTP Probe
    # -------------------------------------------------------------------------
    @staticmethod
    async def check_tcp_socket(
        proxy_str: str,
        timeout_s: float = 3.0,
    ) -> Tuple[bool, float, Optional[str]]:
        """
        Fast TCP connection ping to proxy host:port (< 1500ms -> good, 1500-2500ms -> slow, > 2500ms -> bad).
        Returns: (success, latency_ms, error_message)
        """
        parsed = AdsPowerClient.parse_proxy_string(proxy_str)
        if not parsed:
            return False, 0.0, "Invalid proxy format"

        host = parsed["proxy_host"]
        try:
            port = int(parsed["proxy_port"])
        except (ValueError, TypeError):
            return False, 0.0, f"Invalid port: {parsed.get('proxy_port')}"

        t0 = time.perf_counter()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_s,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return True, latency_ms, None
        except asyncio.TimeoutError:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return False, latency_ms, f"TCP connection timed out (> {timeout_s}s)"
        except ConnectionRefusedError:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return False, latency_ms, "Connection refused (port closed)"
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return False, latency_ms, f"Network error: {exc}"
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    @staticmethod
    def _format_httpx_proxy_url(parsed: Dict[str, Any]) -> str:
        """Construct standard proxy URL string for httpx client."""
        ptype = parsed.get("proxy_type", "http").lower()
        if ptype not in ("http", "https", "socks5"):
            ptype = "http"
        user = parsed.get("proxy_user", "").strip()
        pwd = parsed.get("proxy_password", "").strip()
        host = parsed.get("proxy_host", "").strip()
        port = parsed.get("proxy_port", "").strip()

        if user and pwd:
            return f"{ptype}://{user}:{pwd}@{host}:{port}"
        elif user:
            return f"{ptype}://{user}@{host}:{port}"
        return f"{ptype}://{host}:{port}"

    @classmethod
    async def check_http_probe(
        cls,
        proxy_str: str,
        timeout_s: float = 5.0,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Lightweight HTTP probe to an external neutral IP service to validate credentials and egress IP.
        NEVER contacts etix.com before actual show check.
        Returns: (success, egress_ip, error_message)
        """
        parsed = AdsPowerClient.parse_proxy_string(proxy_str)
        if not parsed:
            return False, None, "Invalid proxy format"

        proxy_url = cls._format_httpx_proxy_url(parsed)

        for probe_url in HTTP_PROBE_URLS:
            try:
                # httpx >= 0.24 uses 'proxy' keyword, older versions support 'proxies'
                kwargs: Dict[str, Any] = {"timeout": timeout_s}
                try:
                    client_cm = httpx.AsyncClient(proxy=proxy_url, **kwargs)
                except TypeError:
                    client_cm = httpx.AsyncClient(proxies=proxy_url, **kwargs)

                async with client_cm as client:
                    resp = await client.get(probe_url)
                    if resp.status_code == 200:
                        body = resp.text.strip()
                        # Extract IP from response
                        m = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", body)
                        egress_ip = m.group(0) if m else body[:50]
                        return True, egress_ip, None
                    elif resp.status_code == 407:
                        return False, None, "Proxy Authentication Required (HTTP 407: invalid username/password)"
                    else:
                        continue  # Try next probe service
            except Exception as exc:
                err_str = str(exc)
                if "407" in err_str or "Proxy-Authenticate" in err_str:
                    return False, None, "Proxy Authentication Required (HTTP 407: invalid username/password)"
                continue

        return False, None, "All external HTTP probe services unreachable through proxy"

    async def check_single_proxy(
        self,
        proxy_str: str,
        use_http_probe: bool = True,
        tcp_timeout_s: float = 3.0,
        http_timeout_s: float = 5.0,
    ) -> ProxyCheckResult:
        """
        Performs hybrid check on a single proxy:
          1. TCP Socket Ping (<1500ms -> GOOD, 1500-2500ms -> SLOW, >2500ms -> BAD)
          2. Light HTTP probe (if TCP succeeded) to confirm credentials.
        """
        tcp_ok, latency_ms, tcp_err = await self.check_tcp_socket(proxy_str, timeout_s=tcp_timeout_s)
        if not tcp_ok:
            return ProxyCheckResult(
                proxy=proxy_str,
                status="BAD",
                latency_ms=latency_ms,
                error=tcp_err or "TCP connection failed",
            )

        egress_ip = None
        if use_http_probe:
            http_ok, e_ip, http_err = await self.check_http_probe(proxy_str, timeout_s=http_timeout_s)
            if not http_ok:
                return ProxyCheckResult(
                    proxy=proxy_str,
                    status="BAD",
                    latency_ms=latency_ms,
                    error=http_err or "HTTP probe failed",
                )
            egress_ip = e_ip

        if latency_ms < 1500.0:
            status = "GOOD"
        elif latency_ms <= 2500.0:
            status = "SLOW"
        else:
            status = "BAD"

        return ProxyCheckResult(
            proxy=proxy_str,
            status=status,
            latency_ms=latency_ms,
            egress_ip=egress_ip,
        )

    # -------------------------------------------------------------------------
    # 4. Batch Pre-flight Checker
    # -------------------------------------------------------------------------
    async def run_preflight_check(
        self,
        proxies: List[str],
        concurrency: int = 20,
        use_http_probe: bool = True,
        on_progress: Optional[Callable[[int, int, ProxyCheckResult], None]] = None,
    ) -> Dict[str, List[ProxyCheckResult]]:
        """
        Executes pre-flight check on an entire list of proxies using async semaphore.
        Sorts into Good, Slow, and Bad categories and saves to corresponding files.
        """
        clean_proxies = [p.strip() for p in proxies if p.strip()]
        total = len(clean_proxies)
        if total == 0:
            return {"good": [], "slow": [], "bad": []}

        sem = asyncio.Semaphore(concurrency)
        results: List[ProxyCheckResult] = []
        completed = 0

        async def _worker(p_str: str) -> ProxyCheckResult:
            nonlocal completed
            async with sem:
                res = await self.check_single_proxy(p_str, use_http_probe=use_http_probe)
                completed += 1
                if on_progress:
                    try:
                        on_progress(completed, total, res)
                    except Exception:
                        pass
                return res

        tasks = [_worker(p) for p in clean_proxies]
        results = await asyncio.gather(*tasks)

        good_results = [r for r in results if r.status == "GOOD"]
        slow_results = [r for r in results if r.status == "SLOW"]
        bad_results = [r for r in results if r.status == "BAD"]

        # Smart merge good proxies
        if good_results:
            self.smart_append_and_merge(
                self.good_proxies_file, [r.proxy for r in good_results]
            )

        # Smart merge slow proxies
        if slow_results:
            self.smart_append_and_merge(
                self.slow_proxies_file, [r.proxy for r in slow_results]
            )

        # Record bad proxies
        if bad_results:
            self._record_bad_proxies_batch(bad_results)

        # Refresh in-memory pools
        self.reload_pools()

        LOGGER.info(
            f"Pre-flight check completed ({total} proxies): "
            f"Good: {len(good_results)}, Slow: {len(slow_results)}, Bad: {len(bad_results)}"
        )

        return {
            "good": good_results,
            "slow": slow_results,
            "bad": bad_results,
        }

    def _record_bad_proxies_batch(self, bad_results: List[ProxyCheckResult]) -> None:
        """Append bad proxies with error reasons to bad_proxies_file."""
        self.bad_proxies_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.bad_proxies_file, "a", encoding="utf-8") as f:
                for r in bad_results:
                    f.write(f"{r.timestamp} | {r.proxy} | Reason: {r.error or 'High latency / failed'}\n")
        except Exception as exc:
            LOGGER.warning(f"Could not record bad proxies batch: {exc}")

    # -------------------------------------------------------------------------
    # 5. Collision Prevention & Real-time Allocation
    # -------------------------------------------------------------------------
    def get_next_available_proxy(
        self,
        prefer_good: bool = True,
    ) -> Optional[Tuple[str, str]]:
        """
        Returns (proxy_str, pool_type) where pool_type is 'good' or 'slow'.
        Ensures strict 1-proxy-per-1-profile collision prevention:
        the returned proxy is immediately registered in self.in_use_proxies.
        """
        candidate = None
        pool_type = None

        if prefer_good and self.available_good_proxies:
            candidate = self.available_good_proxies.pop(0)
            pool_type = "good"
        elif self.available_slow_proxies:
            candidate = self.available_slow_proxies.pop(0)
            pool_type = "slow"
            if not self._notified_slow_transition:
                self._notified_slow_transition = True
                LOGGER.warning(
                    "Primary good proxies pool exhausted! Transitioning to SLOW proxies pool."
                )
                if self.on_slow_pool_transition:
                    try:
                        self.on_slow_pool_transition()
                    except Exception:
                        pass
        elif self.available_good_proxies:
            candidate = self.available_good_proxies.pop(0)
            pool_type = "good"

        if candidate:
            self.in_use_proxies.add(candidate)
            return candidate, pool_type

        return None

    def release_proxy(self, proxy_str: str) -> None:
        """Release a proxy back to pool when worker finishes or closes."""
        clean = proxy_str.strip()
        if clean in self.in_use_proxies:
            self.in_use_proxies.remove(clean)

    def mark_proxy_bad(self, proxy_str: str, reason: str = "") -> None:
        """Permanently remove proxy from good/slow pools and record in bad_proxies.txt."""
        clean = proxy_str.strip()
        if clean in self.in_use_proxies:
            self.in_use_proxies.remove(clean)

        if clean in self.available_good_proxies:
            self.available_good_proxies.remove(clean)
        if clean in self.available_slow_proxies:
            self.available_slow_proxies.remove(clean)

        # Remove from good_proxies_file
        all_good = self.load_proxies(self.good_proxies_file)
        if clean in all_good:
            all_good.remove(clean)
            self.save_proxies(self.good_proxies_file, all_good)

        # Remove from slow_proxies_file
        all_slow = self.load_proxies(self.slow_proxies_file)
        if clean in all_slow:
            all_slow.remove(clean)
            self.save_proxies(self.slow_proxies_file, all_slow)

        # Append to bad_proxies_file
        self._record_bad_proxies_batch([
            ProxyCheckResult(
                proxy=clean,
                status="BAD",
                latency_ms=0.0,
                error=reason or "Marked bad by engine",
            )
        ])
        LOGGER.info(f"Proxy '{clean}' marked BAD and recorded ({reason}).")

    def get_stats(self) -> Dict[str, int]:
        """Return counts of available, in-use, and total proxies across pools."""
        total_good = len(self.load_proxies(self.good_proxies_file))
        total_slow = len(self.load_proxies(self.slow_proxies_file))
        total_bad = len(self.load_proxies(self.bad_proxies_file))

        return {
            "good_available": len(self.available_good_proxies),
            "slow_available": len(self.available_slow_proxies),
            "in_use": len(self.in_use_proxies),
            "total_good": total_good,
            "total_slow": total_slow,
            "total_bad": total_bad,
        }
