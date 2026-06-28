#!/usr/bin/env python3
"""
QuietWater ETL
==============
Finds publicly-accessible, lower-pressure bank-fishing water in Franklin County, OH.

Pipeline (all free, no API keys):
  1. Pull every tagged water body from OpenStreetMap via the Overpass API.
  2. Pull public-land polygons (parks, preserves, recreation grounds, protected areas).
  3. Keep only water that touches public land  -> drops private retention ponds.
  4. Score each survivor for "obscurity" (small, unnamed, walk-in = lower pressure).
  5. Write spots.geojson for the map to read.

Run locally:  python etl.py
In CI:        committed by .github/workflows/update.yml on a schedule.

NOTE: v1 handles closed OSM *ways* (the vast majority of small ponds). Large
reservoirs are usually OSM *relations* and are skipped on purpose -- those are
the known, high-pressure waters this tool is built to avoid anyway.
"""

import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

from shapely.geometry import Polygon, Point, shape, mapping
from shapely.ops import unary_union, transform
from pyproj import Transformer

# ----------------------------------------------------------------------------
# CONFIG  -- everything tunable lives here. Freeze it once it feels right.
# ----------------------------------------------------------------------------

# Franklin County, OH bounding box (south, west, north, east).
# Widen or shift this to scout a different area.
BBOX = (39.80, -83.21, 40.18, -82.76)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",  # fallback mirror
]

ACRE = 4046.86  # m^2 per acre

# Scoring knobs. Higher score = quieter / better bank spot.
SCORE = {
    "base": 50,
    "unnamed_bonus": 15,        # no name on the map = less known
    "fishing_tag_bonus": 5,     # OSM says fishing here = confirmed access
    "named_known_fishing": -10, # named AND tagged fishing = pressured
    "sweet_acres": (0.25, 10),  # ideal pond size
    "sweet_acres_bonus": 20,
    "mid_acres": (10, 50),
    "mid_acres_bonus": 5,
    "huge_acres_penalty": -20,  # >50 acres, almost always a known reservoir
    "tiny_acres_penalty": -10,  # <0.1 acre, barely fishable
    "walkin_range_m": (120, 800),  # far enough to thin crowds, close enough to reach
    "walkin_bonus": 15,
    "near_range_m": (800, 1600),
    "near_bonus": 5,
    "roadside_bonus": 0,        # <120 m from parking = easy = pressured
    "remote_penalty": -5,       # >1600 m = brutal bank access
}

TIERS = [
    ("HIDDEN GEM", 75),
    ("SLEEPER", 60),
    ("WORTH A LOOK", 45),
    ("SKIP", 0),
]

# Owners / land types we treat as public access.
PUBLIC_LAND_QUERY = """
  way["leisure"="park"]({bbox});
  way["leisure"="nature_reserve"]({bbox});
  way["leisure"="recreation_ground"]({bbox});
  way["landuse"="recreation_ground"]({bbox});
  way["boundary"="protected_area"]({bbox});
  way["landuse"="forest"]["access"!="private"]({bbox});
"""

PROJ = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True).transform  # WGS84 -> UTM 17N (meters)


def overpass(query: str) -> dict:
    """POST an Overpass QL query, trying mirrors in order.

    Sends a proper User-Agent (public mirrors reject anonymous requests with
    HTTP 406) and submits the query form-encoded as `data=`, which is the
    format Overpass expects.
    """
    ql = "[out:json][timeout:180];" + query
    body = b"data=" + urllib.parse.quote(ql).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "QuietWater/1.0 (personal fishing-spot map; contact: daboli69 on github)",
        "Accept": "application/json",
    }
    last_err = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=200) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            print(f"  ! {url} failed ({e}); trying next mirror...", file=sys.stderr)
            time.sleep(5)
    raise RuntimeError(f"All Overpass mirrors failed: {last_err}")


def ways_to_polys(elements):
    """Turn closed OSM ways (with inline geometry) into shapely polygons + tags."""
    polys = []
    for el in elements:
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if len(geom) < 4:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        if coords[0] != coords[-1]:
            continue  # not a closed way
        try:
            poly = Polygon(coords)
            if poly.is_valid and poly.area > 0:
                polys.append((poly, el.get("tags", {}), el.get("id")))
        except Exception:
            continue
    return polys


def parking_points(elements):
    """Collect parking locations as shapely points (nodes + way centroids)."""
    pts = []
    for el in elements:
        if el.get("type") == "node":
            pts.append(Point(el["lon"], el["lat"]))
        elif el.get("type") == "way" and el.get("geometry"):
            g = el["geometry"]
            lon = sum(p["lon"] for p in g) / len(g)
            lat = sum(p["lat"] for p in g) / len(g)
            pts.append(Point(lon, lat))
    return pts


def tier_for(score):
    for name, cutoff in TIERS:
        if score >= cutoff:
            return name
    return "SKIP"


# Flowing / non-target water we never want (rivers, streams, canals, etc.).
EXCLUDE_WATER = {"river", "stream", "canal", "ditch", "lock", "wastewater",
                 "stream_pool", "fish_pass", "moat"}


def classify_water(tags, acres):
    """Return 'pond' | 'lake' | 'reservoir', or None to drop (flowing water)."""
    wt = tags.get("water", "")
    if wt in EXCLUDE_WATER:
        return None
    if wt == "pond":
        return "pond"
    if wt == "reservoir":
        return "reservoir"
    if wt == "lake":
        return "lake"
    # No water subtag -> infer from size.
    if acres < 5:
        return "pond"
    if acres < 25:
        return "lake"
    return "reservoir"


def species_for(wtype, acres):
    """Likely warm-water community for central OH by water type/size.
    Heuristic from water characteristics + region -- NOT a per-pond survey."""
    if wtype is None:
        return []
    if wtype == "pond" and acres < 0.5:
        return ["Largemouth bass", "Bluegill"]
    base = ["Largemouth bass", "Bluegill", "Black crappie", "Channel catfish"]
    if wtype in ("lake", "reservoir") and acres >= 25:
        return base + ["Saugeye", "White bass"]
    return base


def bass_rating(wtype, acres):
    """Largemouth-bass potential by size/type. PRIME small ponds & lakes."""
    if wtype is None:
        return None
    if wtype in ("pond", "lake") and 0.5 <= acres <= 15:
        return "PRIME"
    if wtype == "pond" and 0.25 <= acres < 0.5:
        return "GOOD"
    if wtype in ("pond", "lake") and 15 < acres <= 50:
        return "GOOD"
    return "FAIR"


def easy_access(dist_park_m):
    """True = parking within a short, girlfriend-friendly walk."""
    return dist_park_m is not None and dist_park_m <= 250


def score_spot(acres, has_name, has_fishing, dist_park_m):
    s = SCORE["base"]
    if not has_name:
        s += SCORE["unnamed_bonus"]
    if has_fishing:
        s += SCORE["fishing_tag_bonus"]
    if has_name and has_fishing:
        s += SCORE["named_known_fishing"]

    lo, hi = SCORE["sweet_acres"]
    mlo, mhi = SCORE["mid_acres"]
    if lo <= acres <= hi:
        s += SCORE["sweet_acres_bonus"]
    elif mlo < acres <= mhi:
        s += SCORE["mid_acres_bonus"]
    elif acres > mhi:
        s += SCORE["huge_acres_penalty"]
    elif acres < 0.1:
        s += SCORE["tiny_acres_penalty"]

    if dist_park_m is not None:
        wlo, whi = SCORE["walkin_range_m"]
        nlo, nhi = SCORE["near_range_m"]
        if dist_park_m < wlo:
            s += SCORE["roadside_bonus"]
        elif wlo <= dist_park_m <= whi:
            s += SCORE["walkin_bonus"]
        elif nlo < dist_park_m <= nhi:
            s += SCORE["near_bonus"]
        elif dist_park_m > nhi:
            s += SCORE["remote_penalty"]

    return max(0, min(100, s))


def main():
    bbox = ",".join(str(b) for b in BBOX)
    print("Pulling water bodies from OSM...")
    water_raw = overpass(f'(way["natural"="water"]({bbox});'
                         f'way["water"~"pond|lake|reservoir"]({bbox}););out geom;')
    print("Pulling public land...")
    land_raw = overpass(f'({PUBLIC_LAND_QUERY.format(bbox=bbox)});out geom;')
    print("Pulling parking...")
    park_raw = overpass(f'(node["amenity"="parking"]({bbox});'
                        f'way["amenity"="parking"]({bbox}););out geom;')

    water = ways_to_polys(water_raw.get("elements", []))
    land = ways_to_polys(land_raw.get("elements", []))
    parks = parking_points(park_raw.get("elements", []))
    print(f"  water={len(water)}  public_land={len(land)}  parking={len(parks)}")

    if not land:
        print("No public land found; check BBOX.", file=sys.stderr)
        sys.exit(1)

    # Project everything to meters once.
    land_m = unary_union([transform(PROJ, p) for p, _, _ in land])
    park_m = [transform(PROJ, p) for p in parks]

    features = []
    for poly, tags, osm_id in water:
        poly_m = transform(PROJ, poly)
        if not poly_m.intersects(land_m):
            continue  # private water -> drop

        acres = poly_m.area / ACRE

        wtype = classify_water(tags, acres)
        if wtype is None:
            continue  # flowing / non-target water -> drop

        name = tags.get("name")
        has_fishing = tags.get("leisure") == "fishing" or "fishing" in tags

        # Public shoreline = water edge that lies on public land (meters).
        shore = poly_m.exterior.intersection(land_m)
        shore_m = round(shore.length) if not shore.is_empty else 0

        # Nearest parking (meters).
        cen_m = poly_m.centroid
        dist_park = round(min((cen_m.distance(pp) for pp in park_m), default=None)) \
            if park_m else None

        score = score_spot(acres, bool(name), has_fishing, dist_park)
        tier = tier_for(score)

        cen = poly.centroid
        features.append({
            "type": "Feature",
            "geometry": mapping(poly),
            "properties": {
                "name": name or "Unnamed water",
                "tier": tier,
                "score": score,
                "wtype": wtype,
                "acres": round(acres, 2),
                "species": species_for(wtype, acres),
                "bass": bass_rating(wtype, acres),
                "easy_access": easy_access(dist_park),
                "public_shore_m": shore_m,
                "nearest_park_m": dist_park,
                "osm_id": osm_id,
                "lat": round(cen.y, 6),
                "lon": round(cen.x, 6),
            },
        })

    features.sort(key=lambda f: f["properties"]["score"], reverse=True)
    out = {"type": "FeatureCollection",
           "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "count": len(features),
           "features": features}

    with open("spots.geojson", "w") as f:
        json.dump(out, f)
    print(f"Wrote spots.geojson with {len(features)} public-access spots.")
    by_tier = {}
    for f in features:
        by_tier[f["properties"]["tier"]] = by_tier.get(f["properties"]["tier"], 0) + 1
    print("  " + "  ".join(f"{k}:{v}" for k, v in by_tier.items()))


if __name__ == "__main__":
    main()
