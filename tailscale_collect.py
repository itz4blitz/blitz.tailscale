#!/usr/bin/env python3
"""Collect Tailscale inventory for the blitz.tailscale bar widget.

Discovers the default tailscaled plus any extra /run/tailscale-*.sock daemons.
No account names, tailnets, or service lists are hardcoded.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ALLOWED_ACTIONS = ("open", "ping", "logs")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SECRET_KEYS = ("nodekey", "private", "authkey", "key", "token", "cookie")
DEFAULT_DAEMON = {
    "id": "default",
    "label": "Tailscale",
    "socket": "",
    "unit": "tailscaled",
}


def safe_action(value: str) -> bool:
    return str(value or "") in ALLOWED_ACTIONS


def safe_name(value: str) -> bool:
    return bool(NAME_RE.fullmatch(str(value or "")))


def safe_daemon(value: str, daemons: list[dict] | None = None) -> bool:
    if not safe_name(value):
        return False
    if daemons is None:
        return True
    return any(spec["id"] == value for spec in daemons)


def browser_command(_daemon_id: str, url: str) -> list[str]:
    return ["xdg-open", url]


def discover_daemons(run_dir: Path | None = None) -> list[dict]:
    daemons = [dict(DEFAULT_DAEMON)]
    root = Path(run_dir or "/run")
    seen = {"default"}
    sockets = list(root.glob("tailscale*.sock"))
    nested = root / "tailscale"
    if nested.is_dir():
        sockets.extend(nested.glob("*.sock"))
    for path in sorted({p.resolve() for p in sockets if p.is_file() or p.exists()}):
        name = path.name
        if name in ("tailscaled.sock",):
            continue
        ident = name.removeprefix("tailscaled-").removeprefix("tailscale-").removesuffix(".sock")
        if ident in ("", "tailscaled", "default") or ident in seen:
            continue
        seen.add(ident)
        daemons.append(
            {
                "id": ident,
                "label": ident,
                "socket": str(path),
                "unit": f"tailscaled-{ident}",
            }
        )
    return daemons


def _daemon_spec(daemon_id: str, daemons: list[dict] | None = None) -> dict | None:
    for spec in daemons or discover_daemons():
        if spec["id"] == daemon_id:
            return spec
    return None


def _clean_host(dns_name: str) -> str:
    return str(dns_name or "").rstrip(".")


def _node_item(kind: str, node: dict, suffix: str, probes: dict, is_self: bool = False) -> dict:
    name = str(node.get("HostName") or "").strip()
    dns = _clean_host(node.get("DNSName") or "")
    if not dns and name and suffix:
        dns = f"{name}.{suffix}"
    url = f"https://{dns}" if dns else ""
    ips = [ip for ip in (node.get("TailscaleIPs") or []) if isinstance(ip, str) and ":" not in ip]
    last_seen = str(node.get("LastSeen") or "")
    if last_seen in ("0001-01-01T00:00:00Z", "0001-01-01 00:00:00 +0000 UTC"):
        last_seen = ""
    return {
        "kind": kind,
        "name": name,
        "online": bool(node.get("Online")),
        "self": bool(is_self),
        "url": url,
        "ip": ips[0] if ips else "",
        "os": str(node.get("OS") or ""),
        "http": probes.get(url),
        "lastSeen": last_seen,
    }


def open_url_for(spec: dict, name: str, status: dict | None) -> str:
    if not safe_name(name):
        return ""
    suffix = ""
    if isinstance(status, dict):
        suffix = str((status.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or "").rstrip(".")
    if not suffix:
        return ""
    return f"https://{name}.{suffix}"


def merge_status(statuses: dict, probes: dict | None = None, daemons: list[dict] | None = None) -> dict:
    probes = probes or {}
    specs = daemons if daemons is not None else discover_daemons()
    out = []
    for spec in specs:
        raw = (statuses or {}).get(spec["id"])
        suffix = ""
        online = False
        items = []
        if isinstance(raw, dict):
            suffix = str((raw.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or "").rstrip(".")
            self = raw.get("Self") or {}
            online = bool(self.get("Online")) and str(raw.get("BackendState") or "") == "Running"
            if self.get("HostName"):
                items.append(_node_item("machine", self, suffix, probes, is_self=True))
            for peer in (raw.get("Peer") or {}).values():
                if not isinstance(peer, dict) or not peer.get("HostName"):
                    continue
                items.append(_node_item("machine", peer, suffix, probes))
        items.sort(key=lambda item: (not item["online"], item["name"].lower()))
        online_count = sum(1 for item in items if item["online"])
        out.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "unit": spec.get("unit") or "tailscaled",
                "online": online,
                "suffix": suffix,
                "selfName": str(((raw or {}).get("Self") or {}).get("HostName") or ""),
                "onlineCount": online_count,
                "totalCount": len(items),
                "items": items,
            }
        )
    daemon_online = sum(1 for daemon in out if daemon["online"])
    payload = {
        "ready": daemon_online > 0,
        "status": "ok" if daemon_online else "unavailable",
        "daemonOnline": daemon_online,
        "daemonTotal": len(out),
        "onlineCount": sum(d["onlineCount"] for d in out),
        "totalCount": sum(d["totalCount"] for d in out),
        "daemons": out,
    }
    return _redact(payload)


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if str(key).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _status_cmd(socket: str) -> list[str]:
    cmd = ["tailscale"]
    if socket:
        cmd.extend(["--socket", socket])
    cmd.extend(["status", "--json"])
    return cmd


def _load_status(run, spec: dict, timeout: float) -> dict | None:
    try:
        raw = run(_status_cmd(spec["socket"]), text=True, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def probe_url(url: str, run=None, timeout: float = 2.0) -> int | None:
    if not url.startswith("https://"):
        return None
    runner = run or subprocess.check_output
    try:
        raw = runner(
            ["curl", "-skI", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(int(timeout)), url],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
    except Exception:
        return None
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        code = int(text.strip()[:3])
    except ValueError:
        return None
    return code if 100 <= code <= 599 else None


def collect(run=None, timeout: float = 4.0, probe=None, daemons: list[dict] | None = None) -> dict:
    runner = run or subprocess.check_output
    specs = daemons if daemons is not None else discover_daemons()
    statuses = {}
    for spec in specs:
        statuses[spec["id"]] = _load_status(runner, spec, timeout)
    probes = {}
    prober = probe if probe is not None else (lambda _url: None)
    if probe is not None:
        for spec in specs:
            raw = statuses.get(spec["id"])
            if not isinstance(raw, dict):
                continue
            suffix = str((raw.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or "").rstrip(".")
            for node in [raw.get("Self") or {}, *list((raw.get("Peer") or {}).values())]:
                if not isinstance(node, dict):
                    continue
                dns = _clean_host(node.get("DNSName") or "")
                if not dns and node.get("HostName") and suffix:
                    dns = f"{node.get('HostName')}.{suffix}"
                if dns:
                    url = f"https://{dns}"
                    probes[url] = prober(url)
    elif prober is not probe_url:
        pass
    return merge_status(statuses, probes, daemons=specs)


def run_action(action: str, daemon_id: str, name: str, run=None, timeout: float = 6.0, daemons: list[dict] | None = None) -> int:
    specs = daemons if daemons is not None else discover_daemons()
    if not safe_action(action) or not safe_daemon(daemon_id, specs) or not safe_name(name):
        return 2
    spec = _daemon_spec(daemon_id, specs)
    if spec is None:
        return 2
    runner = run or subprocess.check_call
    if action == "open":
        status = _load_status(run or subprocess.check_output, spec, timeout)
        url = open_url_for(spec, name, status)
        if not url:
            return 2
        command = browser_command(daemon_id, url)
    elif action == "ping":
        command = ["tailscale"]
        if spec["socket"]:
            command.extend(["--socket", spec["socket"]])
        command.extend(["ping", "-c", "1", name])
    else:
        return 2
    try:
        runner(command, timeout=timeout)
        return 0
    except Exception:
        return 1


def read_logs(daemon_id: str, run=None, tail: int = 80, timeout: float = 4.0, daemons: list[dict] | None = None) -> str:
    spec = _daemon_spec(daemon_id, daemons)
    if spec is None:
        return ""
    runner = run or subprocess.check_output
    try:
        raw = runner(
            ["journalctl", "-u", spec["unit"], "--no-pager", "-n", str(int(tail))],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    specs = discover_daemons()
    if args and args[0] == "logs":
        if len(args) != 2 or not safe_daemon(args[1], specs):
            return 2
        sys.stdout.write(read_logs(args[1], daemons=specs))
        return 0
    if args and args[0] == "action":
        if len(args) != 4:
            return 2
        _, daemon_id, action, name = args
        if action == "ping":
            if not safe_daemon(daemon_id, specs) or not safe_name(name):
                return 2
            spec = _daemon_spec(daemon_id, specs)
            if spec is None:
                return 2
            command = ["tailscale"]
            if spec["socket"]:
                command.extend(["--socket", spec["socket"]])
            command.extend(["ping", "-c", "1", name])
            try:
                raw = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=6)
            except Exception as exc:
                sys.stdout.write(str(exc) + "\n")
                return 1
            sys.stdout.write(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            return 0
        code = run_action(action, daemon_id, name, daemons=specs)
        if code == 0:
            print(json.dumps(collect(daemons=specs)))
        else:
            print(json.dumps({"ready": False, "status": "action-failed" if code == 1 else "invalid"}))
        return code
    print(json.dumps(collect(daemons=specs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
