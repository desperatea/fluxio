"""Тесты загрузки конфигурации."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_config_yaml_exists() -> None:
    """config.yaml должен существовать в корне проекта."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    assert config_path.exists(), "config.yaml не найден"


def test_config_yaml_valid() -> None:
    """config.yaml должен быть валидным YAML."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "trading" in data
    assert "monitoring" in data


def test_config_dry_run_default() -> None:
    """В config.yaml по умолчанию dry_run должен быть True."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["trading"]["dry_run"] is True, "dry_run должен быть True по умолчанию"


def test_env_example_exists() -> None:
    """.env.example должен существовать."""
    env_path = Path(__file__).parent.parent / ".env.example"
    assert env_path.exists(), ".env.example не найден"


def test_env_not_in_gitignore() -> None:
    """.env должен быть в .gitignore."""
    gitignore_path = Path(__file__).parent.parent / ".gitignore"
    assert gitignore_path.exists()
    content = gitignore_path.read_text()
    assert ".env" in content, ".env должен быть в .gitignore"
