"""Resolve current Tailscale peers without stale IPs or ambiguous short-name routing.

All status responses and receiver probes are injected. These tests never query the real tailnet,
send messages, or read the user's Ishoni home.
"""
import importlib.util
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from support.pkg import REPO


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("tailmsg_peer_resolution_under_test", REPO / "tailmsg.py")
Y = load("ishoni_peer_resolution_under_test", REPO / "plugins/ishoni/scripts/ishoni.py")


def node(name, ip, online=True, hostname="research-box"):
    return {"node": name + ".example.ts.net", "host": name, "hostname": hostname,
            "ip": ip, "ips": [ip] if ip else [], "online": online, "self": False}


def status(nodes):
    def entry(n):
        return {"DNSName": n["node"] + ".", "HostName": n.get("hostname", n["host"]),
                "TailscaleIPs": n.get("ips", [n["ip"]]), "Online": n["online"], "OS": "linux"}
    me = node("sender", "100.64.0.1", hostname="sender")
    return {"BackendState": "Running", "Self": entry(me),
            "Peer": {str(i): entry(n) for i, n in enumerate(nodes)}}


class SelectPeerTests(unittest.TestCase):
    def setUp(self):
        self.old = node("research-box", "100.64.0.2", online=False)
        self.new = node("research-box-1", "100.64.0.3")

    def test_unique_online_hostname_wins_regardless_of_status_order(self):
        for nodes in ([self.old, self.new], [self.new, self.old]):
            for name in ("research-box", "RESEARCH-BOX."):
                with self.subTest(order=[n["node"] for n in nodes], name=name):
                    self.assertEqual(self.new, core.select_peer(nodes, name))

    def test_exact_fqdn_and_ip_keep_the_requested_node_even_when_offline(self):
        nodes = [self.new, self.old]
        for name in (self.old["node"], self.old["node"].upper() + ".", self.old["ip"]):
            self.assertEqual(self.old, core.select_peer(nodes, name))

    def test_secondary_ip_is_an_exact_address(self):
        ipv6 = "fd7a:115c:a1e0::2"
        self.old["ips"].append(ipv6)
        self.assertEqual(self.old, core.select_peer([self.new, self.old], ipv6))

    def test_multiple_online_or_offline_matches_fail_with_exact_candidates(self):
        for online in (True, False):
            nodes = [dict(self.old, online=online), dict(self.new, online=online)]
            for ordered in (nodes, list(reversed(nodes))):
                with self.subTest(online=online, order=[n["node"] for n in ordered]):
                    with self.assertRaisesRegex(ValueError, "(?i)ambiguous") as error:
                        core.select_peer(ordered, "research-box")
                    for n in nodes:
                        self.assertIn(n["node"], str(error.exception))

    def test_unknown_names_do_not_use_prefix_or_numeric_suffix_guessing(self):
        different = node("research-box-42", "100.64.0.4", hostname="fresh-install")
        for name in ("research", "research-box", "research-box-4",
                     "research-box.example.ts.net", "fresh-install.example.ts.net", "100.64.0.99"):
            with self.subTest(name=name):
                self.assertIsNone(core.select_peer([different], name))

    def test_missing_dns_or_ip_does_not_shadow_a_usable_match(self):
        broken = [dict(self.old, ip=None, ips=[]), dict(self.old, node=""), {}]
        self.assertEqual(self.new, core.select_peer(broken + [self.new], "research-box"))
        self.assertIsNone(core.select_peer(broken, "research-box"))

    def test_legacy_normalized_records_need_no_new_optional_fields(self):
        old_shape = {k: v for k, v in self.new.items() if k not in ("hostname", "ips")}
        for name in (old_shape["node"], old_shape["host"], old_shape["ip"]):
            self.assertEqual(old_shape, core.select_peer([old_shape], name))

    def test_one_offline_match_still_resolves_for_caller_status_handling(self):
        self.assertEqual(self.old, core.select_peer([self.old], "research-box"))


class ContextResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.object(core, "log"):
            self.ctx = core.Ctx(self.tmp.name)
        self.old = node("research-box", "100.64.0.2", online=False)
        self.new = node("research-box-1", "100.64.0.3")

    def test_send_resolution_handles_old_offline_node_in_either_order(self):
        for nodes in ([self.old, self.new], [self.new, self.old]):
            with mock.patch.object(self.ctx, "status", return_value=status(nodes)):
                self.assertEqual((self.new["node"], self.new["ip"]), self.ctx.resolve("research-box"))

    def test_each_resolution_rereads_the_current_ip(self):
        changed = dict(self.new, ip="100.64.0.9", ips=["100.64.0.9"])
        with mock.patch.object(self.ctx, "status", side_effect=[status([self.new]), status([changed])]) as read:
            self.assertEqual((self.new["node"], self.new["ip"]), self.ctx.resolve("research-box"))
            self.assertEqual((changed["node"], changed["ip"]), self.ctx.resolve("research-box"))
            self.assertEqual(2, read.call_count)

    def test_ambiguous_alias_does_not_send_to_an_arbitrary_node(self):
        with mock.patch.object(self.ctx, "status", return_value=status([dict(self.old, online=True), self.new])):
            with self.assertRaisesRegex(RuntimeError, "(?i)ambiguous"):
                self.ctx.resolve("research-box")

    def test_explicit_identity_and_missing_address_handling(self):
        with mock.patch.object(self.ctx, "status", return_value=status([self.new, self.old])):
            for name in (self.old["node"], self.old["ip"]):
                self.assertEqual((self.old["node"], self.old["ip"]), self.ctx.resolve(name))
        broken = dict(self.old, ip=None, ips=[])
        with mock.patch.object(self.ctx, "status", return_value=status([broken, self.new])):
            self.assertEqual((self.new["node"], self.new["ip"]), self.ctx.resolve("research-box"))

    def send_args(self):
        return SimpleNamespace(peer="research-box/grok", agent=None, as_agent="codex", kind="text",
                               text="Please review the report.", reply_to=None, wake="none", file=[])

    def test_message_uses_the_current_ip_and_fqdn_preserving_agent_route(self):
        with mock.patch.object(self.ctx, "status", return_value=status([self.old, self.new])), \
                mock.patch.object(core, "http_json", return_value=(202, {"ok": True})) as post, \
                mock.patch.object(core.sys, "stdout", new_callable=io.StringIO) as output:
            core.cmd_send(self.ctx, self.send_args())
        self.assertEqual(1, post.call_count)
        self.assertEqual((self.new["ip"], 7766, "POST", "/msg"), post.call_args.args[:4])
        envelope = post.call_args.args[4]
        self.assertEqual(self.new["node"], envelope["to"])
        self.assertEqual("sender.example.ts.net", envelope["from"])
        self.assertEqual(("grok", "codex"), (envelope["agent"], envelope["from_agent"]))
        self.assertEqual(self.new["node"], post.call_args.kwargs["host"])
        self.assertEqual(self.new["node"], json.loads(output.getvalue())["to"])

    def test_ambiguous_send_stops_before_any_http_request(self):
        with mock.patch.object(self.ctx, "status", return_value=status([dict(self.old, online=True), self.new])), \
                mock.patch.object(core, "http_json") as post:
            with self.assertRaisesRegex(RuntimeError, "(?i)ambiguous"):
                core.cmd_send(self.ctx, self.send_args())
        post.assert_not_called()


class PluginResolutionTests(unittest.TestCase):
    def setUp(self):
        self.old = node("research-box", "100.64.0.2", online=False)
        self.new = node("research-box-1", "100.64.0.3")

    def nodes(self):
        with mock.patch.object(Y, "run_tailscale", return_value=(json.dumps(status([self.old, self.new])), None)):
            nodes, error = Y.tailnet_nodes(["never-call-real-tailscale"])
        self.assertIsNone(error)
        return nodes

    def test_normalization_retains_actual_hostname_and_resolution_agrees(self):
        nodes = self.nodes()
        new = next(n for n in nodes if n["node"] == self.new["node"])
        self.assertEqual("research-box", new["hostname"])
        self.assertEqual("research-box-1", new["host"])
        for ordered in (nodes, list(reversed(nodes))):
            self.assertEqual(new, Y.match_node(ordered, "research-box"))
            self.assertEqual(core.select_peer(ordered, "research-box"), Y.match_node(ordered, "research-box"))

    def test_discovery_peer_filter_uses_the_same_online_alias_selection(self):
        nodes = self.nodes()
        facts = {"cfg": None, "tailscale": {"node": "sender.example.ts.net"}}
        probe_result = {"kind": "unreachable", "http_status": None, "error": "test probe"}
        with mock.patch.object(Y, "probe_info", return_value=probe_result) as probe:
            rows = Y.discovery_rows(None, facts, nodes, [("research-box", 7766)], 7766, 0.2, False)
        self.assertEqual([self.new["node"]], [r["node"] for r in rows])
        self.assertEqual(self.new["ip"], probe.call_args.args[0])
        self.assertEqual(1, probe.call_count)

    def test_plugin_ambiguity_is_a_clean_cli_error(self):
        with mock.patch.object(Y.sys, "stderr") as stderr:
            with self.assertRaises(SystemExit) as error:
                Y.match_node([dict(self.old, online=True), self.new], "research-box")
        self.assertEqual(2, error.exception.code)
        output = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertIn("ambiguous", output.lower())
        self.assertIn(self.old["node"], output)
        self.assertIn(self.new["node"], output)

    def test_new_explicit_upgrade_notice_resolves_alias_to_current_node(self):
        state = {"installations": {}, "notices": {}}
        candidates, unknown = Y.notice_candidates(state, self.nodes(), [("research-box", 7766, "grok")],
                                                  7766, False, "0.4.2")
        self.assertEqual([], unknown)
        self.assertEqual(1, len(candidates))
        self.assertEqual((self.new["node"], self.new["ip"], ["grok"]),
                         (candidates[0]["node"], candidates[0]["ip"], candidates[0]["agents"]))

    def test_maintenance_keeps_previous_fqdn_and_refreshes_only_its_ip(self):
        target = "0.4.2"
        key = Y.notice_key(self.old["node"], 7766, "grok", target)
        state = {"notices": {key: {"key": key, "node": self.old["node"], "port": 7766, "agent": "grok",
                                   "version": target, "status": "pending"}}}
        candidates, keys = Y.maintenance_candidates(state, self.nodes(), target)
        self.assertEqual({key}, keys)
        self.assertEqual([(self.old["node"], self.old["ip"], False)],
                         [(n["node"], n["ip"], n["online"]) for n in candidates])
        changed = dict(self.old, ip="100.64.0.8", ips=["100.64.0.8"], online=True)
        candidates, _ = Y.maintenance_candidates(state, [changed, self.new], target)
        self.assertEqual((changed["node"], changed["ip"]), (candidates[0]["node"], candidates[0]["ip"]))


if __name__ == "__main__":
    unittest.main()
