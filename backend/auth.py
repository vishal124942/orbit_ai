"""
Google OAuth Authentication
Validates Google ID tokens and issues JWT session tokens.
"""

import os
import jwt
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-please-use-random-256-bit-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")


def verify_google_token(credential: str) -> Optional[Dict]:
    """
    Verify a Google ID token (from Google Sign-In).
    Returns user info dict or None if invalid.
    """
    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
        return {
            "sub": idinfo["sub"],          # Google user ID (stable, use as user.id)
            "email": idinfo["email"],
            "name": idinfo.get("name", ""),
            "picture": idinfo.get("picture", ""),
            "email_verified": idinfo.get("email_verified", False),
        }
    except Exception as e:
        print(f"[Auth] Google token verification failed: {e}")
        return None


def create_session_token(user_id: str) -> Dict:
    """Create a JWT session token for the authenticated user."""
    token_id = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)

    payload = {
        "sub": user_id,
        "jti": token_id,
        "exp": expires_at,
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "token_id": token_id,
    }


def decode_session_token(token: str) -> Optional[Dict]:
    """Decode and validate a JWT session token. Returns payload or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def extract_token_from_header(authorization: str) -> Optional[str]:
    """Extract bearer token from Authorization header."""
    if not authorization:
        return None
    parts = authorization.split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None