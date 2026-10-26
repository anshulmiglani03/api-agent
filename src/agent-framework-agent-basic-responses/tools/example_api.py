"""Example API tools. Copy this file for each new API and edit the URLs/params.

The function name, docstring and Annotated descriptions are what the model
sees, so keep them specific about when to use the tool and what it returns.
"""

import os
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from api_client import call_api

BASE_URL = os.getenv("EXAMPLE_API_BASE_URL", "https://api.example.com/v1")


async def search_customers(
    query: Annotated[str, Field(description="Free-text search, e.g. a customer name or email.")],
    limit: Annotated[int, Field(description="Maximum number of results to return.", ge=1, le=50)] = 10,
) -> Any:
    """Search customers by name or email. Returns a list of matching customer records."""
    return await call_api("GET", f"{BASE_URL}/customers", params={"q": query, "limit": limit})


async def get_order(
    order_id: Annotated[str, Field(description="The order identifier, e.g. 'ORD-1001'.")],
) -> Any:
    """Get the details and status of a single order by its identifier."""
    return await call_api("GET", f"{BASE_URL}/orders/{quote(order_id, safe='')}")