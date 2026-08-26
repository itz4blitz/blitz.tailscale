"""Generic Tailscale collector: any account, extra sockets, no Premier hardcoding."""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import tailscale_collect as tc


def _status(*, suffix="example.ts.net", self_name="zen", online=True, peers=None):
    peer_map = {}
    for peer in peers or []:
        peer_map[peer["id"]] = peer["node"]
    return {
        "BackendState": "Running" if online else "Stopped",
        "CurrentTailnet": {"MagicDNSSuffix": suffix},
        "Self": {
            "HostName": self_name,
            "Online": online,
            "DNSName": f"{self_name}.{suffix}.",
            "TailscaleIPs": ["100.1.2.3"],
            "OS": "linux",
            "LastSeen": "",
        },
        "Peer": peer_map,
    }


class DiscoverDaemonsTest(unittest.TestCase):
    def test_default_daemon_when_run_dir_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemons = tc.discover_daemons(Path(tmp))
        self.assertEqual(len(daemons), 1)
        self.assertEqual(daemons[0]["id"], "default")
        self.assertEqual(daemons[0]["socket"], "")
        self.assertEqual(daemons[0]["unit"], "tailscaled")
        self.assertNotEqual(daemons[0]["id"], "premier")

    def test_extra_socket_becomes_a_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tailscale-osunlocked.sock").touch()
            (root / "tailscale").mkdir()
            (root / "tailscale" / "tailscaled.sock").touch()
            daemons = tc.discover_daemons(root)
        ids = [d["id"] for d in daemons]
        self.assertEqual(ids[0], "default")
        self.assertIn("osunlocked", ids)
        extra = next(d for d in daemons if d["id"] == "osunlocked")
        self.assertTrue(extra["socket"].endswith("tailscale-osunlocked.sock"))
        self.assertEqual(extra["unit"], "tailscaled-osunlocked")
        self.assertNotIn("premier", ids)


class MergeStatusTest(unittest.TestCase):
    def test_uses_magicdns_from_status_not_hardcoded_suffix(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        payload = tc.merge_status(
            {"default": _status(suffix="mytailnet.ts.net", self_name="box")},
            daemons=[spec],
        )
        self.assertTrue(payload["ready"])
        daemon = payload["daemons"][0]
        self.assertEqual(daemon["id"], "default")
        self.assertEqual(daemon["label"], "Tailscale")
        self.assertEqual(daemon["suffix"], "mytailnet.ts.net")
        self.assertEqual(daemon["unit"], "tailscaled")
        self_item = next(i for i in daemon["items"] if i["self"])
        self.assertEqual(self_item["url"], "https://box.mytailnet.ts.net")
        self.assertNotIn("taild1bbf", json.dumps(payload))
        self.assertNotIn("premier", json.dumps(payload))

    def test_lists_peers_without_hardcoded_service_names(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        peer = {
            "id": "n1",
            "node": {
                "HostName": "tower",
                "Online": True,
                "DNSName": "tower.example.ts.net.",
                "TailscaleIPs": ["100.9.9.9"],
                "OS": "linux",
                "LastSeen": "",
            },
        }
        payload = tc.merge_status(
            {"default": _status(peers=[peer])},
            daemons=[spec],
        )
        names = [i["name"] for i in payload["daemons"][0]["items"]]
        self.assertEqual(sorted(names), ["tower", "zen"])
        self.assertNotIn("quality", names)
        self.assertNotIn("bugtrace", names)

    def test_tagged_peers_are_services(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        tagged = {
            "id": "n1",
            "node": {
                "HostName": "nas",
                "Online": True,
                "DNSName": "nas.example.ts.net.",
                "TailscaleIPs": ["100.9.9.8"],
                "OS": "linux",
                "LastSeen": "",
                "Tags": ["tag:server"],
            },
        }
        plain = {
            "id": "n2",
            "node": {
                "HostName": "laptop",
                "Online": True,
                "DNSName": "laptop.example.ts.net.",
                "TailscaleIPs": ["100.9.9.7"],
                "OS": "linux",
                "LastSeen": "",
            },
        }
        payload = tc.merge_status(
            {"default": _status(peers=[tagged, plain])},
            daemons=[spec],
        )
        items = payload["daemons"][0]["items"]
        kinds = {i["name"]: i["kind"] for i in items}
        self.assertEqual(kinds["nas"], "service")
        self.assertEqual(kinds["laptop"], "machine")
        self.assertEqual(kinds["zen"], "machine")
        # Services lead the list.
        self.assertEqual(items[0]["name"], "nas")

    def test_configured_services_ride_along_with_dns_online_state(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        peer = {
            "id": "n1",
            "node": {
                "HostName": "git",
                "Online": True,
                "DNSName": "git.example.ts.net.",
                "TailscaleIPs": ["100.9.9.6"],
                "OS": "linux",
                "LastSeen": "",
            },
        }
        payload = tc.merge_status(
            {"default": _status(peers=[peer])},
            probes={"https://ci.example.ts.net": 200},
            daemons=[spec],
            services={"default": ["git", "ci", "retired"]},
            resolve=lambda host: host != "retired.example.ts.net",
        )
        items = payload["daemons"][0]["items"]
        by_name = {i["name"]: i for i in items}
        # A name that is already a visible peer is not duplicated.
        self.assertEqual(by_name["git"]["kind"], "machine")
        self.assertNotIn(("service", "git"), [(i["kind"], i["name"]) for i in items])
        # Hidden-but-registered services appear with DNS-based online state.
        self.assertEqual(by_name["ci"]["kind"], "service")
        self.assertTrue(by_name["ci"]["online"])
        self.assertEqual(by_name["ci"]["url"], "https://ci.example.ts.net")
        self.assertEqual(by_name["ci"]["http"], 200)
        self.assertEqual(by_name["retired"]["kind"], "service")
        self.assertFalse(by_name["retired"]["online"])
        self.assertEqual(payload["daemons"][0]["totalCount"], 4)


class ServiceConfigTest(unittest.TestCase):
    def test_loads_names_per_daemon_and_drops_unsafe_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            path.write_text(
                json.dumps({"default": ["git", "vault"], "work": ["ci"], "bad": ["a;rm -rf"]}),
                encoding="utf-8",
            )
            config = tc.load_service_config(path)
        self.assertEqual(config["default"], ["git", "vault"])
        self.assertEqual(config["work"], ["ci"])
        # Daemon ids with only unsafe names contribute nothing.
        self.assertNotIn("bad", config)
        self.assertTrue(all(tc.safe_name(n) for names in config.values() for n in names))

    def test_missing_or_broken_file_yields_no_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = tc.load_service_config(Path(tmp) / "nope.json")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(missing, {})
            self.assertEqual(tc.load_service_config(broken), {})


class CollectProbesTest(unittest.TestCase):
    def test_collect_probes_only_configured_service_urls(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        peer = {
            "id": "n1",
            "node": {
                "HostName": "tower",
                "Online": True,
                "DNSName": "tower.example.ts.net.",
                "TailscaleIPs": ["100.9.9.5"],
                "OS": "linux",
                "LastSeen": "",
            },
        }
        status = _status(peers=[peer])
        probed = []

        def fake_run(cmd, **_kwargs):
            assert cmd[:2] == ["tailscale", "status"]
            return json.dumps(status)

        def fake_probe(url):
            probed.append(url)
            return 204

        payload = tc.collect(
            run=fake_run,
            probe=fake_probe,
            daemons=[spec],
            services={"default": ["ci"]},
            resolve=lambda _host: True,
        )
        self.assertEqual(probed, ["https://ci.example.ts.net"])
        service = next(i for i in payload["daemons"][0]["items"] if i["name"] == "ci")
        self.assertEqual(service["http"], 204)
        self.assertTrue(service["online"])

    def test_dns_resolves_reports_unresolvable_hosts_as_offline(self) -> None:
        with unittest.mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
            self.assertFalse(tc.dns_resolves("gone.example.ts.net"))
        with unittest.mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, None)]):
            self.assertTrue(tc.dns_resolves("live.example.ts.net"))


class OpenUrlTest(unittest.TestCase):
    def test_open_url_uses_status_suffix(self) -> None:
        spec = {"id": "default", "label": "Tailscale", "socket": "", "unit": "tailscaled"}
        url = tc.open_url_for(
            spec,
            "git",
            status=_status(suffix="corp.ts.net"),
        )
        self.assertEqual(url, "https://git.corp.ts.net")


if __name__ == "__main__":
    unittest.main()
