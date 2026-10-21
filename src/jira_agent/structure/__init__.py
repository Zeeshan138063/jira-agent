from pathlib import Path


def structure_example_path() -> Path:
    return Path(__file__).parent / "example_plan.json"
