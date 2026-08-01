from __future__ import annotations

import json
import socket
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable


def encode_varint(value: int) -> bytes:
    """Encode a non-negative Minecraft protocol VarInt."""
    if value < 0:
        raise ValueError("VarInt must be non-negative")
    output = bytearray()
    while True:
        current = value & 0x7F
        value >>= 7
        output.append(current | (0x80 if value else 0))
        if not value:
            return bytes(output)


def _read_varint(sock: Any) -> int:
    value = 0
    for shift in range(0, 35, 7):
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("socket closed while reading VarInt")
        current = chunk[0]
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value
    raise ValueError("VarInt exceeds five bytes")


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise ValueError("truncated VarInt")
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
    raise ValueError("VarInt exceeds five bytes")


def _recv_exact(sock: Any, length: int) -> bytes:
    if length < 0 or length > 1_048_576:
        raise ValueError("invalid response length")
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise EOFError("socket closed before the response was complete")
        chunks.extend(chunk)
    return bytes(chunks)


def find_process_pid(needle: str, *, proc_root: Path = Path("/proc")) -> int | None:
    """Find the lowest matching PID by reading procfs directly, without a shell."""
    if not needle:
        return None
    candidates = sorted(
        (entry for entry in Path(proc_root).iterdir() if entry.name.isdigit()),
        key=lambda entry: int(entry.name),
    )
    for entry in candidates:
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            # Processes can disappear or become unreadable between enumeration and read.
            continue
        if needle in command:
            return int(entry.name)
    return None


def probe_process(
    needle: str, *, proc_root: Path = Path("/proc")
) -> dict[str, Any]:
    try:
        pid = find_process_pid(needle, proc_root=proc_root)
    except OSError as exc:
        return {
            "ok": False,
            "running": None,
            "pid": None,
            "error": str(exc),
        }
    return {
        "ok": True,
        "running": pid is not None,
        "pid": pid,
        "error": None,
    }


def _run(command: list[str], timeout: int = 3) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def probe_listener(
    port: int,
    protocol: str,
    *,
    runner: Callable[..., dict[str, Any]] = _run,
) -> dict[str, Any]:
    """Probe a declared listener using the transport-specific ss view."""
    normalized_protocol = str(protocol).lower()
    if normalized_protocol not in {"tcp", "udp"}:
        return {
            "port": port,
            "protocol": normalized_protocol,
            "ok": False,
            "listening": None,
            "error": f"unsupported listener protocol: {protocol}",
        }
    command = ["ss", "-ltn" if normalized_protocol == "tcp" else "-lun"]
    try:
        result = runner(command, timeout=3)
    except Exception as exc:  # runner is an injected system boundary
        result = {"ok": False, "stdout": "", "stderr": str(exc)}
    return _listener_result_from_snapshot(result, port, normalized_protocol)


def shared_listener_probe(
    *,
    runner: Callable[..., dict[str, Any]] = _run,
) -> Callable[[int, str], dict[str, Any]]:
    """Return a listener probe that reuses one tcp and one udp ss snapshot.

    Meant to be created once per status collection and shared across all
    games, so listener checks spawn at most two subprocesses (one ``ss -ltn``
    and one ``ss -lun``) instead of one per declared port.
    """
    snapshots: dict[str, dict[str, Any]] = {}

    def probe(port: int, protocol: str) -> dict[str, Any]:
        normalized_protocol = str(protocol).lower()
        if normalized_protocol not in {"tcp", "udp"}:
            return {
                "port": port,
                "protocol": normalized_protocol,
                "ok": False,
                "listening": None,
                "error": f"unsupported listener protocol: {protocol}",
            }
        if normalized_protocol not in snapshots:
            command = ["ss", "-ltn" if normalized_protocol == "tcp" else "-lun"]
            try:
                snapshots[normalized_protocol] = runner(command, timeout=3)
            except Exception as exc:  # runner is an injected system boundary
                snapshots[normalized_protocol] = {
                    "ok": False,
                    "stdout": "",
                    "stderr": str(exc),
                }
        return _listener_result_from_snapshot(
            snapshots[normalized_protocol], port, normalized_protocol
        )

    return probe


def _listener_result_from_snapshot(
    result: dict[str, Any], port: int, protocol: str
) -> dict[str, Any]:
    if not result.get("ok"):
        return {
            "port": port,
            "protocol": protocol,
            "ok": False,
            "listening": None,
            "error": str(result.get("stderr") or "listener probe failed"),
        }

    listening = False
    for line in str(result.get("stdout", "")).splitlines():
        fields = line.split()
        if any(
            field.rsplit(":", 1)[-1] == str(port)
            for field in fields
            if ":" in field
        ):
            listening = True
            break
    return {
        "port": port,
        "protocol": protocol,
        "ok": True,
        "listening": listening,
        "error": None,
    }


def _minecraft_packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def probe_minecraft(
    *,
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 3.0,
    connector: Callable[..., Any] = socket.create_connection,
) -> dict[str, Any]:
    """Run one deterministic Minecraft server-list status query."""
    try:
        encoded_host = host.encode("utf-8")
        handshake = (
            encode_varint(0)
            + encode_varint(len(encoded_host))
            + encoded_host
            + struct.pack(">H", port)
            + encode_varint(1)
        )
        with connector((host, port), timeout=timeout) as connection:
            connection.sendall(_minecraft_packet(0, handshake))
            connection.sendall(_minecraft_packet(0))
            frame_length = _read_varint(connection)
            frame = _recv_exact(connection, frame_length)
        packet_id, offset = _decode_varint(frame)
        if packet_id != 0:
            raise ValueError("unexpected Minecraft response packet")
        document_length, offset = _decode_varint(frame, offset)
        if document_length != len(frame) - offset:
            raise ValueError("Minecraft JSON length does not match frame")
        document = json.loads(frame[offset:].decode("utf-8", errors="strict"))
        if not isinstance(document, dict):
            raise ValueError("Minecraft status document is not an object")
        return {
            "attempted": True,
            "ok": True,
            "errorCode": None,
            "error": None,
            "data": document,
        }
    except (socket.timeout, TimeoutError) as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "timeout",
            "error": str(exc) or "query timed out",
            "data": None,
        }
    except (EOFError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "malformed_response",
            "error": str(exc),
            "data": None,
        }
    except OSError as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "network_error",
            "error": str(exc),
            "data": None,
        }


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise ValueError("unterminated A2S string")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _parse_a2s_info(data: bytes) -> dict[str, Any]:
    if len(data) < 6 or data[:5] != b"\xff\xff\xff\xffI":
        raise ValueError("invalid A2S_INFO header")
    offset = 5
    protocol = data[offset]
    offset += 1
    name, offset = _read_cstring(data, offset)
    map_name, offset = _read_cstring(data, offset)
    folder, offset = _read_cstring(data, offset)
    game, offset = _read_cstring(data, offset)
    if len(data) - offset < 4:
        raise ValueError("truncated A2S_INFO player fields")
    app_id = int.from_bytes(data[offset : offset + 2], "little")
    offset += 2
    players = data[offset]
    max_players = data[offset + 1]
    return {
        "protocol": protocol,
        "name": name,
        "map": map_name,
        "folder": folder,
        "game": game,
        "appId": app_id,
        "players": players,
        "maxPlayers": max_players,
    }


def probe_a2s(
    *,
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 3.0,
    socket_factory: Callable[..., Any] = socket.socket,
) -> dict[str, Any]:
    """Run one deterministic, challenge-free Steam A2S_INFO query."""
    udp_socket = None
    try:
        udp_socket = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.settimeout(timeout)
        udp_socket.sendto(
            b"\xff\xff\xff\xffTSource Engine Query\x00", (host, port)
        )
        data, _address = udp_socket.recvfrom(1400)
        document = _parse_a2s_info(data)
        return {
            "attempted": True,
            "ok": True,
            "errorCode": None,
            "error": None,
            "data": document,
        }
    except (socket.timeout, TimeoutError) as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "timeout",
            "error": str(exc) or "query timed out",
            "data": None,
        }
    except (TypeError, ValueError, UnicodeError) as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "malformed_response",
            "error": str(exc),
            "data": None,
        }
    except OSError as exc:
        return {
            "attempted": True,
            "ok": False,
            "errorCode": "network_error",
            "error": str(exc),
            "data": None,
        }
    finally:
        if udp_socket is not None:
            udp_socket.close()


def aggregate_state(
    readiness: dict[str, Any],
    process: dict[str, Any],
    listeners: list[dict[str, Any]],
    query: dict[str, Any],
) -> str:
    """Combine independent evidence without converting probe errors into stopped."""
    if readiness.get("projectPresent") is False:
        return "not_installed"
    if not process.get("ok") or process.get("running") is None:
        return "unknown"
    if process.get("running") is False:
        return "stopped"

    degraded = readiness.get("readiness") != "ready"
    degraded = degraded or any(
        not listener.get("ok") or listener.get("listening") is not True
        for listener in listeners
    )
    degraded = degraded or (
        query.get("attempted") is True and query.get("ok") is not True
    )
    return "running_degraded" if degraded else "running_ready"


def _endpoint(host: str | None, port: int) -> str | None:
    if not host:
        return None
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{rendered_host}:{port}"


def collect_game(
    *,
    game_id: str,
    name: str,
    adapter: dict[str, Any],
    readiness: dict[str, Any],
    process_probe: Callable[[str], dict[str, Any]] = probe_process,
    listener_probe: Callable[[int, str], dict[str, Any]] = probe_listener,
    minecraft_probe: Callable[..., dict[str, Any]] = probe_minecraft,
    a2s_probe: Callable[..., dict[str, Any]] = probe_a2s,
    lan_address: str | None = None,
) -> dict[str, Any]:
    """Collect independent process, listener, and protocol-query evidence."""
    process = process_probe(str(adapter.get("processSearch", "")))
    default_port = int(adapter["defaultPort"])
    protocol = str(adapter["portProtocol"])
    declared_ports = [
        {"name": "game", "port": default_port, "protocol": protocol},
        *adapter.get("additionalPorts", []),
    ]
    listeners = [
        {
            **dict(port_spec),
            **listener_probe(int(port_spec["port"]), str(port_spec["protocol"])),
        }
        for port_spec in declared_ports
    ]

    collector = str(adapter.get("statusCollector", "process_only"))
    query_port = int(adapter.get("queryPort", default_port))
    query: dict[str, Any] = {
        "attempted": False,
        "ok": None,
        "errorCode": None,
        "error": None,
        "data": None,
    }
    if process.get("ok") and process.get("running"):
        if collector == "minecraft_ping":
            query = minecraft_probe(port=query_port, host="127.0.0.1")
        elif collector == "steam_query":
            query = a2s_probe(port=query_port, host="127.0.0.1")

    state = aggregate_state(readiness, process, listeners, query)
    return {
        "id": game_id,
        "name": name,
        "state": state,
        "online": state in {"running_ready", "running_degraded"},
        "process": process,
        "listeners": listeners,
        "query": {"collector": collector, "port": query_port, **query},
        "connect": {
            "local": _endpoint("127.0.0.1", default_port),
            "lan": _endpoint(lan_address, default_port),
            # Public endpoints require explicit trusted configuration; never discover
            # or invent one in this local status collector.
            "public": None,
        },
    }
