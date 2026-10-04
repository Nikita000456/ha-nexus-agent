"""Synthetic regression checks; no Home Assistant connection is made."""
import unittest
from unittest.mock import patch

from tools import supervisor


class SupervisorRedactionTests(unittest.TestCase):
    def test_plural_secret_names_without_pass_substring_false_positives(self):
        for key in ("passwords", "credentials", "tokens", "api_keys", "apikey", "privateKeys"):
            with self.subTest(key=key):
                self.assertTrue(supervisor._secret_key(key))
        for key in ("passenger", "bypass_cache", "compass", "address", "hotkey", "monkey"):
            with self.subTest(key=key):
                self.assertFalse(supervisor._secret_key(key))

    def test_get_addon_redacts_nested_options_without_changing_source(self):
        payload = {"data": {"options": {
            "api_keys": ["fake-first", "fake-second"],
            "mqtt": {"db_pwd": "fake-pwd", "broker": "mqtt://user:fake-pass@host:1883",
                     "passenger": "allowed", "bypass_cache": True,
                     "opaque": "fake-schema-secret"},
            "enabled": True,
        }, "schema": [{"name": "mqtt", "type": "schema", "schema": [
            {"name": "opaque", "type": "string", "format": "password"}]}]}}
        with patch.object(supervisor, "_supervisor_request", return_value=payload):
            result = supervisor.get_addon("synthetic")
        options = result["data"]["options"]
        self.assertEqual(options["api_keys"], [supervisor._REDACTED] * 2)
        self.assertEqual(options["mqtt"]["db_pwd"], supervisor._REDACTED)
        self.assertEqual(options["mqtt"]["broker"], supervisor._REDACTED)
        self.assertEqual(options["mqtt"]["opaque"], supervisor._REDACTED)
        self.assertEqual(options["mqtt"]["passenger"], "allowed")
        self.assertTrue(options["mqtt"]["bypass_cache"])
        self.assertEqual(payload["data"]["options"]["api_keys"], ["fake-first", "fake-second"])
        self.assertEqual({x["reason"] for x in result["redacted_fields"]},
                         {"key_name_heuristic", "schema_password", "credentials_in_url"})

    def test_setter_preserves_markers_and_does_not_echo_error_detail(self):
        stored = {"data": {"options": {"nested": {"password": "fake-stored"},
                                        "tokens": ["fake-one", "fake-two"]}}}
        sent = {"nested": {"password": supervisor._REDACTED},
                "tokens": [supervisor._REDACTED, "replacement"]}
        with patch.object(supervisor, "_supervisor_request", side_effect=[
            stored, {"error": "HTTP 400", "detail": "fake-stored fake-one"}]) as request:
            result = supervisor.set_addon_options("synthetic", sent)
        self.assertEqual(request.call_args_list[1].kwargs["json"]["options"],
                         {"nested": {"password": "fake-stored"},
                          "tokens": ["fake-one", "replacement"]})
        self.assertNotIn("fake-stored", str(result))
        self.assertNotIn("fake-one", str(result))
        self.assertEqual(sent["nested"]["password"], supervisor._REDACTED)

    def test_setter_does_not_echo_new_explicit_secret_on_error(self):
        with patch.object(supervisor, "_supervisor_request", return_value={
                "error": "HTTP 400", "detail": "invalid fake-new-secret"}) as request:
            result = supervisor.set_addon_options("synthetic", {"password": "fake-new-secret"})
        request.assert_called_once()
        self.assertNotIn("fake-new-secret", str(result))
        self.assertEqual(result["error"], "options_update_failed")

    def test_setter_rejects_ambiguous_or_missing_placeholder(self):
        stored = {"data": {"options": {"tokens": ["fake-one", "fake-two"]}}}
        for options in ({"tokens": [supervisor._REDACTED]},
                        {"missing": supervisor._REDACTED}):
            with self.subTest(options=options), patch.object(
                    supervisor, "_supervisor_request", return_value=stored) as request:
                result = supervisor.set_addon_options("synthetic", options)
                self.assertEqual(result["error"], "redaction_marker_unresolvable")
                request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
