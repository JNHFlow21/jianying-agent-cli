"""Verb modules and their auto-discovery.

Every module in this package that exposes ``register(registry)`` becomes part of
the CLI; ``cli.py`` no longer knows any verb by name.  A new verb therefore lands
as one new file (``jy_cli/verbs/<name>.py``) and never edits its neighbours,
which is what lets several agents add verbs without colliding.

Registration is tolerant: a module that fails to import is recorded in
``registry.errors`` and reported by ``jy doctor`` instead of taking the whole CLI
down (this is also how a half-landed teammate module degrades).
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

DEFAULT_TIMEOUT_MS = 600_000


class Registry:
    """The subparser tree plus the parent parsers every verb shares."""

    def __init__(self, parser, subparsers, parser_class, verbose_parent, output_parent,
                 async_parent):
        self.parser = parser
        self.subparsers = subparsers
        self.parser_class = parser_class
        self.verbose_parent = verbose_parent
        self.output_parent = output_parent
        self.async_parent = async_parent
        self.commands = []
        self.groups = []
        self.errors = {}
        self.skipped = {}

    def parser_for(self, name, *, parents=(), help=None, description=None, group=False):
        """Add one subcommand; ``group=True`` also registers a nested group name."""
        options = {"help": help}
        if description:
            options["description"] = description
        if parents:
            options["parents"] = list(parents)
        parser = self.subparsers.add_parser(name, **options)
        if group:
            self.groups.append(name)
        return parser

    def add(self, name, *, handler, parents=(), help=None, description=None):
        """Add a leaf verb bound to ``handler`` (command label == name)."""
        parser = self.parser_for(name, parents=parents, help=help, description=description)
        self.bind(parser, name, handler)
        return parser

    def bind(self, parser, label, handler):
        """Bind handler + explicit command label (nested verbs: ``draft build``)."""
        parser.set_defaults(handler=handler, command=label)
        self.commands.append(label)
        return parser

    def defaults(self):
        """The flat list of command labels, for argparse-failure labelling."""
        return tuple(self.commands)

    def problems(self):
        return {
            "errors": dict(self.errors),
            "skipped": dict(self.skipped),
            "verbs": len(self.commands),
        }


def discover(registry):
    """Import every sibling module and call its ``register(registry)``."""
    package = sys.modules[__name__]
    root = Path(__file__).parent
    for info in sorted(pkgutil.iter_modules([str(root)]), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module_name = f"{package.__name__}.{info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - one bad verb must not kill the CLI
            registry.errors[module_name] = f"{type(exc).__name__}: {exc}"
            continue
        register = getattr(module, "register", None)
        if register is None:
            registry.skipped[module_name] = "no register(registry) function"
            continue
        try:
            register(registry)
        except Exception as exc:  # noqa: BLE001 - same tolerance for bad registration
            registry.errors[module_name] = f"{type(exc).__name__}: {exc}"
    return registry
