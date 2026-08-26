#!/usr/bin/env python3
"""Collect Tailscale inventory for the blitz.tailscale bar widget.

Discovers the default tailscaled plus any extra /run/tailscale-*.sock daemons.
No account names, tailnets, or service lists are hardcoded.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

ALLOWED_ACTIONS = ("open", "ping", "logs")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SECRET_KEYS = ("nodekey", "private", "authkey", "key", "token", "cookie")
SERVICE_CONFIG_NAME = "services.json"
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


def service_config_path() -> Path:
    return Path(__file__).resolve().parent / SERVICE_CONFIG_NAME


def load_service_config(path: Path | None = None) -> dict[str, list[str]]:
    """Read per-daemon extra service hostnames from services.json.

    The file is user config (gitignored; see services.json.example) so nothing
    about any particular tailnet is baked into the collector:

        {"default": ["git", "vault"], "work": ["ci"]}
    """
    config_file = path or service_config_path()
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    config: dict[str, list[str]] = {}
    for daemon_id, names in raw.items():
        if not isinstance(names, list):
            continue
        cleaned = [name for name in names if safe_name(str(name))]
        if cleaned:
            config[str(daemon_id)] = cleaned
    return config


def dns_resolves(host: str, timeout: float = 1.5) -> bool:
    """True when MagicDNS still has a record for the host.

    Service-only nodes can be hidden from this machine's peer list by ACLs,
    so DNS is the one signal every user's client has for them.
    """
    if not host:
        return False

    def query() -> bool:
        try:
            socket.getaddrinfo(host, None)
        except (socket.gaierror, OSError, UnicodeError):
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return bool(pool.submit(query).result(timeout=timeout))
    except (FutureTimeout, OSError):
        return False


def _clean_host(dns_name: str) -> str:
    return str(dns_name or "").rstrip(".")


def _peer_kind(node: dict) -> str:
    """Tagged nodes are services in Tailscale's own model (ACL tags mark
    service accounts, servers, and serve/funnel endpoints)."""
    tags = node.get("Tags")
    return "service" if isinstance(tags, list) and tags else "machine"


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


def _service_item(name: str, suffix: str, probes: dict, online: bool) -> dict:
    dns = f"{name}.{suffix}" if suffix else name
    url = f"https://{dns}"
    return {
        "kind": "service",
        "name": name,
        "online": bool(online),
        "self": False,
        "url": url,
        "ip": "",
        "os": "",
        "http": probes.get(url),
        "lastSeen": "",
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


def merge_status(
    statuses: dict,
    probes: dict | None = None,
    daemons: list[dict] | None = None,
    services: dict[str, list[str]] | None = None,
    resolve=None,
) -> dict:
    probes = probes or {}
    specs = daemons if daemons is not None else discover_daemons()
    extra_services = services or {}
    resolver = resolve if resolve is not None else (lambda host: False)
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
                items.append(_node_item(_peer_kind(peer), peer, suffix, probes))
            # Services the user registered in services.json ride along even
            # when the tailnet hides them from this machine's peer list.
            known = {item["name"].lower() for item in items}
            for name in extra_services.get(spec["id"], []):
                if name.lower() in known:
                    continue
                host = f"{name}.{suffix}" if suffix else name
                items.append(_service_item(name, suffix, probes, online=resolver(host)))
        items.sort(key=lambda item: (item["kind"] != "service", not item["online"], item["name"].lower()))
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


def demo_view() -> str:
    flag = Path(__file__).resolve().parent / "DEMO"
    try:
        if flag.is_file():
            return flag.read_text(encoding="utf-8").strip() or "overview"
    except OSError:
        pass
    return ""


def demo_payload(view: str = "overview") -> dict:
    def machine(name, suffix, online=True, self=False, os_name="linux"):
        return {
            "kind": "machine", "name": name, "online": online, "self": self,
            "url": f"https://{name}.{suffix}", "ip": "100.64.0.2", "os": os_name,
            "http": 200 if online else None, "lastSeen": "" if online else "2h ago",
        }

    def service(name, suffix, online=True):
        return {
            "kind": "service", "name": name, "online": online, "self": False,
            "url": f"https://{name}.{suffix}", "ip": "", "os": "",
            "http": 200 if online else None, "lastSeen": "",
        }

    home_items = [
        service("git", "example.ts.net"), service("cloud", "example.ts.net"),
        machine("studio", "example.ts.net", self=True), machine("nas", "example.ts.net"),
        machine("laptop", "example.ts.net", online=False, os_name="macos"),
    ]
    work_items = [
        service("ci", "corp.ts.net"), machine("builder", "corp.ts.net"),
        machine("review", "corp.ts.net"), machine("old-box", "corp.ts.net", online=False),
    ]
    daemons = [
        {"id": "home", "label": "Home", "unit": "tailscaled", "online": True, "suffix": "example.ts.net",
         "selfName": "studio", "onlineCount": 4, "totalCount": 5, "items": home_items},
        {"id": "work", "label": "Work", "unit": "tailscaled-work", "online": True, "suffix": "corp.ts.net",
         "selfName": "", "onlineCount": 3, "totalCount": 4, "items": work_items},
    ]
    return {
        "ready": True, "status": "ok", "demo": True, "demoView": view,
        "daemonOnline": 2, "daemonTotal": 2, "onlineCount": 7, "totalCount": 9,
        "daemons": daemons,
    }


def synthetic_service_urls(statuses: dict, specs: list[dict], services: dict[str, list[str]]) -> list[str]:
    """URLs for configured services: probe targets the netmap cannot give us."""
    urls = []
    for spec in specs:
        raw = (statuses or {}).get(spec["id"])
        if not isinstance(raw, dict):
            continue
        suffix = str((raw.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or "").rstrip(".")
        if not suffix:
            continue
        machine_names = {
            str(node.get("HostName") or "").lower()
            for node in [raw.get("Self") or {}, *list((raw.get("Peer") or {}).values())]
            if isinstance(node, dict)
        }
        for name in services.get(spec["id"], []):
            if name.lower() in machine_names:
                continue
            urls.append(f"https://{name}.{suffix}")
    return urls


def collect(
    run=None,
    timeout: float = 4.0,
    probe=None,
    daemons: list[dict] | None = None,
    services: dict[str, list[str]] | None = None,
    resolve=None,
) -> dict:
    view = demo_view()
    if view:
        return demo_payload(view)
    runner = run or subprocess.check_output
    specs = daemons if daemons is not None else discover_daemons()
    cfg = services if services is not None else load_service_config()
    statuses = {}
    for spec in specs:
        statuses[spec["id"]] = _load_status(runner, spec, timeout)
    prober = probe if probe is not None else probe_url
    probes = {url: prober(url) for url in synthetic_service_urls(statuses, specs, cfg)}
    resolver = resolve if resolve is not None else dns_resolves
    return merge_status(statuses, probes, daemons=specs, services=cfg, resolve=resolver)


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
