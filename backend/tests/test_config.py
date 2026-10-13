"""Settings loading: blank values, bad numbers, JSON settings and the startup warnings."""
import json

from fleet.config import Settings


def test_nothing_set_gives_safe_empty_defaults():
    s = Settings({})
    assert s.FLEET_API_KEY is None and s.APP_SECRET is None and s.VERIFY_TOKEN is None
    assert s.MANAGER_WHATSAPP_NUMBER == "" and s.WHATSAPP_PHONE_ID == ""
    assert s.DRIVER_WHATSAPP_MAPPING == {} and s.TEST_VENDOR_NUMBERS == [] and s.TEST_HUB_NUMBERS == []
    assert s.ALLOW_INSECURE_DEV is False and s.ENABLE_DEBUG_ENDPOINTS is False
    assert s.FOLLOWUP_CHECK_DELAY_SECONDS == 7200


def test_blank_values_count_as_unset():
    s = Settings({"FLEET_API_KEY": "  ", "WHATSAPP_APP_SECRET": "", "FOLLOWUP_CHECK_DELAY_SECONDS": ""})
    assert s.FLEET_API_KEY is None and s.APP_SECRET is None
    assert s.FOLLOWUP_CHECK_DELAY_SECONDS == 7200      # used to crash with int("")


def test_invalid_number_falls_back_to_default():
    assert Settings({"PREDICTIVE_PATTERN_MIN_REPEATS": "many"}).PREDICTIVE_PATTERN_MIN_REPEATS == 2
    assert Settings({"PREDICTIVE_PATTERN_MIN_REPEATS": " 5 "}).PREDICTIVE_PATTERN_MIN_REPEATS == 5


def test_flags():
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        assert Settings({"ALLOW_INSECURE_DEV": truthy}).ALLOW_INSECURE_DEV is True
    for falsy in ("0", "false", "no", "", "maybe"):
        assert Settings({"ALLOW_INSECURE_DEV": falsy}).ALLOW_INSECURE_DEV is False


def test_values_are_stripped_and_env_names_mapped():
    s = Settings({"WHATSAPP_TOKEN": " tok ", "PHONE_NUMBER_ID": "99", "WHATSAPP_VERIFY_TOKEN": "v",
                  "WHATSAPP_APP_SECRET": "s", "MANAGER_WHATSAPP_NUMBER": "+15555550100"})
    assert (s.WHATSAPP_TOKEN, s.WHATSAPP_PHONE_ID, s.VERIFY_TOKEN, s.APP_SECRET) == ("tok", "99", "v", "s")
    assert s.MANAGER_WHATSAPP_NUMBER == "+15555550100"


def test_json_settings_from_env():
    s = Settings({
        "DRIVER_WHATSAPP_MAPPING": json.dumps({"TRK1": "+15555550101", "BAD": 5, "EMPTY": " "}),
        "TEST_VENDOR_NUMBERS": json.dumps([{"name": "A", "phone": "+15555550102"}, {"name": "no phone"}, "junk"]),
        "TEST_HUB_NUMBERS": json.dumps(["+15555550103", "", 7]),
    })
    assert s.DRIVER_WHATSAPP_MAPPING == {"TRK1": "+15555550101"}
    assert s.TEST_VENDOR_NUMBERS == [{"name": "A", "phone": "+15555550102"}]
    assert s.TEST_HUB_NUMBERS == ["+15555550103"]
    assert s.DRIVER_PHONE_TO_VEHICLE == {"5555550101": "TRK1"}


def test_malformed_json_is_ignored_not_fatal():
    s = Settings({"DRIVER_WHATSAPP_MAPPING": "{oops", "TEST_VENDOR_NUMBERS": "[1,", "TEST_HUB_NUMBERS": "x"})
    assert s.DRIVER_WHATSAPP_MAPPING == {} and s.TEST_VENDOR_NUMBERS == [] and s.TEST_HUB_NUMBERS == []


def test_settings_can_come_from_a_config_file_and_env_wins(tmp_path):
    f = tmp_path / "fleet.json"
    f.write_text(json.dumps({"DRIVER_WHATSAPP_MAPPING": {"A": "+15555550101"},
                             "TEST_HUB_NUMBERS": ["+15555550103"]}))
    s = Settings({"FLEET_CONFIG_FILE": str(f), "TEST_HUB_NUMBERS": json.dumps(["+15555550104"])})
    assert s.DRIVER_WHATSAPP_MAPPING == {"A": "+15555550101"}
    assert s.TEST_HUB_NUMBERS == ["+15555550104"]


def test_bad_config_file_is_ignored(tmp_path):
    assert Settings({"FLEET_CONFIG_FILE": str(tmp_path / "missing.json")}).DRIVER_WHATSAPP_MAPPING == {}
    f = tmp_path / "list.json"
    f.write_text("[1, 2]")
    assert Settings({"FLEET_CONFIG_FILE": str(f)}).TEST_HUB_NUMBERS == []
    f.write_text("{broken")
    assert Settings({"FLEET_CONFIG_FILE": str(f)}).TEST_HUB_NUMBERS == []


def test_allowed_origins_default_and_list():
    assert Settings({}).ALLOWED_ORIGINS == ["*"]
    assert Settings({"ALLOWED_ORIGINS": "https://a.app, https://b.app"}).ALLOWED_ORIGINS == ["https://a.app", "https://b.app"]


def test_startup_warnings_name_each_missing_secret():
    text = "\n".join(Settings({}).startup_warnings())
    for name in ("FLEET_API_KEY", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
                 "MANAGER_WHATSAPP_NUMBER", "DRIVER_WHATSAPP_MAPPING"):
        assert name in text
    assert "refused" in text


def test_startup_warnings_are_quiet_when_configured_and_loud_in_insecure_mode():
    ok = Settings({"FLEET_API_KEY": "k", "WHATSAPP_APP_SECRET": "s", "WHATSAPP_VERIFY_TOKEN": "v",
                   "MANAGER_WHATSAPP_NUMBER": "+15555550100",
                   "DRIVER_WHATSAPP_MAPPING": json.dumps({"A": "+15555550101"})})
    assert ok.startup_warnings() == []
    assert "ALLOW_INSECURE_DEV is ON" in "\n".join(Settings({"ALLOW_INSECURE_DEV": "true"}).startup_warnings())
    assert "PHONE_NUMBER_ID" in "\n".join(Settings({"WHATSAPP_TOKEN": "t"}).startup_warnings())
