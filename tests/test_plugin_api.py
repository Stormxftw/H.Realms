import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "hermes-plugin" / "dashboard" / "plugin_api.py"


class PluginApiTests(unittest.TestCase):
    def test_app_html_is_rebased_to_authenticated_plugin_routes(self):
        try:
            import fastapi  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("plugin API runs in the Hermes venv, which provides FastAPI")
        spec = importlib.util.spec_from_file_location("game_host_plugin_api", MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = '<html lang="en"><link rel="stylesheet" href="/static/app.css"><script src="/static/app.js"></script></html>'
        result = module.build_app_html(source)

        self.assertIn('data-api-base="/api/plugins/game-host-console/proxy"', result)
        self.assertIn('data-embedded="true"', result)
        self.assertIn('/api/plugins/game-host-console/assets/app.css', result)
        self.assertIn('/api/plugins/game-host-console/assets/app.js', result)


if __name__ == "__main__":
    unittest.main()
