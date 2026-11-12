"""Machine facts and a cheap resource snapshot (sent with each heartbeat).

Uses psutil when installed; otherwise /proc on Linux and Win32 calls on Windows. CPU is
measured between two calls (no sleeping). GPU data comes from `nvidia-smi`, cached for a
minute, and only if it is installed. Nothing here starts a background thread.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time

from celius_agent import PROTOCOL_VERSION, __version__

IS_WINDOWS = sys.platform == "win32"
GPU_CACHE_SECONDS = 60

try:  # optional
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    psutil = None


def os_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def machine_facts() -> dict:
    return {
        "hostname": socket.gethostname(),
        "os": os_name(),
        "os_version": f"{platform.system()} {platform.release()} ({platform.version()})"[:200],
        "arch": platform.machine().lower(),
        "agent_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
    }


class Monitor:
    def __init__(self, disk_paths: list[str] | None = None):
        self.disk_paths = disk_paths or []
        self._cpu_last: tuple[float, float] | None = None
        self._gpu_cache: tuple[float, list[dict] | None] = (0.0, None)

    # ------------------------------------------------------------------ cpu

    def _cpu_times(self) -> tuple[float, float] | None:
        """(idle, total) since boot."""
        if IS_WINDOWS:  # pragma: no cover - Windows only
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None

            def value(ft) -> float:
                return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime)

            # Kernel time includes idle time.
            return value(idle), value(kernel) + value(user)
        try:
            with open("/proc/stat", encoding="ascii") as handle:
                fields = [float(v) for v in handle.readline().split()[1:]]
        except OSError:
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return idle, sum(fields)

    def cpu_percent(self) -> float | None:
        if psutil is not None:
            return psutil.cpu_percent(interval=None)
        current = self._cpu_times()
        if current is None:
            return None
        last, self._cpu_last = self._cpu_last, current
        if last is None:
            return None
        idle = current[0] - last[0]
        total = current[1] - last[1]
        return round(100.0 * (1 - idle / total), 1) if total > 0 else None

    # ------------------------------------------------------------------ memory

    def memory(self) -> dict | None:
        if psutil is not None:
            vm = psutil.virtual_memory()
            return {
                "total_mb": vm.total // 2**20,
                "available_mb": vm.available // 2**20,
                "percent": vm.percent,
            }
        if IS_WINDOWS:  # pragma: no cover - Windows only
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return {
                "total_mb": status.ullTotalPhys // 2**20,
                "available_mb": status.ullAvailPhys // 2**20,
                "percent": float(status.dwMemoryLoad),
            }
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0])  # kB
        except (OSError, ValueError, IndexError):
            return None
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        if not total:
            return None
        return {
            "total_mb": total // 1024,
            "available_mb": available // 1024,
            "percent": round(100.0 * (1 - available / total), 1),
        }

    # ------------------------------------------------------------------ disk / gpu

    def disks(self) -> list[dict]:
        seen, out = set(), []
        for path in self.disk_paths or [os.path.expanduser("~")]:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            key = (usage.total, usage.free)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "path": path,
                    "total_gb": round(usage.total / 2**30, 1),
                    "free_gb": round(usage.free / 2**30, 1),
                }
            )
        return out

    def gpus(self) -> list[dict] | None:
        stamp, cached = self._gpu_cache
        if time.monotonic() - stamp < GPU_CACHE_SECONDS:
            return cached
        found = None
        smi = shutil.which("nvidia-smi")
        if smi:
            try:
                output = subprocess.run(
                    [
                        smi,
                        "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                ).stdout
                found = []
                for line in output.strip().splitlines():
                    name, util, used, total = [v.strip() for v in line.split(",")[:4]]
                    found.append(
                        {
                            "name": name,
                            "utilization_percent": float(util),
                            "memory_used_mb": float(used),
                            "memory_total_mb": float(total),
                        }
                    )
            except (OSError, subprocess.SubprocessError, ValueError):
                found = None
        self._gpu_cache = (time.monotonic(), found)
        return found

    def snapshot(self, *, running_commands: int, open_sessions: int) -> dict:
        snap = {
            "cpu_percent": self.cpu_percent(),
            "cpu_count": os.cpu_count(),
            "memory": self.memory(),
            "disks": self.disks(),
            "running_commands": running_commands,
            "open_sessions": open_sessions,
            "taken_at": time.time(),
        }
        if (gpus := self.gpus()) is not None:
            snap["gpus"] = gpus
        try:
            snap["load_average"] = [round(v, 2) for v in os.getloadavg()]
        except (AttributeError, OSError):
            pass
        return snap
