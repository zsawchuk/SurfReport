#!/usr/bin/env python3
"""
San Diego Swell Report generator.

Pulls real-time spectral wave data (.spec files) from every San Diego-area
NDBC/CDIP buoy, skips any station whose last reading is stale, and writes
a static HTML report to docs/index.html.

No third-party packages required — standard library only, so this runs
in GitHub Actions with zero pip installs.
"""

import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# --- San Diego-area stations -------------------------------------------------
# id, human name, rough spot relevance note
STATIONS = [
    ("46225", "Torrey Pines Outer", "Primary offshore reference buoy"),
    ("46232", "Point Loma South", "South county / Sunset Cliffs, OB, Coronado"),
    ("46254", "Scripps Nearshore", "La Jolla, Blacks Beach"),
    ("46266", "Del Mar Nearshore", "Del Mar, Torrey Pines State Beach"),
    ("46274", "Leucadia Nearshore", "Encinitas, Swami's, Cardiff"),
    ("46258", "Mission Bay West", "Mission Beach, Pacific Beach"),
    ("46235", "Imperial Beach Nearshore", "Imperial Beach, south county"),
]

# How old a reading can be before we treat the station as offline (hours)
STALE_HOURS = 6

# Land/pier station that reports wind (most nearshore buoys only measure waves)
WIND_STATION = ("LJAC1", "La Jolla (Scripps Pier)")

# San Diego's coast runs roughly N-S facing W, so wind FROM the east quadrant
# blows offshore (good, holds up the wave face) and FROM the west quadrant
# blows onshore (bad, blows it out). These are rough ranges, not exact per-spot.
OFFSHORE_RANGE = (45, 135)
ONSHORE_RANGE = (225, 315)
WIND_QUALITY_FACTOR = {"offshore": 1.0, "cross-shore": 0.75, "onshore": 0.5}

# --- Spot recommendation rules ------------------------------------------------
# Each spot: name, best swell direction range (degrees), a break-type
# multiplier (reefs/points focus energy and run bigger than the open-ocean
# buoy reading; wide sandy beaches spread it out and run smaller), notes.
# The multiplier is a rough heuristic, not a measured transfer function.
SPOTS = [
    ("Blacks Beach", 170, 230, 1.15, "Best overall shape when south/southwest is running"),
    ("Sunset Cliffs / OB Jetty", 160, 250, 1.05, "Widest open exposure to south swells"),
    ("Swami's / Cardiff Reef", 180, 240, 1.20, "Reef amplifies mid-period south swells nicely"),
    ("La Jolla Shores", 170, 240, 0.85, "Soft and forgiving, good for smaller days"),
    ("Del Mar / Torrey Pines", 190, 250, 0.90, "Picks up SW well, less crowded"),
    ("Imperial Beach", 170, 230, 0.95, "Wide open south exposure, can get windy"),
    ("Oceanside / North County", 260, 320, 0.90, "Needs W/NW swell, sits in the shadow of S/SW"),
]


def fetch_latest_reading(station_id):
    """Return dict with the most recent parsed reading, or None on failure."""
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{station_id}.spec"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None

    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    if not lines:
        return None

    # Columns: YY MM DD hh mm WVHT SwH SwP WWH WWP SwD WWD STEEPNESS APD MWD
    parts = lines[0].split()
    if len(parts) < 15:
        return None

    try:
        yr, mo, dy, hr, mn = (int(parts[i]) for i in range(5))
        wvht_m = float(parts[5])
        swp = float(parts[7])
        mwd = int(parts[14])
    except ValueError:
        return None

    ts = datetime(yr, mo, dy, hr, mn, tzinfo=timezone.utc)
    return {
        "timestamp": ts,
        "wave_height_ft": round(wvht_m * 3.28084, 1),
        "period_s": swp,
        "direction_deg": mwd,
    }


def deg_to_compass(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(deg / 22.5) % 16
    return dirs[ix]


def fetch_latest_wind(station_id):
    """Return dict with latest wind reading, or None if unavailable/stale-format."""
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None

    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    for line in lines:
        parts = line.split()
        if len(parts) < 7:
            continue
        wdir_raw, wspd_raw = parts[5], parts[6]
        if wdir_raw == "MM" or wspd_raw == "MM":
            continue  # skip rows with missing wind, use the next one back
        try:
            yr, mo, dy, hr, mn = (int(parts[i]) for i in range(5))
            wdir = int(wdir_raw)
            wspd_mph = round(float(wspd_raw) * 2.23694, 1)
        except ValueError:
            continue
        ts = datetime(yr, mo, dy, hr, mn, tzinfo=timezone.utc)
        return {"timestamp": ts, "direction_deg": wdir, "speed_mph": wspd_mph}
    return None


def wind_condition(direction_deg):
    if OFFSHORE_RANGE[0] <= direction_deg <= OFFSHORE_RANGE[1]:
        return "offshore"
    if ONSHORE_RANGE[0] <= direction_deg <= ONSHORE_RANGE[1]:
        return "onshore"
    return "cross-shore"


def exposure_factor(direction_deg, min_dir, max_dir):
    """0.15-1.0 score for how well a swell direction fits a spot's window."""
    center = (min_dir + max_dir) / 2
    half_width = (max_dir - min_dir) / 2
    distance = abs(direction_deg - center)
    if distance <= half_width:
        return round(1.0 - 0.4 * (distance / half_width), 2)
    over = distance - half_width
    return round(max(0.15, 0.6 - 0.05 * over), 2)


def rate_spot(exposure, wind_factor):
    score = exposure * wind_factor
    if score >= 0.65:
        return "good", score
    if score >= 0.4:
        return "fair", score
    return "poor", score


def recommend_spots(direction_deg, wave_height_ft, wind_factor):
    results = []
    for name, min_dir, max_dir, mult, note in SPOTS:
        exp = exposure_factor(direction_deg, min_dir, max_dir)
        est_height = round(wave_height_ft * exp * mult, 1)
        rating, score = rate_spot(exp, wind_factor)
        results.append({
            "name": name, "note": note, "est_height_ft": est_height,
            "rating": rating, "score": score,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def build_report():
    now = datetime.now(timezone.utc)
    results = []
    for station_id, name, note in STATIONS:
        reading = fetch_latest_reading(station_id)
        if reading is None:
            results.append({"id": station_id, "name": name, "note": note, "status": "unavailable"})
            continue
        age_hours = (now - reading["timestamp"]).total_seconds() / 3600
        status = "live" if age_hours <= STALE_HOURS else "stale"
        results.append({
            "id": station_id, "name": name, "note": note, "status": status,
            "age_hours": round(age_hours, 1), **reading,
        })

    live = [r for r in results if r["status"] == "live"]

    # Use the freshest live reading as the "primary" number for the header
    primary = min(live, key=lambda r: r["age_hours"]) if live else None

    wind = fetch_latest_wind(WIND_STATION[0])
    wind_info = None
    if wind:
        age_hours = (now - wind["timestamp"]).total_seconds() / 3600
        condition = wind_condition(wind["direction_deg"])
        wind_info = {
            **wind, "age_hours": round(age_hours, 1), "condition": condition,
            "station_name": WIND_STATION[1],
            "stale": age_hours > STALE_HOURS,
        }

    wind_factor = WIND_QUALITY_FACTOR[wind_info["condition"]] if wind_info and not wind_info["stale"] else 0.75

    spots = []
    if primary:
        spots = recommend_spots(primary["direction_deg"], primary["wave_height_ft"], wind_factor)

    return now, results, primary, wind_info, spots


RATING_COLOR = {"good": "#2e9e4f", "fair": "#d99a1b", "poor": "#c94b4b"}
RATING_BG = {"good": "#eaf6ee", "fair": "#fdf3df", "poor": "#fbeaea"}
RATING_LABEL = {"good": "Good", "fair": "Fair", "poor": "Poor"}


def render_html(now, results, primary, wind_info, spots):
    updated = now.astimezone(PACIFIC).strftime("%b %d, %Y %I:%M %p %Z")

    def station_row(r):
        if r["status"] == "unavailable":
            dot, label, val = "#9a9890", "Unavailable", "no data"
        elif r["status"] == "stale":
            dot, label, val = "#d64545", f"Stale · {r['age_hours']}h old", f"{r['wave_height_ft']} ft"
        else:
            dot, label, val = "#3a9a5c", f"Live · {r['age_hours']}h ago", f"{r['wave_height_ft']} ft"
        return f"""
        <div class="row">
          <span class="dot" style="background:{dot}"></span>
          <div class="row-text">
            <p class="row-title">{r['id']} &middot; {r['name']}</p>
            <p class="row-sub">{label} &middot; {r['note']}</p>
          </div>
          <span class="row-val">{val}</span>
        </div>"""

    stations_html = "\n".join(station_row(r) for r in results)

    # --- wind card ---
    if wind_info and not wind_info["stale"]:
        cond = wind_info["condition"]
        cond_label = {"offshore": "Offshore", "onshore": "Onshore", "cross-shore": "Cross-shore"}[cond]
        wind_html = f"""
        <div class="stat-card">
          <p class="stat-label">Wind &middot; {wind_info['station_name']}</p>
          <p class="stat-val" style="color:{RATING_COLOR['good'] if cond=='offshore' else RATING_COLOR['poor'] if cond=='onshore' else RATING_COLOR['fair']};">
            {cond_label}
          </p>
          <p class="stat-sub">{deg_to_compass(wind_info['direction_deg'])} ({wind_info['direction_deg']}&deg;) at {wind_info['speed_mph']} mph</p>
        </div>"""
    else:
        wind_html = """
        <div class="stat-card">
          <p class="stat-label">Wind</p>
          <p class="stat-val" style="font-size:16px;">Unavailable</p>
          <p class="stat-sub">Station offline or stale</p>
        </div>"""

    if primary:
        header_html = f"""
        <div class="stat-grid">
          <div class="stat-card"><p class="stat-label">Wave height</p><p class="stat-val">{primary['wave_height_ft']} ft</p></div>
          <div class="stat-card"><p class="stat-label">Period</p><p class="stat-val">{primary['period_s']} s</p></div>
          <div class="stat-card"><p class="stat-label">Direction</p><p class="stat-val">{deg_to_compass(primary['direction_deg'])} <span class="stat-sub">{primary['direction_deg']}&deg;</span></p></div>
          {wind_html}
        </div>"""

        spot_cards = []
        for s in spots:
            color = RATING_COLOR[s["rating"]]
            bg = RATING_BG[s["rating"]]
            spot_cards.append(f"""
        <div class="spot-card" style="border-left:4px solid {color};">
          <div class="spot-row">
            <p class="spot-name">{s['name']}</p>
            <span class="badge" style="background:{bg}; color:{color};">{RATING_LABEL[s['rating']]}</span>
          </div>
          <p class="spot-note">{s['note']}</p>
          <p class="spot-height">Est. face height: <strong>{s['est_height_ft']} ft</strong></p>
        </div>""")
        spots_html = "\n".join(spot_cards)
    else:
        header_html = "<p class='row-sub'>No live buoy data available right now — every station is stale or unreachable.</p>"
        spots_html = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>San Diego Swell Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background:#f7f6f2; color:#2a2a28; margin:0; padding:24px 16px 60px; }}
  .wrap {{ max-width:560px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 2px; }}
  .updated {{ font-size:12px; color:#8a8880; margin:0 0 20px; }}
  .stat-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:24px; }}
  .stat-card {{ background:#fff; border-radius:14px; padding:16px; border:1px solid #e7e5df; }}
  .stat-label {{ font-size:12px; color:#8a8880; margin:0 0 4px; }}
  .stat-val {{ font-size:22px; font-weight:600; margin:0; }}
  .stat-sub {{ font-size:13px; color:#8a8880; font-weight:400; margin:4px 0 0; }}
  h2 {{ font-size:14px; color:#6b6a63; text-transform:uppercase; letter-spacing:.04em; margin:28px 0 10px; }}
  .row {{ display:flex; align-items:center; gap:12px; background:#fff; border:1px solid #e7e5df;
          border-radius:12px; padding:12px 14px; margin-bottom:8px; }}
  .dot {{ width:8px; height:8px; border-radius:50%; flex-shrink:0; }}
  .row-text {{ flex:1; }}
  .row-title {{ font-size:14px; font-weight:500; margin:0; }}
  .row-sub {{ font-size:12px; color:#8a8880; margin:0; }}
  .row-val {{ font-size:13px; font-weight:500; }}
  .spot-card {{ background:#fff; border:1px solid #e7e5df; border-radius:12px; padding:12px 14px; margin-bottom:8px; }}
  .spot-row {{ display:flex; align-items:center; justify-content:space-between; }}
  .spot-name {{ font-size:14px; font-weight:500; margin:0; }}
  .spot-note {{ font-size:12px; color:#8a8880; margin:4px 0 0; }}
  .spot-height {{ font-size:13px; margin:6px 0 0; }}
  .badge {{ font-size:11px; font-weight:600; padding:3px 9px; border-radius:20px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>San Diego Swell Report</h1>
  <p class="updated">Updated {updated}</p>

  {header_html}

  <h2>Spot ratings today</h2>
  {spots_html}

  <h2>Buoy network status</h2>
  {stations_html}
</div>
</body>
</html>"""


def main():
    now, results, primary, wind_info, spots = build_report()
    html = render_html(now, results, primary, wind_info, spots)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote docs/index.html — primary station: {primary['name'] if primary else 'none live'}")


if __name__ == "__main__":
    main()
