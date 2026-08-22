"""Generic Tailscale collector: any account, extra sockets, no Premier hardcoding."""

from __future__ import annotations

import json
import tempfile
import unittest
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
