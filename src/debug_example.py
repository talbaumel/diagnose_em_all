# ruff: noqa: I001

import argparse
import json
from pathlib import Path
from typing import Any

from src.realtime_conversation import PatientType, Test, strat_conversation


PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "data" / "prompts" / "01_common_cold_kid.json"


def load_conversation_parameters(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as prompt_file:
        parameters = json.load(prompt_file)

    parameters["patient_type"] = PatientType[parameters["patient_type"]]
    parameters["tests"] = [Test(**test) for test in parameters["tests"]]
    return parameters


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "prompt",
        nargs="?",
        type=Path,
        default=DEFAULT_PROMPT,
        help="Patient prompt JSON file",
    )
    arguments = parser.parse_args()
    strat_conversation(**load_conversation_parameters(arguments.prompt))
