"""Provision every Microsoft Foundry model deployment required by the game."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class RequiredModel:
    deployment_name: str
    model_name: str
    capacity: int


REQUIRED_MODELS = (
    RequiredModel("gpt-realtime-2.1", "gpt-realtime-2.1", 1),
    RequiredModel("MAI-Thinking-1", "MAI-Thinking-1", 1),
)
PREFERRED_SKUS = ("GlobalStandard", "DataZoneStandard", "Standard")


class ProvisioningError(RuntimeError):
    pass


class AzureCli:
    def __init__(self, subscription: str | None) -> None:
        self.subscription = subscription

    def json(self, arguments: Sequence[str]) -> Any:
        command = ["az", *arguments]
        if self.subscription:
            command.extend(("--subscription", self.subscription))
        command.extend(("--only-show-errors", "--output", "json"))
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise ProvisioningError("Azure CLI was not found. Install 'az' and sign in first.") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or error.stdout.strip() or "Azure CLI command failed"
            raise ProvisioningError(detail) from error
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ProvisioningError("Azure CLI returned invalid JSON.") from error


def _default_catalog_entry(catalog: Sequence[dict[str, Any]], model_name: str,
                           account_kind: str) -> dict[str, Any]:
    candidates = [
        entry for entry in catalog
        if entry.get("kind", "").casefold() == account_kind.casefold()
        and entry.get("model", {}).get("name") == model_name
    ]
    if not candidates:
        raise ProvisioningError(
            f"Model {model_name!r} is not available for {account_kind!r} accounts in this location."
        )
    defaults = [entry for entry in candidates if entry["model"].get("isDefaultVersion")]
    return max(defaults or candidates, key=lambda entry: entry["model"].get("version", ""))


def _select_sku(entry: dict[str, Any], usage: Sequence[dict[str, Any]],
                capacity: int) -> str:
    quota_by_name = {
        item.get("name", {}).get("value"): float(item.get("limit", 0)) - float(item.get("currentValue", 0))
        for item in usage
    }
    skus = {sku.get("name"): sku for sku in entry["model"].get("skus", [])}
    for name in PREFERRED_SKUS:
        sku = skus.get(name)
        if sku and quota_by_name.get(sku.get("usageName"), 0) >= capacity:
            return name
    model_name = entry["model"]["name"]
    raise ProvisioningError(
        f"No supported SKU has {capacity} available quota unit(s) for {model_name!r}."
    )


def _deployment_model(deployment: dict[str, Any]) -> dict[str, Any]:
    return deployment.get("properties", {}).get("model") or deployment.get("model") or {}


def _deployment_name(deployment: dict[str, Any]) -> str | None:
    return deployment.get("name") or deployment.get("deploymentName")


def _matching_deployment(existing: Sequence[dict[str, Any]], required: RequiredModel,
                         model_format: str) -> bool:
    deployment = next((item for item in existing if _deployment_name(item) == required.deployment_name), None)
    if deployment is None:
        return False
    model = _deployment_model(deployment)
    if model.get("name") == required.model_name and model.get("format") == model_format:
        return True
    raise ProvisioningError(
        f"Deployment {required.deployment_name!r} already exists but targets "
        f"{model.get('format')!r}/{model.get('name')!r}; refusing to overwrite it."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", required=True, help="Resource group containing the Foundry account")
    parser.add_argument("--account-name", required=True, help="Existing Azure AI Services/Foundry account name")
    parser.add_argument("--subscription", help="Azure subscription name or ID; defaults to the active subscription")
    parser.add_argument("--location", help="Catalog location; defaults to the account location")
    parser.add_argument("--yes", action="store_true", help="Create deployments without an interactive confirmation")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print changes without creating deployments")
    return parser


def provision(arguments: argparse.Namespace, cli: AzureCli) -> None:
    account = cli.json((
        "cognitiveservices", "account", "show",
        "--resource-group", arguments.resource_group,
        "--name", arguments.account_name,
    ))
    location = arguments.location or account["location"]
    account_kind = account["kind"]
    catalog = cli.json(("cognitiveservices", "model", "list", "--location", location))
    usage = cli.json(("cognitiveservices", "usage", "list", "--location", location))
    existing = cli.json((
        "cognitiveservices", "account", "deployment", "list",
        "--resource-group", arguments.resource_group,
        "--name", arguments.account_name,
    ))

    pending: list[tuple[RequiredModel, dict[str, Any], str]] = []
    for required in REQUIRED_MODELS:
        entry = _default_catalog_entry(catalog, required.model_name, account_kind)
        model = entry["model"]
        if _matching_deployment(existing, required, model["format"]):
            print(f"Already provisioned: {required.deployment_name}")
            continue
        sku = _select_sku(entry, usage, required.capacity)
        pending.append((required, model, sku))

    print(f"Target: {arguments.account_name} ({account_kind}) in {location}, resource group {arguments.resource_group}")
    if not pending:
        print("All required model deployments are already provisioned.")
        return
    for required, model, sku in pending:
        print(
            f"Plan: create {required.deployment_name} -> "
            f"{model['format']}/{model['name']}:{model['version']} "
            f"with {sku} capacity {required.capacity}"
        )

    if arguments.dry_run:
        print("Dry run complete; no deployments were created.")
        return
    if not arguments.yes:
        answer = input("Create these deployments? [y/N] ").strip().casefold()
        if answer not in {"y", "yes"}:
            print("Cancelled; no deployments were created.")
            return

    for required, model, sku in pending:
        cli.json((
            "cognitiveservices", "account", "deployment", "create",
            "--resource-group", arguments.resource_group,
            "--name", arguments.account_name,
            "--deployment-name", required.deployment_name,
            "--model-name", model["name"],
            "--model-version", model["version"],
            "--model-format", model["format"],
            "--sku-name", sku,
            "--sku-capacity", str(required.capacity),
        ))
        print(f"Provisioned: {required.deployment_name}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        provision(arguments, AzureCli(arguments.subscription))
    except ProvisioningError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())