"""Tests for generate_tools.py, using a made-up spec. Run: python -m unittest scripts/test_generate_tools.py"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import generate_tools  # noqa: E402

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Demo", "version": "1"},
    "security": [{"apiKey": [], "jwtBearer": ["demo.read"]}],
    "paths": {
        "/query": {
            "get": {
                "operationId": "searchItems",
                "tags": ["search"],
                "summary": "Search items.",
                "description": 'Full-text "search". Use for lookups.',
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string", "minLength": 2}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
                    {"name": "sort", "in": "query", "schema": {"$ref": "#/components/schemas/Sort"}},
                    {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}},
                ],
            }
        },
        "/health": {"get": {"operationId": "healthCheck", "tags": ["health check"], "security": []}},
        "/items/{itemId}": {
            "parameters": [{"name": "itemId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "put": {
                "operationId": "updateItem",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemUpdate"}}},
                },
            },
        },
    },
    "components": {
        "securitySchemes": {
            "apiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"},
            "jwtBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        },
        "schemas": {
            "Sort": {"type": "string", "enum": ["asc", "desc"]},
            "ItemUpdate": {
                "allOf": [{"$ref": "#/components/schemas/Base"}, {"type": "object", "properties": {"note": {"type": "string"}}}]
            },
            "Base": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "id": {"type": "string", "readOnly": True}}},
        },
    },
}


def generate(tmp: Path, spec=SPEC, extra=()) -> tuple[int, Path]:
    spec_file = tmp / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp / "demo_api.py"
    code = generate_tools.main([str(spec_file), "--out", str(out), "--tags", "search", "health check",
                                "--operations", "updateItem", *extra])
    return code, out


def load(path: Path, calls: list):
    async def call_api(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"ok": True}

    fakes = {
        "api_client": types.SimpleNamespace(call_api=call_api),
        "auth": types.SimpleNamespace(get_api_key=lambda env="API_KEY": "KEY"),
        "agent_framework": types.SimpleNamespace(tool=lambda **kw: (lambda f: f)),
    }
    saved = {k: sys.modules.get(k) for k in fakes}
    sys.modules.update({k: v for k, v in fakes.items()})
    try:
        spec = importlib.util.spec_from_file_location("demo_api", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DEMO_API_BASE_URL": "http://demo.test/"})
        env.start()
        self.addCleanup(env.stop)

    def test_generates_valid_module_with_expected_shape(self):
        code, out = generate(self.tmp)
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        compile(text, str(out), "exec")
        self.assertIn('@tool(approval_mode="always_require")\nasync def update_item', text)
        self.assertNotIn("@tool(approval_mode=\"always_require\")\nasync def search_items", text)
        self.assertIn("DEMO_API_TOOLS = [", text)
        self.assertIn("from_: Annotated[str | None", text)  # reserved word renamed
        self.assertNotIn("    id: Annotated", text)  # readOnly property excluded
        self.assertIn("# needs scope: demo.read", text)

    def test_search_call_sends_key_bearer_and_filters_none(self):
        _, out = generate(self.tmp)
        calls = []
        module = load(out, calls)
        asyncio.run(module.search_items(q="abc", sort="asc"))
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("GET", "http://demo.test/query"))
        self.assertEqual(kwargs["params"], {"q": "abc", "limit": 10, "sort": "asc"})
        self.assertEqual(kwargs["extra_headers"], {"x-api-key": "KEY"})
        self.assertNotIn("auth", kwargs)  # bearer stays on (call_api default)

    def test_health_check_has_no_auth(self):
        _, out = generate(self.tmp)
        calls = []
        asyncio.run(load(out, calls).health_check())
        self.assertEqual(calls[0][2], {"auth": False})

    def test_path_value_is_encoded_and_body_flattened(self):
        _, out = generate(self.tmp)
        calls = []
        module = load(out, calls)
        asyncio.run(module.update_item(item_id="../admin?x=1", name="n"))
        _, url, kwargs = calls[0]
        self.assertTrue(url.endswith("/items/..%2Fadmin%3Fx%3D1"))
        self.assertEqual(kwargs["json"], {"name": "n"})

    def test_base_url_required(self):
        _, out = generate(self.tmp)
        module = load(out, [])
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            module._url("/x")

    def test_refuses_overwrite_and_swagger2(self):
        code, out = generate(self.tmp)
        self.assertEqual(generate(self.tmp)[0], 1)  # already exists
        self.assertEqual(generate(self.tmp, extra=["--force"])[0], 0)
        self.assertEqual(generate(self.tmp, spec={"swagger": "2.0"}, extra=["--force"])[0], 1)

    def test_requires_selection_and_known_ids(self):
        spec_file = self.tmp / "s.json"
        spec_file.write_text(json.dumps(SPEC), encoding="utf-8")
        self.assertEqual(generate_tools.main([str(spec_file)]), 1)
        self.assertEqual(generate_tools.main([str(spec_file), "--operations", "nope"]), 1)
        self.assertEqual(generate_tools.main([str(spec_file), "--list"]), 0)


if __name__ == "__main__":
    unittest.main()
