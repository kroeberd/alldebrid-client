"""
aria2 JSON-RPC client.

URL formats:
  http://localhost:6800/jsonrpc                    (no secret)
  http://localhost:6800/jsonrpc#mysecret           (secret after #)
  http://token:mysecret@localhost:6800/jsonrpc     (secret in URL)
"""
import aiohttp
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger("alldebrid.aria2")


def _parse_url(raw: str) -> tuple[str, str]:
    """Returns (rpc_url, secret)."""
    raw = raw.strip()

    # http://token:SECRET@host:port/jsonrpc
    if "://" in raw and "@" in raw:
        p = urlparse(raw)
        secret = p.password or p.username or ""
        netloc = p.hostname + (f":{p.port}" if p.port else "")
        rpc_url = f"{p.scheme}://{netloc}{p.path or '/jsonrpc'}"
        return rpc_url, secret

    # SECRET@host:port
    if "@" in raw and "://" not in raw:
        secret, host = raw.split("@", 1)
        if not host.startswith("http"):
            host = f"http://{host}"
        if "/jsonrpc" not in host:
            host = host.rstrip("/") + "/jsonrpc"
        return host, secret

    # http://host:port/jsonrpc#SECRET
    if "#" in raw:
        url, secret = raw.rsplit("#", 1)
        if not url.startswith("http"):
            url = f"http://{url}"
        return url, secret

    # plain URL
    if not raw.startswith("http"):
        raw = f"http://{raw}"
    if "/jsonrpc" not in raw:
        raw = raw.rstrip("/") + "/jsonrpc"
    return raw, ""


class Aria2Client:
    def __init__(self, url: str):
        self.rpc_url, self.secret = _parse_url(url)
        self._rid = 0
        logger.debug(f"aria2 RPC: {self.rpc_url}, secret={'***' if self.secret else 'none'}")

    def _next_rid(self) -> int:
        self._rid += 1
        return self._rid

    def _params(self, *args) -> list:
        base = [f"token:{self.secret}"] if self.secret else []
        return base + list(args)

    async def _call(self, method: str, *args) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": f"adc-{self._next_rid()}",
            "params": self._params(*args),
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        raise Exception(f"aria2 HTTP {resp.status}")
                    result = await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as e:
            raise Exception(f"Cannot connect to aria2 at {self.rpc_url}: {e}")

        if "error" in result:
            err = result["error"]
            code = err.get("code", "?")
            msg  = err.get("message", str(err))
            raise Exception(f"aria2 error [{code}]: {msg}")

        return result.get("result")

    async def get_version(self) -> Dict:
        return await self._call("aria2.getVersion")

    async def add_uri(self, uri: str, dest_dir: str,
                      filename: Optional[str] = None) -> str:
        """Add a URI download. Returns GID."""
        options: Dict[str, Any] = {"dir": dest_dir}
        if filename:
            options["out"] = filename
        gid = await self._call("aria2.addUri", [uri], options)
        logger.info(f"aria2 addUri → GID={gid} | dir={dest_dir} | url={uri[:60]}...")
        return gid

    async def tell_status(self, gid: str) -> Dict:
        return await self._call("aria2.tellStatus", gid,
                                ["status","totalLength","completedLength","errorMessage"])

    async def get_global_stat(self) -> Dict:
        return await self._call("aria2.getGlobalStat")
