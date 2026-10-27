"""Generate Agent Framework tool functions from an OpenAPI 3.x spec (JSON).

Typical use (run from the repo root; the spec stays on your machine):

    python scripts/generate_tools.py path/to/spec.json --list
    python scripts/generate_tools.py path/to/spec.json --tags search --out search_api

The generated module is plain code: review it, trim the descriptions, then
register its TOOLS list in tools/__init__.py. Standard library only.
"""

import argparse
import json
import keyword
import re
import sys
from pathlib import Path

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
WRITE_METHODS = {"post", "put", "patch", "delete"}
SKIPPED_HEADERS = {"authorization", "content-type", "accept"}
TOOLS_DIR = Path(__file__).resolve().parent.parent / "src" / "agent-framework-agent-basic-responses" / "tools"
MAX_DESCRIPTION = 1000


class SpecError(Exception):
    pass


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- spec helpers


class Spec:
    def __init__(self, doc: dict):
        version = str(doc.get("openapi", ""))
        if not version.startswith("3."):
            raise SpecError("Only OpenAPI 3.x is supported (found %r). Swagger 2.0 must be converted first." % version)
        self.doc = doc

    def resolve(self, node):
        """Follow a chain of local $refs."""
        seen = []
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise SpecError(f"Only local $ref values are supported: {ref}")
            if ref in seen:
                raise SpecError(f"Circular $ref: {ref}")
            seen.append(ref)
            target = self.doc
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            node = target
        return node

    def schema(self, node, depth: int = 0) -> dict:
        """Resolve a schema and merge allOf members into one flat schema."""
        node = self.resolve(node) if isinstance(node, dict) else {}
        if "allOf" not in node or depth > 5:
            return node
        merged = {k: v for k, v in node.items() if k != "allOf"}
        properties, required = dict(merged.get("properties", {})), list(merged.get("required", []))
        for part in node["allOf"]:
            part = self.schema(part, depth + 1)
            properties.update(part.get("properties", {}))
            required += part.get("required", [])
            for key in ("type", "description"):
                merged.setdefault(key, part.get(key))
        merged["properties"], merged["required"] = properties, required
        return merged


def py_identifier(name: str, taken: set[str]) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    text = re.sub(r"\W+", "_", text).strip("_").lower() or "value"
    if text[0].isdigit():
        text = "_" + text
    if keyword.iskeyword(text):
        text += "_"
    candidate, n = text, 2
    while candidate in taken:
        candidate, n = f"{text}_{n}", n + 1
    taken.add(candidate)
    return candidate


def short_text(*parts) -> str:
    text = " ".join(" ".join(str(p).split()) for p in parts if p)
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 3] + "..."


def py_type(spec: Spec, node, depth: int = 0) -> tuple[str, dict, str]:
    """Return (python type, Field constraints, extra description hint) for a schema."""
    schema = spec.schema(node)
    kind, hints, kwargs = schema.get("type"), [], {}
    if schema.get("format"):
        hints.append(f"Format: {schema['format']}.")
    example = schema.get("example")
    if isinstance(example, (str, int, float, bool)):
        hints.append(f"Example: {example!r}.")
    enum = schema.get("enum")
    if enum and len(enum) <= 50 and all(isinstance(v, (str, int, bool)) for v in enum):
        return "Literal[%s]" % ", ".join(repr(v) for v in enum), kwargs, " ".join(hints)
    if kind == "string":
        result = "str"
        if "minLength" in schema:
            kwargs["min_length"] = schema["minLength"]
        if "maxLength" in schema:
            kwargs["max_length"] = schema["maxLength"]
        pattern = schema.get("pattern")
        if pattern:
            try:
                re.compile(pattern)
                kwargs["pattern"] = pattern
            except re.error:
                warn(f"ignoring pattern that Python cannot compile: {pattern!r}")
    elif kind in ("integer", "number"):
        result = "int" if kind == "integer" else "float"
        if "minimum" in schema:
            kwargs["gt" if schema.get("exclusiveMinimum") is True else "ge"] = schema["minimum"]
        if "maximum" in schema:
            kwargs["lt" if schema.get("exclusiveMaximum") is True else "le"] = schema["maximum"]
    elif kind == "boolean":
        result = "bool"
    elif kind == "array":
        inner = py_type(spec, schema.get("items", {}), depth + 1)[0] if depth < 3 else "Any"
        result = f"list[{inner}]"
        if "minItems" in schema:
            kwargs["min_length"] = schema["minItems"]
        if "maxItems" in schema:
            kwargs["max_length"] = schema["maxItems"]
    elif kind == "object" or "properties" in schema:
        result = "dict[str, Any]"
    else:
        result = "Any"
    return result, kwargs, " ".join(hints)


# --------------------------------------------------------------------------- operations


class Param:
    def __init__(self, name, original, where, type_, kwargs, description, required, default):
        self.name, self.original, self.where = name, original, where
        self.type, self.kwargs, self.description = type_, kwargs, description
        self.required, self.default = required, default


class Operation:
    def __init__(self, spec, path, method, item):
        self.spec, self.path, self.method = spec, path, method
        node = item[method]
        self.node = node
        self.id = node.get("operationId") or f"{method}_{path}"
        self.tags = node.get("tags", [])
        self.summary = node.get("summary", "")
        self.description = node.get("description", "")
        self.shared = item.get("parameters", [])
        self.security = node.get("security", spec.doc.get("security", []))

    @property
    def is_write(self) -> bool:
        return self.method in WRITE_METHODS

    def auth_plan(self) -> tuple[bool, list[tuple[str, str]], list[str]]:
        """Return (needs bearer token, [(api key location, name)], scopes)."""
        requirements = [r for r in self.security if r]
        if not requirements:
            return False, [], []
        if len(requirements) > 1:
            warn(f"{self.id}: several alternative security requirements; using the first one.")
        bearer, keys, scopes = False, [], []
        schemes = self.spec.doc.get("components", {}).get("securitySchemes", {})
        for scheme_name, scheme_scopes in requirements[0].items():
            scheme = self.spec.resolve(schemes.get(scheme_name, {}))
            kind = scheme.get("type")
            if kind == "http" and str(scheme.get("scheme", "")).lower() == "bearer" or kind in ("oauth2", "openIdConnect"):
                bearer = True
                scopes += scheme_scopes
            elif kind == "apiKey" and scheme.get("in") in ("header", "query"):
                keys.append((scheme["in"], scheme["name"]))
            else:
                warn(f"{self.id}: security scheme {scheme_name!r} is not supported and will not be sent.")
        return bearer, keys, scopes

    def parameters(self, taken: set[str]) -> list[Param]:
        spec, found = self.spec, {}
        for raw in [*self.shared, *self.node.get("parameters", [])]:
            raw = spec.resolve(raw)
            found[(raw.get("name"), raw.get("in"))] = raw
        params = []
        for (name, where), raw in found.items():
            if where == "cookie" or (where == "header" and name.lower() in SKIPPED_HEADERS):
                if where == "cookie":
                    warn(f"{self.id}: cookie parameter {name!r} is not supported and was skipped.")
                continue
            type_, kwargs, hint = py_type(spec, raw.get("schema", {}))
            schema = spec.schema(raw.get("schema", {}))
            required = bool(raw.get("required")) or where == "path"
            description = short_text(raw.get("description") or schema.get("description"), hint)
            params.append(
                Param(py_identifier(name, taken), name, where, type_, kwargs, description, required, schema.get("default"))
            )
        return params

    def body_params(self, taken: set[str]) -> tuple[list[Param], bool]:
        """Return (params, whole_body). whole_body means a single dict is sent as-is."""
        body = self.spec.resolve(self.node.get("requestBody", {}))
        if not body:
            return [], False
        content = body.get("content", {})
        media = next((m for m in content if m.split(";")[0].strip() == "application/json" or m.endswith("+json")), None)
        if media is None:
            raise SpecError("request body is not JSON")
        schema = self.spec.schema(content[media].get("schema", {}))
        body_required = bool(body.get("required"))
        properties = schema.get("properties")
        if schema.get("type", "object") == "object" and properties:
            params = []
            for name, prop in properties.items():
                resolved = self.spec.schema(prop)
                if resolved.get("readOnly"):
                    continue
                type_, kwargs, hint = py_type(self.spec, prop)
                required = body_required and name in schema.get("required", [])
                params.append(
                    Param(py_identifier(name, taken), name, "body", type_, kwargs,
                          short_text(resolved.get("description"), hint), required, resolved.get("default"))
                )
            return params, False
        type_, kwargs, hint = py_type(self.spec, schema)
        description = short_text(body.get("description") or schema.get("description"), hint) or "Request body."
        return [Param(py_identifier("body", taken), "body", "body", type_, kwargs, description, body_required, None)], True


def collect_operations(spec: Spec) -> list[Operation]:
    operations = []
    for path, item in spec.doc.get("paths", {}).items():
        item = spec.resolve(item)
        for method in HTTP_METHODS:
            if method in item:
                operations.append(Operation(spec, path, method, item))
    return operations


# --------------------------------------------------------------------------- code generation


def docstring(op: Operation, bearer: bool, scopes: list[str]) -> str:
    lines = [op.summary.strip() or f"{op.method.upper()} {op.path}"]
    if op.description.strip():
        lines += ["", short_text(op.description)]
    if op.is_write:
        lines += ["", "This changes data on the remote system."]
    text = "\n".join(lines).replace("\\", "\\\\").replace('"', '\\"')
    return "\n".join(f"    {line}" if line else line for line in text.split("\n")).lstrip()


def render_function(op: Operation, used_names: set[str], api_key_env: str) -> str:
    taken: set[str] = set()
    params = op.parameters(taken)
    body_params, whole_body = op.body_params(taken)
    all_params = params + body_params
    bearer, keys, scopes = op.auth_plan()

    function = py_identifier(op.id, used_names)
    ordered = [p for p in all_params if p.required] + [p for p in all_params if not p.required]

    signature = []
    for p in ordered:
        kwargs = {"description": p.description or p.original, **p.kwargs}
        annotation = p.type if p.required or p.default is not None else f"{p.type} | None"
        field = "Field(%s)" % ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        default = "" if p.required else f" = {p.default!r}" if p.default is not None else " = None"
        signature.append(f"    {p.name}: Annotated[{annotation}, {field}]{default},")

    body = []
    path_params = [p for p in params if p.where == "path"]
    if path_params:
        args = ", ".join(f"{p.name}=quote(str({p.name}), safe='')" for p in path_params)
        template = re.sub(r"\{([^}]+)\}", lambda m: "{%s}" % next(
            (p.name for p in path_params if p.original == m.group(1)), m.group(1)), op.path)
        body.append(f"    path = {template!r}.format({args})")
        url = "_url(path)"
    else:
        url = f"_url({op.path!r})"

    call = [f"{op.method.upper()!r}, {url}"]
    query = [p for p in params if p.where == "query"]
    if query or any(where == "query" for where, _ in keys):
        items = ", ".join(f"{p.original!r}: {p.name}" for p in query)
        body.append(f"    params = {{{items}}}")
        for where, name in keys:
            if where == "query":
                body.append(f"    params[{name!r}] = get_api_key({api_key_env!r})")
        body.append("    params = {k: v for k, v in params.items() if v is not None}")
        call.append("params=params")
    headers = [p for p in params if p.where == "header"]
    if headers or any(where == "header" for where, _ in keys):
        items = ", ".join(f"{p.original!r}: {p.name}" for p in headers)
        body.append(f"    headers = {{{items}}}")
        for where, name in keys:
            if where == "header":
                body.append(f"    headers[{name!r}] = get_api_key({api_key_env!r})")
        body.append("    headers = {k: v for k, v in headers.items() if v is not None}")
        call.append("extra_headers=headers")
    if whole_body:
        call.append(f"json={body_params[0].name}")
    elif body_params:
        items = ", ".join(f"{p.original!r}: {p.name}" for p in body_params)
        body.append(f"    body = {{{items}}}")
        body.append("    body = {k: v for k, v in body.items() if v is not None}")
        call.append("json=body")
    if not bearer:
        call.append("auth=False")

    decorator = '@tool(approval_mode="always_require")\n' if op.is_write else ""
    note = f"  # needs scope: {', '.join(scopes)}" if scopes else ""
    return (
        f"{decorator}async def {function}(" + ("\n" + "\n".join(signature) + "\n" if signature else "") + ") -> Any:\n"
        f'    """{docstring(op, bearer, scopes)}"""{note}\n'
        + ("\n".join(body) + "\n" if body else "")
        + f"    return await call_api({', '.join(call)})\n"
    ), function


def render_module(operations: list[Operation], stem: str, base_url_env: str, default_base_url: str, api_key_env: str) -> str:
    used_names: set[str] = set()
    functions = []
    for op in operations:
        try:
            code, name = render_function(op, used_names, api_key_env)
        except SpecError as e:
            warn(f"{op.id}: skipped ({e}).")
            continue
        functions.append((code, name))
    if not functions:
        raise SpecError("No operations could be generated.")

    body = "\n\n".join(code for code, _ in functions)
    imports = ["import os", "from typing import Annotated, Any" + (", Literal" if "Literal[" in body else "")]
    if "quote(" in body:
        imports.append("from urllib.parse import quote")
    third_party = ["from pydantic import Field"]
    if "@tool(" in body:
        third_party.insert(0, "from agent_framework import tool")
    local = ["from api_client import call_api"]
    if "get_api_key(" in body:
        local.append("from auth import get_api_key")

    tools_name = f"{stem.upper()}_TOOLS"
    return (
        '"""Tools generated from an OpenAPI spec by scripts/generate_tools.py.\n\n'
        "Review before use: descriptions are what the model sees, and responses are returned unfiltered,\n"
        "so trim large payloads. Write operations require human approval.\n"
        '"""\n\n'
        + "\n".join(imports) + "\n\n" + "\n".join(third_party) + "\n\n" + "\n".join(local) + "\n\n"
        f"BASE_URL_ENV = {base_url_env!r}\n"
        f"DEFAULT_BASE_URL = {default_base_url!r}\n\n\n"
        "def _url(path: str) -> str:\n"
        "    base = os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL\n"
        "    if not base:\n"
        '        raise RuntimeError(f"{BASE_URL_ENV} is not set.")\n'
        '    return base.rstrip("/") + path\n\n\n'
        + body + "\n\n"
        f"{tools_name} = [\n" + "".join(f"    {name},\n" for _, name in functions) + "]\n"
    ), tools_name


# --------------------------------------------------------------------------- CLI


def describe(op: Operation) -> str:
    bearer, keys, _ = op.auth_plan()
    auth = "+".join(filter(None, ["bearer" if bearer else "", "apikey" if keys else ""])) or "none"
    return f"{op.id:<40} {op.method.upper():<6} {op.path:<40} tags={','.join(op.tags) or '-':<16} auth={auth:<14} {op.summary}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", type=Path, help="OpenAPI 3.x JSON file")
    parser.add_argument("--list", action="store_true", help="list the operations in the spec and exit")
    parser.add_argument("--operations", nargs="+", metavar="ID", default=[], help="operationIds to generate")
    parser.add_argument("--tags", nargs="+", metavar="TAG", default=[], help="generate every operation with these tags")
    parser.add_argument("--out", default="generated_api", help="module name (written to the tools folder) or file path")
    parser.add_argument("--base-url-env", help="environment variable holding the API base URL (default <MODULE>_BASE_URL)")
    parser.add_argument("--default-base-url", default="", help="base URL used when the variable is unset (default none)")
    parser.add_argument("--api-key-env", default="API_KEY", help="environment variable holding the API key")
    parser.add_argument("--force", action="store_true", help="overwrite the output file if it exists")
    args = parser.parse_args(argv)

    try:
        spec = Spec(json.loads(args.spec.read_text(encoding="utf-8")))
        operations = collect_operations(spec)
        if args.list:
            for op in operations:
                print(describe(op))
            return 0
        if not args.operations and not args.tags:
            raise SpecError("Choose what to generate with --operations and/or --tags (see --list).")
        known = {op.id for op in operations}
        unknown = [i for i in args.operations if i not in known]
        if unknown:
            raise SpecError(f"Unknown operationId(s): {', '.join(unknown)}")
        selected = [op for op in operations if op.id in args.operations or set(op.tags) & set(args.tags)]
        if not selected:
            raise SpecError("Nothing matched the given --operations/--tags.")

        out = Path(args.out)
        if out.suffix != ".py":
            out = TOOLS_DIR / f"{args.out}.py"
        stem = out.stem
        if not stem.isidentifier():
            raise SpecError(f"{stem!r} is not a valid Python module name.")
        if out.exists() and not args.force:
            raise SpecError(f"{out} already exists; use --force to overwrite.")
        code, tools_name = render_module(
            selected, stem, args.base_url_env or f"{stem.upper()}_BASE_URL", args.default_base_url, args.api_key_env
        )
    except (SpecError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out.write_text(code, encoding="utf-8", newline="\n")
    print(f"Wrote {out}")
    print(f"Next: in tools/__init__.py add `from tools.{stem} import {tools_name}` and `*{tools_name}` to ALL_TOOLS.")
    print(f"Set {args.base_url_env or stem.upper() + '_BASE_URL'} and any needed credentials in your environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
