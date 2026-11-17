import unittest
from urllib.parse import parse_qs, urlsplit

from streamlit.web.server.starlette import starlette_auth_routes as routes

from cognito_logout import install_cognito_logout_compatibility


class CognitoLogoutTests(unittest.TestCase):
    def test_cognito_logout_returns_to_home_without_token_in_url(self):
        install_cognito_logout_compatibility()
        url = routes.build_logout_url(
            end_session_endpoint="https://store.auth.us-east-1.amazoncognito.com/logout",
            client_id="client-123",
            post_logout_redirect_uri="http://localhost:8501/oauth2callback",
            id_token="private-token",
        )
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/logout")
        self.assertEqual(
            parse_qs(parts.query),
            {"client_id": ["client-123"], "logout_uri": ["http://localhost:8501/"]},
        )

    def test_other_providers_keep_standard_logout_url(self):
        install_cognito_logout_compatibility()
        url = routes.build_logout_url(
            end_session_endpoint="https://identity.example.com/logout",
            client_id="client-123",
            post_logout_redirect_uri="https://store.example.com/oauth2callback",
            id_token="private-token",
        )
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["post_logout_redirect_uri"],
                         ["https://store.example.com/oauth2callback"])
        self.assertEqual(params["id_token_hint"], ["private-token"])


if __name__ == "__main__":
    unittest.main()
