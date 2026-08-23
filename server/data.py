"""
Data loading module for MPK Kraków Ticket Cost Calculator.

Loads all processed GTFS data and builds lookup structures used by the server.
All data structures are module-level globals available to other server modules.
"""

import json
import logging
import os
import re
from collections import defaultdict

logger = logging.getLogger('mpk.data')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')

# ============================================================
# Load processed data
# ============================================================
logger.info("Loading processed data...")

with open(os.path.join(PROCESSED_DIR, 'stops.json'), encoding='utf-8') as f:
    stops_list = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'adjacency.json'), encoding='utf-8') as f:
    adjacency_raw = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'routes.json'), encoding='utf-8') as f:
    routes_list = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'shapes.json'), encoding='utf-8') as f:
    route_shapes = json.load(f)

# Feed metadata (version, validity period, publisher) - optional
_metadata_path = os.path.join(PROCESSED_DIR, 'metadata.json')
if os.path.isfile(_metadata_path):
    with open(_metadata_path, encoding='utf-8') as f:
        feed_metadata = json.load(f)
else:
    feed_metadata = {}

logger.info("Processed data loaded", extra={"stops": len(stops_list), "routes": len(routes_list)})

# ============================================================
# Build data structures
# ============================================================

stops_by_id = {s['id']: s for s in stops_list}

stops_grouped = {}
stops_by_name_grouped = defaultdict(list)

for s in stops_list:
    name_lower = s['name'].lower()
    if name_lower not in stops_by_name_grouped:
        group_id = f"group_{len(stops_grouped)}"
        stops_grouped[group_id] = {
            'id': group_id,
            'name': s['name'],
            'platforms': [],
            'lat': s['lat'],
            'lon': s['lon'],
            'modes': set(),
        }
        stops_by_name_grouped[name_lower].append(group_id)
    
    group_id = stops_by_name_grouped[name_lower][0]
    group = stops_grouped[group_id]
    group['platforms'].append({
        'id': s['id'],
        'code': s['code'],
        'lat': s['lat'],
        'lon': s['lon'],
        'mode': s['mode'],
    })
    group['modes'].add(s['mode'])
    n = len(group['platforms'])
    group['lat'] = (group['lat'] * (n - 1) + s['lat']) / n
    group['lon'] = (group['lon'] * (n - 1) + s['lon']) / n

for g in stops_grouped.values():
    g['modes'] = sorted(list(g['modes']))

stop_to_group = {}
for group_id, group in stops_grouped.items():
    for p in group['platforms']:
        stop_to_group[p['id']] = group_id

logger.info("Stop groups built", extra={"platforms": len(stops_by_id), "groups": len(stops_grouped)})

routes_by_id = {r['route_id']: r for r in routes_list}

adjacency = defaultdict(list)
for stop_id, edges in adjacency_raw.items():
    for edge in edges:
        adjacency[stop_id].append(dict(edge))
        to_stop = edge['to']
        reverse_edge = {
            'to': stop_id,
            'distance': edge['distance'],
            'time': edge.get('time'),
            'route_id': edge['route_id'],
            'direction': edge['direction'],
            'mode': edge['mode'],
            'headsign': edge['headsign'],
        }
        adjacency[to_stop].append(reverse_edge)

logger.info("Adjacency list built", extra={"nodes": len(adjacency)})

# Free memory: adjacency_raw is no longer needed after building adjacency
del adjacency_raw

# ============================================================
# Build search index for fast stop name lookups
# ============================================================
stop_search_index = {}
for name_lower, group_ids in stops_by_name_grouped.items():
    for i in range(len(name_lower)):
        for length in range(2, min(6, len(name_lower) - i + 1)):
            prefix = name_lower[i:i+length]
            if prefix not in stop_search_index:
                stop_search_index[prefix] = []
            for gid in group_ids:
                stop_search_index[prefix].append((name_lower, gid))

for s in stops_list:
    code = s.get('code', '').lower()
    if code:
        group_id = stop_to_group.get(s['id'])
        if group_id:
            for i in range(len(code)):
                for length in range(2, min(6, len(code) - i + 1)):
                    prefix = code[i:i+length]
                    if prefix not in stop_search_index:
                        stop_search_index[prefix] = []
                    stop_search_index[prefix].append(('_code_' + code, group_id))

logger.info("Search index built", extra={"prefixes": len(stop_search_index)})

# Free memory: stops_list is no longer needed after building all stop structures
del stops_list

# ============================================================
# Ticket pricing configuration
# ============================================================
# Pricing constants live in cost.py (single source of truth), which loads
# pricing.json at import. Nothing to duplicate here.

# ============================================================
# Load and clean logo SVG
# ============================================================
_logo_svg_path = os.path.join(PUBLIC_DIR, 'logo.svg')
with open(_logo_svg_path, encoding='utf-8') as f:
    logo_svg_content = f.read()


def _clean_logo_svg(content):
    """Clean logo SVG: strip XML declaration, Inkscape/Sodipodi metadata,
    and empty defs. Keeps style= attributes (they may override fill colors)."""
    content = re.sub(r'<\?xml[^>]*\?>', '', content, count=1)
    content = re.sub(r'<sodipodi:namedview.*?</sodipodi:namedview>', '', content, flags=re.S)
    content = re.sub(r'<inkscape:grid[^>]*/>', '', content)
    content = re.sub(r'<defs[^>]*>\s*</defs>', '', content)
    content = re.sub(r'<defs[^>]*/>', '', content)
    content = re.sub(r'\s+id="[^"]*"', '', content)
    content = re.sub(r'\n\s*\n+', '\n', content)
    return content.strip()


logo_svg_content = _clean_logo_svg(logo_svg_content)
logger.info("Logo loaded", extra={"path": _logo_svg_path})
