"""One config loader for startup and reload (R-9.6, D155).

Startup and a ``SIGHUP`` reload must accept and reject exactly the same config,
so both call :func:`load_runtime_config`: the same loaders in the same order,
then the same blocking checks.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from pydantic import ValidationError

from invoicebot.config.checks import run_checks
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.extract.parsing_rules import load_rules
from invoicebot.routing.router import CompiledRule, compile_rules
from invoicebot.settings import Settings

#: The file is not valid YAML, or its document shape breaks a loader invariant.
REASON_YAML = "yaml"
#: The file fails pydantic validation.
REASON_VALIDATION = "validation"
#: The parsing-rules file is missing, malformed or invalid (S3.6).
REASON_PARSING_RULES = "parsing_rules"
#: A routing rule cannot be compiled (R-3.1).
REASON_ROUTING = "routing"


class ConfigRejected(Exception):
    """A candidate config failed to load; ``reason`` is a short code, never a value (R-9.6)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything a start or reload needs from the config files (R-9.6)."""

    cfg: Config
    directory: StoreDirectory
    rules: tuple[CompiledRule, ...]


def load_runtime_config(settings: Settings) -> RuntimeConfig:
    """Load and check the config, store directory, parsing rules and routing rules (R-9.6).

    Raises:
        ConfigRejected: a file is unusable or a check found a blocking problem.
    """
    cfg = _load_cfg(settings)
    directory = _load_directory(settings)
    _load_parsing_rules(settings)
    rules = _compile_rules(cfg)
    _check(cfg, directory)
    return RuntimeConfig(cfg=cfg, directory=directory, rules=rules)


def _load_cfg(settings: Settings) -> Config:
    try:
        return load_config(settings.config_path)
    except ValidationError as err:
        raise ConfigRejected(REASON_VALIDATION) from err
    except (yaml.YAMLError, ValueError) as err:
        raise ConfigRejected(REASON_YAML) from err


def _load_directory(settings: Settings) -> StoreDirectory:
    try:
        return load_directory(settings.config_path.parent)
    except ValidationError as err:
        raise ConfigRejected(REASON_VALIDATION) from err
    except (yaml.YAMLError, ValueError) as err:
        raise ConfigRejected(REASON_YAML) from err


def _load_parsing_rules(settings: Settings) -> None:
    try:
        load_rules(settings.parsing_rules_path)
    except (yaml.YAMLError, ValueError) as err:
        raise ConfigRejected(REASON_PARSING_RULES) from err


def _compile_rules(cfg: Config) -> tuple[CompiledRule, ...]:
    try:
        return compile_rules(cfg.routing)
    except ValueError as err:
        raise ConfigRejected(REASON_ROUTING) from err


def _check(cfg: Config, directory: StoreDirectory) -> None:
    blocking = next(
        (problem for problem in run_checks(cfg, directory, changed_portals=frozenset()) if problem.blocking),
        None,
    )
    if blocking is not None:
        raise ConfigRejected(blocking.code)
