import asyncio
import importlib.util
import io
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "hermes-plugin" / "dashboard" / "plugin_api.py"


def load_plugin_api():
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        return None
    spec = importlib.util.spec_from_file_location("game_host_plugin_api", MODULE_PATH)
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
            ("GET", "api/diagnostics/minecraft"),
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

    def test_authenticated_proxy_routes_register_only_get_and_post(self):
        module = load_plugin_api()
        if module is None:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")

        proxy_methods = {
            method
            for route in module.router.routes
            if route.path == "/proxy/{path:path}"
            for method in route.methods
        }

        self.assertEqual({"GET", "POST"}, proxy_methods)

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
