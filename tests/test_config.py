from release_watch.config import Settings, get_settings, load_secrets_file


SECRETS = (
    "DOC-EXTRACTOR=dxk_x\n"
    "OPENROUTER=sk-or\n"
    "# comment\n"
    "\n"
    "AZURE-OPENAI-KEY=az\n"
    "AZURE-OPENAI-ENDPOINT=https://e.openai.azure.com/\n"
    "SOMETHING-ELSE=ignored\n"
)


def test_secrets_file_maps_names(tmp_path):
    path = tmp_path / ".secrets"
    path.write_text(SECRETS)
    assert load_secrets_file(path) == {
        "docextractor_api_key": "dxk_x",
        "openrouter_api_key": "sk-or",
        "azure_openai_api_key": "az",
        "azure_openai_endpoint": "https://e.openai.azure.com/",
    }


def test_env_overrides_secrets_file(tmp_path, monkeypatch):
    (tmp_path / ".secrets").write_text(SECRETS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RW_DATABASE_URL", "postgresql+psycopg://x@y/z")
    monkeypatch.setenv("RW_DOCEXTRACTOR_API_KEY", "env")
    settings = get_settings()
    assert settings.docextractor_api_key.get_secret_value() == "env"
    assert settings.openrouter_api_key.get_secret_value() == "sk-or"


def test_secret_not_in_repr(tmp_path, monkeypatch):
    (tmp_path / ".secrets").write_text(SECRETS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RW_DATABASE_URL", "postgresql+psycopg://x@y/z")
    settings = get_settings()
    assert "dxk_x" not in repr(settings)
    assert "dxk_x" not in str(settings)


def test_default_exclusions(monkeypatch):
    settings = Settings(database_url="postgresql+psycopg://x@y/z", docextractor_api_key="k")
    assert "Veeam Kasten for Kubernetes" not in settings.excluded_products
    assert "Veeam Backup & Replication" in settings.excluded_products
    assert len(settings.excluded_products) == 6
