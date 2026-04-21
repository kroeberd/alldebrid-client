"""Tests for config_validator.validate_and_sanitise()"""
import pytest
from core.config import AppSettings
from core.config_validator import validate_and_sanitise, _validate


def make_cfg(**kwargs) -> AppSettings:
    return AppSettings(**kwargs)


class TestValidateAndSanitise:

    def test_clean_config_no_changes(self):
        cfg = make_cfg()
        result = validate_and_sanitise(cfg)
        assert result.alldebrid_api_key == cfg.alldebrid_api_key

    def test_data_uri_avatar_reset(self):
        cfg = make_cfg(discord_avatar_url="data:image/png;base64,abc123")
        result = validate_and_sanitise(cfg)
        # data URIs are cleared (Discord rejects them; user must configure a valid PNG/JPG URL)
        assert result.discord_avatar_url == ""

    def test_invalid_json_schedules_reset(self):
        cfg = make_cfg(flexget_task_schedules_json="{not valid json}")
        result = validate_and_sanitise(cfg)
        assert result.flexget_task_schedules_json == "[]"

    def test_invalid_db_type_reset(self):
        cfg = make_cfg(db_type="mysql")
        result = validate_and_sanitise(cfg)
        assert result.db_type == "sqlite"

    def test_invalid_download_client_reset(self):
        cfg = make_cfg(download_client="transmission")
        result = validate_and_sanitise(cfg)
        assert result.download_client == "aria2"

    def test_numeric_below_min_clamped(self):
        cfg = make_cfg(max_concurrent_downloads=0)
        result = validate_and_sanitise(cfg)
        assert result.max_concurrent_downloads == 1

    def test_numeric_above_max_clamped(self):
        cfg = make_cfg(max_concurrent_downloads=999)
        result = validate_and_sanitise(cfg)
        assert result.max_concurrent_downloads == 20

    def test_valid_numeric_unchanged(self):
        cfg = make_cfg(max_concurrent_downloads=5)
        result = validate_and_sanitise(cfg)
        assert result.max_concurrent_downloads == 5

    def test_invalid_webhook_url_warned_not_cleared(self):
        # Bad URLs are warned but not auto-cleared (user must fix intentionally)
        cfg = make_cfg(discord_webhook_url="not-a-url")
        result = validate_and_sanitise(cfg)
        # URL still there — not auto-fixed (no fixed_value for URL format issues)
        assert result.discord_webhook_url == "not-a-url"

    def test_empty_webhook_url_ok(self):
        cfg = make_cfg(discord_webhook_url="")
        issues = _validate(cfg)
        url_issues = [i for i in issues if i[0] == "discord_webhook_url"]
        assert not url_issues

    def test_backup_keep_days_clamped(self):
        cfg = make_cfg(backup_keep_days=0)
        result = validate_and_sanitise(cfg)
        assert result.backup_keep_days == 1

    def test_postgres_port_clamped(self):
        cfg = make_cfg(postgres_port=99999)
        result = validate_and_sanitise(cfg)
        assert result.postgres_port == 65535

    def test_multiple_fixes_applied(self):
        cfg = make_cfg(
            discord_avatar_url="data:image/png;base64,xxx",
            flexget_task_schedules_json="broken",
            db_type="oracle",
        )
        result = validate_and_sanitise(cfg)
        assert not result.discord_avatar_url.startswith("data:")
        assert result.flexget_task_schedules_json == "[]"
        assert result.db_type == "sqlite"

    def test_returns_same_object_when_no_fixes(self):
        # Use explicit safe values to ensure no validator rule fires
        cfg = make_cfg(discord_avatar_url="")  # empty is valid (no avatar)
        result = validate_and_sanitise(cfg)
        assert result.model_dump() == cfg.model_dump()
