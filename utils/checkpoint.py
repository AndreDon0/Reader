import json
from pathlib import Path

CHECKPOINT_PATH = Path("tmp/checkpoint.json")


def load_checkpoint(checkpoint_name: str, data: dict) -> dict:
    if not CHECKPOINT_PATH.exists():
        save_checkpoint(checkpoint_name, data)
        return data

    with CHECKPOINT_PATH.open("r", encoding="utf-8") as f:
        checkpoints = json.load(f)

    if checkpoint_name not in checkpoints:
        save_checkpoint(checkpoint_name, data)
        return data

    return checkpoints[checkpoint_name]


def save_checkpoint(checkpoint_name: str, data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CHECKPOINT_PATH.exists():
        with CHECKPOINT_PATH.open("r", encoding="utf-8") as f:
            checkpoints = json.load(f)
    else:
        checkpoints = {}

    checkpoints[checkpoint_name] = data

    with CHECKPOINT_PATH.open("w", encoding="utf-8") as f:
        json.dump(checkpoints, f, indent=4, ensure_ascii=False)