from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import get_settings
from services.aria2 import Aria2Service

logger = logging.getLogger("alldebrid.aria2.runtime")

BUILTIN_ARIA2_SECRET = "alldebrid-client-internal-aria2-rpc"


def is_builtin_mode(cfg=None) -> bool:
    cfg = cfg or get_settings()
    return getattr(cfg, "aria2_mode", "external") == "builtin"


def builtin_rpc_url(cfg=None) -> str:
    cfg = cfg or get_settings()
    port = int(getattr(cfg, "aria2_builtin_port", 6800) or 6800)
    return f"http://127.0.0.1:{port}/jsonrpc"


def effective_rpc_config(cfg=None) -> tuple[str, str]:
    cfg = cfg or get_settings()
    if is_builtin_mode(cfg):
        return builtin_rpc_url(cfg), BUILTIN_ARIA2_SECRET
    return (getattr(cfg, "aria2_url", "") or "").strip(), (getattr(cfg, "aria2_secret", "") or "").strip()


def aria2_global_options(cfg=None, *, include_safety: bool = False) -> Dict[str, str]:
    cfg = cfg or get_settings()
    options: Dict[str, str] = {
        "max-download-result": str(int(getattr(cfg, "aria2_max_download_result", 50) or 50)),
        "keep-unfinished-download-result": "true" if bool(getattr(cfg, "aria2_keep_unfinished_download_result", False)) else "false",
        "max-concurrent-downloads": str(int(getattr(cfg, "aria2_max_active_downloads", 3) or 3)),
        "split": str(int(getattr(cfg, "aria2_split", 8) or 8)),
        "min-split-size": str(getattr(cfg, "aria2_min_split_size", "10M") or "10M"),
        "max-connection-per-server": str(int(getattr(cfg, "aria2_max_connection_per_server", 8) or 8)),
        "disk-cache": str(getattr(cfg, "aria2_disk_cache", "64M") or "64M"),
        "file-allocation": str(getattr(cfg, "aria2_file_allocation", "falloc") or "falloc"),
        "continue": "true" if bool(getattr(cfg, "aria2_continue_downloads", True)) else "false",
        "lowest-speed-limit": str(getattr(cfg, "aria2_lowest_speed_limit", "0") or "0"),
    }
    if include_safety:
        options.update({
            "follow-torrent": "false",
            "enable-dht": "false",
            "enable-dht6": "false",
            "enable-peer-exchange": "false",
            "bt-enable-lpd": "false",
        })
    return options


class BuiltinAria2Runtime:
    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started_at: float = 0.0
        self._last_error: str = ""
        self._lock = asyncio.Lock()

    def _service(self) -> Aria2Service:
        url, secret = effective_rpc_config()
        return Aria2Service(url, secret, get_settings().aria2_operation_timeout_seconds)

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _runtime_paths(self) -> tuple[Path, Path]:
        cfg = get_settings()
        log_file = Path(getattr(cfg, "aria2_builtin_log_file", "/app/data/aria2/aria2.log") or "/app/data/aria2/aria2.log")
        session_file = Path(getattr(cfg, "aria2_builtin_session_file", "/app/data/aria2/aria2.session") or "/app/data/aria2/aria2.session")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.touch(exist_ok=True)
        return log_file, session_file

    def _command(self) -> list[str]:
        cfg = get_settings()
        log_file, session_file = self._runtime_paths()
        download_dir = Path(getattr(cfg, "aria2_download_path", "") or getattr(cfg, "download_folder", "/app/data/downloads"))
        download_dir.mkdir(parents=True, exist_ok=True)
        options = aria2_global_options(cfg, include_safety=True)
        cmd = [
            "aria2c",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={int(getattr(cfg, 'aria2_builtin_port', 6800) or 6800)}",
            f"--rpc-secret={BUILTIN_ARIA2_SECRET}",
            "--rpc-allow-origin-all=false",
            f"--dir={download_dir}",
            f"--input-file={session_file}",
            f"--save-session={session_file}",
            "--save-session-interval=30",
            "--auto-save-interval=30",
            f"--log={log_file}",
            "--log-level=notice",
            "--summary-interval=0",
            "--disable-ipv6=true",
        ]
        cmd.extend(f"--{key}={value}" for key, value in options.items())
        return cmd

    async def ensure_started(self) -> Dict[str, Any]:
        cfg = get_settings()
        if not is_builtin_mode(cfg):
            return await self.status()
        if not bool(getattr(cfg, "aria2_builtin_auto_start", True)):
            return await self.status()
        return await self.start()

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            cfg = get_settings()
            if not is_builtin_mode(cfg):
                return await self.status()
            if self._is_process_alive():
                return await self.status()
            if not shutil.which("aria2c"):
                self._last_error = "aria2c binary not found in container"
                logger.warning("Built-in aria2 start skipped: %s", self._last_error)
                return await self.status()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._command(),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self._started_at = time.time()
                self._last_error = ""
                await self._wait_until_healthy()
                logger.info("Built-in aria2 started on %s", builtin_rpc_url(cfg))
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Built-in aria2 start failed: %s", exc)
            return await self.status()

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            try:
                if is_builtin_mode():
                    try:
                        await self._service()._call("aria2.shutdown")
                    except Exception:
                        pass
                if self._process and self._process.returncode is None:
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self._process.terminate()
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            self._process.kill()
                self._started_at = 0.0
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Built-in aria2 stop failed: %s", exc)
            return await self.status()

    async def restart(self) -> Dict[str, Any]:
        await self.stop()
        return await self.start()

    async def apply_options(self) -> Dict[str, Any]:
        if not is_builtin_mode():
            return {"ok": False, "enabled": False}
        svc = self._service()
        options = aria2_global_options(include_safety=True)
        await svc.change_global_options(options)
        return {"ok": True, "options": options}

    async def status(self) -> Dict[str, Any]:
        cfg = get_settings()
        enabled = is_builtin_mode(cfg)
        process_running = self._is_process_alive()
        rpc_ok = False
        version = ""
        rpc_error = ""
        if enabled:
            try:
                result = await self._service().test()
                rpc_ok = True
                version = result.get("version", "")
            except Exception as exc:
                rpc_error = str(exc)
        return {
            "enabled": enabled,
            "mode": getattr(cfg, "aria2_mode", "external"),
            "auto_start": bool(getattr(cfg, "aria2_builtin_auto_start", True)),
            "running": bool(enabled and (process_running or rpc_ok)),
            "process_running": process_running,
            "rpc_ok": rpc_ok,
            "rpc_url": builtin_rpc_url(cfg) if enabled else (getattr(cfg, "aria2_url", "") or ""),
            "secret_managed": enabled,
            "version": version,
            "uptime_seconds": int(time.time() - self._started_at) if self._started_at else 0,
            "last_error": self._last_error or rpc_error,
            "safety": aria2_global_options(cfg, include_safety=True) if enabled else {},
        }

    async def _wait_until_healthy(self) -> None:
        deadline = time.time() + 10
        last_error = ""
        while time.time() < deadline:
            try:
                await self._service().test()
                await self.apply_options()
                return
            except Exception as exc:
                last_error = str(exc)
                await asyncio.sleep(0.25)
        raise RuntimeError(last_error or "aria2 RPC did not become healthy")


runtime = BuiltinAria2Runtime()
