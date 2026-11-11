"""Typed runtime configuration (R-9.6)."""

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

__all__ = ["Config", "load_config"]
