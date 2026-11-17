"""Sitewide OIDC authentication and explicit account authorization."""

from __future__ import annotations

import os
from collections.abc import Mapping

import streamlit as st


def allowed_identity(user: Mapping[str, object], subjects: str, emails: str) -> bool:
    """Authorize a stable OIDC subject or a provider-verified email address."""
    permitted_subjects = {value.strip() for value in subjects.split(",") if value.strip()}
    permitted_emails = {value.strip().casefold() for value in emails.split(",") if value.strip()}
    subject = str(user.get("sub") or "").strip()
    if subject and subject in permitted_subjects:
        return True
    verified = user.get("email_verified") is True or str(user.get("email_verified")).lower() == "true"
    email = str(user.get("email") or "").strip().casefold()
    return bool(verified and email and email in permitted_emails)


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

    if not allowed_identity(
        st.user,
        os.environ.get("STORE_ALLOWED_SUBJECTS", ""),
        os.environ.get("STORE_ALLOWED_EMAILS", ""),
    ):
        st.error("This account is not allowed to use Store Forecast.")
        subject = st.user.get("sub")
        if subject:
            st.caption(f"Account subject: {subject}")
        if st.button("Sign out"):
            st.logout()
        st.stop()
