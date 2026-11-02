"""Config safety: file mode, placeholders, and no secrets in messages."""

import json
import os
import stat
import unittest

from support import QDRANT_KEY, ROOT, PluginTestCase, config_dict


class ConfigFileTests(PluginTestCase):
    def test_valid_config_loads(self):
        config = self.config_module.load_config()
        self.assertEqual(config.qdrant.collection, "team_memory")
        self.assertTrue(config.confirm_tool_share)
        self.assertFalse(config.auto_recall.enabled)

    def test_group_or_world_access_refused(self):
        for mode in (0o640, 0o604, 0o660, 0o644):
            os.chmod(self.config_file, mode)
            self.config_module.reset_cache()
            with self.assertRaisesRegex(self.config_module.ConfigError, "chmod 600"):
                self.config_module.load_config()
            self.assertFalse(self.tools.is_available())

    def test_loosened_mode_is_noticed_without_a_restart(self):
        self.config_module.load_config()
        os.chmod(self.config_file, 0o644)
        with self.assertRaises(self.config_module.ConfigError):
            self.config_module.load_config()

    def test_changed_file_is_reloaded(self):
        self.assertEqual(self.config_module.load_config().search_limit, 5)
        self.config_file.write_text(json.dumps(config_dict(self.backend.port, search={"limit": 9})))
        os.chmod(self.config_file, 0o600)
        self.assertEqual(self.config_module.load_config().search_limit, 9)

    def test_missing_file_hides_tools(self):
        self.config_file.unlink()
        self.config_module.reset_cache()
        self.assertFalse(self.tools.is_available())

    def test_invalid_json_message_has_no_content(self):
        self.config_file.write_text('{"qdrant": {"api_key": "' + QDRANT_KEY + '" oops')
        os.chmod(self.config_file, 0o600)
        self.config_module.reset_cache()
        with self.assertRaises(self.config_module.ConfigError) as caught:
            self.config_module.load_config()
        self.assertNotIn(QDRANT_KEY, str(caught.exception))

    def test_validation_errors_have_no_secrets(self):
        data = config_dict(self.backend.port)
        data["qdrant"]["url"] = "not a url"
        with self.assertRaises(self.config_module.ConfigError) as caught:
            self.config_module.parse_config(data)
        self.assertNotIn(QDRANT_KEY, str(caught.exception))

    def test_url_with_credentials_refused(self):
        data = config_dict(self.backend.port)
        data["qdrant"]["url"] = "http://user:pass@127.0.0.1:6333"
        with self.assertRaisesRegex(self.config_module.ConfigError, "credentials"):
            self.config_module.parse_config(data)

    def test_unsafe_collection_name_refused(self):
        data = config_dict(self.backend.port)
        data["qdrant"]["collection"] = "../mem0"
        with self.assertRaisesRegex(self.config_module.ConfigError, "collection"):
            self.config_module.parse_config(data)

    def test_platforms_are_required(self):
        data = config_dict(self.backend.port)
        data["access"]["allowed_platforms"] = []
        with self.assertRaisesRegex(self.config_module.ConfigError, "allowed_platforms"):
            self.config_module.parse_config(data)

    def test_admins_must_include_platform(self):
        data = config_dict(self.backend.port)
        data["access"]["admins"] = ["12345"]
        with self.assertRaisesRegex(self.config_module.ConfigError, "platform:user_id"):
            self.config_module.parse_config(data)

    def test_max_chars_has_an_upper_limit(self):
        data = config_dict(self.backend.port, sharing={"max_chars": self.config_module.MAX_SHAREABLE_CHARS + 1})
        with self.assertRaisesRegex(self.config_module.ConfigError, "max_chars"):
            self.config_module.parse_config(data)


class ExampleConfigTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.example = json.loads((ROOT / "team_memory.example.json").read_text())

    def test_unedited_example_is_refused(self):
        with self.assertRaisesRegex(self.config_module.ConfigError, "placeholder"):
            self.config_module.parse_config(self.example)

    def test_edited_example_is_valid_and_keeps_defaults(self):
        self.example["qdrant"].update(url="http://qdrant.invalid:6333", api_key="k")
        self.example["embedding"]["base_url"] = "http://embed.invalid:11434/v1"
        self.example["access"]["admins"] = ["telegram:1"]
        config = self.config_module.parse_config(self.example)
        self.assertEqual(config.embedding.dimensions, 768)
        self.assertEqual(config.qdrant.collection, "team_memory")
        self.assertTrue(config.confirm_tool_share)
        self.assertFalse(config.auto_recall.enabled)


if __name__ == "__main__":
    unittest.main()
