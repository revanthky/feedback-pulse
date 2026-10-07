"""Feedback Pulse - a Streamlit app for LLM-powered customer feedback analysis.

Pages: Settings, Analyze, Dashboard (sidebar navigation).
Config (Postgres / MinIO) comes from environment variables.
LLM config (base URL, token, model) is managed on the Settings page and
persisted in Postgres table app_settings, falling back to env vars
LLM_BASE_URL, LLM_TOKEN, LLM_MODEL when nothing is saved.
"""

import io
import json
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
import streamlit as st
from minio import Minio
from minio.error import S3Error

# --------------------------------------------------------------------------
# Configuration (env vars)
# --------------------------------------------------------------------------

# DATABASE_URL (a full postgresql:// DSN) takes precedence when set — this is how the
# DKubeX platform injects an auto-provisioned postgres dependency. Discrete PG_* vars remain
# supported for local/docker-compose testing.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "feedback")
PG_USER = os.environ.get("PG_USER", "feedback")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "feedback")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "feedback")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").strip().lower() in ("1", "true", "yes")

ENV_LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
ENV_LLM_TOKEN = os.environ.get("LLM_TOKEN", "")
ENV_LLM_MODEL = os.environ.get("LLM_MODEL", "")

SENTIMENTS = ["positive", "neutral", "negative"]
CATEGORIES = ["pricing", "quality", "delivery", "support", "other"]


# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

def get_pg_conn():
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
    else:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD,
        )
    conn.autocommit = True
    return conn


def init_db():
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id SERIAL PRIMARY KEY,
                    text TEXT,
                    sentiment TEXT,
                    category TEXT,
                    summary TEXT,
                    minio_key TEXT,
                    created_at TIMESTAMPTZ
                )
                """
            )
    finally:
        conn.close()


def get_setting(key: str) -> str:
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else ""
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
    finally:
        conn.close()


def get_llm_settings():
    base_url = get_setting("llm_base_url") or ENV_LLM_BASE_URL
    token = get_setting("llm_token") or ENV_LLM_TOKEN
    model = get_setting("llm_model") or ENV_LLM_MODEL
    return base_url, token, model


def insert_feedback(text, sentiment, category, summary, minio_key):
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO feedback (text, sentiment, category, summary, minio_key, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (text, sentiment, category, summary, minio_key, datetime.now(timezone.utc)),
            )
    finally:
        conn.close()


def query_group_counts(column: str):
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {column}, COUNT(*) FROM feedback GROUP BY {column} ORDER BY {column}")
            return cur.fetchall()
    finally:
        conn.close()


def query_recent(limit: int = 20):
    conn = get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, text, sentiment, category, summary, minio_key, created_at "
                "FROM feedback ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# MinIO helpers
# --------------------------------------------------------------------------

def get_minio_client():
    # MINIO_ENDPOINT may be given as a bare "host:port" (local/docker-compose testing) or as a
    # full "http(s)://host:port" URL (the form the DKubeX platform's minio dependency injects).
    # Strip any scheme and let it decide `secure` when present.
    endpoint = MINIO_ENDPOINT
    secure = MINIO_SECURE
    if "://" in endpoint:
        scheme, endpoint = endpoint.split("://", 1)
        secure = scheme == "https"
    return Minio(
        endpoint,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=secure,
    )


def save_feedback_to_minio(client, key: str, payload: dict):
    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)
    data = json.dumps(payload, indent=2).encode("utf-8")
    client.put_object(
        MINIO_BUCKET, key, io.BytesIO(data), length=len(data), content_type="application/json",
    )


# --------------------------------------------------------------------------
# LLM helpers
# --------------------------------------------------------------------------

def auth_headers(token: str):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z]*\s*\n?(.*)\n?```$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def normalize_sentiment(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in SENTIMENTS else "neutral"


def normalize_category(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in CATEGORIES else "other"


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

def check_startup():
    errors = []
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Postgres is unreachable: {exc}")

    try:
        client = get_minio_client()
        client.list_buckets()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"MinIO is unreachable: {exc}")

    return errors


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def page_settings():
    st.title("Settings")

    saved_base_url, saved_token, saved_model = get_llm_settings()

    if "model_options" not in st.session_state:
        st.session_state["model_options"] = []

    base_url = st.text_input(
        "LLM base URL",
        value=saved_base_url,
        placeholder="http://<host>:<port>/v1",
        help="Must include /v1",
    )

    token_input = st.text_input(
        "API token (optional)",
        value="",
        type="password",
        placeholder="Leave blank to keep existing token",
    )
    if saved_token:
        masked = "*" * max(len(saved_token) - 4, 0) + saved_token[-4:]
        st.caption(f"Saved token: {masked}")

    fetch_disabled = not base_url.strip()
    if st.button("Fetch models", disabled=fetch_disabled):
        clean_url = base_url.strip().rstrip("/")
        effective_token = token_input or saved_token
        try:
            resp = requests.get(
                f"{clean_url}/models",
                headers=auth_headers(effective_token),
                timeout=10,
            )
            if resp.ok:
                data = resp.json().get("data", [])
                ids = [item.get("id") for item in data if item.get("id")]
                st.session_state["model_options"] = ids
                st.success(f"Found {len(ids)} models")
            else:
                st.error(f"HTTP {resp.status_code}: {resp.text}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error fetching models: {exc}")

    options = list(st.session_state["model_options"])
    default_index = 0
    if saved_model:
        if saved_model not in options:
            options.append(saved_model)
        default_index = options.index(saved_model)

    model = st.selectbox(
        "Model",
        options=options,
        index=default_index if options else None,
        accept_new_options=True,
    )

    if st.button("Save"):
        clean_url = base_url.strip().rstrip("/")
        effective_token = token_input or saved_token
        set_setting("llm_base_url", clean_url)
        set_setting("llm_token", effective_token)
        set_setting("llm_model", model or "")
        st.success("Settings saved")

    if st.button("Test connection"):
        clean_url = base_url.strip().rstrip("/")
        effective_token = token_input or saved_token
        try:
            resp = requests.post(
                f"{clean_url}/chat/completions",
                headers=auth_headers(effective_token),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                timeout=15,
            )
            if resp.ok:
                st.success("Connection OK")
            else:
                st.error(f"HTTP {resp.status_code}: {resp.text}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error: {exc}")


def page_analyze():
    st.title("Analyze")

    base_url, token, model = get_llm_settings()
    if not base_url or not model:
        st.warning("LLM base URL or model is not configured. Open Settings to configure it.")
        st.stop()

    text = st.text_area("Customer feedback", height=150)
    analyze_disabled = not text.strip()

    if st.button("Analyze", disabled=analyze_disabled):
        system_prompt = (
            "You are a feedback analysis assistant. Given a piece of customer "
            "feedback, respond with ONLY a JSON object, no other text, matching "
            'exactly this schema: {"sentiment": "positive" | "neutral" | '
            '"negative", "category": "pricing" | "quality" | "delivery" | '
            '"support" | "other", "summary": "one short sentence"}'
        )
        raw_content = ""
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=auth_headers(token),
                json={
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                },
                timeout=60,
            )
            if not resp.ok:
                st.error(f"HTTP {resp.status_code}: {resp.text}")
                return
            raw_content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error calling LLM: {exc}")
            return

        cleaned = strip_code_fences(raw_content)
        try:
            parsed = json.loads(cleaned)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed to parse model response as JSON: {exc}")
            st.text_area("Raw response", value=raw_content, height=120)
            return

        sentiment = normalize_sentiment(parsed.get("sentiment", ""))
        category = normalize_category(parsed.get("category", ""))
        summary = parsed.get("summary", "")

        st.subheader("Result")
        st.json({"sentiment": sentiment, "category": category, "summary": summary})

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        short_uuid = uuid.uuid4().hex[:8]
        minio_key = f"feedback/{timestamp}_{short_uuid}.json"

        payload = {
            "text": text,
            "result": {"sentiment": sentiment, "category": category, "summary": summary},
        }

        try:
            client = get_minio_client()
            save_feedback_to_minio(client, minio_key, payload)
        except (S3Error, Exception) as exc:  # noqa: BLE001
            st.error(f"Failed to save to MinIO: {exc}")
            return

        try:
            insert_feedback(text, sentiment, category, summary, minio_key)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed to save to Postgres: {exc}")
            return

        st.success(f"Saved feedback (MinIO key: {minio_key})")


def page_dashboard():
    st.title("Dashboard")

    try:
        sentiment_counts = query_group_counts("sentiment")
        category_counts = query_group_counts("category")
        recent = query_recent(20)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to query Postgres: {exc}")
        return

    st.subheader("Count by sentiment")
    if sentiment_counts:
        st.bar_chart({"count": {row[0]: row[1] for row in sentiment_counts}})
    else:
        st.info("No data yet.")

    st.subheader("Count by category")
    if category_counts:
        st.bar_chart({"count": {row[0]: row[1] for row in category_counts}})
    else:
        st.info("No data yet.")

    st.subheader("20 most recent entries")
    if recent:
        st.dataframe(recent, width="stretch")
    else:
        st.info("No data yet.")


# --------------------------------------------------------------------------
# DKubeX platform integration (theming, identity) — no-ops outside the platform
# --------------------------------------------------------------------------

def inject_dkubex_theme_sync():
    """Follow the DKubeX shell's theme preference.

    Reads localStorage['dkubex-ui-theme'] ("dark" | "light" | "system", default "dark"),
    resolves "system" via prefers-color-scheme, and overrides Streamlit's CSS variables to
    match. Stays in sync with the platform's theme toggle via the "storage" event. This is the
    only theme key read or written — the platform toggle remains the single source of truth.
    """
    st.iframe(
        """
        <script>
        (function() {
          const DARK = {bg: "#0e1117", bgSecondary: "#262730", text: "#fafafa"};
          const LIGHT = {bg: "#ffffff", bgSecondary: "#f0f2f6", text: "#31333f"};

          function resolve(pref) {
            if (pref === "system") {
              return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
            }
            return pref === "light" ? "light" : "dark";
          }

          function apply() {
            const pref = window.parent.localStorage.getItem("dkubex-ui-theme") || "dark";
            const resolved = resolve(pref);
            const palette = resolved === "light" ? LIGHT : DARK;
            const doc = window.parent.document;
            doc.documentElement.style.setProperty("--background-color", palette.bg);
            doc.documentElement.style.setProperty("--secondary-background-color", palette.bgSecondary);
            doc.documentElement.style.setProperty("--text-color", palette.text);
            doc.documentElement.setAttribute("data-theme", resolved);
          }

          apply();
          window.parent.addEventListener("storage", function(e) {
            if (e.key === "dkubex-ui-theme") apply();
          });
        })();
        </script>
        """,
        height=1,
    )


def dkubex_user_caption():
    """Show the platform-authenticated user, if the app is running behind the gateway.

    The app never implements its own login — the gateway already authenticated the request
    before it reached here; this only reflects that identity back in the UI.
    """
    headers = getattr(st.context, "headers", None) or {}
    user = headers.get("X-Auth-Request-User")
    if user:
        st.sidebar.caption(f"Signed in as {user}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Feedback Pulse", layout="wide")
    inject_dkubex_theme_sync()
    st.sidebar.title("Feedback Pulse")
    dkubex_user_caption()
    page = st.sidebar.radio("Navigate", ["Settings", "Analyze", "Dashboard"])

    errors = check_startup()
    if errors:
        for err in errors:
            st.error(err)
        st.stop()

    if page == "Settings":
        page_settings()
    elif page == "Analyze":
        page_analyze()
    elif page == "Dashboard":
        page_dashboard()


if __name__ == "__main__":
    main()
