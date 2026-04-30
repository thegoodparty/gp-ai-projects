#!/usr/bin/env python3
"""Cluster voters into walkable canvassing areas by geographic proximity.

Usage:
  python3 cluster_voters.py <voters.json> <output_areas.json> [--max-doors 50] [--min-doors 10] [--grid-size 0.005]

Input: JSON array of voter records with Residence_Addresses_Latitude/Longitude.
Output: JSON array of area objects ready for walking_plan assembly.

Grid-based clustering:
  1. Round lat/lon to grid cells (~500m blocks at default 0.005°)
  2. Group voters by grid cell
  3. Split cells with >max_doors unique addresses
  4. Merge cells with <min_doors into nearest neighbor
  5. Sort by door density (most doors first)
"""
import argparse
import json
import math
import sys
from collections import OrderedDict, defaultdict


def get_address_number(addr):
    parts = addr.split()
    try:
        return int(parts[0]) if parts else 0
    except ValueError:
        return 0


def get_street_name(addr):
    parts = addr.split()
    try:
        int(parts[0])
        return " ".join(parts[1:]).strip() if len(parts) > 1 else addr
    except (ValueError, IndexError):
        return addr


def haversine_approx(lat1, lon1, lat2, lon2):
    """Approximate distance in meters between two lat/lon points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def grid_key(lat, lon, grid_size):
    """Round lat/lon to grid cell."""
    return (round(lat / grid_size) * grid_size, round(lon / grid_size) * grid_size)


def centroid(voters):
    """Average lat/lon of a voter group."""
    lats = [v["_lat"] for v in voters if v.get("_lat")]
    lons = [v["_lon"] for v in voters if v.get("_lon")]
    if not lats:
        return (0, 0)
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def unique_addresses(voters):
    """Count unique addresses in a voter group."""
    return len(set(v.get("Residence_Addresses_AddressLine", "") for v in voters))


def cluster_name(voters):
    """Generate a human-readable name for a cluster based on dominant streets."""
    streets = defaultdict(int)
    for v in voters:
        addr = v.get("Residence_Addresses_AddressLine", "")
        street = get_street_name(addr)
        if street:
            streets[street] += 1

    if not streets:
        return "Unknown Area"

    top = sorted(streets.items(), key=lambda x: -x[1])
    main_street = top[0][0]

    # Add address range for the main street
    addrs_on_main = [v for v in voters if get_street_name(v.get("Residence_Addresses_AddressLine", "")) == main_street]
    nums = sorted(get_address_number(v.get("Residence_Addresses_AddressLine", "")) for v in addrs_on_main)
    nums = [n for n in nums if n > 0]

    if nums and len(top) == 1:
        return f"{main_street} {nums[0]}-{nums[-1]}"
    elif len(top) > 1:
        return f"{main_street} & {len(top) - 1} nearby"
    else:
        return main_street


def cluster_voters(voters, max_doors=50, min_doors=10, grid_size=0.005):
    """Main clustering function.

    Args:
        voters: list of voter dicts with lat/lon and address fields
        max_doors: max unique addresses per area
        min_doors: min unique addresses per area (smaller clusters merge or drop)
        grid_size: grid cell size in degrees (~0.005° ≈ 500m)

    Returns:
        list of (area_name, voter_list) tuples, sorted by door count descending
    """
    # Parse lat/lon
    has_geo = 0
    no_geo = 0
    for v in voters:
        lat = v.get("Residence_Addresses_Latitude")
        lon = v.get("Residence_Addresses_Longitude")
        try:
            v["_lat"] = float(lat) if lat is not None else None
            v["_lon"] = float(lon) if lon is not None else None
            if v["_lat"] and v["_lon"]:
                has_geo += 1
            else:
                no_geo += 1
        except (ValueError, TypeError):
            v["_lat"] = None
            v["_lon"] = None
            no_geo += 1

    print(f"Geo coverage: {has_geo}/{has_geo + no_geo} voters have lat/lon", file=sys.stderr)

    # Fall back to street-name clustering if <50% have geo data
    if has_geo < len(voters) * 0.5:
        print("WARNING: <50% geo coverage, falling back to street-name clustering", file=sys.stderr)
        return _cluster_by_street(voters, max_doors, min_doors)

    # Step 1: Grid-based grouping
    cells = defaultdict(list)
    no_geo_bucket = []
    for v in voters:
        if v.get("_lat") and v.get("_lon"):
            key = grid_key(v["_lat"], v["_lon"], grid_size)
            cells[key].append(v)
        else:
            no_geo_bucket.append(v)

    # Add no-geo voters to nearest cell by city/zip match
    if no_geo_bucket and cells:
        # Find nearest cell by matching city/zip
        for v in no_geo_bucket:
            v_zip = v.get("Residence_Addresses_Zip", "")
            v_city = v.get("Residence_Addresses_City", "")
            best_key = None
            best_score = -1
            for key, cell_voters in cells.items():
                score = sum(1 for cv in cell_voters[:5]
                           if cv.get("Residence_Addresses_Zip") == v_zip
                           or cv.get("Residence_Addresses_City") == v_city)
                if score > best_score:
                    best_score = score
                    best_key = key
            if best_key:
                cells[best_key].append(v)

    # Step 2: Split large cells
    split_cells = []
    for key, cell_voters in cells.items():
        doors = unique_addresses(cell_voters)
        if doors > max_doors:
            # Sub-split by street + address number blocks
            street_groups = defaultdict(list)
            for v in cell_voters:
                street = get_street_name(v.get("Residence_Addresses_AddressLine", ""))
                street_groups[street].append(v)

            for street, svs in street_groups.items():
                svs.sort(key=lambda v: get_address_number(v.get("Residence_Addresses_AddressLine", "")))
                # Split into chunks that respect max_doors by address
                chunk = []
                chunk_addrs = set()
                for v in svs:
                    addr = v.get("Residence_Addresses_AddressLine", "")
                    chunk.append(v)
                    chunk_addrs.add(addr)
                    if len(chunk_addrs) >= max_doors:
                        split_cells.append(chunk)
                        chunk = []
                        chunk_addrs = set()
                if chunk:
                    split_cells.append(chunk)
        else:
            split_cells.append(cell_voters)

    # Step 3: Merge small cells into nearest neighbor
    areas = []
    small = []
    for cell_voters in split_cells:
        if unique_addresses(cell_voters) >= min_doors:
            areas.append(cell_voters)
        else:
            small.append(cell_voters)

    # Merge each small cell into the nearest large cell
    for small_cell in small:
        if not areas:
            areas.append(small_cell)
            continue
        sc = centroid(small_cell)
        best_idx = 0
        best_dist = float("inf")
        for i, area in enumerate(areas):
            ac = centroid(area)
            dist = haversine_approx(sc[0], sc[1], ac[0], ac[1])
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        areas[best_idx].extend(small_cell)

    # Re-split any areas that grew beyond max_doors after merging
    final = []
    for area_voters in areas:
        doors = unique_addresses(area_voters)
        if doors > max_doors:
            area_voters.sort(key=lambda v: get_address_number(v.get("Residence_Addresses_AddressLine", "")))
            chunk = []
            chunk_addrs = set()
            for v in area_voters:
                addr = v.get("Residence_Addresses_AddressLine", "")
                chunk.append(v)
                chunk_addrs.add(addr)
                if len(chunk_addrs) >= max_doors:
                    final.append(chunk)
                    chunk = []
                    chunk_addrs = set()
            if chunk and unique_addresses(chunk) >= min_doors:
                final.append(chunk)
            elif chunk:
                # Merge remainder into last area
                if final:
                    final[-1].extend(chunk)
                else:
                    final.append(chunk)
        else:
            final.append(area_voters)

    # Drop any area still under min_doors
    final = [a for a in final if unique_addresses(a) >= min_doors]

    # Name and sort
    result = [(cluster_name(area), area) for area in final]
    result.sort(key=lambda x: -unique_addresses(x[1]))

    return result


def _cluster_by_street(voters, max_doors, min_doors):
    """Fallback: cluster by street name when geo data is unavailable."""
    street_groups = defaultdict(list)
    for v in voters:
        addr = v.get("Residence_Addresses_AddressLine", "")
        street = get_street_name(addr)
        street_groups[street].append(v)

    merged = []
    small_bucket = []
    for street, svs in sorted(street_groups.items(), key=lambda x: -len(x[1])):
        doors = unique_addresses(svs)
        if doors > max_doors:
            svs.sort(key=lambda v: get_address_number(v.get("Residence_Addresses_AddressLine", "")))
            chunk = []
            chunk_addrs = set()
            for v in svs:
                addr = v.get("Residence_Addresses_AddressLine", "")
                chunk.append(v)
                chunk_addrs.add(addr)
                if len(chunk_addrs) >= max_doors:
                    lo = get_address_number(chunk[0].get("Residence_Addresses_AddressLine", ""))
                    hi = get_address_number(chunk[-1].get("Residence_Addresses_AddressLine", ""))
                    merged.append((f"{street} {lo}-{hi}", chunk))
                    chunk = []
                    chunk_addrs = set()
            if chunk and unique_addresses(chunk) >= min_doors:
                lo = get_address_number(chunk[0].get("Residence_Addresses_AddressLine", ""))
                hi = get_address_number(chunk[-1].get("Residence_Addresses_AddressLine", ""))
                merged.append((f"{street} {lo}-{hi}", chunk))
        elif doors >= min_doors:
            merged.append((street, svs))
        else:
            small_bucket.extend(svs)

    if small_bucket:
        small_bucket.sort(key=lambda v: (
            get_street_name(v.get("Residence_Addresses_AddressLine", "")),
            get_address_number(v.get("Residence_Addresses_AddressLine", "")),
        ))
        chunk_size = 30
        for i in range(0, len(small_bucket), chunk_size):
            chunk = small_bucket[i:i + chunk_size]
            if unique_addresses(chunk) >= min_doors:
                merged.append((cluster_name(chunk), chunk))

    merged.sort(key=lambda x: -unique_addresses(x[1]))
    return merged


def main():
    parser = argparse.ArgumentParser(description="Cluster voters into walkable canvassing areas")
    parser.add_argument("voters_json", help="Path to voter records JSON array")
    parser.add_argument("output_json", help="Path to write clustered areas JSON")
    parser.add_argument("--max-doors", type=int, default=50, help="Max unique addresses per area (default: 50)")
    parser.add_argument("--min-doors", type=int, default=10, help="Min unique addresses per area (default: 10)")
    parser.add_argument("--grid-size", type=float, default=0.005, help="Grid cell size in degrees (default: 0.005 ≈ 500m)")
    args = parser.parse_args()

    voters = json.load(open(args.voters_json))
    print(f"Input: {len(voters)} voters", file=sys.stderr)

    areas = cluster_voters(voters, args.max_doors, args.min_doors, args.grid_size)

    # Output as JSON array of {name, voters} objects
    output = []
    for name, area_voters in areas:
        doors = unique_addresses(area_voters)
        output.append({
            "name": name,
            "door_count": doors,
            "voter_count": len(area_voters),
            "voters": area_voters,
        })

    with open(args.output_json, "w") as f:
        json.dump(output, f, indent=2, default=str)

    total_doors = sum(a["door_count"] for a in output)
    total_voters = sum(a["voter_count"] for a in output)
    print(f"Output: {len(output)} areas, {total_doors} doors, {total_voters} voters", file=sys.stderr)
    for a in output[:5]:
        print(f"  {a['name']}: {a['door_count']} doors, {a['voter_count']} voters", file=sys.stderr)
    if len(output) > 5:
        print(f"  ... and {len(output) - 5} more areas", file=sys.stderr)


if __name__ == "__main__":
    main()
