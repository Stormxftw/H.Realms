import asyncio
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


PLUGIN_DIR = Path(__file__).parents[1] / "hermes-plugin" / "dashboard"
MODULE_PATH = PLUGIN_DIR / "plugin_api.py"
PLUGIN_PREFIX = "/api/plugins/game-host-console"
HERMES_AGENT_ROOT = Path("/path/to/hermes-agent")


def load_plugin_api(module_path=MODULE_PATH):
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        return None
    spec = importlib.util.spec_from_file_location("game_host_plugin_api", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingStream(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size: int | None = -1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            raise AssertionError("upstream response was read without a bound")
        return super().read(size)


class UpstreamResponse:
    status = 200

    def __init__(self, content: bytes):
        self.stream = RecordingStream(content)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.stream.read(size)


class PluginApiTests(unittest.TestCase):
    def test_app_html_is_rebased_to_authenticated_plugin_routes(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        source = '<html lang="en"><link rel="stylesheet" href="/static/app.css"><script src="/static/app.js"></script></html>'
        result = module.build_app_html(source)

        self.assertIn('data-api-base="/api/plugins/game-host-console/proxy"', result)
        self.assertIn('data-embedded="true"', result)
        self.assertIn('/api/plugins/game-host-console/assets/app.css', result)
        self.assertIn('/api/plugins/game-host-console/assets/app.js', result)

    def test_proxy_rejects_unapproved_methods_and_paths_before_upstream(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        with mock.patch.object(module.urllib.request, "urlopen") as urlopen:
            for method, path in (
                ("DELETE", "api/control/apply"),
                ("GET", "api/control/apply"),
                ("POST", "api/status"),
                ("GET", "api/controls/extra"),
                ("GET", "../health"),
            ):
                with self.subTest(method=method, path=path):
                    with self.assertRaises(module.HTTPException) as rejected:
                        module._proxy(method, path)
                    self.assertEqual(404, rejected.exception.status_code)

        urlopen.assert_not_called()

    def test_proxy_allow_list_has_limits_for_every_supported_endpoint(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        allowed = (
            ("GET", "health"),
            ("GET", "api/status"),
            ("GET", "api/controls"),
            ("GET", "api/operations"),
            ("GET", "api/operations/op-123"),

            ("POST", "api/control/plan"),
            ("POST", "api/control/apply"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                rule = module._proxy_rule(method, path)
                self.assertGreater(rule.max_response, 0)
                self.assertLessEqual(rule.max_response, module.MAX_RESPONSE)
                if method == "POST":
                    self.assertGreater(rule.max_body, 0)
                else:
                    self.assertEqual(0, rule.max_body)

    def test_operations_query_is_validated_and_encoded_before_forwarding(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")
        upstream = UpstreamResponse(b"{}")
        with mock.patch.object(module.urllib.request, "urlopen", return_value=upstream) as urlopen:
            module._proxy(
                "GET",
                "api/operations",
                query={"limit": "1", "gameId": "minecraft", "state": "failed"},
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            "http://127.0.0.1:5057/api/operations?limit=1&gameId=minecraft&state=failed",
            request.full_url,
        )

        for query in (
            {"unknown": "x"},
            {"limit": "0"},
            {"limit": "501"},
            {"limit": "nope"},
            {"state": "not-a-state"},
            {"gameId": "../escape"},
            {"limit": ["1", "2"]},
        ):
            with self.subTest(query=query):
                with self.assertRaises(module.HTTPException) as rejected:
                    module._proxy("GET", "api/operations", query=query)
                self.assertEqual(400, rejected.exception.status_code)

    def test_diagnostics_routes_are_not_advertised_without_local_handlers(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")
        for path in ("api/diagnostics", "api/diagnostics/minecraft"):
            with self.assertRaises(module.HTTPException) as rejected:
                module._proxy_rule("GET", path)
            self.assertEqual(404, rejected.exception.status_code)

    def test_service_origin_rejects_nonlocal_or_ambiguous_overrides_before_upstream(self):
        invalid_origins = (
            "not a url",
            "http://localhost:5057",
            "http://192.168.1.25:5057",
            "http://8.8.8.8:5057",
            "https://127.0.0.1:5057",
            "http://[::1]:5057",
            "http://user:pass@127.0.0.1:5057",
            "http://127.0.0.1:5057/path",
            "http://127.0.0.1:5057?query=yes",
            "http://127.0.0.1:5057#fragment",
            "http://127.0.0.1:0",
            "http://127.0.0.1:not-a-port",
        )
        for origin in invalid_origins:
            with self.subTest(origin=origin):
                with mock.patch.dict(os.environ, {"GAME_HOST_SERVICE_URL": origin}):
                    with mock.patch("urllib.request.urlopen") as urlopen:
                        with self.assertRaisesRegex(ValueError, "GAME_HOST_SERVICE_URL"):
                            load_plugin_api()
                urlopen.assert_not_called()

    def test_service_origin_accepts_only_plain_loopback_ipv4_origin(self):
        with mock.patch.dict(
            os.environ, {"GAME_HOST_SERVICE_URL": "http://127.0.0.2:5058"}
        ):
            module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        self.assertEqual("http://127.0.0.2:5058", module.SERVICE_BASE)

    def test_proxy_coroutines_offload_blocking_upstream_work(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        class EmptyRequest:
            headers = {}

            class URL:
                query = ""

            url = URL()

            async def stream(self):
                yield b""

        sentinel = object()

        def slow_proxy(*_args, **_kwargs):
            time.sleep(0.2)
            return sentinel

        async def exercise(proxy_call):
            started = time.monotonic()
            task = asyncio.create_task(proxy_call())
            await asyncio.sleep(0.02)
            unrelated_resumed_after = time.monotonic() - started
            result = await task
            return unrelated_resumed_after, result

        calls = (
            lambda: module.proxy_get("health", EmptyRequest()),
            lambda: module.proxy_post("api/control/plan", EmptyRequest()),
        )
        with mock.patch.object(module, "_proxy", side_effect=slow_proxy):
            for proxy_call in calls:
                with self.subTest(proxy_call=proxy_call):
                    resumed_after, result = asyncio.run(exercise(proxy_call))
                    self.assertLess(resumed_after, 0.1)
                    self.assertIs(sentinel, result)

    def test_get_proxy_rejects_declared_or_streamed_body_before_upstream(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        host = FastAPI()
        host.include_router(module.router, prefix=PLUGIN_PREFIX)

        async def streamed_body():
            yield b"x"

        async def request_bodies():
            transport = ASGITransport(app=host)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                declared = await client.request(
                    "GET",
                    f"{PLUGIN_PREFIX}/proxy/health",
                    content=b"x",
                )
                streamed = await client.request(
                    "GET",
                    f"{PLUGIN_PREFIX}/proxy/health",
                    content=streamed_body(),
                )
            return declared, streamed

        with mock.patch.object(module.urllib.request, "urlopen") as urlopen:
            declared, streamed = asyncio.run(request_bodies())

        self.assertEqual(413, declared.status_code)
        self.assertEqual(413, streamed.status_code)
        urlopen.assert_not_called()

    def test_real_hermes_loader_mounts_enabled_plugin_behind_session_auth(self):
        self.assertTrue(
            (HERMES_AGENT_ROOT / "hermes_cli" / "web_server.py").is_file(),
            "real local Hermes loader is required for this integration test",
        )
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / "hermes-home"
            plugin_target = hermes_home / "plugins" / "game-host-console"
            shutil.copytree(Path(__file__).parents[1] / "hermes-plugin", plugin_target)
            (hermes_home / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - game-host-console\n",
                encoding="utf-8",
            )
            harness = r'''
import asyncio
import json
import urllib.request
from unittest import mock

from httpx import ASGITransport, AsyncClient
from hermes_cli import web_server

prefix = "/api/plugins/game-host-console"
plugins = web_server._get_dashboard_plugins()
entry = next(plugin for plugin in plugins if plugin["name"] == "game-host-console")
assert entry["source"] == "user"
assert entry["has_api"] is True
assert entry["_api_file"] == "plugin_api.py"

route_methods = {
    method
    for route in web_server.app.routes
    if getattr(route, "path", None) == f"{prefix}/proxy/{{path:path}}"
    for method in getattr(route, "methods", set())
}
assert route_methods == {"GET", "POST"}

class Upstream:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        assert 0 < size <= 16_385
        return b'{"status":"ok"}'

async def exercise():
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    transport = ASGITransport(app=web_server.app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        unauthenticated = await client.get(f"{prefix}/proxy/health")
        plugin_list = await client.get(
            "/api/dashboard/plugins",
            headers={"X-Hermes-Session-Token": "real-loader-session"},
        )
        authenticated = await client.get(
            f"{prefix}/proxy/health",
            headers={"X-Hermes-Session-Token": "real-loader-session"},
        )
    return unauthenticated, plugin_list, authenticated

with mock.patch.object(urllib.request, "urlopen", return_value=Upstream()) as urlopen:
    unauthenticated, plugin_list, authenticated = asyncio.run(exercise())

assert unauthenticated.status_code == 401
assert plugin_list.status_code == 200
visible = next(plugin for plugin in plugin_list.json() if plugin["name"] == "game-host-console")
assert visible["has_api"] is True
assert authenticated.status_code == 200
assert authenticated.json() == {"status": "ok"}
urlopen.assert_called_once()
request = urlopen.call_args.args[0]
assert request.full_url == "http://127.0.0.1:5057/health"
print("REAL_LOADER_RESULT=" + json.dumps({
    "has_api": visible["has_api"],
    "route_methods": sorted(route_methods),
    "unauthenticated": unauthenticated.status_code,
    "authenticated": authenticated.status_code,
}))
'''
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONPATH": str(HERMES_AGENT_ROOT),
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_DASHBOARD_SESSION_TOKEN": "real-loader-session",
                    "GAME_HOST_SERVICE_URL": "http://127.0.0.1:5057",
                }
            )
            for name in ("HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV"):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, "-c", harness],
                cwd=Path(__file__).parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        evidence = next(
            line.removeprefix("REAL_LOADER_RESULT=")
            for line in result.stdout.splitlines()
            if line.startswith("REAL_LOADER_RESULT=")
        )
        self.assertEqual(
            {
                "has_api": True,
                "route_methods": ["GET", "POST"],
                "unauthenticated": 401,
                "authenticated": 200,
            },
            json.loads(evidence),
        )

    def test_success_response_limit_uses_a_bounded_upstream_read(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        rule = module._proxy_rule("GET", "health")
        upstream = UpstreamResponse(b"x" * (rule.max_response + 1))
        with mock.patch.object(module.urllib.request, "urlopen", return_value=upstream):
            with self.assertRaises(module.HTTPException) as rejected:
                module._proxy("GET", "health")

        self.assertEqual(502, rejected.exception.status_code)
        self.assertEqual([rule.max_response + 1], upstream.stream.read_sizes)

    def test_error_response_limit_uses_the_endpoint_bound(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        rule = module._proxy_rule("GET", "health")
        stream = RecordingStream(b"x" * (rule.max_response + 1))
        upstream_error = urllib.error.HTTPError(
            "http://127.0.0.1:5057/health",
            500,
            "upstream error",
            Message(),
            stream,
        )
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=upstream_error):
            with self.assertRaises(module.HTTPException) as rejected:
                module._proxy("GET", "health")

        self.assertEqual(502, rejected.exception.status_code)
        self.assertEqual([rule.max_response + 1], stream.read_sizes)

    def test_urlerror_is_opaque_and_never_leaks_local_details(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        upstream_error = urllib.error.URLError(
            OSError("/home/operator/.hermes/secret-socket: connection refused")
        )
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=upstream_error):
            with self.assertRaises(module.HTTPException) as rejected:
                module._proxy("GET", "health")

        self.assertEqual(503, rejected.exception.status_code)
        self.assertEqual("Local Game Host Console is unavailable.", rejected.exception.detail)
        self.assertNotIn("secret", str(rejected.exception.detail))
        self.assertNotIn("connection refused", str(rejected.exception.detail))

    def test_proxy_post_rejects_declared_oversize_before_reading_body(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        class OversizeRequest:
            headers = {"content-length": str(module.MAX_BODY + 1)}
            body_called = False

            async def body(self):
                self.body_called = True
                return b"x" * (module.MAX_BODY + 1)

        request = OversizeRequest()
        with self.assertRaises(module.HTTPException) as rejected:
            asyncio.run(module.proxy_post("api/control/plan", request))

        self.assertEqual(413, rejected.exception.status_code)
        self.assertFalse(request.body_called)

    def test_proxy_post_streams_bounded_body_without_request_body_buffer(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        class StreamingRequest:
            headers = {}

            async def body(self):
                raise AssertionError("request.body() would buffer without a bound")

            async def stream(self):
                yield b'{"gameId":"mine'
                yield b'craft"}'

        sentinel = object()
        with mock.patch.object(module, "_proxy", return_value=sentinel) as proxy:
            result = asyncio.run(module.proxy_post("api/control/plan", StreamingRequest()))

        self.assertIs(sentinel, result)
        proxy.assert_called_once_with("POST", "api/control/plan", b'{"gameId":"minecraft"}')

    def test_proxy_post_rejects_stream_when_accumulated_body_exceeds_limit(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        class StreamingOversizeRequest:
            headers = {}

            async def stream(self):
                yield b"x" * module.MAX_BODY
                yield b"x"

        with self.assertRaises(module.HTTPException) as rejected:
            asyncio.run(module.proxy_post("api/control/plan", StreamingOversizeRequest()))

        self.assertEqual(413, rejected.exception.status_code)

    def test_proxy_post_rejects_path_before_reading_body(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        class UnapprovedRequest:
            headers = {}
            body_called = False

            async def body(self):
                self.body_called = True
                return b"{}"

        request = UnapprovedRequest()
        with self.assertRaises(module.HTTPException) as rejected:
            asyncio.run(module.proxy_post("api/status", request))

        self.assertEqual(404, rejected.exception.status_code)
        self.assertFalse(request.body_called)

    def test_proxy_rejects_oversize_direct_body_before_upstream(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        with mock.patch.object(module.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(module.HTTPException) as rejected:
                module._proxy("POST", "api/control/plan", b"x" * (module.MAX_BODY + 1))

        self.assertEqual(413, rejected.exception.status_code)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
