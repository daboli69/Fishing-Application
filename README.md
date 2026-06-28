# QuietWater

A private, mobile-friendly map of **publicly-accessible, lower-pressure bank-fishing
water** in Franklin County, OH. Free to host, no API keys, no server.

Same shape as a Going Yard build: a scheduled Python ETL pulls open data, commits a
GeoJSON snapshot, and a static Leaflet page renders it.

## How it finds spots
1. **Every water body** from OpenStreetMap (Overpass API).
2. **Public land** polygons from OSM (parks, preserves, recreation grounds, protected areas).
3. **Keep only water touching public land** → drops private retention ponds.
4. **Obscurity score** rewards small, unnamed, walk-in water and downranks big named reservoirs.

Tiers, Going-Yard style: `HIDDEN GEM` (75+) · `SLEEPER` (60–74) · `WORTH A LOOK` (45–59) · `SKIP`.

## Deploy (one time)
1. New GitHub repo → drop these files in.
2. **Settings → Pages →** deploy from `main`, root. Your map is at
   `https://<user>.github.io/<repo>/`.
3. **Actions tab →** enable workflows → **Run workflow** once to generate real data
   (the included `spots.geojson` is sample data until you do).
4. Add the URL to your iPhone home screen — it behaves like an app.

After that it refreshes itself every Monday. Run it manually anytime from the Actions tab.

## Run locally
```bash
pip install shapely pyproj
python etl.py        # writes spots.geojson
```

## Tuning
All knobs live in the `CONFIG` block at the top of `etl.py`:
- `BBOX` — change the search area (or widen beyond Franklin County).
- `SCORE` — weights for size, walk-in distance, unnamed bonus, etc.
- `TIERS` — score cutoffs.

Freeze it once it's dialed in, like the Going Yard model.

## One honest caveat
A public-owned parcel is a strong *candidate* signal, not legal clearance. Some
city ponds carry local no-fishing rules, and Ohio stream-access law is murky. The
map surfaces candidates — you verify signage and rules on the ground.

## v2 ideas
- Swap/augment OSM public land with the **Franklin County Auditor parcel layer**
  (filter owner to CITY OF COLUMBUS / STATE OF OHIO / METRO PARKS) for authoritative access.
- Pull **ODNR stocking + fish-survey data** to weight spots by what actually lives there.
- Assemble OSM **relations** to include large reservoirs (currently skipped on purpose).
