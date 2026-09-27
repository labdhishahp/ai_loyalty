"""Whether this repository can actually be deployed, not whether it works here.

WHY THIS FILE EXISTS. The suite had 226 passing tests and the project could not
have booted on Vercel: `mcp` and `PyJWT` were imported at module scope but
absent from requirements.txt. Every test passed because they were installed in
the developer's virtualenv by `pip install -e .`, which Vercel never runs. The
tests were measuring the wrong environment.

So these tests deliberately do NOT ask "does it import here". They ask
questions whose answers are properties of the repository: does the declared
dependency set cover the import graph, does the entry point export an app, does
the configuration a deployment needs actually exist. Those hold or fail
identically on a laptop and in a build container.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Packages the Vercel function imports. Everything else in the repo (seed, eval,
# db, tests) runs from a laptop and may depend on anything.
RUNTIME_PACKAGES = ("api", "core", "agent", "tools", "llm",
                    "knowledge", "actions", "metrics", "mcp_server")

# import name -> distribution name, where they differ.
DISTRIBUTION_OF = {
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
    "psycopg": "psycopg",
    "yaml": "pyyaml",
}

# Third-party imports that arrive as transitive dependencies of something
# declared, and so need no line of their own. Each is justified, because an
# unjustified entry here is how a real gap gets waved through.
TRANSITIVE = {
    "anyio",      # via fastapi -> starlette, and via anthropic
    "pydantic",   # via fastapi and mcp
    "starlette",  # via fastapi
    "httpx",      # via anthropic / openai
}


def stdlib_names() -> set[str]:
    return set(sys.stdlib_module_names)


def first_party() -> set[str]:
    """Top-level directories of this repo, which are never pip dependencies."""
    return {p.name for p in REPO.iterdir()
            if p.is_dir() and (p / "__init__.py").exists()} | {"db", "eval", "tests"}


def imported_top_level_modules() -> dict[str, set[str]]:
    """Every top-level module imported by the runtime packages, with sources.

    Parsed rather than imported: importing would prove only that THIS machine
    has the packages, which is the exact mistake this file exists to correct.
    """
    found: dict[str, set[str]] = {}
    for package in RUNTIME_PACKAGES:
        for path in (REPO / package).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # A relative import is first-party by definition.
                    if node.level or node.module is None:
                        continue
                    names = [node.module]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    found.setdefault(root, set()).add(
                        str(path.relative_to(REPO)))
    return found


def declared_distributions(text: str) -> set[str]:
    """Distribution names from a requirements file, normalised and de-extra'd."""
    names = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = line.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_requirements_covers_every_runtime_import():
    """The blocker, pinned.

    If someone adds an import to the runtime path and not to requirements.txt,
    this fails here rather than on Vercel, where the only symptom is a 500 from
    every route at once.
    """
    declared = declared_distributions((REPO / "requirements.txt").read_text())
    ignore = stdlib_names() | first_party() | TRANSITIVE | {"__future__"}

    missing = {}
    for module, sources in imported_top_level_modules().items():
        if module in ignore:
            continue
        distribution = DISTRIBUTION_OF.get(module, module).lower()
        if distribution not in declared:
            missing[module] = sorted(sources)[:2]

    assert not missing, (
        f"imported on the runtime path but not in requirements.txt: {missing}")


def test_requirements_and_pyproject_agree():
    """They drifted once, and the drift was the deployment blocker."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    declared = declared_distributions((REPO / "requirements.txt").read_text())
    project = {d.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
               .strip().lower().replace("_", "-")
               for d in pyproject["project"]["dependencies"]}
    assert project == declared, (
        f"only in pyproject: {project - declared}; "
        f"only in requirements.txt: {declared - project}")


def test_every_runtime_dependency_has_an_upper_bound():
    """A bare `>=` lets a Vercel build months from now resolve a major this
    project has never run. The MCP SDK's 1.x -> 2.x rename already cost a day."""
    unbounded = [line.split("#")[0].strip()
                 for line in (REPO / "requirements.txt").read_text().splitlines()
                 if line.split("#")[0].strip() and "<" not in line.split("#")[0]]
    assert not unbounded, f"no upper bound on: {unbounded}"


def test_the_vercel_entry_point_exports_an_asgi_app():
    """vercel.json points at api/index.py and the runtime looks for `app`."""
    source = (REPO / "api" / "index.py").read_text()
    tree = ast.parse(source)
    exported = {alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    assert "app" in exported


def test_the_entry_point_is_importable_without_package_context():
    """Vercel may load the handler by file path rather than as `api.index`.

    An earlier version used `from .app import app`, which works only under the
    dotted form and raises "attempted relative import with no known parent
    package" under the other. An absolute import works under both, and costs
    nothing.
    """
    source = (REPO / "api" / "index.py").read_text()
    tree = ast.parse(source)
    relative = [node for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level]
    assert not relative, "api/index.py uses a relative import"


def test_vercel_json_routes_the_api_to_the_entry_point():
    import json
    config = json.loads((REPO / "vercel.json").read_text())
    assert "api/index.py" in config["functions"]
    assert any(r["destination"].startswith("/api/index")
               for r in config["rewrites"])


@pytest.mark.parametrize("variable", [
    "SUPABASE_URL",        # required at import time by build_http()
    "DATABASE_POOL_URL",   # required in production by config.pool_url()
    "APP_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "PUBLIC_BASE_URL",
])
def test_every_production_variable_is_documented(variable):
    """A variable the code requires but the template omits is discovered at
    deploy time, which is the most expensive moment to discover it.
    SUPABASE_URL was exactly that: required to IMPORT the app, absent from the
    deployment runbook."""
    assert f"{variable}=" in (REPO / ".env.example").read_text(), \
        f"{variable} is not in .env.example"
    assert variable in (REPO / "docs" / "deployment.md").read_text(), \
        f"{variable} is not in docs/deployment.md"


def test_no_env_file_is_tracked_by_git():
    """The template is tracked; the filled-in files never are."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
    leaked = [f for f in tracked
              if pathlib.Path(f).name in (".env", ".env.local")
              or f.endswith(".env.production")]
    assert not leaked, f"secret files tracked: {leaked}"
