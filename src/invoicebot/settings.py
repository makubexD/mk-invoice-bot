"""Process settings read from the environment at startup.

Only paths and the database URL live here. Secrets never do: they come from the
age-encrypted file named by ``SECRETS_PATH`` (R-9.9), and no field name ends in
a secret suffix, so the startup guard never trips on these variables.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Where Compose mounts the runtime config directory.
RUNTIME_CONFIG_DIR = Path("/app/runtime-config")


class Settings(BaseSettings):
    """Environment variables of the app process, matched case-insensitively."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: str | None = None
    config_path: Path = RUNTIME_CONFIG_DIR / "config.yaml"
    secrets_path: Path | None = None
    age_identity_path: Path | None = None
    data_dir: Path = Path("/data")

    def runtime_files_present(self) -> bool:
        """True when both the config and the secrets file exist on disk."""
        return self.config_path.is_file() and self.secrets_path is not None and self.secrets_path.is_file()
