"""Adapt Streamlit's OIDC logout URL to Cognito's logout endpoint."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def install_cognito_logout_compatibility() -> None:
    """Use Cognito's ``logout_uri`` while leaving other OIDC providers alone."""
    from streamlit.web.server.starlette import starlette_auth_routes as routes

    current_builder = routes.build_logout_url
    if getattr(current_builder, "_store_cognito_compatibility", False):
        return

    def build_logout_url(
        end_session_endpoint: str,
        client_id: str,
        post_logout_redirect_uri: str,
        id_token: str | None = None,
    ) -> str:
        endpoint = urlsplit(end_session_endpoint)
        hostname = (endpoint.hostname or "").lower()
        if not (hostname.endswith(".amazoncognito.com") and endpoint.path == "/logout"):
            return current_builder(
                end_session_endpoint=end_session_endpoint,
                client_id=client_id,
                post_logout_redirect_uri=post_logout_redirect_uri,
                id_token=id_token,
            )

        callback = urlsplit(post_logout_redirect_uri)
        home_url = urlunsplit((callback.scheme, callback.netloc, "/", "", ""))
        params = dict(parse_qsl(endpoint.query))
        params.pop("post_logout_redirect_uri", None)
        params.pop("id_token_hint", None)
        params.update({"client_id": client_id, "logout_uri": home_url})
        return urlunsplit(endpoint._replace(query=urlencode(params)))

    build_logout_url._store_cognito_compatibility = True  # type: ignore[attr-defined]
    routes.build_logout_url = build_logout_url
