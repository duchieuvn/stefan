import sqlite3
from pathlib import Path

import pandas as pd

from config import DEFAULT_PLOT_CONFIG, PlotAllConfig
from plot_all.plot_visualization import (
    plot_directed_link,
    plot_pair,
    plot_bme_1h_smooth,
    plot_overview_colored,
    write_index_html,
)
from plot_all.data_helpers import (
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


CONFIG = DEFAULT_PLOT_CONFIG


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_pair(a: int, b: int) -> tuple[int, int]:
    return (min(int(a), int(b)), max(int(a), int(b)))


def validate_db_path(db: Path) -> Path:
    db_path = Path(db)
    if not db_path.exists():
        raise FileNotFoundError(f"DB nicht gefunden: {db_path}")

    return db_path


def prepare_output_dirs(out: Path) -> tuple[Path, Path, Path, Path]:
    out_root = Path(out)
    out_directed = out_root / "directed"
    out_pairs = out_root / "pairs"
    out_bme = out_root / "bme"

    for path in (out_root, out_directed, out_pairs, out_bme):
        ensure_dir(path)

    return out_root, out_directed, out_pairs, out_bme


def load_plot_data(
    db_path: Path,
    nodes: list[int],
    bme_nodes: list[int],
    config: PlotAllConfig,
) -> tuple[dict[tuple[int, int], pd.DataFrame], dict[tuple[int, int], pd.DataFrame], pd.DataFrame]:
    with sqlite3.connect(db_path) as con:
        configure_sqlite(con)
        schema = detect_schema(con)
        print("Detected schema:", schema)

        if config.create_index:
            create_helpful_index(con, schema)

        if config.discover_links:
            print("Discovering directed links ...")
            links = discover_directed_links(con, schema, start=config.start, end=config.end, fallback_nodes=nodes)
        else:
            links = make_directed_links_from_nodes(nodes)

        print(f"Links to process: {len(links)}")

        print("Loading directed links LINK-BY-LINK as 1h SQL aggregation ...")
        dir_1h = load_all_directed_links_linkwise(
            con=con,
            schema=schema,
            links=links,
            rule="1h",
            start=config.start,
            end=config.end,
        )

        if not dir_1h:
            raise RuntimeError("Keine RSSI-Daten gefunden.")

        dir_1h = insert_missing_time_bins(dir_1h, freq="1h", start=config.start, end=config.end)
        print(f"Directed links with data: {len(dir_1h)}")

        if config.use_15min:
            print("Loading directed links LINK-BY-LINK as 15min SQL aggregation ...")
            dir_15m = load_all_directed_links_linkwise(
                con=con,
                schema=schema,
                links=links,
                rule="15min",
                start=config.start,
                end=config.end,
            )
            dir_15m = insert_missing_time_bins(dir_15m, freq="15min", start=config.start, end=config.end)
        else:
            dir_15m = {}

        print("Loading BME node-by-node as 1h SQL aggregation ...")
        df_bme_1h = load_bme_nodes_1h_linkwise(
            con=con,
            schema=schema,
            nodes=bme_nodes,
            start=config.start,
            end=config.end,
        )

        if df_bme_1h.empty:
            print("WARN: Keine BME Daten gefunden.")
        else:
            df_bme_1h = insert_missing_bme_bins(df_bme_1h, freq="1h", start=config.start, end=config.end)

    return dir_1h, dir_15m, df_bme_1h


def plot_directed_links(
    dir_1h: dict[tuple[int, int], pd.DataFrame],
    dir_15m: dict[tuple[int, int], pd.DataFrame],
    out_directed: Path,
    dpi: int,
    use_15min: bool,
) -> None:
    directed_overview_map: dict[tuple[int, int], pd.Series] = {}

    for (s, r), d1 in sorted(dir_1h.items()):
        if d1 is None or d1.empty:
            continue

        d15 = dir_15m.get((s, r)) if use_15min else None
        out_path = out_directed / f"directed_{s}_to_{r}_rssi.png"

        plot_directed_link(
            sender_id=s,
            receiver_id=r,
            df_1h=d1,
            df_15m=d15,
            out_path=out_path,
            dpi=dpi,
        )

        directed_overview_map[(s, r)] = d1["rssi_mean"]

    plot_overview_colored(
        series_map=directed_overview_map,
        title="Overview: directed links – RSSI mean 1h | gaps preserved",
        out_path=out_directed / "overview_directed_links_1h_colored.png",
        dpi=dpi,
        label_fmt=lambda k: f"{k[0]}→{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )


def plot_pairs(
    dir_1h: dict[tuple[int, int], pd.DataFrame],
    out_pairs: Path,
    dpi: int,
) -> None:
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
            dpi=dpi,
        )

        if pair_mean is not None and not pair_mean.empty:
            pair_overview_map[(a, b)] = pair_mean

    plot_overview_colored(
        series_map=pair_overview_map,
        title="Overview: pairs – pair mean RSSI 1h | gaps preserved",
        out_path=out_pairs / "overview_pairs_1h_colored.png",
        dpi=dpi,
        label_fmt=lambda k: f"{k[0]}↔{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )


def write_outputs(
    dir_1h: dict[tuple[int, int], pd.DataFrame],
    out_root: Path,
) -> None:
    write_link_summary(dir_1h, out_root / "link_summary_1h.csv")
    write_index_html(out_root)


def main(config: PlotAllConfig = CONFIG) -> None:
    db_path = validate_db_path(config.db_path)
    out_root, out_directed, out_pairs, out_bme = prepare_output_dirs(config.out_dir)

    nodes = parse_nodes(config.nodes)
    bme_nodes = parse_nodes(config.bme_nodes)
    dir_1h, dir_15m, df_bme_1h = load_plot_data(db_path, nodes, bme_nodes, config)

    plot_directed_links(dir_1h, dir_15m, out_directed, config.dpi, config.use_15min)
    plot_pairs(dir_1h, out_pairs, config.dpi)
    plot_bme_1h_smooth(df_bme_1h, out_bme, config.dpi, config.bme_smooth_hours, config.bme_hide_raw1h)
    write_outputs(dir_1h, out_root)

    print("\nDONE ✅")
    print("Output:", out_root)
    print("Open:", out_root / "index.html")


if __name__ == "__main__":
    main()
