"""Static API policy for uploaded event strategies.

This is a compatibility and misuse filter, not a security boundary. Uploaded
code still runs only inside the isolated worker container.
"""

from __future__ import annotations

import ast


ALLOWED_IMPORTS = frozenset({
    "collections",
    "decimal",
    "functools",
    "itertools",
    "math",
    "statistics",
})
_FORBIDDEN_CALLS = frozenset({
    "__import__", "compile", "eval", "exec", "getattr", "globals", "locals",
    "open", "setattr", "delattr", "vars",
})


class StrategySourcePolicyError(ValueError):
    """Uploaded source is outside the supported strategy API."""


def validate_strategy_source(source: str) -> ast.Module:
    """Parse source and enforce the same narrow API policy at both boundaries."""
    if not isinstance(source, str) or not source.strip():
        raise StrategySourcePolicyError("strategy source must be non-empty text")
    try:
        tree = ast.parse(source, filename="<uploaded-strategy>", mode="exec")
    except SyntaxError as exc:
        raise StrategySourcePolicyError(
            f"strategy source has a syntax error at line {exc.lineno}"
        ) from exc
    entrypoints = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_event"
    ]
    if len(entrypoints) != 1 or not isinstance(entrypoints[0], ast.FunctionDef):
        raise StrategySourcePolicyError("strategy source must define a top-level on_event(event, state) function")
    entrypoint = entrypoints[0]
    positional_count = len(entrypoint.args.posonlyargs) + len(entrypoint.args.args)
    positional_names = [arg.arg for arg in (*entrypoint.args.posonlyargs, *entrypoint.args.args)]
    if (
        positional_count != 2
        or positional_names != ["event", "state"]
        or entrypoint.args.defaults
        or entrypoint.args.kwonlyargs
        or entrypoint.args.vararg is not None
        or entrypoint.args.kwarg is not None
        or entrypoint.decorator_list
    ):
        raise StrategySourcePolicyError("on_event must accept exactly two positional arguments: event and state")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    raise StrategySourcePolicyError("strategy imports a module outside the supported allowlist")
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module not in ALLOWED_IMPORTS:
                raise StrategySourcePolicyError("strategy imports a module outside the supported allowlist")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise StrategySourcePolicyError("strategy may not access interpreter dunder names")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise StrategySourcePolicyError("strategy may not access interpreter dunder attributes")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            raise StrategySourcePolicyError("strategy uses a dynamic execution or reflection API")
    return tree
