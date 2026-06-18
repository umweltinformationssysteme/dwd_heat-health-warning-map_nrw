#!/usr/bin/env python3
"""
NRW Heat Warning Map – Daily Generation
Fetches DWD data, overlays districts on a Sentinel-2 satellite image, and saves as JPG.

DWD Data Format (Beschreibung_hwtrend_json.pdf, 21.03.2025):
  JSON dict with DWD abbreviation as key:
    {"DOX": {"Name": "City of Dortmund", "State": "12", "Trend": [0,0,0,0,0,0,0,0]}, ...}
  Trend[0] = today's warning status.
  Warning levels: 0=none, 1=strong, 2=extreme, 3-7=trend values.

Abbreviation Mapping: Source cap_warncellids.csv (DWD), column CCC, filtered for State=NW
and WARNCELLID prefix 105xxxxx (= district level). AGS = WARNCELLID[1:6].
"""

import io
import sys
import datetime

import requests
import numpy as np
import geopandas as gpd
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import to_rgba
from PIL import Image

# ── Configuration ──────────────────────────────────────────────────────────────
OUTPUT_FILE  = "heat-warning-map-nrw-today.jpg"
IMG_W_PX     = 1280
IMG_H_PX     = 640
NRW_H_FRAC   = 620 / 640       # NRW outline is ~620 px high
DWD_BASE_URL = "https://opendata.dwd.de/climate_environment/health/forecasts/heat/"
GEOJSON_FILE = "landkreise.geojson"
TIFF_FILE    = "background.tiff"

# Colors (RGB + alpha=0.70)
COLORS = {
    0: (*to_rgba("#ffffff")[:3], 0.70),
    1: (*to_rgba("#cc99ff")[:3], 0.70),
    2: (*to_rgba("#9e46f8")[:3], 0.70),
}
# Trend levels 3-7 → Warning level (3 = no longer used → 0)
TREND_TO_WARN = {0: 0, 1: 1, 2: 2, 3: 0, 4: 1, 5: 1, 6: 2, 7: 2}

# ── AGS → DWD Abbreviation Mapping ────────────────────────────────────────────
# Source: DWD cap_warncellids.csv, column CCC, district level only (WARNCELLID 105xxxxx)
# AGS = WARNCELLID digits 2-6 (e.g., WARNCELLID 105111000 → AGS 05111)
AGS_TO_DWD = {
    # Administrative District: Düsseldorf
    "05111": "DXX",  # City of Düsseldorf
    "05112": "DUX",  # City of Duisburg
    "05113": "EXX",  # City of Essen
    "05114": "KRX",  # City of Krefeld
    "05116": "MGX",  # City of Mönchengladbach
    "05117": "MHX",  # City of Mülheim an der Ruhr
    "05119": "OBX",  # City of Oberhausen
    "05120": "RSX",  # City of Remscheid
    "05122": "SGX",  # City of Solingen
    "05124": "WXX",  # City of Wuppertal
    "05154": "KLE",  # District of Kleve
    "05158": "MEX",  # District of Mettmann
    "05162": "NEX",  # Rhine-District of Neuss
    "05166": "VIE",  # District of Viersen
    "05170": "WES",  # District of Wesel
    # Administrative District: Cologne
    "05314": "BNX",  # City of Bonn
    "05315": "KXX",  # City of Cologne
    "05316": "LEV",  # City of Leverkusen
    "05334": "ACX",  # Städteregion Aachen
    "05358": "DNX",  # District of Düren
    "05362": "BMX",  # Rhein-Erft-District
    "05366": "EUS",  # District of Euskirchen
    "05370": "HSX",  # District of Heinsberg
    "05374": "GMX",  # Oberbergischer District
    "05378": "GLX",  # Rheinisch-Bergischer District
    "05382": "SUX",  # Rhein-Sieg-District
    # Administrative District: Münster
    "05512": "BOT",  # City of Bottrop
    "05513": "GEX",  # City of Gelsenkirchen
    "05515": "MSX",  # City of Münster
    "05554": "BOR",  # District of Borken
    "05558": "COE",  # District of Coesfeld
    "05562": "REX",  # District of Recklinghausen
    "05566": "STX",  # District of Steinfurt
    "05570": "WAF",  # District of Warendorf
    # Administrative District: Detmold
    "05711": "BIX",  # City of Bielefeld
    "05754": "GTX",  # District of Gütersloh
    "05758": "HFX",  # District of Herford
    "05762": "HXX",  # District of Höxter
    "05766": "LIP",  # District of Lippe
    "05770": "MIX",  # District of Minden-Lübbecke
    "05774": "PBX",  # District of Paderborn
    # Administrative District: Arnsberg
    "05911": "BOX",  # City of Bochum
    "05913": "DOX",  # City of Dortmund
    "05914": "HAX",  # City of Hagen
    "05915": "HAM",  # City of Hamm
    "05916": "HER",  # City of Herne
    "05954": "ENX",  # Ennepe-Ruhr-District
    "05958": "HSK",  # Hochsauerland District
    "05962": "MKX",  # Märkischer District
    "05966": "OEX",  # District of Olpe
    "05970": "SIX",  # District of Siegen-Wittgenstein
    "05974": "SOX",  # District of Soest
    "05978": "UNX",  # District of Unna
}


def fetch_dwd_data(date: datetime.date) -> dict:
    """
    Downloads hwtrend_YYYYMMDD.json from DWD.
    Returns the raw dict or {} on error.
    """
    filename = f"hwtrend_{date.strftime('%Y%m%d')}.json"
    url = DWD_BASE_URL + filename
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected JSON format: {type(data)}, expected dict")
        print(f"DWD-JSON loaded: {len(data)} warning areas ({filename})")
        # Debug: output all abbreviations included in the mapping
        for ags, kuerzel in sorted(AGS_TO_DWD.items()):
            entry = data.get(kuerzel)
            trend0 = entry["Trend"][0] if entry and entry.get("Trend") else "—"
            warn   = TREND_TO_WARN.get(int(trend0), 0) if trend0 != "—" else "—"
            print(f"  AGS {ags}  {kuerzel:4s}  Trend[0]={trend0}  →Level {warn}"
                  f"  ({entry['Name'] if entry else 'NOT FOUND'})")
        return data
    except Exception as e:
        print(f"WARNING: Could not load DWD data: {e}", file=sys.stderr)
        return {}


def assign_warning_levels(gdf: gpd.GeoDataFrame, dwd_data: dict) -> gpd.GeoDataFrame:
    """Assigns each district its warning level for today (Trend[0])."""
    def get_level(row):
        ags     = str(row["AGS"])
        kuerzel = AGS_TO_DWD.get(ags)
        if not kuerzel:
            print(f"  NO MAPPING: AGS {ags} ({row.get('GEN')})", file=sys.stderr)
            return 0
        entry = dwd_data.get(kuerzel)
        if not entry:
            print(f"  ABBREVIATION MISSING IN JSON: {kuerzel} ({row.get('GEN')})", file=sys.stderr)
            return 0
        trend = entry.get("Trend", [0])
        return TREND_TO_WARN.get(int(trend[0]) if trend else 0, 0)

    gdf = gdf.copy()
    gdf["warn_level"] = gdf.apply(get_level, axis=1)
    gdf["color"] = gdf["warn_level"].apply(lambda l: COLORS.get(l, COLORS[0]))
    return gdf


def load_geodata() -> gpd.GeoDataFrame:
    gdf = gpd.read_file(GEOJSON_FILE)
    gdf = gdf[gdf["AGS"].str.startswith("05")].copy()
    gdf["AGS"] = gdf["AGS"].astype(str)
    return gdf


def compute_map_extent(gdf: gpd.GeoDataFrame):
    b     = gdf.total_bounds        # minx, miny, maxx, maxy
    map_h = (b[3] - b[1]) / NRW_H_FRAC
    map_w = map_h * (IMG_W_PX / IMG_H_PX)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return (cx - map_w / 2, cx + map_w / 2), (cy - map_h / 2, cy + map_h / 2)


def render_map(gdf: gpd.GeoDataFrame, date: datetime.date):
    with rasterio.open(TIFF_FILE) as src:
        tiff_crs    = src.crs
        tiff_bounds = src.bounds
        tiff_data   = src.read()

    gdf_proj = gdf.to_crs(tiff_crs)
    xlim, ylim = compute_map_extent(gdf_proj)

    dpi = 100
    fig, ax = plt.subplots(figsize=(IMG_W_PX / dpi, IMG_H_PX / dpi), dpi=dpi)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_axis_off()

    # Satellite image
    n   = tiff_data.shape[0]
    rgb = np.stack([tiff_data[i] for i in range(min(3, n))] if n >= 3
                   else [tiff_data[0]] * 3, axis=-1)
    if   rgb.dtype == np.uint16: rgb = (rgb / 65535.0).clip(0, 1)
    elif rgb.dtype == np.uint8:  rgb = (rgb / 255.0).clip(0, 1)
    else:
        lo, hi = rgb.min(), rgb.max()
        rgb = ((rgb - lo) / (hi - lo + 1e-9)).clip(0, 1)

    ax.imshow(rgb,
              extent=[tiff_bounds.left, tiff_bounds.right,
                      tiff_bounds.bottom, tiff_bounds.top],
              origin="upper", aspect="auto", interpolation="bilinear")

    # District areas
    for _, row in gdf_proj.iterrows():
        gpd.GeoDataFrame([row], crs=gdf_proj.crs).plot(
            ax=ax, color=[row["color"]], edgecolor="#444444", linewidth=0.4)

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    # Legend at bottom right
    c = gdf_proj["warn_level"].value_counts().to_dict()
    handles = [
        mpatches.Patch(facecolor=COLORS[0][:3] + (1.0,), edgecolor="#888",
                       label=f"Keine Warnung  ({c.get(0, 0)} Kreise)"),
        mpatches.Patch(facecolor=COLORS[1][:3] + (1.0,), edgecolor="#888",
                       label=f"Starke W\u00e4rmebelastung  ({c.get(1, 0)})"),
        mpatches.Patch(facecolor=COLORS[2][:3] + (1.0,), edgecolor="#888",
                       label=f"Extreme W\u00e4rmebelastung  ({c.get(2, 0)})"),
    ]
    # Legend: right edge exactly at 948 px from left edge.
    # bbox_to_anchor with loc="lower right" anchors the bottom-right corner of the legend.
    LEGEND_RIGHT_PX  = 948
    LEGEND_BOTTOM_PX = 12
    x_anchor = LEGEND_RIGHT_PX  / IMG_W_PX
    y_anchor = LEGEND_BOTTOM_PX / IMG_H_PX

    leg = ax.legend(handles=handles,
                    loc="lower right",
                    bbox_to_anchor=(x_anchor, y_anchor),
                    bbox_transform=ax.transAxes,
                    fontsize=7,
                    framealpha=0.85, edgecolor="#bbbbbb", facecolor="#ffffff",
                    handlelength=1.2, handleheight=1.0,
                    borderpad=0.7, labelspacing=0.4,
                    title=f"NRW Hitzewarnungen\n{date.strftime('%d.%m.%Y')}",
                    title_fontsize=7.5)
    leg.get_title().set_fontweight("bold")

    ax.text(0.01, 0.01,
            "Data source: Deutscher Wetterdienst · CC BY 4.0  |  Background: Sentinel-2",
            transform=ax.transAxes, fontsize=5.5, color="white", alpha=0.9,
            va="bottom", ha="left",
            bbox=dict(facecolor="black", alpha=0.3, pad=2, edgecolor="none"))

    # PNG-Buffer → PIL → JPEG (avoids matplotlib JPG quality bug)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    img = img.resize((IMG_W_PX, IMG_H_PX), Image.LANCZOS)
    img.save(OUTPUT_FILE, format="JPEG", quality=88, optimize=True)
    print(f"Map saved: {OUTPUT_FILE}  ({img.size[0]}x{img.size[1]} px)")


def main():
    today = datetime.date.today()
    print(f"Generating heat map for {today.strftime('%d.%m.%Y')} …")
    dwd_data = fetch_dwd_data(today)
    gdf      = load_geodata()
    gdf      = assign_warning_levels(gdf, dwd_data)
    warned   = (gdf["warn_level"] > 0).sum()
    print(f"Districts with warnings: {warned}/53")
    render_map(gdf, today)
    print("Done.")


if __name__ == "__main__":
    main()
