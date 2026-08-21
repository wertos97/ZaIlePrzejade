"""Ticket cost calculation for MPK Kraków routes.

Pricing is loaded from pricing.json and used by calculate_cost and
calculate_route_cost to compute regular and reduced fares.
"""

import json
import math
import os

# Module-level pricing constants — set by init_pricing()
BASE_DISTANCE: float
BASE_COST_REGULAR: float
BASE_COST_REDUCED: float
SEGMENT_DISTANCE: float
SEGMENT_COST_REGULAR: float
SEGMENT_COST_REDUCED: float
MAX_COST_REGULAR: float
MAX_COST_REDUCED: float
MAX_DAILY_COST_REGULAR: float
MAX_DAILY_COST_REDUCED: float

_DEFAULT_PRICING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'pricing.json'
)


def init_pricing(pricing_path: str | None = None) -> None:
    """Load pricing.json and set module-level constants."""
    global BASE_DISTANCE, BASE_COST_REGULAR, BASE_COST_REDUCED
    global SEGMENT_DISTANCE, SEGMENT_COST_REGULAR, SEGMENT_COST_REDUCED
    global MAX_COST_REGULAR, MAX_COST_REDUCED
    global MAX_DAILY_COST_REGULAR, MAX_DAILY_COST_REDUCED

    path = pricing_path or _DEFAULT_PRICING_PATH
    with open(path, 'r', encoding='utf-8') as f:
        pricing = json.load(f)

    BASE_DISTANCE = pricing['base_distance_km']
    BASE_COST_REGULAR = pricing['base_cost_regular']
    BASE_COST_REDUCED = pricing['base_cost_reduced']
    SEGMENT_DISTANCE = pricing['segment_distance_km']
    SEGMENT_COST_REGULAR = pricing['segment_cost_regular']
    SEGMENT_COST_REDUCED = pricing['segment_cost_reduced']
    MAX_COST_REGULAR = pricing['max_cost_regular']
    MAX_COST_REDUCED = pricing['max_cost_reduced']
    MAX_DAILY_COST_REGULAR = pricing['max_daily_cost_regular']
    MAX_DAILY_COST_REDUCED = pricing['max_daily_cost_reduced']


def calculate_cost(distance_km: float) -> tuple[float, float]:
    """Calculate ticket cost based on distance.

    Returns (cost_regular, cost_reduced).
    """
    if distance_km <= 0:
        return 0.0, 0.0
    if distance_km <= BASE_DISTANCE:
        return BASE_COST_REGULAR, BASE_COST_REDUCED
    additional_distance = distance_km - BASE_DISTANCE
    additional_segments = math.ceil(additional_distance / SEGMENT_DISTANCE)
    cost_regular = BASE_COST_REGULAR + additional_segments * SEGMENT_COST_REGULAR
    cost_reduced = BASE_COST_REDUCED + additional_segments * SEGMENT_COST_REDUCED
    cost_regular = min(cost_regular, MAX_COST_REGULAR)
    cost_reduced = min(cost_reduced, MAX_COST_REDUCED)
    return round(cost_regular, 2), round(cost_reduced, 2)


def calculate_route_cost(segments: list[dict]) -> tuple[float, float]:
    """Calculate total route cost as the sum of individual segment (ride) costs.

    Each segment (ride between transfers) is a separate ticket, priced from zero.
    The daily limit caps the total cost (after reaching it, further rides are free).

    Returns (total_regular, total_reduced).
    """
    total_regular = 0.0
    total_reduced = 0.0
    for seg in segments:
        reg, red = calculate_cost(seg.get('distance', 0.0))
        total_regular += reg
        total_reduced += red
    # Apply daily limit
    total_regular = min(total_regular, MAX_DAILY_COST_REGULAR)
    total_reduced = min(total_reduced, MAX_DAILY_COST_REDUCED)
    return round(total_regular, 2), round(total_reduced, 2)


# Initialize pricing on import using the default pricing.json
init_pricing()
