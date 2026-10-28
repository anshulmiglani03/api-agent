import os
import threading
import time

_REFRESH_MARGIN = 60  # seconds before expiry at which a new token is fetched
_DEFAULT_TTL = 300  # assumed lifetime in seconds when the response has no usable expires_in

_lock = threading.Lock()
_cached_token: str | None = None
_cached_expiry = 0.0  # time.monotonic() value at which the cached token expires


def _fetch_token() -> dict:
    """Generate a brand-new token and return the OAuth-style dict {access_token, token_type, expires_in}.

    Placeholder: replace the body with your real token generation (or call your
    existing module). It runs only when no valid cached token exists.
    """
    token = os.getenv("API_BEARER_TOKEN")
    if not token:
        raise RuntimeError("_fetch_token is not implemented. Replace it in auth.py or set API_BEARER_TOKEN.")
    return {"access_token": token, "token_type": "Bearer"}


def get_bearer_token() -> str:
    """Return the bearer token used by every API tool, reusing it until shortly before it expires."""
    global _cached_token, _cached_expiry
    with _lock:
        if _cached_token is None or time.monotonic() >= _cached_expiry - _REFRESH_MARGIN:
            response = _fetch_token()
            try:
                ttl = float(response["expires_in"])
            except (KeyError, TypeError, ValueError):
                ttl = _DEFAULT_TTL
            _cached_token, _cached_expiry = response["access_token"], time.monotonic() + ttl
        return _cached_token


def get_api_key(env_var: str = "API_KEY") -> str:
    """Return the static API key sent with operations that declare an apiKey security scheme."""
    key = os.getenv(env_var)
    if not key:
        raise RuntimeError(f"{env_var} is not set.")
    return key
