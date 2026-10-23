"""Language adapters. ``get_language`` maps the ``language:`` config key to one."""

from __future__ import annotations

from testgate.core import GateError, Language


def get_language(name: str) -> Language:
    if name == "python":
        from .python import PythonLanguage

        return PythonLanguage()
    if name == "js":
        from .js import JsLanguage  # imported lazily: js.py is optional at import time

        return JsLanguage()
    raise GateError(f"unknown language: {name!r}")
