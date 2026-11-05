"""Settings, read from RW_* environment variables and, for local runs, a `.secrets` file."""

import os
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Names used in the owner's `.secrets` file, mapped to Settings fields.
SECRETS_FILE_NAMES = {
    "DOC-EXTRACTOR": "docextractor_api_key",
    "OPENROUTER": "openrouter_api_key",
    "AZURE-OPENAI-KEY": "azure_openai_api_key",
    "AZURE-OPENAI-ENDPOINT": "azure_openai_endpoint",
}

# Veeam core products the owner already follows (spec §4). Kasten, VDC and Securiti.ai stay watched.
DEFAULT_EXCLUDED_PRODUCTS = [
    "Veeam Backup & Replication",
    "Veeam Backup for Microsoft 365",
    "Veeam Backup for Salesforce",
    "Veeam ONE",
    "Veeam Recovery Orchestrator",
    "Veeam Service Provider Console",
]

CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


# Browserless' Chromium says "HeadlessChrome" by default, which some edges (Cohesity, Akamai) refuse.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RW_", extra="ignore", hide_input_in_errors=True)

    database_url: str
    docextractor_base_url: str = "http://docextractor.k3s.home.lan/api"
    docextractor_api_key: SecretStr
    docextractor_ui_url: str = "http://docextractor.k3s.home.lan"
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_model: str = "gpt-5-mini"
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "openai/gpt-5-mini"
    excluded_products: list[str] = list(DEFAULT_EXCLUDED_PRODUCTS)
    user_agent: str = CHROME_USER_AGENT
    confirm_delay_seconds: int = 600
    teams_webhook_url: SecretStr = SecretStr("")
    web_base_url: str = "http://release-watch.k3s.home.lan"
    # The shared in-cluster Browserless (a real Chrome), for hosts that refuse plain HTTP clients.
    browserless_url: str = "http://browserless.browserless.svc.cluster.local:3000"
    browserless_token: SecretStr = SecretStr("")
    browser_user_agent: str = BROWSER_USER_AGENT
    # The weekly login visit (watch-login). Only that job's pod gets these.
    avepoint_user: SecretStr = SecretStr("")
    avepoint_password: SecretStr = SecretStr("")
    securiti_user: SecretStr = SecretStr("")
    securiti_password: SecretStr = SecretStr("")
    login_confirm_delay_seconds: int = 300


def load_secrets_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        field = SECRETS_FILE_NAMES.get(name.strip())
        if field:
            values[field] = value.strip()
    return values


def get_settings() -> Settings:
    """Settings from the environment; `.secrets` in the CWD fills in whatever the env leaves unset."""
    path = Path(".secrets")
    file_values = load_secrets_file(path) if path.is_file() else {}
    # Init kwargs beat env vars in pydantic-settings, so pass only fields the env doesn't set.
    env = {k.upper() for k in os.environ}
    env_set = {f for f in Settings.model_fields if f"RW_{f.upper()}" in env}
    return Settings(**{k: v for k, v in file_values.items() if k not in env_set})

