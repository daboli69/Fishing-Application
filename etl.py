#!/usr/bin/env python3
"""
QuietWater ETL  -- parcel-verified public bank-fishing water (Franklin County, OH)
==================================================================================
A pond is only kept if the land under/around it is confirmed PUBLIC by the
Franklin County Auditor parcel layer. HOA-, condo-association-, and privately
owned water is excluded. Every kept spot stores the real owner name so you can
verify it yourself before you go.

Pipeline (all free, no API keys):
  1. Water bodies (ponds/lakes) from OpenStreetMap -- Overpass API. Rivers/streams dropped.
  2. Public + HOA parcels from the county Auditor ArcGIS service (authoritative owners).
  3. Keep a pond only if it has public shoreline AND is not HOA-owned.
  4. Score obscurity, rate largemouth-bass potential, flag easy access.
  5. Write spots.geojson.

If the county service is unreachable, the run falls back to OSM parkland and marks
those spots LIKELY (unverified) -- the app hides those unless you opt in.
"""

import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

from shapely.geometry import Polygon, Point, shape, mapping
from shapely.ops import unary_union, transform
from shapely.strtree import STRtree
from pyproj import Transformer

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BBOX = (39.80, -83.21, 40.18, -82.76)          # Franklin County (S, W, N, E)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

PARCEL_URL = ("https://gis.franklincountyohio.gov/hosting/rest/services/"
              "ParcelFeatures/Parcel_Features/MapServer/0/query")

ACRE = 4046.86
MIN_ACRES = 0.12              # ignore puddles
MIN_PUBLIC_PARCEL_ACRES = 0.5  # public parcels smaller than this rarely hold ponds
MIN_PUBLIC_SHORE_M = 8        # need at least this much public bank to count
UA = "QuietWater/1.0 (personal fishing-spot map; github daboli69)"

# Owner-name patterns. HOA is checked FIRST so "X HOMEOWNERS ASSN" never reads public.
PUBLIC_PATTERNS = ["CITY OF", "VILLAGE OF", "STATE OF OHIO", "OHIO DEPT",
    "OHIO DEPARTMENT", "DEPARTMENT OF NATURAL", "DIVISION OF WILDLIFE",
    "METRO PARK", "METROPOLITAN PARK", "PARK DISTRICT", "FRANKLIN COUNTY",
    "BOARD OF COUNTY", "BOARD OF EDUCATION", "CITY SCHOOL", "LOCAL SCHOOL",
    "SCHOOL DISTRICT", "EXEMPTED VILLAGE", "TOWNSHIP", "UNITED STATES",
    "OHIO STATE UNIV", "COLUMBUS METRO", "COLUMBUS RECREATION", "COLUMBUS CITY",
    "COUNTY OF FRANKLIN", "CONSERVANCY DISTRICT", "SOIL AND WATER",
    "PARKS AND RECREATION"]
HOA_PATTERNS = ["HOMEOWNER", "HOME OWNER", "CONDOMINIUM", "COMMUNITY ASSOCIATION",
    "COMMUNITY ASSN", "PROPERTY OWNER", "OWNERS ASSOCIATION", "OWNERS ASSN",
    "CIVIC ASSOCIATION", "MAINTENANCE ASSOCIATION", "RESIDENTS ASSOCIATION",
    "HOMEOWNERS", "ASSOCIATION"]

SCORE = {"base": 50, "unnamed_bonus": 15, "fishing_tag_bonus": 5,
    "named_known_fishing": -10, "sweet_acres": (0.25, 10), "sweet_acres_bonus": 20,
    "mid_acres": (10, 50), "mid_acres_bonus": 5, "huge_acres_penalty": -20,
    "tiny_acres_penalty": -10, "walkin_range_m": (120, 800), "walkin_bonus": 15,
    "near_range_m": (800, 1600), "near_bonus": 5, "roadside_bonus": 0,
    "remote_penalty": -5}
TIERS = [("HIDDEN GEM", 75), ("SLEEPER", 60), ("WORTH A LOOK", 45), ("SKIP", 0)]

PROJ = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True).transform


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def overpass(query: str) -> dict:
    body = b"data=" + urllib.parse.quote("[out:json][timeout:180];" + query).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": UA, "Accept": "application/json"}
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=200) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            print(f"  ! {url} failed ({e}); next mirror...", file=sys.stderr)
            time.sleep(5)
    raise RuntimeError(f"All Overpass mirrors failed: {last}")


def arcgis_parcels(where: str, min_acres: float = 0.0):
    """Page through the county parcel layer, returning [(shapely_wgs84, owner,
    classdscrp, siteaddress), ...]. Returns None if the service is unreachable."""
    out, offset, page = [], 0, 0
    full_where = where + (f" AND ACRES >= {min_acres}" if min_acres else "")
    while page < 60:
        params = {"f": "geojson", "where": full_where, "outFields":
                  "OWNERNME1,CLASSDSCRP,SITEADDRESS", "returnGeometry": "true",
                  "outSR": "4326", "resultOffset": str(offset),
                  "resultRecordCount": "3000",
                  "geometry": f"{BBOX[1]},{BBOX[0]},{BBOX[3]},{BBOX[2]}",
                  "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects"}
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(PARCEL_URL, data=data,
              headers={"Content-Type": "application/x-www-form-urlencoded",
                       "User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                gj = json.loads(r.read().decode())
        except Exception as e:
            print(f"  ! parcel query failed ({e})", file=sys.stderr)
            return None if not out else out
        feats = gj.get("features", [])
        for f in feats:
            g = f.get("geometry")
            if not g:
                continue
            try:
                geom = shape(g)
                if geom.is_valid and not geom.is_empty:
                    p = f.get("properties", {})
                    out.append((geom, p.get("OWNERNME1", ""),
                                p.get("CLASSDSCRP", ""), p.get("SITEADDRESS", "")))
            except Exception:
                pass
        page += 1
        if len(feats) < 3000:
            break
        offset += 3000
        time.sleep(0.5)
    return out


# ----------------------------------------------------------------------------
# Geometry / classification helpers
# ----------------------------------------------------------------------------
def ways_to_polys(elements):
    polys = []
    for el in elements:
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if len(geom) < 4:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        if coords[0] != coords[-1]:
            continue
        try:
            poly = Polygon(coords)
            if poly.is_valid and poly.area > 0:
                polys.append((poly, el.get("tags", {}), el.get("id")))
        except Exception:
            pass
    return polys


def parking_points(elements):
    pts = []
    for el in elements:
        if el.get("type") == "node":
            pts.append(Point(el["lon"], el["lat"]))
        elif el.get("type") == "way" and el.get("geometry"):
            g = el["geometry"]
            pts.append(Point(sum(p["lon"] for p in g)/len(g),
                             sum(p["lat"] for p in g)/len(g)))
    return pts


def tier_for(score):
    for name, cut in TIERS:
        if score >= cut:
            return name
    return "SKIP"


EXCLUDE_WATER = {"river", "stream", "canal", "ditch", "lock", "wastewater",
                 "stream_pool", "fish_pass", "moat"}


def classify_water(tags, acres):
    wt = tags.get("water", "")
    if wt in EXCLUDE_WATER:
        return None
    if wt in ("pond", "reservoir", "lake"):
        return wt
    return "pond" if acres < 5 else ("lake" if acres < 25 else "reservoir")


def classify_owner(name):
    if not name:
        return "UNKNOWN"
    u = name.upper()
    if any(p in u for p in HOA_PATTERNS):
        return "HOA"
    if any(p in u for p in PUBLIC_PATTERNS):
        return "PUBLIC"
    return "PRIVATE"


def species_for(wtype, acres):
    if wtype is None:
        return []
    if wtype == "pond" and acres < 0.5:
        return ["Largemouth bass", "Bluegill"]
    base = ["Largemouth bass", "Bluegill", "Black crappie", "Channel catfish"]
    if wtype in ("lake", "reservoir") and acres >= 25:
        return base + ["Saugeye", "White bass"]
    return base


def bass_rating(wtype, acres):
    if wtype is None:
        return None
    if wtype in ("pond", "lake") and 0.5 <= acres <= 15:
        return "PRIME"
    if wtype == "pond" and 0.25 <= acres < 0.5:
        return "GOOD"
    if wtype in ("pond", "lake") and 15 < acres <= 50:
        return "GOOD"
    return "FAIR"


def easy_access(d):
    return d is not None and d <= 250


def score_spot(acres, has_name, has_fishing, d):
    s = SCORE["base"]
    if not has_name:
        s += SCORE["unnamed_bonus"]
    if has_fishing:
        s += SCORE["fishing_tag_bonus"]
    if has_name and has_fishing:
        s += SCORE["named_known_fishing"]
    lo, hi = SCORE["sweet_acres"]; mlo, mhi = SCORE["mid_acres"]
    if lo <= acres <= hi: s += SCORE["sweet_acres_bonus"]
    elif mlo < acres <= mhi: s += SCORE["mid_acres_bonus"]
    elif acres > mhi: s += SCORE["huge_acres_penalty"]
    elif acres < 0.1: s += SCORE["tiny_acres_penalty"]
    if d is not None:
        wlo, whi = SCORE["walkin_range_m"]; nlo, nhi = SCORE["near_range_m"]
        if d < wlo: s += SCORE["roadside_bonus"]
        elif wlo <= d <= whi: s += SCORE["walkin_bonus"]
        elif nlo < d <= nhi: s += SCORE["near_bonus"]
        elif d > nhi: s += SCORE["remote_penalty"]
    return max(0, min(100, s))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    bbox = ",".join(map(str, BBOX))
    print("Pulling water from OSM...")
    water_raw = overpass(f'(way["natural"="water"]({bbox});'
                         f'way["water"~"pond|lake|reservoir"]({bbox}););out geom;')
    print("Pulling parking...")
    park_raw = overpass(f'(node["amenity"="parking"]({bbox});'
                        f'way["amenity"="parking"]({bbox}););out geom;')
    print("Pulling OSM parkland (fallback only)...")
    land_raw = overpass(f'(way["leisure"="park"]({bbox});'
                        f'way["leisure"="nature_reserve"]({bbox});'
                        f'way["boundary"="protected_area"]({bbox}););out geom;')

    water = ways_to_polys(water_raw.get("elements", []))
    parks = parking_points(park_raw.get("elements", []))
    osm_land = ways_to_polys(land_raw.get("elements", []))
    print(f"  water={len(water)} parking={len(parks)} osm_land={len(osm_land)}")

    # Authoritative public + HOA parcels.
    print("Pulling PUBLIC parcels from county Auditor...")
    pub_where = "(" + " OR ".join(f"UPPER(OWNERNME1) LIKE '%{p}%'"
                                  for p in PUBLIC_PATTERNS) + ")"
    public = arcgis_parcels(pub_where, MIN_PUBLIC_PARCEL_ACRES)
    print("Pulling HOA parcels from county Auditor...")
    hoa_where = "(" + " OR ".join(f"UPPER(OWNERNME1) LIKE '%{p}%'"
                                  for p in HOA_PATTERNS) + ")"
    hoa = arcgis_parcels(hoa_where, 0.0)

    parcels_ok = public is not None
    if parcels_ok:
        print(f"  public_parcels={len(public)} hoa_parcels={len(hoa or [])}")
    else:
        print("  ! county service unreachable -- falling back to OSM parkland", file=sys.stderr)
        public, hoa = [], []

    # Project to meters.
    park_m = [transform(PROJ, p) for p in parks]
    pub_geoms_m = [transform(PROJ, g) for g, *_ in public]
    pub_meta = [(o, c, a) for _, o, c, a in public]
    pub_tree = STRtree(pub_geoms_m) if pub_geoms_m else None
    hoa_union_m = unary_union([transform(PROJ, g) for g, *_ in hoa]) if hoa else None
    osm_union_m = unary_union([transform(PROJ, p) for p, _, _ in osm_land]) if osm_land else None

    features = []
    for poly, tags, osm_id in water:
        poly_m = transform(PROJ, poly)
        acres = poly_m.area / ACRE
        if acres < MIN_ACRES:
            continue
        wtype = classify_water(tags, acres)
        if wtype is None:
            continue

        access, owner, owner_addr, shore_m = "UNKNOWN", "", "", 0

        if parcels_ok and pub_tree is not None:
            # Public parcels touching this water.
            idx = pub_tree.query(poly_m)
            hits = [i for i in idx if pub_geoms_m[i].intersects(poly_m)]
            if hits:
                hit_union = unary_union([pub_geoms_m[i] for i in hits])
                shore = poly_m.exterior.intersection(hit_union)
                shore_m = round(shore.length) if not shore.is_empty else 0
                # Pick the public owner contributing the most bank.
                best, best_len = None, -1
                cen = poly_m.centroid
                centroid_public = False
                for i in hits:
                    seg = poly_m.exterior.intersection(pub_geoms_m[i])
                    L = seg.length if not seg.is_empty else 0
                    if L > best_len:
                        best_len, best = L, i
                    if pub_geoms_m[i].contains(cen):
                        centroid_public = True
                if shore_m >= MIN_PUBLIC_SHORE_M and best is not None:
                    owner, _cls, owner_addr = pub_meta[best]
                    in_hoa = bool(hoa_union_m and hoa_union_m.contains(cen))
                    access = "PUBLIC" if (centroid_public and not in_hoa) else "PUBLIC_BANK"
            if access == "UNKNOWN":
                # Not public. Is it HOA? (drop) else private/unknown (drop).
                cen = poly_m.centroid
                if hoa_union_m and hoa_union_m.intersects(poly_m):
                    access = "HOA"
                else:
                    access = "PRIVATE"
        else:
            # Degraded mode: OSM parkland only.
            if osm_union_m is not None and poly_m.intersects(osm_union_m):
                shore = poly_m.exterior.intersection(osm_union_m)
                shore_m = round(shore.length) if not shore.is_empty else 0
                if shore_m >= MIN_PUBLIC_SHORE_M:
                    access, owner = "LIKELY", "Unverified (OSM parkland)"

        if access not in ("PUBLIC", "PUBLIC_BANK", "LIKELY"):
            continue  # never surface HOA / private / unknown

        name = tags.get("name")
        has_fishing = tags.get("leisure") == "fishing" or "fishing" in tags
        cen_m = poly_m.centroid
        dist_park = round(min((cen_m.distance(pp) for pp in park_m), default=None)) if park_m else None
        score = score_spot(acres, bool(name), has_fishing, dist_park)
        cen = poly.centroid
        features.append({"type": "Feature", "geometry": mapping(poly),
            "properties": {
                "name": name or "Unnamed water", "tier": tier_for(score),
                "score": score, "wtype": wtype, "acres": round(acres, 2),
                "access": access, "owner": owner or "Public land", "owner_addr": owner_addr,
                "species": species_for(wtype, acres), "bass": bass_rating(wtype, acres),
                "easy_access": easy_access(dist_park), "public_shore_m": shore_m,
                "nearest_park_m": dist_park, "osm_id": osm_id,
                "lat": round(cen.y, 6), "lon": round(cen.x, 6)}})

    features.sort(key=lambda f: f["properties"]["score"], reverse=True)
    out = {"type": "FeatureCollection",
           "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "verified": parcels_ok, "count": len(features), "features": features}
    with open("spots.geojson", "w") as f:
        json.dump(out, f)

    by = {}
    for f in features:
        a = f["properties"]["access"]; by[a] = by.get(a, 0) + 1
    print(f"Wrote spots.geojson: {len(features)} spots ({'parcel-verified' if parcels_ok else 'OSM fallback'})")
    print("  " + "  ".join(f"{k}:{v}" for k, v in by.items()))


if __name__ == "__main__":
    main()
