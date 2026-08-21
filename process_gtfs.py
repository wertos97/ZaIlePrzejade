#!/usr/bin/env python3
"""
Process GTFS data from MPK Kraków feeds into a graph structure for route finding.
Reads tram, bus, and Mobilis GTFS feeds and produces:
- stops.json: All unique stops with coordinates
- graph.json: Stop-to-stop connections with distances and route info
- routes.json: Route metadata
- shapes.json: Simplified route shapes for map visualization
"""

import csv
import json
import math
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'processed')

# Feed configurations: (directory, feed_name, mode, zip_filename, download_url)
FEEDS = [
    ('T_tram', 'tram', 'tram', 'GTFS_KRK_T.zip', 'https://gtfs.ztp.krakow.pl/GTFS_KRK_T.zip'),
    ('A_bus', 'bus', 'bus', 'GTFS_KRK_A.zip', 'https://gtfs.ztp.krakow.pl/GTFS_KRK_A.zip'),
    ('M_mob', 'mobilis', 'bus', 'GTFS_KRK_M.zip', 'https://gtfs.ztp.krakow.pl/GTFS_KRK_M.zip'),
]

# Default transfer time in seconds (5 minutes, added per transfer)
DEFAULT_TRANSFER_TIME = 300

# Krakow center coordinates for map initialization
KRAKOW_LAT = 50.0647
KRAKOW_LON = 19.9450


def haversine(lat1, lon1, lat2, lon2):
    """Calculate great-circle distance between two points in kilometers."""
    R = 6371.0  # Earth radius in km
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def parse_float(val):
    """Safely parse a float value."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def read_csv(filepath):
    """Read a CSV file and return list of dictionaries."""
    with open(filepath, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return list(reader)


def parse_time_to_seconds(time_str):
    """Convert GTFS time (HH:MM:SS) to seconds since midnight. Returns None if invalid."""
    if not time_str:
        return None
    parts = time_str.strip().split(':')
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return h * 3600 + m * 60 + s
    except (ValueError, TypeError):
        return None


def download_gtfs(force=False):
    """
    Download the latest GTFS feeds from ZTP Kraków and extract them into data/.
    By default skips download if the zip file already exists (allows offline
    processing). With force=True, always re-downloads and re-extracts.
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    for feed_dir, feed_name, mode, zip_filename, url in FEEDS:
        zip_path = os.path.join(DATA_DIR, zip_filename)
        extract_dir = os.path.join(DATA_DIR, feed_dir)

        # Download if the zip doesn't exist yet, or force is requested
        if force or not os.path.exists(zip_path):
            print(f"  Downloading {zip_filename} from {url}...")
            try:
                urllib.request.urlretrieve(url, zip_path)
                print(f"    Downloaded {zip_filename}")
            except Exception as e:
                print(f"    ERROR downloading {zip_filename}: {e}")
                print(f"    Continuing with existing data (if any).")
                continue
        else:
            print(f"  {zip_filename} already exists, skipping download.")

        # Extract if the directory doesn't exist, is empty, or force is requested
        if force or not os.path.isdir(extract_dir) or not os.listdir(extract_dir):
            print(f"  Extracting {zip_filename}...")
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(extract_dir)
                print(f"    Extracted to {extract_dir}")
            except Exception as e:
                print(f"    ERROR extracting {zip_filename}: {e}")
        else:
            print(f"  {feed_dir} already extracted, skipping.")


def read_feed_metadata():
    """Read feed metadata (version and validity period) from the first available
    feed's feed_info.txt. Returns a dict with feed_version, start_date, end_date,
    and publisher info. Falls back to defaults if feed_info.txt is missing."""
    for feed_dir, _, _, _, _ in FEEDS:
        feed_info_path = os.path.join(DATA_DIR, feed_dir, 'feed_info.txt')
        if os.path.isfile(feed_info_path):
            try:
                with open(feed_info_path, encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        return {
                            'publisher': row.get('feed_publisher_name', '').strip(),
                            'url': row.get('feed_publisher_url', '').strip(),
                            'lang': row.get('feed_lang', '').strip(),
                            'start_date': row.get('feed_start_date', '').strip(),
                            'end_date': row.get('feed_end_date', '').strip(),
                            'version': row.get('feed_version', '').strip(),
                            'contact_email': row.get('feed_contact_email', '').strip(),
                            'contact_url': row.get('feed_contact_url', '').strip(),
                        }
            except (OSError, csv.Error):
                pass
    return {
        'publisher': '',
        'url': '',
        'lang': '',
        'start_date': '',
        'end_date': '',
        'version': '',
        'contact_email': '',
        'contact_url': '',
    }


def get_stop_code_prefix(stop_code):
    """Extract the stop number prefix from a stop code (e.g., '101-03' -> '101')."""
    if not stop_code:
        return None
    parts = stop_code.split('-')
    if len(parts) >= 1:
        return parts[0]
    return stop_code


def process_stops():
    """
    Process stops from all feeds.
    Returns:
        stops: dict of stop_id -> {stop_code, name, lat, lon, mode, code_prefix}
        code_to_stops: dict of stop_code -> list of stop_ids (for transfer matching)
        prefix_to_stops: dict of code_prefix -> list of stop_ids (for transfer matching)
    """
    stops = {}
    code_to_stops = defaultdict(list)
    prefix_to_stops = defaultdict(list)

    for feed_dir, feed_name, mode, zip_filename, url in FEEDS:
        filepath = os.path.join(DATA_DIR, feed_dir, 'stops.txt')
        if not os.path.exists(filepath):
            print(f"  Warning: {filepath} not found")
            continue

        rows = read_csv(filepath)
        for row in rows:
            stop_id = row['stop_id']
            stop_code = row.get('stop_code', '').strip()
            stop_name = row.get('stop_name', '').strip()
            lat = parse_float(row.get('stop_lat', ''))
            lon = parse_float(row.get('stop_lon', ''))

            if lat is None or lon is None:
                continue

            prefix = get_stop_code_prefix(stop_code)

            stops[stop_id] = {
                'stop_id': stop_id,
                'stop_code': stop_code,
                'name': stop_name,
                'lat': lat,
                'lon': lon,
                'mode': mode,
                'feed': feed_name,
                'code_prefix': prefix,
            }

            if stop_code:
                code_to_stops[stop_code].append(stop_id)
            if prefix:
                prefix_to_stops[prefix].append(stop_id)

    print(f"  Processed {len(stops)} stops from {len(FEEDS)} feeds")
    return stops, code_to_stops, prefix_to_stops


def process_routes():
    """Process routes from all feeds."""
    routes = {}
    for feed_dir, feed_name, mode, zip_filename, url in FEEDS:
        filepath = os.path.join(DATA_DIR, feed_dir, 'routes.txt')
        if not os.path.exists(filepath):
            continue

        rows = read_csv(filepath)
        for row in rows:
            route_id = row['route_id']
            routes[route_id] = {
                'route_id': route_id,
                'short_name': row.get('route_short_name', '').strip(),
                'long_name': row.get('route_long_name', '').strip(),
                'route_type': row.get('route_type', ''),
                'mode': mode,
                'feed': feed_name,
                'color': row.get('route_color', '').strip(),
                'text_color': row.get('route_text_color', '').strip(),
            }

    print(f"  Processed {len(routes)} routes")
    return routes


def process_trips_and_connections(stops, routes):
    """
    Process trips and stop_times to build stop-to-stop connections.
    Returns a list of edges: {from, to, distance, route_id, direction, mode}
    """
    edges = []
    edge_lookup = set()  # For deduplication

    for feed_dir, feed_name, mode, zip_filename, url in FEEDS:
        stop_times_path = os.path.join(DATA_DIR, feed_dir, 'stop_times.txt')
        trips_path = os.path.join(DATA_DIR, feed_dir, 'trips.txt')

        if not os.path.exists(stop_times_path) or not os.path.exists(trips_path):
            print(f"  Warning: Missing trip data for {feed_name}")
            continue

        print(f"  Processing {feed_name} trips and stop_times...")

        # Read trips to get route_id for each trip
        trips = read_csv(trips_path)
        trip_to_route = {}
        trip_to_direction = {}
        trip_to_headsign = {}
        for trip in trips:
            trip_id = trip['trip_id']
            trip_to_route[trip_id] = trip.get('route_id', '')
            trip_to_direction[trip_id] = trip.get('direction_id', '0')
            trip_to_headsign[trip_id] = trip.get('trip_headsign', '').strip()

        # Read stop_times and group by trip_id
        stop_times = read_csv(stop_times_path)
        print(f"    Read {len(stop_times)} stop_time entries")

        # Group by trip_id
        trip_stops = defaultdict(list)
        for st in stop_times:
            trip_id = st['trip_id']
            stop_id = st['stop_id']
            stop_seq = int(st.get('stop_sequence', '0'))
            shape_dist = parse_float(st.get('shape_dist_traveled', ''))
            arrival = parse_time_to_seconds(st.get('arrival_time', ''))
            departure = parse_time_to_seconds(st.get('departure_time', ''))

            if stop_id not in stops:
                continue

            trip_stops[trip_id].append({
                'stop_id': stop_id,
                'sequence': stop_seq,
                'shape_dist': shape_dist,
                'arrival': arrival,
                'departure': departure,
            })

        print(f"    Found {len(trip_stops)} trips with stop data")

        # Build edges for each trip
        for trip_id, stop_list in trip_stops.items():
            if len(stop_list) < 2:
                continue

            # Sort by sequence
            stop_list.sort(key=lambda x: x['sequence'])

            route_id = trip_to_route.get(trip_id, '')
            direction = trip_to_direction.get(trip_id, '0')
            headsign = trip_to_headsign.get(trip_id, '')

            for i in range(len(stop_list) - 1):
                from_stop = stop_list[i]['stop_id']
                to_stop = stop_list[i + 1]['stop_id']

                # Calculate distance
                dist = 0.0
                if stop_list[i]['shape_dist'] is not None and stop_list[i + 1]['shape_dist'] is not None:
                    # Use shape_dist_traveled if available
                    dist = stop_list[i + 1]['shape_dist'] - stop_list[i]['shape_dist']
                    if dist < 0:
                        dist = 0.0
                else:
                    # Calculate using Haversine distance
                    from_stop_data = stops[from_stop]
                    to_stop_data = stops[to_stop]
                    dist = haversine(
                        from_stop_data['lat'], from_stop_data['lon'],
                        to_stop_data['lat'], to_stop_data['lon']
                    )

                # Calculate travel time (seconds) between stops:
                # departure from current stop -> arrival at next stop
                travel_time = None
                dep_time = stop_list[i].get('departure')
                arr_time = stop_list[i + 1].get('arrival')
                if dep_time is not None and arr_time is not None:
                    travel_time = arr_time - dep_time
                    if travel_time < 0:
                        travel_time = None  # Invalid (crosses midnight or bad data)

                # Deduplicate edges
                edge_key = (from_stop, to_stop, route_id, direction)
                if edge_key in edge_lookup:
                    continue
                edge_lookup.add(edge_key)

                edges.append({
                    'from': from_stop,
                    'to': to_stop,
                    'distance': round(dist, 4),
                    'time': travel_time,
                    'route_id': route_id,
                    'direction': direction,
                    'mode': mode,
                    'headsign': headsign,
                })

        print(f"    Created edges for {feed_name}")

    print(f"  Total edges: {len(edges)}")
    return edges


def add_transfer_edges(edges, stops, prefix_to_stops):
    """
    Add transfer edges between stops at the same location.
    Stops with the same code_prefix (same physical stop, different platforms/modes)
    are connected with 0 distance.
    """
    transfer_count = 0
    transfer_edge_lookup = set()

    for prefix, stop_ids in prefix_to_stops.items():
        if len(stop_ids) < 2:
            continue

        # Get unique stop_ids
        unique_stops = list(set(stop_ids))

        # Connect all pairs of stops at the same location
        for i in range(len(unique_stops)):
            for j in range(len(unique_stops)):
                if i == j:
                    continue

                from_stop = unique_stops[i]
                to_stop = unique_stops[j]

                edge_key = (from_stop, to_stop, 'transfer', '')
                if edge_key in transfer_edge_lookup:
                    continue
                transfer_edge_lookup.add(edge_key)

                # Verify stops are actually close (within 200m)
                s1 = stops[from_stop]
                s2 = stops[to_stop]
                dist = haversine(s1['lat'], s1['lon'], s2['lat'], s2['lon'])

                if dist > 0.2:  # Skip if more than 200m apart
                    continue

                edges.append({
                    'from': from_stop,
                    'to': to_stop,
                    'distance': 0.0,
                    'time': DEFAULT_TRANSFER_TIME,
                    'route_id': 'transfer',
                    'direction': '',
                    'mode': 'transfer',
                    'headsign': '',
                })
                transfer_count += 1

    print(f"  Added {transfer_count} transfer edges")
    return edges


def build_adjacency_list(edges):
    """Build adjacency list from edges for efficient graph traversal."""
    adj = defaultdict(list)
    for edge in edges:
        adj[edge['from']].append({
            'to': edge['to'],
            'distance': edge['distance'],
            'time': edge.get('time'),
            'route_id': edge['route_id'],
            'direction': edge['direction'],
            'mode': edge['mode'],
            'headsign': edge['headsign'],
        })
    return adj


def process_shapes(routes):
    """
    Process shapes from all feeds and create simplified route shapes.
    Returns dict of route_id -> list of [lat, lon] points.
    """
    route_shapes = {}

    for feed_dir, feed_name, mode, zip_filename, url in FEEDS:
        shapes_path = os.path.join(DATA_DIR, feed_dir, 'shapes.txt')
        trips_path = os.path.join(DATA_DIR, feed_dir, 'trips.txt')

        if not os.path.exists(shapes_path) or not os.path.exists(trips_path):
            continue

        print(f"  Processing shapes for {feed_name}...")

        # Read trips to map shape_id -> route_id
        trips = read_csv(trips_path)
        shape_to_route = {}
        for trip in trips:
            shape_id = trip.get('shape_id', '')
            route_id = trip.get('route_id', '')
            if shape_id and route_id and shape_id not in shape_to_route:
                shape_to_route[shape_id] = route_id

        # Read shapes and group by shape_id
        shapes = read_csv(shapes_path)
        print(f"    Read {len(shapes)} shape points")

        shape_points = defaultdict(list)
        for s in shapes:
            shape_id = s['shape_id']
            lat = parse_float(s.get('shape_pt_lat', ''))
            lon = parse_float(s.get('shape_pt_lon', ''))
            seq = int(s.get('shape_pt_sequence', '0'))
            if lat is not None and lon is not None:
                shape_points[shape_id].append((seq, lat, lon))

        # Sort points by sequence and store
        for shape_id, points in shape_points.items():
            points.sort(key=lambda x: x[0])
            route_id = shape_to_route.get(shape_id, '')
            if route_id:
                # Simplify: take every Nth point to reduce size
                simplified = [(lat, lon) for _, lat, lon in points[::5]]
                if route_id not in route_shapes or len(simplified) > len(route_shapes[route_id]):
                    route_shapes[route_id] = simplified

        print(f"    Processed shapes for {len(shape_points)} routes")

    print(f"  Total route shapes: {len(route_shapes)}")
    return route_shapes


def update_public_texts():
    """Update the data-access dates in the public markdown files (author/warning)
    to today's date, so the site always reflects when the GTFS data was fetched.

    Replaces dates in DD.MM.YYYY format inside the "dostęp"/"pobrane" phrases in
    public/author.md and public/warning.md.
    """
    import re
    from datetime import date

    today = date.today().strftime('%d.%m.%Y')
    public_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public')
    files = ['author.md', 'warning.md']

    # Match a date in DD.MM.YYYY format that follows "dostęp " or "pobrane "
    # (optionally inside parentheses). We replace only the date token.
    pattern = re.compile(r'((?:dostęp|pobrane)\s+)(\d{2}\.\d{2}\.\d{4})')

    updated = 0
    for fname in files:
        path = os.path.join(public_dir, fname)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            content = f.read()
        new_content, n = pattern.subn(lambda m: m.group(1) + today, content)
        if n > 0:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            updated += n
            print(f"  Updated {fname}: {n} date(s) -> {today}")

    if updated == 0:
        print("  No dates found to update in public text files.")
    return updated


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Parse CLI args: --force forces re-download of GTFS feeds
    force = '--force' in sys.argv

    print("=" * 60)
    print("Processing GTFS data for MPK Kraków")
    print("=" * 60)

    print("\n0. Downloading latest GTFS data...")
    download_gtfs(force=force)

    print("\n1. Processing stops...")
    stops, code_to_stops, prefix_to_stops = process_stops()

    print("\n2. Processing routes...")
    routes = process_routes()

    print("\n3. Processing trips and building connections...")
    edges = process_trips_and_connections(stops, routes)

    print("\n4. Adding transfer edges...")
    edges = add_transfer_edges(edges, stops, prefix_to_stops)

    print("\n5. Building adjacency list...")
    adj = build_adjacency_list(edges)

    print("\n6. Processing route shapes...")
    route_shapes = process_shapes(routes)

    # Save processed data
    print("\n7. Saving processed data...")

    # Save stops (simplified for frontend)
    stops_list = []
    for stop_id, stop in stops.items():
        stops_list.append({
            'id': stop_id,
            'code': stop['stop_code'],
            'name': stop['name'],
            'lat': stop['lat'],
            'lon': stop['lon'],
            'mode': stop['mode'],
        })
    with open(os.path.join(OUTPUT_DIR, 'stops.json'), 'w') as f:
        json.dump(stops_list, f, ensure_ascii=False)
    print(f"  Saved stops.json ({len(stops_list)} stops)")

    # Save routes
    routes_list = list(routes.values())
    with open(os.path.join(OUTPUT_DIR, 'routes.json'), 'w') as f:
        json.dump(routes_list, f, ensure_ascii=False)
    print(f"  Saved routes.json ({len(routes_list)} routes)")

    # Save adjacency list
    adj_serializable = {k: v for k, v in adj.items()}
    with open(os.path.join(OUTPUT_DIR, 'adjacency.json'), 'w') as f:
        json.dump(adj_serializable, f, ensure_ascii=False)
    print(f"  Saved adjacency.json ({len(adj_serializable)} nodes)")

    # Save route shapes
    with open(os.path.join(OUTPUT_DIR, 'shapes.json'), 'w') as f:
        json.dump(route_shapes, f, ensure_ascii=False)
    print(f"  Saved shapes.json ({len(route_shapes)} route shapes)")

    # Save feed metadata (version, validity period, publisher)
    metadata = read_feed_metadata()
    with open(os.path.join(OUTPUT_DIR, 'metadata.json'), 'w') as f:
        json.dump(metadata, f, ensure_ascii=False)
    print(f"  Saved metadata.json (version: {metadata.get('version', '') or 'unknown'})")

    # Update the data-access dates in the public text files to today
    print("\n8. Updating public text dates...")
    update_public_texts()

    print("\n" + "=" * 60)
    print("Processing complete!")
    print(f"Output directory: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == '__main__':
    main()
