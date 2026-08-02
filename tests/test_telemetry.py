import json
import socket
import tempfile
import unittest
from pathlib import Path

import telemetry


class FakeTcpSocket:
    def __init__(self, payload=None, error=None):
        self.payload = bytearray(payload or b"")
        self.error = error
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        if self.error:
            raise self.error
        if not self.payload:
            return b""
        data = bytes(self.payload[:count])
        del self.payload[:count]
        return data


class FakeUdpSocket:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.timeout = None
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, _count):
        if self.error:
            raise self.error
        return self.payload, ("127.0.0.1", 27015)

    def close(self):
        self.closed = True


def minecraft_packet(document):
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return telemetry.encode_varint(0) + telemetry.encode_varint(len(raw)) + raw


def a2s_packet():
    strings = b"Alpha\x00arena\x00alpha\x00Alpha Game\x00"
    return b"\xff\xff\xff\xffI" + bytes([17]) + strings + b"\x34\x12" + bytes([3, 16])


class TelemetryTests(unittest.TestCase):
    def test_find_process_pid_reads_proc_cmdline_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp)
            wanted = proc_root / "123"
            unrelated = proc_root / "456"
            wanted.mkdir()
            unrelated.mkdir()
            (wanted / "cmdline").write_bytes(
                b"/srv/Pal/Binaries/Linux/PalServer-Linux-Shipping\x00Pal\x00-port=8211\x00"
            )
            (unrelated / "cmdline").write_bytes(b"python3\x00app.py\x00")

            pid = telemetry.find_process_pid(
                "PalServer-Linux-Shipping", proc_root=proc_root
            )

            self.assertEqual(123, pid)

    def test_listener_probe_uses_declared_transport_and_keeps_errors(self):
        calls = []

        def runner(command, timeout=3):
            calls.append((command, timeout))
            if "-ltn" in command:
                return {
                    "ok": True,
                    "stdout": "LISTEN 0 128 0.0.0.0:7777 0.0.0.0:*",
                    "stderr": "",
                }
            return {"ok": False, "stdout": "", "stderr": "ss unavailable"}

        tcp = telemetry.probe_listener(7777, "tcp", runner=runner)
        udp = telemetry.probe_listener(8211, "udp", runner=runner)

        self.assertEqual([["ss", "-ltn"], ["ss", "-lun"]], [call[0] for call in calls])
        self.assertTrue(tcp["ok"])
        self.assertTrue(tcp["listening"])
        self.assertFalse(udp["ok"])
        self.assertIsNone(udp["listening"])
        self.assertEqual("ss unavailable", udp["error"])

    def test_shared_listener_probe_captures_one_snapshot_per_transport(self):
        calls = []

        def runner(command, timeout=3):
            calls.append(command)
            if "-ltn" in command:
                return {
                    "ok": True,
                    "stdout": "LISTEN 0 128 0.0.0.0:7777 0.0.0.0:*\n"
                    "LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*",
                    "stderr": "",
                }
            return {"ok": True, "stdout": "UNCONN 0 0 0.0.0.0:8211 0.0.0.0:*", "stderr": ""}

        probe = telemetry.shared_listener_probe(runner=runner)

        tcp_a = probe(7777, "tcp")
        tcp_b = probe(8080, "tcp")
        tcp_missing = probe(9999, "tcp")
        udp = probe(8211, "udp")

        self.assertEqual([["ss", "-ltn"], ["ss", "-lun"]], calls)
        self.assertTrue(tcp_a["listening"])
        self.assertTrue(tcp_b["listening"])
        self.assertFalse(tcp_missing["listening"])
        self.assertTrue(udp["listening"])
        self.assertEqual("tcp", tcp_b["protocol"])
        self.assertEqual("udp", udp["protocol"])

    def test_shared_listener_probe_keeps_error_boundary_shapes(self):
        calls = []

        def runner(command, timeout=3):
            calls.append(command)
            return {"ok": False, "stdout": "", "stderr": "permission denied"}

        probe = telemetry.shared_listener_probe(runner=runner)

        first = probe(25565, "tcp")
        second = probe(25566, "tcp")
        unsupported = probe(1, "sctp")

        self.assertEqual([["ss", "-ltn"]], calls)
        self.assertFalse(first["ok"])
        self.assertIsNone(first["listening"])
        self.assertEqual("permission denied", first["error"])
        self.assertFalse(second["ok"])
        self.assertFalse(unsupported["ok"])
        self.assertIn("unsupported", unsupported["error"])

    def test_minecraft_probe_parses_success_timeout_and_malformed_packets(self):
        document = {
            "description": {"text": "Alpha"},
            "version": {"name": "1.21", "protocol": 767},
            "players": {"online": 2, "max": 10, "sample": [{"name": "Alex"}]},
        }
        payload = minecraft_packet(document)
        framed = telemetry.encode_varint(len(payload)) + payload

        success = telemetry.probe_minecraft(
            port=25565,
            connector=lambda *_args, **_kwargs: FakeTcpSocket(framed),
        )
        timeout = telemetry.probe_minecraft(
            port=25565,
            connector=lambda *_args, **_kwargs: FakeTcpSocket(
                error=socket.timeout("timed out")
            ),
        )
        malformed = telemetry.probe_minecraft(
            port=25565,
            connector=lambda *_args, **_kwargs: FakeTcpSocket(b"\x02\x01x"),
        )

        self.assertTrue(success["ok"])
        self.assertEqual("1.21", success["data"]["version"]["name"])
        self.assertEqual("timeout", timeout["errorCode"])
        self.assertFalse(timeout["ok"])
        self.assertEqual("malformed_response", malformed["errorCode"])
        self.assertFalse(malformed["ok"])

    def test_a2s_probe_parses_success_timeout_and_malformed_packets(self):
        success_socket = FakeUdpSocket(a2s_packet())
        success = telemetry.probe_a2s(
            port=27015, socket_factory=lambda *_args: success_socket
        )
        timeout = telemetry.probe_a2s(
            port=27015,
            socket_factory=lambda *_args: FakeUdpSocket(
                error=socket.timeout("timed out")
            ),
        )
        malformed = telemetry.probe_a2s(
            port=27015,
            socket_factory=lambda *_args: FakeUdpSocket(b"not-a2s"),
        )

        self.assertTrue(success["ok"])
        self.assertEqual("Alpha", success["data"]["name"])
        self.assertEqual(3, success["data"]["players"])
        self.assertTrue(success_socket.closed)
        self.assertEqual("timeout", timeout["errorCode"])
        self.assertEqual("malformed_response", malformed["errorCode"])

    def test_aggregate_state_never_turns_probe_failure_into_stopped(self):
        ready = {"readiness": "ready", "projectPresent": True}
        listener_ok = [{"ok": True, "listening": True, "error": None}]
        query_ok = {"attempted": True, "ok": True, "error": None}

        self.assertEqual(
            "running_ready",
            telemetry.aggregate_state(
                ready,
                {"ok": True, "running": True},
                listener_ok,
                query_ok,
            ),
        )
        self.assertEqual(
            "running_degraded",
            telemetry.aggregate_state(
                ready,
                {"ok": True, "running": True},
                [{"ok": False, "listening": None, "error": "ss failed"}],
                query_ok,
            ),
        )
        self.assertEqual(
            "stopped",
            telemetry.aggregate_state(
                ready,
                {"ok": True, "running": False},
                [],
                {"attempted": False, "ok": None, "error": None},
            ),
        )
        self.assertEqual(
            "unknown",
            telemetry.aggregate_state(
                ready,
                {"ok": False, "running": None, "error": "proc denied"},
                listener_ok,
                query_ok,
            ),
        )
        self.assertEqual(
            "not_installed",
            telemetry.aggregate_state(
                {"readiness": "needs_setup", "projectPresent": False},
                {"ok": True, "running": False},
                [],
                {"attempted": False, "ok": None, "error": None},
            ),
        )

    def test_collection_respects_query_and_additional_ports(self):
        listener_calls = []
        query_calls = []

        def listener(port, protocol):
            listener_calls.append((port, protocol))
            return {
                "port": port,
                "protocol": protocol,
                "ok": True,
                "listening": True,
                "error": None,
            }

        def query(**kwargs):
            query_calls.append(kwargs)
            return {"attempted": True, "ok": True, "error": None, "data": {}}

        result = telemetry.collect_game(
            game_id="alpha",
            name="Alpha",
            adapter={
                "statusCollector": "steam_query",
                "processSearch": "alpha-server",
                "defaultPort": 2456,
                "portProtocol": "udp",
                "queryPort": 2457,
                "additionalPorts": [
                    {"name": "admin", "port": 8080, "protocol": "tcp"}
                ],
            },
            readiness={"readiness": "ready", "projectPresent": True},
            process_probe=lambda _needle: {
                "ok": True,
                "running": True,
                "pid": 42,
                "error": None,
            },
            listener_probe=listener,
            a2s_probe=query,
            lan_address="192.168.50.10",
        )

        self.assertEqual([(2456, "udp"), (8080, "tcp")], listener_calls)
        self.assertEqual(2457, query_calls[0]["port"])
        self.assertEqual("running_ready", result["state"])
        self.assertEqual("192.168.50.10:2456", result["connect"]["lan"])
        self.assertIsNone(result["connect"]["public"])


    def test_aggregate_state_running_beats_setup_readiness(self):
        needs_setup = {"readiness": "needs_setup", "projectPresent": True}
        listener_ok = [{"ok": True, "listening": True, "error": None}]
        query_unused = {"attempted": False, "ok": None, "error": None}

        self.assertEqual(
            "running_ready",
            telemetry.aggregate_state(
                needs_setup,
                {"ok": True, "running": True},
                listener_ok,
                query_unused,
            ),
        )

    def test_players_extraction_across_supported_documents(self):
        minecraft = {
            "data": {"players": {"online": 2, "max": 10, "sample": [{"name": "Alex"}]}}
        }
        a2s = {
            "data": {"players": 3, "maxPlayers": 16, "name": "Alpha", "map": "arena"}
        }
        palworld = {"data": {"players": 4, "server_max_players": 32, "version": "v0.4"}}
        empty = {"data": {}, "attempted": True}

        self.assertEqual({"online": 2, "max": 10}, telemetry._players_from_query(minecraft))
        self.assertEqual({"online": 3, "max": 16}, telemetry._players_from_query(a2s))
        self.assertEqual({"online": 4, "max": 32}, telemetry._players_from_query(palworld))
        self.assertIsNone(telemetry._players_from_query(empty))
        self.assertIsNone(telemetry._players_from_query({"data": None}))

    def test_probe_palworld_rest_parses_info_and_fails_soft(self):
        import io

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        ok = FakeResponse(b'{"players": 7, "server_max_players": 32}')
        success = telemetry.probe_palworld_rest(port=8212, opener=lambda *_a, **_k: ok)
        self.assertTrue(success["ok"])
        self.assertEqual(7, success["data"]["players"])

        timeout = telemetry.probe_palworld_rest(
            port=8212, opener=lambda *_a, **_k: (_ for _ in ()).throw(socket.timeout("t"))
        )
        self.assertEqual("timeout", timeout["errorCode"])

        refused = telemetry.probe_palworld_rest(
            port=8212, opener=lambda *_a, **_k: (_ for _ in ()).throw(OSError("refused"))
        )
        self.assertEqual("network_error", refused["errorCode"])

    def test_collect_palworld_rest_never_degrades_healthy_server_on_optional_query(self):
        def listener(port, protocol):
            return {
                "port": port,
                "protocol": protocol,
                "ok": True,
                "listening": True,
                "error": None,
            }

        result_no_rest = telemetry.collect_game(
            game_id="palworld",
            name="Palworld",
            adapter={
                "statusCollector": "palworld_rest",
                "processSearch": "PalServer-Linux-Shipping",
                "defaultPort": 8211,
                "portProtocol": "udp",
                "restPort": 8212,
                "additionalPorts": [{"name": "query", "port": 27015, "protocol": "udp"}],
            },
            readiness={"readiness": "needs_setup", "projectPresent": True},
            process_probe=lambda _n: {"ok": True, "running": True, "pid": 42, "error": None},
            listener_probe=listener,
            palworld_rest_probe=lambda **_kwargs: {
                "attempted": True,
                "ok": False,
                "errorCode": "network_error",
                "error": "REST disabled",
                "data": None,
            },
            lan_address="192.168.50.10",
        )
        self.assertEqual("running_ready", result_no_rest["state"])
        self.assertTrue(result_no_rest["online"])
        self.assertIsNone(result_no_rest["players"])

        result_with_rest = telemetry.collect_game(
            game_id="palworld",
            name="Palworld",
            adapter={
                "statusCollector": "palworld_rest",
                "processSearch": "PalServer-Linux-Shipping",
                "defaultPort": 8211,
                "portProtocol": "udp",
                "restPort": 8212,
                "additionalPorts": [{"name": "query", "port": 27015, "protocol": "udp"}],
            },
            readiness={"readiness": "needs_setup", "projectPresent": True},
            process_probe=lambda _n: {"ok": True, "running": True, "pid": 42, "error": None},
            listener_probe=listener,
            palworld_rest_probe=lambda **_kwargs: {
                "attempted": True,
                "ok": True,
                "errorCode": None,
                "error": None,
                "data": {"players": 7, "server_max_players": 32},
            },
        )
        self.assertEqual("running_ready", result_with_rest["state"])
        self.assertEqual({"online": 7, "max": 32}, result_with_rest["players"])


if __name__ == "__main__":
    unittest.main()
