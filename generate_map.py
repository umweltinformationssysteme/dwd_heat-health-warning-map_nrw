#!/usr/bin/env python3
"""
NRW Hitzewarnungs-Karte – tägliche Generierung
Fetcht DWD-Daten, überlagert Kreise auf Sentinel-2-Satellitenbild, speichert als JPG.

DWD-Datenformat (Beschreibung_hwtrend_json.pdf, 21.03.2025):
  JSON-Dict mit DWD-Kürzel als Key:
    {"DOX": {"Name": "Stadt Dortmund", "Bundesland": "12", "Trend": [0,0,0,0,0,0,0,0]}, ...}
  Trend[0] = heutiger Warnstatus.
  Warnstufen: 0=keine, 1=stark, 2=extrem, 3-7=Trendwerte.

Kürzel-Mapping: Quelle cap_warncellids.csv (DWD), Spalte CCC, gefiltert auf BL=NW
und WARNCELLID-Präfix 105xxxxx (= Kreisebene). AGS = WARNCELLID[1:6].
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

# ── Konfiguration ──────────────────────────────────────────────────────────────
OUTPUT_FILE  = "Hitzekarte_NRW_heute.jpg"
IMG_W_PX     = 1280
IMG_H_PX     = 640
NRW_H_FRAC   = 620 / 640       # NRW-Umring ~620 px hoch
DWD_BASE_URL = "https://opendata.dwd.de/climate_environment/health/forecasts/heat/"
GEOJSON_FILE = "landkreise.geojson"
TIFF_FILE    = "background.tiff"

# Farben (RGB + alpha=0.70)
COLORS = {
    0: (*to_rgba("#ffffff")[:3], 0.70),
    1: (*to_rgba("#cc99ff")[:3], 0.70),
    2: (*to_rgba("#9e46f8")[:3], 0.70),
}
# Trend-Stufen 3-7 → Warnstufe (3 = nicht mehr verwendet → 0)
TREND_TO_WARN = {0: 0, 1: 1, 2: 2, 3: 0, 4: 1, 5: 1, 6: 2, 7: 2}

# ── AGS → DWD-Kürzel Mapping ──────────────────────────────────────────────────
# Quelle: DWD cap_warncellids.csv, Spalte CCC, nur Kreisebene (WARNCELLID 105xxxxx)
# AGS = WARNCELLID-Stellen 2-6 (z.B. WARNCELLID 105111000 → AGS 05111)
AGS_TO_DWD = {
    # Reg.-Bez. Düsseldorf
    "05111": "DXX",  # Stadt Düsseldorf
    "05112": "DUX",  # Stadt Duisburg
    "05113": "EXX",  # Stadt Essen
    "05114": "KRX",  # Stadt Krefeld
    "05116": "MGX",  # Stadt Mönchengladbach
    "05117": "MHX",  # Stadt Mülheim an der Ruhr
    "05119": "OBX",  # Stadt Oberhausen
    "05120": "RSX",  # Stadt Remscheid
    "05122": "SGX",  # Stadt Solingen
    "05124": "WXX",  # Stadt Wuppertal
    "05154": "KLE",  # Kreis Kleve
    "05158": "MEX",  # Kreis Mettmann
    "05162": "NEX",  # Rhein-Kreis Neuss
    "05166": "VIE",  # Kreis Viersen
    "05170": "WES",  # Kreis Wesel
    # Reg.-Bez. Köln
    "05314": "BNX",  # Stadt Bonn
    "05315": "KXX",  # Stadt Köln
    "05316": "LEV",  # Stadt Leverkusen
    "05334": "ACX",  # StädteRegion Aachen
    "05358": "DNX",  # Kreis Düren
    "05362": "BMX",  # Rhein-Erft-Kreis
    "05366": "EUS",  # Kreis Euskirchen
    "05370": "HSX",  # Kreis Heinsberg
    "05374": "GMX",  # Oberbergischer Kreis
    "05378": "GLX",  # Rheinisch-Bergischer Kreis
    "05382": "SUX",  # Rhein-Sieg-Kreis
    # Reg.-Bez. Münster
    "05512": "BOT",  # Stadt Bottrop
    "05513": "GEX",  # Stadt Gelsenkirchen
    "05515": "MSX",  # Stadt Münster
    "05554": "BOR",  # Kreis Borken
    "05558": "COE",  # Kreis Coesfeld
    "05562": "REX",  # Kreis Recklinghausen
    "05566": "STX",  # Kreis Steinfurt
    "05570": "WAF",  # Kreis Warendorf
    # Reg.-Bez. Detmold
    "05711": "BIX",  # Stadt Bielefeld
    "05754": "GTX",  # Kreis Gütersloh
    "05758": "HFX",  # Kreis Herford
    "05762": "HXX",  # Kreis Höxter
    "05766": "LIP",  # Kreis Lippe
    "05770": "MIX",  # Kreis Minden-Lübbecke
    "05774": "PBX",  # Kreis Paderborn
    # Reg.-Bez. Arnsberg
    "05911": "BOX",  # Stadt Bochum
    "05913": "DOX",  # Stadt Dortmund
    "05914": "HAX",  # Stadt Hagen
    "05915": "HAM",  # Stadt Hamm
    "05916": "HER",  # Stadt Herne
    "05954": "ENX",  # Ennepe-Ruhr-Kreis
    "05958": "HSK",  # Hochsauerlandkreis
    "05962": "MKX",  # Märkischer Kreis
    "05966": "OEX",  # Kreis Olpe
    "05970": "SIX",  # Kreis Siegen-Wittgenstein
    "05974": "SOX",  # Kreis Soest
    "05978": "UNX",  # Kreis Unna
}


def fetch_dwd_data(date: datetime.date) -> dict:
    """
    Lädt hwtrend_YYYYMMDD.json vom DWD.
    Gibt das rohe Dict zurück oder {} bei Fehler.
    """
    filename = f"hwtrend_{date.strftime('%Y%m%d')}.json"
    url = DWD_BASE_URL + filename
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"Unerwartetes JSON-Format: {type(data)}, erwartet dict")
        print(f"DWD-JSON geladen: {len(data)} Warnkreise ({filename})")
        # Debug: alle im Mapping enthaltenen Kürzel ausgeben
        for ags, kuerzel in sorted(AGS_TO_DWD.items()):
            entry = data.get(kuerzel)
            trend0 = entry["Trend"][0] if entry and entry.get("Trend") else "—"
            warn   = TREND_TO_WARN.get(int(trend0), 0) if trend0 != "—" else "—"
            print(f"  AGS {ags}  {kuerzel:4s}  Trend[0]={trend0}  →Stufe {warn}"
                  f"  ({entry['Name'] if entry else 'NICHT GEFUNDEN'})")
        return data
    except Exception as e:
        print(f"WARNUNG: DWD-Daten nicht ladbar: {e}", file=sys.stderr)
        return {}


def assign_warning_levels(gdf: gpd.GeoDataFrame, dwd_data: dict) -> gpd.GeoDataFrame:
    """Ordnet jedem Kreis seine Warnstufe für heute (Trend[0]) zu."""
    def get_level(row):
        ags     = str(row["AGS"])
        kuerzel = AGS_TO_DWD.get(ags)
        if not kuerzel:
            print(f"  KEIN MAPPING: AGS {ags} ({row.get('GEN')})", file=sys.stderr)
            return 0
        entry = dwd_data.get(kuerzel)
        if not entry:
            print(f"  KÜRZEL FEHLT IM JSON: {kuerzel} ({row.get('GEN')})", file=sys.stderr)
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

    # Satellitenbild
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

    # Kreisflächen
    for _, row in gdf_proj.iterrows():
        gpd.GeoDataFrame([row], crs=gdf_proj.crs).plot(
            ax=ax, color=[row["color"]], edgecolor="#444444", linewidth=0.4)

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    # Legende rechts unten
    c = gdf_proj["warn_level"].value_counts().to_dict()
    handles = [
        mpatches.Patch(facecolor=COLORS[0][:3] + (1.0,), edgecolor="#888",
                       label=f"Keine Warnung  ({c.get(0, 0)} Kreise)"),
        mpatches.Patch(facecolor=COLORS[1][:3] + (1.0,), edgecolor="#888",
                       label=f"Starke Wärmebelastung  ({c.get(1, 0)})"),
        mpatches.Patch(facecolor=COLORS[2][:3] + (1.0,), edgecolor="#888",
                       label=f"Extreme Wärmebelastung  ({c.get(2, 0)})"),
    ]
    # Legende: rechter Rand exakt bei 948 px vom linken Bildrand.
    # bbox_to_anchor mit loc="lower right" verankert die rechte untere Ecke der Legende.
    # Koordinaten als Axes-Fraktion (0..1): 948/1280 horizontal, 12px Abstand unten.
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
            "Datenbasis: Deutscher Wetterdienst · CC BY 4.0  |  Hintergrund: Sentinel-2",
            transform=ax.transAxes, fontsize=5.5, color="white", alpha=0.9,
            va="bottom", ha="left",
            bbox=dict(facecolor="black", alpha=0.3, pad=2, edgecolor="none"))

    # PNG-Buffer → PIL → JPEG (vermeidet matplotlib JPG-Qualitäts-Bug)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    img = img.resize((IMG_W_PX, IMG_H_PX), Image.LANCZOS)
    img.save(OUTPUT_FILE, format="JPEG", quality=88, optimize=True)
    print(f"Karte gespeichert: {OUTPUT_FILE}  ({img.size[0]}x{img.size[1]} px)")


def main():
    today = datetime.date.today()
    print(f"Generiere Hitzekarte für {today.strftime('%d.%m.%Y')} …")
    dwd_data = fetch_dwd_data(today)
    gdf      = load_geodata()
    gdf      = assign_warning_levels(gdf, dwd_data)
    warned   = (gdf["warn_level"] > 0).sum()
    print(f"Kreise mit Warnung: {warned}/53")
    render_map(gdf, today)
    print("Fertig.")


if __name__ == "__main__":
    main()
