"""Discover OpenFlexure instruments and verify their stable identifiers."""

import concurrent.futures
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MICROWORLD_API = "org.microworld"
LEGACY_API = "org.openmhs"
DEVICE_ID = re.compile(r"openflexure:microscope:([a-zA-Z0-9-]+)")


def read_json(url, timeout=3):
    with HTTP.open(url, timeout=timeout) as response:
        return json.load(response)


def origin(address):
    address = address.strip()
    if "://" not in address:
        address = "http://" + address
    parsed = urlsplit(address)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
    ):
        raise ValueError("Enter an address such as microscope.local:5000.")
    port = parsed.port or (443 if parsed.scheme == "https" else 5000)
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


def identify(address, timeout=3):
    url = origin(address)
    data = read_json(url + "/api/v2/", timeout)
    identity = DEVICE_ID.search(str(data.get("title", "")) + " " + str(data.get("id", "")))
    if "org.openflexure.microscope" not in data.get("@type", []) or not identity:
        raise ValueError(f"{url} did not identify itself as an OpenFlexure microscope.")
    actions = data.get("actions") or {}
    namespace = next((ns for ns in (MICROWORLD_API, LEGACY_API)
                      if ns + ".scan-mosaic/tile" in actions), None)
    return {"id": identity.group(1).lower(), "url": url,
            "openmhs": namespace is not None, "api_namespace": namespace or MICROWORLD_API}


def run_output(argv, timeout):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lan_interfaces():
    if sys.platform.startswith("linux"):
        try:
            interfaces = json.loads(run_output(["ip", "-j", "-4", "address", "show", "up", "scope", "global"], 2))
        except ValueError:
            return
        for device in interfaces:
            for address in device.get("addr_info", []):
                if address.get("broadcast"):
                    yield ipaddress.IPv4Interface((address["local"], address["prefixlen"]))
        return

    for line in run_output(["/sbin/ifconfig", "-u"], 2).splitlines():
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", line)
        if match and "broadcast" in line:
            local, mask = match.groups()
            yield ipaddress.IPv4Interface((local, str(ipaddress.IPv4Address(int(mask, 16)))))


def lan_hosts():
    for interface in lan_interfaces():
        local = str(interface.ip)
        network = interface.network
        # A large campus network can have tens of thousands of hosts. Limit
        # active discovery to the local /24; Bonjour/manual cover routed hosts.
        if network.prefixlen < 24:
            network = ipaddress.IPv4Network((local, 24), strict=False)
        for address in network.hosts():
            if str(address) != local:
                yield str(address)


def bonjour_hosts():
    if sys.platform.startswith("linux"):
        text = run_output(["avahi-browse", "-rtp", "_http._tcp"], 2)
        for line in text.splitlines():
            fields = line.split(";")
            if len(fields) >= 9 and fields[0] == "=" and fields[2] == "IPv4":
                yield f"{fields[7]}:{fields[8]}"
        return
    # dns-sd intentionally keeps browsing. Collect its initial result window.
    try:
        with subprocess.Popen(
            ["/usr/bin/dns-sd", "-Z", "_http._tcp", "local."],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        ) as browser:
            try:
                output, _ = browser.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                browser.terminate()
                output, _ = browser.communicate(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return
    for port, host in re.findall(r"\bSRV\s+\d+\s+\d+\s+(\d+)\s+(\S+)", output):
        yield f"{host.rstrip('.')}:{port}"


def tailscale_hosts():
    commands = [shutil.which("tailscale"), "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
    for command in dict.fromkeys(item for item in commands if item):
        if not os.path.isfile(command):
            continue
        raw = run_output([command, "status", "--json"], 2)
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        status = state.get("BackendState", "Unknown")
        if status != "Running":
            return [], status
        peers = state.get("Peer") or {}
        hosts = []
        for peer in list(peers.values())[:256]:
            if peer.get("Online") is False:
                continue
            addresses = peer.get("TailscaleIPs") or []
            hosts.extend(address for address in addresses if ":" not in address)
        return hosts, status
    return [], "Unavailable"


def discover(saved):
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        local = pool.submit(lambda: list(lan_hosts()))
        bonjour = pool.submit(lambda: list(bonjour_hosts()))
        tail = pool.submit(tailscale_hosts)
        vpn, state = tail.result()
        candidates = {address: "Tailscale" for address in vpn}
        candidates.update({address: "LAN" for address in local.result()})
        candidates.update({address: "Bonjour" for address in bonjour.result()})
    for url in saved + ["microscope.local"]:
        candidates.setdefault(url, "Saved" if url in saved else "Bonjour")

    def probe(candidate):
        address, source = candidate
        try:
            url = origin(address)
            parts = urlsplit(url)
            if parts.scheme == "http":
                with socket.create_connection((parts.hostname, parts.port), timeout=.65):
                    pass
            item = identify(url, timeout=2)
            return item, source
        except (OSError, ValueError, TypeError):
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        matches = [entry for entry in pool.map(probe, candidates.items()) if entry]
    devices = {}
    priorities = {"LAN": 0, "Bonjour": 1, "Tailscale": 2, "Saved": 3}
    for item, source in sorted(matches, key=lambda entry: (priorities[entry[1]], entry[0]["url"])):
        device = devices.setdefault(item["id"], {"id": item["id"], "openmhs": item["openmhs"], "addresses": []})
        if item["url"] not in [address["url"] for address in device["addresses"]]:
            device["addresses"].append({"url": item["url"], "source": source})
    return {"devices": list(devices.values()), "tailscale": state}
