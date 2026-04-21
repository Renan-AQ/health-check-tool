from __future__ import annotations

import os
import posixpath
import socket
import ssl
import stat
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import paramiko
import requests

from .models import CheckResult, StepState


@dataclass
class AppConfig:
    host: str = "172.16.50.254"
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    logs_remote_path: str = "/var/logs/"
    logs_local_path: str = str(Path.home() / "server_health_logs")
    verify_tls: bool = False
    timeout_seconds: int = 5


class SSHClientManager:
    def __init__(self, config: AppConfig):
        self.config = config

    def connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.config.host,
            "port": self.config.ssh_port,
            "username": self.config.ssh_username,
            "timeout": self.config.timeout_seconds,
            "banner_timeout": self.config.timeout_seconds,
            "auth_timeout": self.config.timeout_seconds,
        }
        if self.config.ssh_key_path:
            kwargs["key_filename"] = self.config.ssh_key_path
        else:
            kwargs["password"] = self.config.ssh_password
        client.connect(**kwargs)
        return client


def _run_ssh_command(client: paramiko.SSHClient, command: str, timeout: int = 8) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")


def step2_service_check(config: AppConfig) -> CheckResult:
    try:
        with SSHClientManager(config).connect() as client:
            checks = {}
            for service in ["fbs_modbus", "modbus_server"]:
                code, out, err = _run_ssh_command(client, f"systemctl is-active {service}")
                checks[service] = (code == 0 and out.strip() == "active", out.strip() or err.strip())

        failed = [name for name, (ok, _) in checks.items() if not ok]
        if failed:
            details = "\n".join(f"{name}: {checks[name][1]}" for name in checks)
            return CheckResult(
                step_id=2,
                title="Service Check",
                state=StepState.FAILED,
                summary="One or more required services are not active.",
                details=details,
                severity="High",
                suggestion="Restart the failed services and inspect systemctl/journal logs on the server.",
            )
        return CheckResult(
            step_id=2,
            title="Service Check",
            state=StepState.PASSED,
            summary="Required services are active.",
            details="\n".join(f"{name}: active" for name in checks),
            severity="Info",
            suggestion="No action required.",
        )
    except Exception as exc:
        return CheckResult(
            step_id=2,
            title="Service Check",
            state=StepState.FAILED,
            summary="Could not complete service check over SSH.",
            details=str(exc),
            severity="High",
            suggestion="Verify SSH credentials, SSH service availability, and permissions.",
        )


def step3_docker_check(config: AppConfig) -> CheckResult:
    try:
        with SSHClientManager(config).connect() as client:
            code, out, err = _run_ssh_command(client, "systemctl is-active docker")
            docker_active = code == 0 and out.strip() == "active"
            code2, out2, err2 = _run_ssh_command(client, "docker ps --format 'table {{.Names}}\t{{.Status}}'")

        if not docker_active:
            return CheckResult(
                step_id=3,
                title="Docker Check",
                state=StepState.FAILED,
                summary="Docker service is not active.",
                details=out.strip() or err.strip() or "systemctl did not report docker as active.",
                severity="High",
                suggestion="Start or restart the docker service and review daemon logs.",
            )
        if code2 != 0:
            return CheckResult(
                step_id=3,
                title="Docker Check",
                state=StepState.WARNING,
                summary="Docker service is active, but container listing failed.",
                details=err2.strip() or out2.strip(),
                severity="Medium",
                suggestion="Verify docker CLI permissions and inspect the daemon state.",
            )
        return CheckResult(
            step_id=3,
            title="Docker Check",
            state=StepState.PASSED,
            summary="Docker is active and containers were listed successfully.",
            details=out2.strip(),
            severity="Info",
            suggestion="No action required.",
        )
    except Exception as exc:
        return CheckResult(
            step_id=3,
            title="Docker Check",
            state=StepState.FAILED,
            summary="Could not complete Docker check.",
            details=str(exc),
            severity="High",
            suggestion="Verify SSH credentials, docker availability, and permissions.",
        )


def _web_check(url: str, step_id: int, title: str, verify_tls: bool, timeout: int) -> CheckResult:
    try:
        response = requests.get(url, timeout=timeout, verify=verify_tls)
        if 200 <= response.status_code < 400:
            return CheckResult(
                step_id=step_id,
                title=title,
                state=StepState.PASSED,
                summary=f"{url} responded successfully.",
                details=f"HTTP status: {response.status_code}",
                severity="Info",
                suggestion="No action required.",
            )
        return CheckResult(
            step_id=step_id,
            title=title,
            state=StepState.FAILED,
            summary=f"{url} responded with an unexpected status.",
            details=f"HTTP status: {response.status_code}",
            severity="Medium",
            suggestion="Inspect the web service, reverse proxy, and application logs.",
        )
    except requests.exceptions.SSLError as exc:
        return CheckResult(
            step_id=step_id,
            title=title,
            state=StepState.WARNING,
            summary="TLS handshake failed or the certificate is untrusted.",
            details=str(exc),
            severity="Medium",
            suggestion="If this is an expected self-signed certificate, disable TLS verification in the app. Otherwise inspect the certificate and HTTPS endpoint.",
        )
    except Exception as exc:
        return CheckResult(
            step_id=step_id,
            title=title,
            state=StepState.FAILED,
            summary=f"Could not reach {url}.",
            details=str(exc),
            severity="High",
            suggestion="Verify the web service is listening and reachable on the target port.",
        )


def step4_main_ui_check(config: AppConfig) -> CheckResult:
    return _web_check(f"http://{config.host}", 4, "Main Web UI Check", config.verify_tls, config.timeout_seconds)


def step5_secure_ui_check(config: AppConfig) -> CheckResult:
    return _web_check(f"https://{config.host}:5000", 5, "Port 5000 Secure UI Check", config.verify_tls, config.timeout_seconds)


def _download_dir(sftp: paramiko.SFTPClient, remote_dir: str, local_dir: Path, downloaded: list[str]) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        remote_path = posixpath.join(remote_dir, entry.filename)
        local_path = local_dir / entry.filename
        if stat.S_ISDIR(entry.st_mode):
            _download_dir(sftp, remote_path, local_path, downloaded)
        else:
            try:
                sftp.get(remote_path, str(local_path))
                downloaded.append(str(local_path))
            except Exception:
                # Keep going even if one file fails.
                continue


def step6_copy_logs(config: AppConfig) -> CheckResult:
    target = Path(config.logs_local_path)
    downloaded: list[str] = []
    try:
        with SSHClientManager(config).connect() as client:
            sftp = client.open_sftp()
            try:
                _download_dir(sftp, config.logs_remote_path, target, downloaded)
            finally:
                sftp.close()

        if not downloaded:
            return CheckResult(
                step_id=6,
                title="Log Download",
                state=StepState.WARNING,
                summary="Connected, but no log files were copied.",
                details=f"Remote path checked: {config.logs_remote_path}\nLocal folder: {target}",
                severity="Medium",
                suggestion="Verify the remote log path, permissions, and whether logs exist.",
            )
        preview = "\n".join(downloaded[:10])
        more = "" if len(downloaded) <= 10 else f"\n...and {len(downloaded) - 10} more files"
        return CheckResult(
            step_id=6,
            title="Log Download",
            state=StepState.PASSED,
            summary=f"Copied {len(downloaded)} log file(s).",
            details=f"Saved to: {target}\n{preview}{more}",
            severity="Info",
            suggestion="Open the downloaded logs folder for deeper analysis if needed.",
        )
    except Exception as exc:
        return CheckResult(
            step_id=6,
            title="Log Download",
            state=StepState.FAILED,
            summary="Could not copy logs from the server.",
            details=str(exc),
            severity="High",
            suggestion="Verify SSH/SFTP access, path `/var/logs/`, and permissions.",
        )


def parallel_checks(config: AppConfig) -> List[CheckResult]:
    jobs: Dict[int, Callable[[AppConfig], CheckResult]] = {
        2: step2_service_check,
        3: step3_docker_check,
        4: step4_main_ui_check,
        5: step5_secure_ui_check,
        6: step6_copy_logs,
    }
    results: Dict[int, CheckResult] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(func, config): step_id for step_id, func in jobs.items()}
        for future in as_completed(future_map):
            result = future.result()
            results[result.step_id] = result
    return [results[i] for i in sorted(results)]
