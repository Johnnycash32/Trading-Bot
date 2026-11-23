"""SQLite database and authentication helpers for ORBITIQ."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "orbitiq.db"

TOKEN_EXPIRY_HOURS = 1
TOKEN_LENGTH = 32


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                reset_token TEXT,
                reset_expires TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    combined = salt + password
    digest = hashlib.pbkdf2_hmac("sha256", combined.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, stored_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    combined = salt + password
    digest = hashlib.pbkdf2_hmac("sha256", combined.encode("utf-8"), salt.encode("utf-8"), 100000)
    return secrets.compare_digest(digest.hex(), stored_digest)


def get_user_by_email(email: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    finally:
        conn.close()


def create_user(name: str, email: str, password: str) -> sqlite3.Row:
    conn = _connect()
    try:
        pw_hash = hash_password(password)
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            (name.strip(), email.lower().strip(), pw_hash),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    finally:
        conn.close()


def generate_reset_token(email: str) -> str | None:
    user = get_user_by_email(email)
    if not user:
        return None
    token = secrets.token_hex(TOKEN_LENGTH)
    expires = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS)).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_expires = ? WHERE email = ?",
            (token, expires, email.lower().strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def reset_password_with_token(token: str, new_password: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE reset_token = ?", (token,)
        ).fetchone()
        if not row:
            return False
        expires = row["reset_expires"]
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                if datetime.now(timezone.utc) > exp_dt:
                    conn.execute(
                        "UPDATE users SET reset_token = NULL, reset_expires = NULL WHERE id = ?",
                        (row["id"],),
                    )
                    conn.commit()
                    return False
            except ValueError:
                return False
        pw_hash = hash_password(new_password)
        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_expires = NULL WHERE id = ?",
            (pw_hash, row["id"]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def send_reset_email(to_email: str, reset_link: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", "noreply@orbitiq.local")

    subject = "ORBITIQ — Password Reset"
    body = (
        "You requested a password reset for your ORBITIQ account.\n\n"
        f"Click the link below to reset your password:\n{reset_link}\n\n"
        "This link expires in 1 hour.\n\n"
        "If you did not request this, ignore this email."
    )

    if not smtp_host or not smtp_user:
        print(f"[ORBITIQ] Reset email (no SMTP configured) to {to_email}: {reset_link}")
        return True

    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = to_email
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to_email], msg.as_string())
        return True
    except Exception as exc:
        print(f"[ORBITIQ] SMTP send failed ({exc}); falling back to terminal print")
        print(f"[ORBITIQ] Reset email to {to_email}: {reset_link}")
        return True