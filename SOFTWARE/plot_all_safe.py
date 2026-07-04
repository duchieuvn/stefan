import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from plot_visualization import (
    plot_directed_link,
    plot_pair,
    plot_bme_1h_smooth,
    plot_overview_colored,
    write_index_html,
)
from plot_workflow import (
    configure_sqlite,
    parse_nodes,
    detect_schema,
    create_helpful_index,
    discover_directed_links,
    make_directed_links_from_nodes,
    load_all_directed_links_linkwise,
    insert_missing_time_bins,
    load_bme_nodes_1h_linkwise,
    insert_missing_bme_bins,
    write_link_summary,
)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_pair(a: int, b: int) -> tuple[int, int]:
    return (min(int(a), int(b)), max(int(a), int(b)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Memory-safe Link-für-Link Auswertung für sensor_data.db")

    parser.add_argument("--db", default="sensor_data.db", help="Pfad zur SQLite DB")
    parser.add_argument("--out", default="out_plots", help="Output-Ordner")
    parser.add_argument("--dpi", type=int, default=140, help="PNG DPI")

    parser.add_argument("--use-15min", action="store_true", help="15min-Mittel zusätzlich in directed plots")
    parser.add_argument("--nodes", default="1,2,3,4,5,6,7,8", help="Fallback/Standard-Knoten, z.B. 1,2,3,4,5,6,7,8")
    parser.add_argument("--discover-links", action="store_true", help="Links per SELECT DISTINCT suchen. Sonst werden alle gerichteten Links aus --nodes verwendet.")
    parser.add_argument("--create-index", action="store_true", help="Optional Index auf sender,receiver,time erzeugen. Kann bei großer DB dauern, beschleunigt danach stark.")

    parser.add_argument("--bme-nodes", default="5,6,7,8", help="receiver_id-Knoten für BME, z.B. 5,6,7,8")
    parser.add_argument("--bme-smooth-hours", type=int, default=6, help="Glättung über 1h-Werte")
    parser.add_argument("--bme-hide-raw1h", action="store_true", help="BME 1h-Mittel im Plot ausblenden")

    parser.add_argument("--start", default=None, help="Optionaler Startzeitpunkt, z.B. 2026-04-01 00:00:00")
    parser.add_argument("--end", default=None, help="Optionaler Endzeitpunkt, z.B. 2026-05-01 00:00:00")

    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"DB nicht gefunden: {db_path}")

    out_root = Path(args.out)
    out_directed = out_root / "directed"
    out_pairs = out_root / "pairs"
    out_bme = out_root / "bme"

    ensure_dir(out_root)
    ensure_dir(out_directed)
    ensure_dir(out_pairs)
    ensure_dir(out_bme)

    nodes = parse_nodes(args.nodes)
    bme_nodes = parse_nodes(args.bme_nodes)

    with sqlite3.connect(db_path) as con:
        configure_sqlite(con)
        schema = detect_schema(con)
        print("Detected schema:", schema)

        if args.create_index:
            create_helpful_index(con, schema)

        if args.discover_links:
            print("Discovering directed links ...")
            links = discover_directed_links(con, schema, start=args.start, end=args.end, fallback_nodes=nodes)
        else:
            links = make_directed_links_from_nodes(nodes)

        print(f"Links to process: {len(links)}")

        print("Loading directed links LINK-BY-LINK as 1h SQL aggregation ...")
        dir_1h = load_all_directed_links_linkwise(
            con=con,
            schema=schema,
            links=links,
            rule="1h",
            start=args.start,
            end=args.end,
        )

        if not dir_1h:
            raise RuntimeError("Keine RSSI-Daten gefunden.")

        dir_1h = insert_missing_time_bins(dir_1h, freq="1h", start=args.start, end=args.end)
        print(f"Directed links with data: {len(dir_1h)}")

        if args.use_15min:
            print("Loading directed links LINK-BY-LINK as 15min SQL aggregation ...")
            dir_15m = load_all_directed_links_linkwise(
                con=con,
                schema=schema,
                links=links,
                rule="15min",
                start=args.start,
                end=args.end,
            )
            dir_15m = insert_missing_time_bins(dir_15m, freq="15min", start=args.start, end=args.end)
        else:
            dir_15m = {}

        print("Loading BME node-by-node as 1h SQL aggregation ...")
        df_bme_1h = load_bme_nodes_1h_linkwise(
            con=con,
            schema=schema,
            nodes=bme_nodes,
            start=args.start,
            end=args.end,
        )

        if df_bme_1h.empty:
            print("WARN: Keine BME Daten gefunden.")
        else:
            df_bme_1h = insert_missing_bme_bins(df_bme_1h, freq="1h", start=args.start, end=args.end)

    directed_overview_map: dict[tuple[int, int], pd.Series] = {}

    for (s, r), d1 in sorted(dir_1h.items()):
        if d1 is None or d1.empty:
            continue

        d15 = dir_15m.get((s, r)) if args.use_15min else None
        out_path = out_directed / f"directed_{s}_to_{r}_rssi.png"

        plot_directed_link(
            sender_id=s,
            receiver_id=r,
            df_1h=d1,
            df_15m=d15,
            out_path=out_path,
            dpi=args.dpi,
        )

        directed_overview_map[(s, r)] = d1["rssi_mean"]

    plot_overview_colored(
        series_map=directed_overview_map,
        title="Overview: directed links – RSSI mean 1h | gaps preserved",
        out_path=out_directed / "overview_directed_links_1h_colored.png",
        dpi=args.dpi,
        label_fmt=lambda k: f"{k[0]}→{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )

    pair_overview_map: dict[tuple[int, int], pd.Series] = {}
    pairs = sorted({normalize_pair(s, r) for (s, r) in dir_1h.keys()})

    for (a, b) in pairs:
        ab = dir_1h.get((a, b))
        ba = dir_1h.get((b, a))

        ab_s = ab["rssi_mean"] if ab is not None and "rssi_mean" in ab.columns else pd.Series(dtype=float)
        ba_s = ba["rssi_mean"] if ba is not None and "rssi_mean" in ba.columns else pd.Series(dtype=float)

        if ab_s.empty and ba_s.empty:
            continue

        out_path = out_pairs / f"pair_{a}_{b}_rssi.png"

        pair_mean = plot_pair(
            a=a,
            b=b,
            ab_1h=ab_s,
            ba_1h=ba_s,
            out_path=out_path,
            dpi=args.dpi,
        )

        if pair_mean is not None and not pair_mean.empty:
            pair_overview_map[(a, b)] = pair_mean

    plot_overview_colored(
        series_map=pair_overview_map,
        title="Overview: pairs – pair mean RSSI 1h | gaps preserved",
        out_path=out_pairs / "overview_pairs_1h_colored.png",
        dpi=args.dpi,
        label_fmt=lambda k: f"{k[0]}↔{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )

    if not df_bme_1h.empty:
        plot_bme_1h_smooth(
            df_bme_1h=df_bme_1h,
            out_dir=out_bme,
            dpi=args.dpi,
            smooth_hours=args.bme_smooth_hours,
            show_raw_1h=not args.bme_hide_raw1h,
        )

    write_link_summary(dir_1h, out_root / "link_summary_1h.csv")
    write_index_html(out_root)

    print("\nDONE ✅")
    print("Output:", out_root)
    print("Open:", out_root / "index.html")


if __name__ == "__main__":
    main()
