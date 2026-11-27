"""Pick up macOS system HTTP(S)/SOCKS proxy (e.g. VEEE) for the PC worker.

GUI apps inherit System Settings proxy; launchd Python often does not.
Call apply_macos_system_proxy() early so PC-side microscope and workspace
requests can use the user's system proxy when needed.
LAN microscope hosts stay on NO_PROXY.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, Optional
from urllib.parse import urlparse


_DEFAULT_NO_PROXY = (
    "127.0.0.1,localhost,::1,"
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "*.local,169.254.0.0/16"
)


def authorize_microscope_request(request):
    """Send the connection token only to the selected microscope origin."""
    token = os.environ.get("MICROWORLD_CONNECTION_TOKEN")
    origin = (os.environ.get("OPENMHS_MICROSCOPE_URL") or "").rstrip("/")
    if token and origin and request.full_url.startswith(origin + "/"):
        request.add_unredirected_header("X-MicroWorld-Connection", token)
    return request


def _truthy_disable(raw: str) -> bool:
    return raw.strip().lower() in ("0", "false", "no", "off")


def _existing_proxy() -> Optional[str]:
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


def _scproxy_dict() -> Dict[str, str]:
    """Use Python's private macOS SystemConfiguration helper (works in launchd)."""
    found: Dict[str, str] = {}
    try:
        from _scproxy import _get_proxy_settings  # type: ignore

        raw = _get_proxy_settings() or {}
    except Exception:
        return found
    http = raw.get("http")
    https = raw.get("https")
    socks = raw.get("socks")
    if isinstance(http, (tuple, list)) and len(http) >= 2 and http[0]:
        found["http"] = "http://{}:{}".format(http[0], http[1])
    if isinstance(https, (tuple, list)) and len(https) >= 2 and https[0]:
        found["https"] = "http://{}:{}".format(https[0], https[1])
    if isinstance(socks, (tuple, list)) and len(socks) >= 2 and socks[0]:
        found["socks"] = "socks5://{}:{}".format(socks[0], socks[1])
    return found


def _scutil_proxy() -> Dict[str, str]:
    """Parse `scutil --proxy` into http/https/socks URLs."""
    try:
        out = subprocess.check_output(["scutil", "--proxy"], text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}

    def flag(name: str) -> bool:
        m = re.search(r"\b" + re.escape(name) + r"\s*:\s*(\S+)", out)
        return bool(m and m.group(1) not in ("0", "false", "False"))

    def host_port(host_key: str, port_key: str) -> Optional[str]:
        hm = re.search(r"\b" + re.escape(host_key) + r"\s*:\s*(\S+)", out)
        pm = re.search(r"\b" + re.escape(port_key) + r"\s*:\s*(\d+)", out)
        if not hm or not pm:
            return None
        return "{}:{}".format(hm.group(1).strip(), pm.group(1).strip())

    found: Dict[str, str] = {}
    if flag("HTTPEnable"):
        hp = host_port("HTTPProxy", "HTTPPort")
        if hp:
            found["http"] = "http://{}".format(hp)
    if flag("HTTPSEnable"):
        hp = host_port("HTTPSProxy", "HTTPSPort")
        if hp:
            found["https"] = "http://{}".format(hp)
    if flag("SOCKSEnable"):
        hp = host_port("SOCKSProxy", "SOCKSPort")
        if hp:
            found["socks"] = "socks5://{}".format(hp)
    return found


def _detect_system_proxy() -> Dict[str, str]:
    return _scproxy_dict() or _scutil_proxy()


def _microscope_no_proxy_hosts() -> str:
    extra = []
    raw = (os.environ.get("OPENMHS_MICROSCOPE_URL") or "").strip()
    if raw:
        try:
            host = urlparse(raw).hostname
            if host:
                extra.append(host)
        except Exception:
            pass
    return ",".join(extra)


def proxy_status() -> Dict[str, Optional[str]]:
    return {
        "http_proxy": (os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "") or None,
        "https_proxy": (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "") or None,
        "all_proxy": (os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or "") or None,
        "no_proxy": (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "") or None,
    }


def apply_macos_system_proxy(*, force: bool = False) -> Dict[str, Optional[str]]:
    """Set http(s)_proxy from macOS System Settings when unset.

    Honors OPENMHS_PROXY=0 to skip. Existing env/.env proxy wins unless force=True.
    """
    if _truthy_disable(os.environ.get("OPENMHS_PROXY", "1")):
        return proxy_status()

    if not force and _existing_proxy():
        _ensure_no_proxy()
        return proxy_status()

    found = _detect_system_proxy()
    http_url = found.get("http") or found.get("https")
    https_url = found.get("https") or found.get("http")
    socks_url = found.get("socks")

    if http_url:
        os.environ["http_proxy"] = http_url
        os.environ["HTTP_PROXY"] = http_url
    if https_url:
        os.environ["https_proxy"] = https_url
        os.environ["HTTPS_PROXY"] = https_url
    if socks_url and not (os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")):
        os.environ["all_proxy"] = socks_url
        os.environ["ALL_PROXY"] = socks_url

    _ensure_no_proxy()
    return proxy_status()


def _ensure_no_proxy() -> None:
    """Keep LAN / localhost off the VPN proxy (Pi microscope must stay direct)."""
    cur = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").strip()
    parts = [p.strip() for p in cur.split(",") if p.strip()]
    for item in _DEFAULT_NO_PROXY.split(","):
        if item and item not in parts:
            parts.append(item)
    extra = _microscope_no_proxy_hosts()
    if extra and extra not in parts:
        parts.append(extra)
    merged = ",".join(parts)
    os.environ["no_proxy"] = merged
    os.environ["NO_PROXY"] = merged
