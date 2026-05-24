from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def test_env_and_example_define_same_keys():
    example_path = ROOT / ".env.example"
    env_path = ROOT / ".env"

    assert example_path.exists(), ".env.example не найден"
    assert env_path.exists(), ".env не найден"

    example_keys = set(dotenv_values(example_path).keys())
    env_keys = set(dotenv_values(env_path).keys())

    assert env_keys == example_keys, (
        "Ключи .env и .env.example не синхронизированы: "
        f"нет в .env: {sorted(example_keys - env_keys)}; "
        f"лишние в .env: {sorted(env_keys - example_keys)}"
    )
