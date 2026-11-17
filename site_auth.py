"""Require an authenticated identity before accessing the website."""

from __future__ import annotations

import streamlit as st


def require_login() -> None:
    """Stop the Streamlit run before any database, S3, SNS, or page operation."""
    if not st.user.get("is_logged_in", False):
        st.title("Store Forecast")
        st.write("Sign in to use the inventory and forecasting website.")
        if st.button("Sign in", type="primary"):
            try:
                st.login()
            except Exception as exc:
                st.error(f"Login is not configured or is unavailable: {exc}")
                st.info("Set the OIDC provider in .streamlit/secrets.toml on this server.")
        st.stop()
