import os


def get_bearer_token() -> str:
    """Return the bearer token used by every API tool.

    Placeholder: replace the body with your real implementation (or import it
    from your existing module). It is called once per API request, so do any
    caching/refresh inside this function.
    """
    token = os.getenv("API_BEARER_TOKEN")
    if not token:
        raise RuntimeError("get_bearer_token is not implemented. Replace it in auth.py or set API_BEARER_TOKEN.")
    return token


def get_api_key(env_var: str = "API_KEY") -> str:
    """Return the static API key sent with operations that declare an apiKey security scheme."""
    key = os.getenv(env_var)
    if not key:
        raise RuntimeError(f"{env_var} is not set.")
    return key
