# ruff: noqa: I001

import argparse
from pathlib import Path
from typing import Any

from src.hospital_game import (
    load_patient_scenario,
    load_patient_scenarios,
    start_hospital_game,
)


PROJECT_ROOT = Path(__file__).parents[1]
PROMPTS_DIR = PROJECT_ROOT / "data" / "prompts"


def load_conversation_parameters(path: Path) -> dict[str, Any]:
    return load_patient_scenario(path).conversation_parameters()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "prompts",
        nargs="*",
        type=Path,
        help="Patient prompt JSON files (defaults to every file in data/prompts)",
    )
    arguments = parser.parse_args()
    prompt_paths = arguments.prompts or sorted(PROMPTS_DIR.glob("*.json"))
    start_hospital_game(load_patient_scenarios(prompt_paths))
