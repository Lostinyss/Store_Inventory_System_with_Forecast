import unittest

from site_auth import allowed_identity


class SiteAuthorizationTests(unittest.TestCase):
    def test_missing_allowlist_denies_access(self):
        self.assertFalse(allowed_identity({"sub": "user-1", "email": "a@example.com",
                                           "email_verified": True}, "", ""))

    def test_subject_allowlist_allows_exact_subject(self):
        self.assertTrue(allowed_identity({"sub": "user-1"}, "user-1,user-2", ""))
        self.assertFalse(allowed_identity({"sub": "user-10"}, "user-1", ""))

    def test_email_requires_provider_verification(self):
        user = {"sub": "other", "email": "A@Example.com", "email_verified": False}
        self.assertFalse(allowed_identity(user, "", "a@example.com"))
        user["email_verified"] = "true"
        self.assertTrue(allowed_identity(user, "", "a@example.com"))

    def test_missing_claims_are_denied(self):
        self.assertFalse(allowed_identity({}, "user-1", "a@example.com"))


if __name__ == "__main__":
    unittest.main()
