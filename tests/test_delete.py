"""Delete permissions are enforced in code, against the session's sender."""

import unittest

from support import PluginTestCase, bind_session


class DeletePermissionTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.entry_id = self.share_as("author-1", "an entry")["id"]

    def assert_kept(self, result):
        self.assertIn("error", result)
        self.assertEqual(len(self.backend.stored_points()), 1)

    def test_author_can_delete(self):
        bind_session(user_id="author-1")
        self.assertEqual(self.call(self.tools.team_delete, {"id": self.entry_id})["status"], "deleted")
        self.assertEqual(self.backend.stored_points(), [])

    def test_admin_can_delete(self):
        bind_session(user_id="admin-1")
        self.assertEqual(self.call(self.tools.team_delete, {"id": self.entry_id})["status"], "deleted")
        self.assertEqual(self.backend.stored_points(), [])

    def test_other_user_cannot_delete(self):
        bind_session(user_id="other-1")
        result = self.call(self.tools.team_delete, {"id": self.entry_id})
        self.assertIn("only the entry's author or an admin", result["error"])
        self.assert_kept(result)

    def test_arguments_cannot_grant_permission(self):
        bind_session(user_id="other-1")
        args = {"id": self.entry_id, "author_id": "telegram:author-1", "user_id": "admin-1",
                "admin": True, "is_admin": True, "force": True}
        self.assert_kept(self.call(self.tools.team_delete, args))

    def test_context_keywords_cannot_grant_permission(self):
        bind_session(user_id="other-1")
        self.assert_kept(self.call(self.tools.team_delete, {"id": self.entry_id}, user_id="admin-1"))

    def test_matching_display_name_grants_nothing(self):
        bind_session(user_id="other-1", user_name="author-1")
        self.assert_kept(self.call(self.tools.team_delete, {"id": self.entry_id}))

    def test_same_user_id_on_other_platform_is_not_the_author(self):
        self.write_config(access={"allowed_platforms": ["telegram", "discord"], "admins": ["telegram:admin-1"]})
        bind_session(platform="discord", user_id="author-1")
        self.assert_kept(self.call(self.tools.team_delete, {"id": self.entry_id}))

    def test_admin_id_on_other_platform_is_not_admin(self):
        self.write_config(access={"allowed_platforms": ["telegram", "discord"], "admins": ["telegram:admin-1"]})
        bind_session(platform="discord", user_id="admin-1")
        self.assert_kept(self.call(self.tools.team_delete, {"id": self.entry_id}))

    def test_entry_without_author_is_admin_only(self):
        self.backend.stored_points()[0]["payload"].pop("author_id")
        bind_session(user_id="other-1")
        self.assert_kept(self.call(self.tools.team_delete, {"id": self.entry_id}))
        bind_session(user_id="admin-1")
        self.assertEqual(self.call(self.tools.team_delete, {"id": self.entry_id})["status"], "deleted")

    def test_unknown_id(self):
        bind_session(user_id="admin-1")
        result = self.call(self.tools.team_delete, {"id": "00000000-0000-4000-8000-000000000000"})
        self.assertIn("no team memory entry", result["error"])

    def test_invalid_ids_never_reach_qdrant(self):
        bind_session(user_id="admin-1")
        before = len(self.backend.requests)
        for bad in ("../../collections/other", "not-a-uuid", "", None, 7, ["x"]):
            self.assertIn("error", self.call(self.tools.team_delete, {"id": bad}))
        self.assertEqual(len(self.backend.requests), before)
        self.assertEqual(len(self.backend.stored_points()), 1)


if __name__ == "__main__":
    unittest.main()
