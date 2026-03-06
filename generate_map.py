#!/usr/bin/env python3
"""
NRW Hitzewarnungs-Karte – tägliche Generierung
Fetcht DWD-Daten, überlagert Kreise auf Sentinel-2-Satellitenbild, speichert als JPG.
"""

import sys
import json
import datetime
import requests
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.transform import from_bounds
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import to_rgba

# ── Konfiguration ──────────────────────────────────────────────────────────────
OUTPUT_FILE   = "Hitzekarte_NRW_heute.jpg"
IMG_W_PX      = 1280
IMG_H_PX      = 640
NRW_H_FRAC    = 620 / 640        # NRW-Umring soll ~620 px hoch sein
DWD_BASE_URL  = "https://opendata.dwd.de/climate_environment/health/forecasts/heat/"
GEOJSON_FILE  = "landkreise.geojson"
TIFF_FILE     = "background.tiff"

# Farben (RGBA, alpha=0.70)
COLORS = {
    0: (*to_rgba("#ffffff")[:3], 0.70),   # keine Warnung  – weiß
    1: (*to_rgba("#cc99ff")[:3], 0.70),   # Stufe 1        – hellviolett
    2: (*to_rgba("#9e46f8")[:3], 0.70),   # Stufe 2        – dunkelviolett
}

# ── AGS → DWD-Schlüssel (CCC) Mapping – alle 53 NRW-Kreise ───────────────────
# Quelle: DWD CAP Warncell-IDs / hwtrend JSON
AGS_TO_CCC = {
    "05111": "511",  # Düsseldorf
    "05112": "512",  # Duisburg
    "05113": "513",  # Essen
    "05114": "514",  # Krefeld
    "05116": "516",  # Mönchengladbach
    "05117": "517",  # Mülheim an der Ruhr
    "05119": "519",  # Oberhausen
    "05120": "520",  # Remscheid
    "05122": "522",  # Solingen
    "05124": "524",  # Wuppertal
    "05154": "554",  # Kleve
    "05158": "558",  # Mettmann
    "05162": "562",  # Rhein-Kreis Neuss
    "05166": "566",  # Viersen
    "05170": "570",  # Wesel
    "05314": "314",  # Bonn
    "05315": "315",  # Köln
    "05316": "316",  # Leverkusen
    "05334": "334",  # Städteregion Aachen
    "05358": "358",  # Düren
    "05362": "362",  # Rhein-Erft-Kreis
    "05366": "366",  # Euskirchen
    "05370": "370",  # Heinsberg
    "05374": "374",  # Oberbergischer Kreis
    "05378": "378",  # Rheinisch-Bergischer Kreis
    "05382": "382",  # Rhein-Sieg-Kreis
    "05512": "512b", # Bottrop  (eigener Key im DWD)
    "05513": "513b", # Gelsenkirchen
    "05515": "515",  # Münster
    "05554": "554b", # Borken
    "05558": "558b", # Coesfeld
    "05562": "562b", # Recklinghausen
    "05566": "566b", # Steinfurt
    "05570": "570b", # Warendorf
    "05711": "711",  # Bielefeld
    "05754": "754",  # Gütersloh
    "05758": "758",  # Herford
    "05762": "762",  # Höxter
    "05766": "766",  # Lippe
    "05770": "770",  # Minden-Lübbecke
    "05774": "774",  # Paderborn
    "05911": "911",  # Bochum
    "05913": "913",  # Dortmund
    "05914": "914",  # Hagen
    "05915": "915",  # Hamm
    "05916": "916",  # Herne
    "05954": "954",  # Ennepe-Ruhr-Kreis
    "05958": "958",  # Hochsauerlandkreis
    "05962": "962",  # Märkischer Kreis
    "05966": "966",  # Olpe
    "05970": "970",  # Siegen-Wittgenstein
    "05974": "974",  # Soest
    "05978": "978",  # Unna
}


def fetch_dwd_warnings(date: datetime.date) -> dict[str, int]:
    """
    Lädt hwtrend_YYYYMMDD.json vom DWD und gibt ein Dict {CCC: level} zurück.
    Gibt bei Fehler ein leeres Dict zurück (alle Kreise = Stufe 0).

    Das DWD-JSON ist ein Array von Objekten, z.B.:
      [{"ccc": "511", "mf": 0, ...}, {"ccc": "512", "mf": 1, ...}, ...]
    oder (neueres Format) ein Dict mit "content"-Key.
    """
    filename = f"hwtrend_{date.strftime('%Y%m%d')}.json"
    url = DWD_BASE_URL + filename
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        # Struktur-Debugging beim ersten Aufruf
        sample = data[:1] if isinstance(data, list) else data
        print(f"DWD JSON-Struktur (Sample): {sample}", file=sys.stderr)

        # Falls Top-Level ein Dict ist, nach Liste suchen
        if isinstance(data, dict):
            # Typische Keys: "content", "items", "data", "districts"
            for key in ("content", "items", "data", "districts", "warnings"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
            else:
                # Fallback: erstes list-Wert im Dict
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break

        warnings = {}
        for entry in data:
            if isinstance(entry, dict):
                # ccc kann "ccc", "CCC", "id", "districtId" heißen
                ccc = str(
                    entry.get("ccc") or entry.get("CCC") or
                    entry.get("id") or entry.get("districtId") or ""
                ).strip()
                # Warnstufe kann "mf", "level", "warnlevel", "value" heißen
                raw_level = (
                    entry.get("mf") or entry.get("level") or
                    entry.get("warnlevel") or entry.get("value") or 0
                )
                level = int(raw_level or 0)
                if ccc:
                    warnings[ccc] = level
            elif isinstance(entry, str):
                # Manchmal: ["511:0", "512:1", ...]  oder nur Keys
                if ":" in entry:
                    parts = entry.split(":")
                    warnings[parts[0].strip()] = int(parts[1].strip() or 0)

        print(f"DWD-Daten geladen: {len(warnings)} Einträge ({filename})")
        return warnings
    except Exception as e:
        print(f"WARNUNG: DWD-Daten konnten nicht geladen werden: {e}", file=sys.stderr)
        return {}


def load_geodata() -> gpd.GeoDataFrame:
    """Lädt NRW-Kreise aus GeoJSON."""
    gdf = gpd.read_file(GEOJSON_FILE)
    gdf = gdf[gdf["AGS"].str.startswith("05")].copy()
    gdf["AGS"] = gdf["AGS"].astype(str)
    return gdf


def assign_warning_levels(gdf: gpd.GeoDataFrame, warnings: dict[str, int]) -> gpd.GeoDataFrame:
    """Ordnet jedem Kreis seine Warnstufe zu."""
    def get_level(ags):
        ccc = AGS_TO_CCC.get(ags)
        if ccc is None:
            return 0
        return warnings.get(ccc, 0)

    gdf["warn_level"] = gdf["AGS"].apply(get_level)
    gdf["color"] = gdf["warn_level"].apply(lambda lvl: COLORS.get(lvl, COLORS[0]))
    return gdf


def compute_map_extent(gdf: gpd.GeoDataFrame, img_w: int, img_h: int, nrw_h_frac: float):
    """
    Berechnet den Kartenausschnitt so, dass NRW ~nrw_h_frac des Bildes hoch ist
    und horizontal zentriert liegt.
    """
    bounds = gdf.total_bounds  # minx, miny, maxx, maxy (in CRS-Einheiten)
    nrw_w = bounds[2] - bounds[0]
    nrw_h = bounds[3] - bounds[1]

    # Ziel: NRW nimmt nrw_h_frac der Bildhöhe ein
    map_h = nrw_h / nrw_h_frac
    # Breite des Kartenausschnitts, angepasst an Seitenverhältnis des Bildes
    aspect = img_w / img_h
    map_w = map_h * aspect

    cx = (bounds[0] + bounds[2]) / 2
    cy = (bounds[1] + bounds[3]) / 2

    xlim = (cx - map_w / 2, cx + map_w / 2)
    ylim = (cy - map_h / 2, cy + map_h / 2)
    return xlim, ylim


def render_map(gdf: gpd.GeoDataFrame, warnings: dict, date: datetime.date):
    """Rendert die Karte und speichert sie als JPG."""

    # Geodaten in EPSG:4326 reprojizieren (passt zum GeoTIFF)
    with rasterio.open(TIFF_FILE) as src:
        tiff_crs = src.crs
        tiff_bounds = src.bounds
        tiff_data = src.read()          # (bands, rows, cols)
        tiff_transform = src.transform

    gdf_proj = gdf.to_crs(tiff_crs)

    xlim, ylim = compute_map_extent(gdf_proj, IMG_W_PX, IMG_H_PX, NRW_H_FRAC)

    # Matplotlib-Figure exakt 1280×640 px
    dpi = 100
    fig, ax = plt.subplots(figsize=(IMG_W_PX / dpi, IMG_H_PX / dpi), dpi=dpi)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_axis_off()

    # ── Satellitenbild als Hintergrund ────────────────────────────────────────
    # Bänder auf RGB normieren (uint8 oder uint16 → 0-1 float)
    bands = tiff_data.shape[0]
    if bands >= 3:
        rgb = np.stack([tiff_data[0], tiff_data[1], tiff_data[2]], axis=-1)
    else:
        rgb = np.stack([tiff_data[0]] * 3, axis=-1)

    if rgb.dtype == np.uint16:
        rgb = (rgb / 65535.0).clip(0, 1)
    elif rgb.dtype == np.uint8:
        rgb = (rgb / 255.0).clip(0, 1)
    else:
        rgb = ((rgb - rgb.min()) / (rgb.max() - rgb.min() + 1e-9)).clip(0, 1)

    # Geographische Ausdehnung des TIFFs
    extent = [tiff_bounds.left, tiff_bounds.right, tiff_bounds.bottom, tiff_bounds.top]
    ax.imshow(rgb, extent=extent, origin="upper", aspect="auto", interpolation="bilinear")

    # ── Kreise einfärben ──────────────────────────────────────────────────────
    for _, row in gdf_proj.iterrows():
        color = row["color"]
        gpd.GeoDataFrame([row], crs=gdf_proj.crs).plot(
            ax=ax,
            color=[color],
            edgecolor="#555555",
            linewidth=0.4,
        )

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    # ── Legende (rechts unten) ────────────────────────────────────────────────
    warn_counts = gdf_proj["warn_level"].value_counts().to_dict()
    legend_items = [
        mpatches.Patch(facecolor=COLORS[0][:3] + (1.0,), edgecolor="#666", label=f"Keine Warnung ({warn_counts.get(0, 0)} Kreise)"),
        mpatches.Patch(facecolor=COLORS[1][:3] + (1.0,), edgecolor="#666", label=f"Starke Wärmebelastung ({warn_counts.get(1, 0)})"),
        mpatches.Patch(facecolor=COLORS[2][:3] + (1.0,), edgecolor="#666", label=f"Extreme Wärmebelastung ({warn_counts.get(2, 0)})"),
    ]
    legend = ax.legend(
        handles=legend_items,
        loc="lower right",
        fontsize=7,
        framealpha=0.82,
        edgecolor="#aaaaaa",
        facecolor="#ffffff",
        handlelength=1.2,
        handleheight=1.0,
        borderpad=0.6,
        labelspacing=0.35,
        title=f"NRW Hitzewarnungen\n{date.strftime('%d.%m.%Y')}",
        title_fontsize=7.5,
    )
    legend.get_title().set_fontweight("bold")

    # Attribution
    ax.text(
        0.01, 0.01,
        "Datenbasis: Deutscher Wetterdienst · CC BY 4.0  |  Hintergrund: Sentinel-2",
        transform=ax.transAxes,
        fontsize=5.5,
        color="white",
        alpha=0.85,
        va="bottom",
        ha="left",
        bbox=dict(facecolor="black", alpha=0.25, pad=2, edgecolor="none"),
    )

    # ── Speichern als JPG ─────────────────────────────────────────────────────
    # Matplotlib savefig als PNG in Buffer, dann via PIL zu JPEG mit Qualität
    import io
    from PIL import Image
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    # Exakt auf 1280×640 skalieren (sollte schon passen, aber sicher ist sicher)
    img = img.resize((IMG_W_PX, IMG_H_PX), Image.LANCZOS)
    img.save(OUTPUT_FILE, format="JPEG", quality=88, optimize=True)
    print(f"Karte gespeichert: {OUTPUT_FILE} ({img.size[0]}×{img.size[1]} px)")


def main():
    today = datetime.date.today()
    print(f"Generiere Hitzekarte für {today.strftime('%d.%m.%Y')} …")

    warnings = fetch_dwd_warnings(today)
    gdf = load_geodata()
    gdf = assign_warning_levels(gdf, warnings)

    warned = (gdf["warn_level"] > 0).sum()
    print(f"Kreise mit Warnung: {warned}/53")

    render_map(gdf, warnings, today)
    print("Fertig.")


if __name__ == "__main__":
    main()
