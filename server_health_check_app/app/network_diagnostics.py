from __future__ import annotations

import ipaddress
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import psutil

from .models import CheckResult, StepState


TARGET_IP = "172.16.50.254"
EXPECTED_SUBNET = ipaddress.ip_network("172.16.50.0/24", strict=False)


@dataclass
class AdapterInfo:
    name: str
    is_up: bool
    speed: int
    mtu: int
    mac: str
    ipv4: List[str]


ETHERNET_HINTS = (
    "ethernet",
    "eth",
    "en",
    "lan",
    "realtek",
    "intel(r) ethernet",
    "gigabit",
    "usb ethernet",
)
IGNORE_HINTS = (
    "loopback",
    "lo",
    "docker",
    "wsl",
    "vbox",
    "vmware",
    "hyper-v",
    "bluetooth",
    "wireless",
    "wi-fi",
    "wifi",
    "wlan",
    "tailscale",
    "zerotier",
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=8)


def list_adapters() -> List[AdapterInfo]:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    adapters: List[AdapterInfo] = []
    for name, addr_list in addrs.items():
        lowered = name.lower()
        if any(h in lowered for h in IGNORE_HINTS):
            continue
        looks_ethernet = any(h in lowered for h in ETHERNET_HINTS)
        if not looks_ethernet:
            # Keep unknown physical-looking adapters; better false positive than miss.
            if lowered.startswith(("eth", "en", "eno", "enp")):
                looks_ethernet = True
        if not looks_ethernet:
            continue

        stat = stats.get(name)
        ipv4 = []
        mac = ""
        for addr in addr_list:
            if addr.family == socket.AF_INET:
                ipv4.append(addr.address)
            elif str(addr.family) == "AddressFamily.AF_PACKET" or getattr(socket, "AF_LINK", object()) == addr.family:
                mac = addr.address
        adapters.append(
            AdapterInfo(
                name=name,
                is_up=bool(stat.isup) if stat else False,
                speed=int(stat.speed) if stat and stat.speed is not None else 0,
                mtu=int(stat.mtu) if stat else 0,
                mac=mac,
                ipv4=ipv4,
            )
        )
    return adapters


def _linux_link_state(name: str) -> Optional[str]:
    if shutil.which("ethtool"):
        try:
            cp = _run(["ethtool", name])
            text = (cp.stdout or "") + "\n" + (cp.stderr or "")
            m = re.search(r"Link detected:\s*(yes|no)", text, flags=re.I)
            if m:
                return m.group(1).lower()
        except Exception:
            return None
    if shutil.which("ip"):
        try:
            cp = _run(["ip", "link", "show", name])
            text = cp.stdout or ""
            if "LOWER_UP" in text:
                return "yes"
            if "NO-CARRIER" in text:
                return "no"
        except Exception:
            return None
    return None


def _windows_adapter_details() -> str:
    text = ""
    try:
        cp = _run(["powershell", "-NoProfile", "-Command", "Get-NetAdapter | Format-List -Property Name,Status,MediaConnectionState,InterfaceDescription"])
        text = cp.stdout or cp.stderr or ""
    except Exception:
        pass
    return text


def ping_host(ip: str) -> tuple[bool, str]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1500", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        cp = _run(cmd)
        text = (cp.stdout or "") + (cp.stderr or "")
        return cp.returncode == 0, text.strip()
    except Exception as exc:
        return False, str(exc)


def tcp_connect(ip: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, f"TCP {port} reachable"
    except Exception as exc:
        return False, str(exc)


def diagnose_connectivity(target_ip: str = TARGET_IP) -> CheckResult:
    adapters = list_adapters()
    if not adapters:
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.FAILED,
            summary="No wired Ethernet adapter detected.",
            details="The app could not find any physical or likely wired Ethernet interface on this laptop.",
            severity="Critical",
            suggestion="Connect or install a wired Ethernet adapter, then retry.",
            cause_code="1A",
            metadata={"adapters": []},
        )

    enabled = [a for a in adapters if a.is_up]
    if not enabled:
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.FAILED,
            summary="Ethernet adapter found, but it appears disabled.",
            details="Detected adapters: " + ", ".join(a.name for a in adapters),
            severity="High",
            suggestion="Enable the Ethernet adapter in the operating system, then retry.",
            cause_code="1C",
            metadata={"adapters": [a.__dict__ for a in adapters]},
        )

    system = platform.system().lower()
    cable_disconnected = False
    cable_candidates = []
    for adapter in enabled:
        if system == "linux":
            state = _linux_link_state(adapter.name)
            cable_candidates.append(f"{adapter.name}: link={state or 'unknown'}")
            if state == "no":
                cable_disconnected = True
        elif system == "windows":
            details = _windows_adapter_details().lower()
            if adapter.name.lower() in details and ("disconnected" in details or "media disconnected" in details):
                cable_disconnected = True
                cable_candidates.append(f"{adapter.name}: disconnected")

    any_ipv4 = any(adapter.ipv4 for adapter in enabled)
    if cable_disconnected and not any_ipv4:
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.FAILED,
            summary="Ethernet cable appears unplugged or link is down.",
            details="; ".join(cable_candidates) if cable_candidates else "Link state indicates no carrier on the active adapter.",
            severity="Critical",
            suggestion="Plug in the Ethernet cable securely on both ends and retry.",
            cause_code="1B",
            metadata={"adapters": [a.__dict__ for a in enabled]},
        )

    if not any_ipv4:
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.FAILED,
            summary="Ethernet is connected, but no IPv4 address is assigned.",
            details="Adapters are enabled, but no IPv4 address was found on the wired interface.",
            severity="High",
            suggestion="Set a static IP such as 172.16.50.10 with subnet mask 255.255.255.0, then retry.",
            cause_code="1D",
            metadata={"adapters": [a.__dict__ for a in enabled]},
        )

    matching = []
    non_matching = []
    for adapter in enabled:
        for ip in adapter.ipv4:
            try:
                if ipaddress.ip_address(ip) in EXPECTED_SUBNET:
                    matching.append((adapter.name, ip))
                else:
                    non_matching.append((adapter.name, ip))
            except ValueError:
                pass

    if not matching:
        extra = ", ".join(f"{name}={ip}" for name, ip in non_matching) or "No compatible IPv4 found."
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.FAILED,
            summary="Laptop IP is not in the required 172.16.50.x/24 subnet.",
            details=extra,
            severity="High",
            suggestion="Set the Ethernet adapter to a static IP like 172.16.50.10 and subnet mask 255.255.255.0. Disable Wi-Fi if routing conflicts continue.",
            cause_code="1E",
            metadata={"adapters": [a.__dict__ for a in enabled]},
        )

    ping_ok, ping_output = ping_host(target_ip)
    ssh_ok, ssh_message = tcp_connect(target_ip, 22, timeout=1.5)
    http_ok, http_message = tcp_connect(target_ip, 80, timeout=1.5)
    https5000_ok, https5000_message = tcp_connect(target_ip, 5000, timeout=1.5)

    if ping_ok:
        ip_text = ", ".join(f"{name}={ip}" for name, ip in matching)
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.PASSED,
            summary="Connectivity to the server is confirmed.",
            details=f"Matching adapter/IP: {ip_text}\nPing to {target_ip} succeeded.",
            severity="Info",
            suggestion="Proceed with all remaining checks.",
            cause_code="OK",
            metadata={"adapters": [a.__dict__ for a in enabled]},
        )

    # Ping failed. Decide whether it is hard failure or likely ICMP blocked.
    if ssh_ok or http_ok or https5000_ok:
        reachable_ports = []
        if ssh_ok:
            reachable_ports.append(f"SSH/22 ({ssh_message})")
        if http_ok:
            reachable_ports.append(f"HTTP/80 ({http_message})")
        if https5000_ok:
            reachable_ports.append(f"HTTPS/5000 ({https5000_message})")
        return CheckResult(
            step_id=1,
            title="Network Connection Verification",
            state=StepState.WARNING,
            summary="Server is reachable, but ICMP ping appears blocked.",
            details=(
                f"Ping to {target_ip} failed, but other services responded: "
                + ", ".join(reachable_ports)
                + "\nDNS is not required for direct IP communication."
            ),
            severity="Medium",
            suggestion="Proceed with checks. If needed, review firewall rules for ICMP or confirm ping is intentionally blocked.",
            cause_code="1H",
            metadata={"adapters": [a.__dict__ for a in enabled], "ping_output": ping_output},
        )

    duplicate_hint = ""
    if "duplicate" in ping_output.lower() or "conflict" in ping_output.lower():
        duplicate_hint = "Possible duplicate IP or address conflict detected."
        cause = "1G"
        suggestion = "Change your laptop to an unused 172.16.50.x address and retry."
        summary = "Possible duplicate IP or IP conflict on the local network."
    else:
        cause = "1F"
        suggestion = "Check server power, verify the correct cable path, confirm the server still uses 172.16.50.254, and disable Wi-Fi if routing conflict exists."
        summary = "Correct subnet detected, but the server did not respond."

    return CheckResult(
        step_id=1,
        title="Network Connection Verification",
        state=StepState.FAILED,
        summary=summary,
        details=f"Ping output: {ping_output or 'No response'}\n{duplicate_hint}\nDNS is not required for direct IP communication.",
        severity="Critical",
        suggestion=suggestion,
        cause_code=cause,
        metadata={"adapters": [a.__dict__ for a in enabled], "ping_output": ping_output},
    )
