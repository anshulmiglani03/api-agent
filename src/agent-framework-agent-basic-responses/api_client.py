from typing import Any

import httpx

from auth import get_bearer_token

_TIMEOUT = 30.0


async def call_api(
    method: str, url: str, *, params: dict[str, Any] | None = None, json: Any = None, auth: bool = True
) -> Any:
    """Call an API with the shared bearer token and return the parsed response.

    Pass auth=False for public third-party APIs so the token is never sent to them.
    Errors are returned as a dict instead of raised, so the model can read
    them and decide what to do next (retry, ask the user, try another tool).
    """
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {get_bearer_token()}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, url, params=params, json=json, headers=headers)
            response.raise_for_status()
            return response.json() if response.content else {}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:500]}
    except httpx.HTTPError as e:
        return {"error": type(e).__name__, "detail": str(e)}
