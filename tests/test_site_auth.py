import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import site_auth


class StopRun(Exception):
    pass


class SiteAuthenticationTests(unittest.TestCase):
    def test_anonymous_user_cannot_continue_to_app(self):
        fake_streamlit = SimpleNamespace(
            user={"is_logged_in": False},
            title=Mock(),
            write=Mock(),
            button=Mock(return_value=False),
            stop=Mock(side_effect=StopRun),
        )
        with patch.object(site_auth, "st", fake_streamlit):
            with self.assertRaises(StopRun):
                site_auth.require_login()
        fake_streamlit.stop.assert_called_once()

    def test_authenticated_user_can_continue_without_allowlist(self):
        fake_streamlit = SimpleNamespace(
            user={"is_logged_in": True, "sub": "any-pool-user"},
            stop=Mock(side_effect=StopRun),
        )
        with patch.object(site_auth, "st", fake_streamlit):
            site_auth.require_login()
        fake_streamlit.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
