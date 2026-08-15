# linkedin_cli/api/voyager.py
def parse_linkedin_voyager_response(response_data: dict) -> dict:
    """Parse raw LinkedIn Voyager API JSON response."""
    if not isinstance(response_data, dict):
        return {}
    return response_data.get("data", response_data)
