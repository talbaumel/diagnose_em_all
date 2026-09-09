from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from tools.provision_foundry_models import (
    ProvisioningError,
    _matching_deployment,
    _select_sku,
    provision,
)


def catalog_entry(name: str, model_format: str, usage_name: str) -> dict:
    return {
        "kind": "AIServices",
        "model": {
            "name": name,
            "format": model_format,
            "version": "2026-01-01",
            "isDefaultVersion": True,
            "skus": [{"name": "GlobalStandard", "usageName": usage_name}],
        },
    }


class FakeCli:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.created = []
        self.catalog = [
            catalog_entry(
                "gpt-realtime-2.1", "OpenAI", "OpenAI.GlobalStandard.gpt-realtime-2.1"
            ),
            catalog_entry(
                "MAI-Thinking-1", "Microsoft", "AIServices.GlobalStandard.MAI-Thinking-1"
            ),
        ]
        self.usage = [
            {"name": {"value": "OpenAI.GlobalStandard.gpt-realtime-2.1"}, "limit": 2, "currentValue": 0},
            {"name": {"value": "AIServices.GlobalStandard.MAI-Thinking-1"}, "limit": 2, "currentValue": 0},
        ]

    def json(self, arguments):
        arguments = tuple(arguments)
        if arguments[:3] == ("cognitiveservices", "account", "show"):
            return {"kind": "AIServices", "location": "eastus2"}
        if arguments[:3] == ("cognitiveservices", "model", "list"):
            return self.catalog
        if arguments[:3] == ("cognitiveservices", "usage", "list"):
            return self.usage
        if arguments[:4] == ("cognitiveservices", "account", "deployment", "list"):
            return self.existing
        if arguments[:4] == ("cognitiveservices", "account", "deployment", "create"):
            self.created.append(arguments)
            return {}
        raise AssertionError(arguments)


def arguments(**overrides):
    values = {
        "resource_group": "rg-game",
        "account_name": "foundry-game",
        "subscription": None,
        "location": None,
        "yes": True,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FoundryProvisioningTests(unittest.TestCase):
    def test_provisions_both_models_with_catalog_formats(self):
        cli = FakeCli()
        with contextlib.redirect_stdout(io.StringIO()):
            provision(arguments(), cli)
        self.assertEqual(len(cli.created), 2)
        commands = {command[command.index("--deployment-name") + 1]: command for command in cli.created}
        self.assertEqual(commands["gpt-realtime-2.1"][commands["gpt-realtime-2.1"].index("--model-format") + 1], "OpenAI")
        self.assertEqual(commands["MAI-Thinking-1"][commands["MAI-Thinking-1"].index("--model-format") + 1], "Microsoft")

    def test_dry_run_does_not_create_deployments(self):
        cli = FakeCli()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            provision(arguments(dry_run=True), cli)
        self.assertEqual(cli.created, [])
        self.assertIn("Dry run complete", output.getvalue())

    def test_matching_existing_deployments_are_skipped(self):
        cli = FakeCli(existing=[
            {"name": "gpt-realtime-2.1", "properties": {"model": {"name": "gpt-realtime-2.1", "format": "OpenAI"}}},
            {"name": "MAI-Thinking-1", "properties": {"model": {"name": "MAI-Thinking-1", "format": "Microsoft"}}},
        ])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            provision(arguments(), cli)
        self.assertEqual(cli.created, [])
        self.assertIn("All required model deployments are already provisioned", output.getvalue())

    def test_conflicting_deployment_is_rejected(self):
        existing = [{
            "name": "gpt-realtime-2.1",
            "properties": {"model": {"name": "other-model", "format": "OpenAI"}},
        }]
        required = type("Required", (), {
            "deployment_name": "gpt-realtime-2.1",
            "model_name": "gpt-realtime-2.1",
        })()
        with self.assertRaisesRegex(ProvisioningError, "refusing to overwrite"):
            _matching_deployment(existing, required, "OpenAI")

    def test_sku_requires_enough_available_quota(self):
        entry = catalog_entry("gpt-realtime-2.1", "OpenAI", "quota-name")
        usage = [{"name": {"value": "quota-name"}, "limit": 10, "currentValue": 10}]
        with self.assertRaisesRegex(ProvisioningError, "No supported SKU"):
            _select_sku(entry, usage, 1)


if __name__ == "__main__":
    unittest.main()