from tools.public_apis import geocode_city, get_country_info, get_weather

# Register every tool the agent may use. To add an API: create a module in
# this package, import its functions here and append them to the list.
ALL_TOOLS = [
    # Example tools (tools/example_api.py) are not registered until a real API is configured:
    # from tools.example_api import get_order, search_customers
    get_country_info,
    geocode_city,
    get_weather,
]
