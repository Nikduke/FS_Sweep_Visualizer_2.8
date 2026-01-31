import os
import hashlib
import json
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import plotly.colors as pc
import plotly.graph_objects as go
from plotly.basedatatypes import BaseTraceType


# ---- Page config ----
st.set_page_config(page_title="FS Sweep Visualizer (Spline)", layout="wide")

# ---- Layout constants ----
# NOTE: Keep the bottom-legend layout; axis overlap is handled by axis title standoff.
DEFAULT_FIGURE_WIDTH_PX = 1400  # Default figure width (px) when auto-width is disabled.
TOP_MARGIN_PX = 40  # Top margin (px); room for title/toolbar while keeping plot-area height stable.
BOTTOM_AXIS_PX = 60  # Bottom margin reserved for x-axis title/ticks (px); also defines plot-to-legend vertical gap.
LEFT_MARGIN_PX = 60  # Left margin (px); room for y-axis title and tick labels.
RIGHT_MARGIN_PX = 20  # Right margin (px); small breathing room to avoid clipping.
LEGEND_ROW_HEIGHT_FACTOR = 1.6  # legend row height ~= legend_font_size * factor
LEGEND_PADDING_PX = 18  # Extra padding (px) below legend to avoid clipping in exports.
# ---- Style settings (single source of truth) ----
# Use Plotly layout styling (not CSS) so on-page and exported PNGs match.
STYLE = {
    "font_family": "Open Sans, verdana, arial, sans-serif",
    "font_color": "#000000",
    "base_font_size_px": 14,
    "tick_font_size_px": 14,
    "axis_title_font_size_px": 16,
    # Space between x tick labels and the x-axis title (px). Set to None to use auto heuristic.
    "xaxis_title_standoff_px": None,
    # Space between y tick labels and the y-axis title (px). Set to None to use auto heuristic.
    "yaxis_title_standoff_px": None,
    "legend_font_size_px": 14,
    "bold_axis_titles": True,
}

AUTO_WIDTH_ESTIMATE_PX = 950  # Estimate width when Plotly auto-sizes (used for legend row estimation only).
WEB_LEGEND_MAX_HEIGHT_PX = 500  # Cap legend reserved area on-page to avoid huge gaps between charts.
DEFAULT_SPLINE_SMOOTHING = 1.0  # Default Plotly spline smoothing when spline mode is enabled.
EXPORT_IMAGE_SCALE = 4  # PNG scale factor for both modebar and "Full Legend" export.


def _clamp_int(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(val)))


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = (int(_clamp_int(c, 0, 255)) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix_rgb(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    tt = float(max(0.0, min(1.0, t)))
    return (
        int(round(a[0] + (b[0] - a[0]) * tt)),
        int(round(a[1] + (b[1] - a[1]) * tt)),
        int(round(a[2] + (b[2] - a[2]) * tt)),
    )


def _parse_color_to_rgb255(color: str) -> Tuple[int, int, int]:
    """
    Accept Plotly palette entries in either hex ("#rrggbb") or "rgb(...)" / "rgba(...)" form.
    """
    c = str(color).strip()
    if not c:
        return (68, 68, 68)
    if c.startswith("#"):
        return tuple(int(v) for v in pc.hex_to_rgb(c))
    if c.lower().startswith("rgb"):
        tup = pc.unlabel_rgb(c)
        if len(tup) >= 3:
            return (int(round(tup[0])), int(round(tup[1])), int(round(tup[2])))
    # handle hex without '#'
    c2 = c.lstrip().lower()
    if len(c2) in (3, 6) and all(ch in "0123456789abcdef" for ch in c2):
        if len(c2) == 3:
            c2 = "".join([ch * 2 for ch in c2])
        return tuple(int(v) for v in pc.hex_to_rgb(f"#{c2}"))
    return (68, 68, 68)


def _shade_hex(base_hex: str, position: float) -> str:
    """
    Create a shade variant of a base color.

    `position` in [-1..1]:
      - negative => darken toward black
      - positive => lighten toward white
    """
    base_rgb = _parse_color_to_rgb255(base_hex)
    p = float(max(-1.0, min(1.0, position)))
    if p >= 0:
        # Lighten
        return _rgb_to_hex(_mix_rgb(base_rgb, (255, 255, 255), t=p * 0.40))
    # Darken
    return _rgb_to_hex(_mix_rgb(base_rgb, (0, 0, 0), t=(-p) * 0.25))


def build_clustered_case_colors(cases: List[str]) -> Dict[str, str]:
    """
    Assign colors so related cases cluster by hue, with lighter/darker shades inside each cluster.

    Location suffix (after `__`) is ignored for grouping.
    """
    if not cases:
        return {}

    bases = [split_case_location(c)[0] for c in cases]
    split_parts = [str(b).split("_") for b in bases]
    max_parts = max((len(p) for p in split_parts), default=0)
    if max_parts <= 0:
        # Fallback to simple palette
        palette = pc.qualitative.Safe or pc.qualitative.Plotly or ["#1f77b4"]
        return {
            c: palette[i % len(palette)]
            for i, c in enumerate(sorted(cases))
        }

    # Normalize parts (pad with "")
    parts_norm = [p + [""] * (max_parts - len(p)) for p in split_parts]

    # Pick "hue part" as the most varying part (ties => earlier part).
    uniq_counts = [len(set(row[i] for row in parts_norm)) for i in range(max_parts)]
    varying = [i for i, n in enumerate(uniq_counts) if n > 1]
    if not varying:
        hue_part = 0
        shade_part = None
    else:
        hue_part = sorted(varying, key=lambda i: (-uniq_counts[i], i))[0]
        rest = [i for i in varying if i != hue_part]
        shade_part = sorted(rest, key=lambda i: (-uniq_counts[i], i))[0] if rest else None

    # Use a combined palette so we have enough distinct hues if there are many groups.
    palette = []
    for pal in (
        getattr(pc.qualitative, "Safe", None),
        getattr(pc.qualitative, "D3", None),
        getattr(pc.qualitative, "Plotly", None),
        getattr(pc.qualitative, "Dark24", None),
        getattr(pc.qualitative, "Light24", None),
    ):
        if pal:
            palette.extend(list(pal))
    if not palette:
        palette = ["#1f77b4"]

    # Group cases
    rows = []
    for case, parts in zip(cases, parts_norm):
        group = parts[hue_part]
        shade_key = parts[shade_part] if shade_part is not None else ""
        rows.append((str(group), str(shade_key), str(case)))

    groups = sorted(set(r[0] for r in rows))
    group_color = {g: palette[i % len(palette)] for i, g in enumerate(groups)}

    case_colors: Dict[str, str] = {}
    for g in groups:
        group_rows = [r for r in rows if r[0] == g]
        group_rows_sorted = sorted(group_rows, key=lambda r: (r[1], r[2]))
        k = len(group_rows_sorted)
        # Spread shades from darker to lighter.
        positions = np.linspace(-1.0, 1.0, k) if k > 1 else np.array([0.0])
        for (row, pos) in zip(group_rows_sorted, positions):
            _group, _shade_key, case = row
            case_colors[case] = _shade_hex(group_color[g], float(pos))

    return case_colors


@st.cache_data(show_spinner=False)
def cached_clustered_case_colors(cases: Tuple[str, ...]) -> Dict[str, str]:
    return build_clustered_case_colors(list(cases))


@st.cache_data(show_spinner=False)
def build_harmonic_shapes(
    n_min: float,
    n_max: float,
    f_base: float,
    show_markers: bool,
    bin_width_hz: float,
) -> Tuple[dict, ...]:
    if not show_markers and (bin_width_hz is None or bin_width_hz <= 0):
        return tuple()
    if not np.isfinite(n_min) or not np.isfinite(n_max) or n_min >= n_max:
        return tuple()
    if not np.isfinite(f_base) or f_base <= 0:
        return tuple()
    shapes: List[dict] = []
    k_start = max(1, int(np.floor(float(n_min))))
    k_end = int(np.ceil(float(n_max)))
    for k in range(k_start, k_end + 1):
        if show_markers:
            shapes.append(
                dict(
                    type="line",
                    xref="x",
                    yref="paper",
                    x0=k,
                    x1=k,
                    y0=0,
                    y1=1,
                    line=dict(color="rgba(0,0,0,0.3)", width=1.5),
                )
            )
        if bin_width_hz and bin_width_hz > 0:
            dn = (float(bin_width_hz) / (2.0 * float(f_base)))
            for edge in (k - dn, k + dn):
                shapes.append(
                    dict(
                        type="line",
                        xref="x",
                        yref="paper",
                        x0=edge,
                        x1=edge,
                        y0=0,
                        y1=1,
                        line=dict(color="rgba(0,0,0,0.2)", width=1, dash="dot"),
                    )
                )
    return tuple(shapes)


def _estimate_legend_height_px(n_traces: int, width_px: int, legend_entrywidth: int) -> int:
    usable_w = max(1, int(width_px) - int(LEFT_MARGIN_PX) - int(RIGHT_MARGIN_PX))
    cols = max(1, int(usable_w // max(1, int(legend_entrywidth))))
    rows = int(np.ceil(float(n_traces) / float(cols))) if n_traces > 0 else 0
    row_h = int(np.ceil(float(STYLE["legend_font_size_px"]) * float(LEGEND_ROW_HEIGHT_FACTOR)))
    return int(rows) * int(row_h) + int(LEGEND_PADDING_PX)


# ---- Data loading ----
@st.cache_data(show_spinner=False)
def load_fs_sweep_xlsx(path_or_buf) -> Dict[str, pd.DataFrame]:
    dfs: Dict[str, pd.DataFrame] = {}
    xls = pd.ExcelFile(path_or_buf)
    for name in ["R1", "X1", "R0", "X0"]:
        if name in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=name)
            # Frequency column normalization
            freq_col = None
            for c in df.columns:
                c_norm = str(c).strip().lower().replace(" ", "")
                if c_norm in ["frequency(hz)", "frequencyhz", "frequency_"]:
                    freq_col = c
                    break
                if str(c).strip().lower() in ["frequency (hz)", "frequency"]:
                    freq_col = c
                    break
            if freq_col is None:
                if "Frequency (Hz)" in df.columns:
                    freq_col = "Frequency (Hz)"
                else:
                    raise ValueError(f"Sheet '{name}' missing 'Frequency (Hz)' column")
            df = df.rename(columns={freq_col: "Frequency (Hz)"})
            df["Frequency (Hz)"] = pd.to_numeric(df["Frequency (Hz)"], errors="coerce")
            df = df.dropna(subset=["Frequency (Hz)"])
            value_cols = [c for c in df.columns if c != "Frequency (Hz)"]
            if value_cols:
                df[value_cols] = df[value_cols].apply(pd.to_numeric, errors="coerce")

            # Prepare numpy arrays once per file load; reused during reruns to speed trace building.
            freq_hz = df["Frequency (Hz)"].to_numpy(copy=False)
            series_map: Dict[object, np.ndarray] = {}
            for c in df.columns:
                if c == "Frequency (Hz)":
                    continue
                series_map[c] = df[c].to_numpy(copy=False)
            df.attrs["__prepared_arrays__"] = (freq_hz, series_map)

            dfs[name] = df
    return dfs


def list_case_columns(df: Optional[pd.DataFrame]) -> List[str]:
    if df is None:
        return []
    return [c for c in df.columns if c != "Frequency (Hz)"]


def split_case_location(name: str) -> Tuple[str, Optional[str]]:
    if "__" in str(name):
        base, loc = str(name).split("__", 1)
        loc = loc if loc else None
        return base, loc
    return str(name), None


def display_case_name(name: str) -> str:
    base, _ = split_case_location(name)
    return base


def prepare_sheet_arrays(df: pd.DataFrame) -> Tuple[np.ndarray, Dict[object, np.ndarray]]:
    cached = getattr(df, "attrs", {}).get("__prepared_arrays__")
    if cached is not None:
        return cached

    freq_hz = df["Frequency (Hz)"].to_numpy(copy=False)
    series_map: Dict[object, np.ndarray] = {}
    for c in df.columns:
        if c == "Frequency (Hz)":
            continue
        series_map[c] = df[c].to_numpy(copy=False)
    return freq_hz, series_map


@st.cache_data(show_spinner=False)
def split_case_parts(cases: List[str]) -> Tuple[List[List[str]], List[str]]:
    if not cases:
        return [], []
    temp_parts: List[Tuple[List[str], str]] = []
    max_parts = 0
    for name in cases:
        base_name, location = split_case_location(name)
        base_parts = str(base_name).split("_")
        max_parts = max(max_parts, len(base_parts))
        temp_parts.append((base_parts, location or ""))

    normalized: List[List[str]] = []
    for base_parts, location in temp_parts:
        padded = list(base_parts)
        if len(padded) < max_parts:
            padded.extend([""] * (max_parts - len(padded)))
        padded.append(location or "")
        normalized.append(padded)

    labels = [f"Case part {i+1}" for i in range(max_parts)] + ["Location"]
    return normalized, labels


@st.cache_data(show_spinner=False)
def _checkbox_key_map(col_key: str, options_disp: Tuple[str, ...]) -> Dict[str, str]:
    keys: Dict[str, str] = {}
    for o in options_disp:
        h = hashlib.sha1(o.encode("utf-8")).hexdigest()[:12]
        keys[o] = f"{col_key}__opt__{h}"
    return keys


def build_filters_for_case_parts(all_cases: List[str]) -> Tuple[List[str], List[str]]:
    st.sidebar.header("Case Filters")
    if not all_cases:
        return [], []
    parts_matrix, part_labels = split_case_parts(all_cases)
    if not part_labels:
        return all_cases, []

    reset_all = st.sidebar.button("Reset all filters", key="case_filters_reset_all")
    keep = np.ones(len(all_cases), dtype=bool)
    parts_arr = np.array(parts_matrix, dtype=object)  # shape=(n_cases, n_parts)
    for i, label in enumerate(part_labels):
        col_key = f"case_part_{i+1}_ms"
        options = sorted(set(parts_arr[:, i].tolist()))
        options_disp = [o if o != "" else "<empty>" for o in options]

        # init/sanitize
        if reset_all or col_key not in st.session_state:
            st.session_state[col_key] = list(options_disp)
        else:
            st.session_state[col_key] = [v for v in st.session_state[col_key] if v in options_disp]

        st.sidebar.markdown(label)
        c1, _c2 = st.sidebar.columns([1, 1])

        checkbox_keys = _checkbox_key_map(col_key, tuple(options_disp))

        if c1.button("Select all", key=f"{col_key}_all"):
            st.session_state[col_key] = list(options_disp)
            for o in options_disp:
                st.session_state[checkbox_keys[o]] = True

        if _c2.button("Clear all", key=f"{col_key}_none"):
            st.session_state[col_key] = []
            for o in options_disp:
                st.session_state[checkbox_keys[o]] = False

        if reset_all:
            for o in options_disp:
                st.session_state[checkbox_keys[o]] = True

        selected_disp: List[str] = []
        selected_set = set(st.session_state[col_key])
        cols = st.sidebar.columns(2)
        for idx, o in enumerate(options_disp):
            opt_key = checkbox_keys[o]
            if opt_key not in st.session_state:
                st.session_state[opt_key] = o in selected_set
            checked = cols[idx % 2].checkbox(o, key=opt_key)
            if checked:
                selected_disp.append(o)
        st.session_state[col_key] = selected_disp

        if i < len(part_labels) - 1:
            st.sidebar.markdown("---")

        selected_raw = ["" if s == "<empty>" else s for s in selected_disp]
        if 0 < len(selected_raw) < len(options):
            mask_i = np.isin(parts_arr[:, i], selected_raw)
            keep &= mask_i
        if len(selected_raw) == 0:
            keep &= False
    filtered = [c for c, k in zip(all_cases, keep) if k]
    return filtered, part_labels


def compute_common_n_range(f_series: List[pd.Series], f_base: float) -> Tuple[float, float]:
    vals: List[float] = []
    for s in f_series:
        if s is None:
            continue
        v = s
        if not pd.api.types.is_numeric_dtype(v):
            v = pd.to_numeric(v, errors="coerce")
        v = v.dropna()
        if not v.empty:
            vals.extend([v.min() / f_base, v.max() / f_base])
    if not vals:
        return 0.0, 1.0
    lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    return (0.0, 1.0) if (not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi) else (lo, hi)


def make_spline_traces(
    df: pd.DataFrame,
    cases: List[str],
    f_base: float,
    y_title: str,
    smooth: float,
    enable_spline: bool,
    strip_location_suffix: bool,
    case_colors: Dict[str, str],
) -> Tuple[List[BaseTraceType], Optional[pd.Series]]:
    if df is None:
        return [], None
    cd, y_map = prepare_sheet_arrays(df)
    n = cd / float(f_base)
    traces: List[BaseTraceType] = []
    TraceCls = go.Scatter if enable_spline else go.Scattergl
    for case in cases:
        y = y_map.get(case)
        if y is None:
            continue
        color = case_colors.get(case)
        tr = TraceCls(
            x=n,
            y=y,
            customdata=cd,
            mode="lines",
            name=display_case_name(case) if strip_location_suffix else str(case),
            meta={"legend_color": color},
            line=dict(color=color),
            hovertemplate=(
                "Case=%{fullData.name}<br>f=%{customdata:.1f} Hz" + f"<br>{y_title}=%{{y}}<extra></extra>"
            ),
        )
        if enable_spline and isinstance(tr, go.Scatter):
            tr.update(line=dict(shape="spline", smoothing=float(smooth), simplify=False, color=color))
        traces.append(tr)
    return traces, df["Frequency (Hz)"]


def apply_common_layout(
    fig: go.Figure,
    plot_height: int,
    y_title: str,
    legend_entrywidth: int,
    n_traces: int,
    use_auto_width: bool,
    figure_width_px: int,
    uirevision_key: str,
):
    font_base = dict(family=STYLE["font_family"], color=STYLE["font_color"])
    # Reserve legend space below the plot so the legend stays at the bottom on-page.
    # Increase x-axis reserved space when fonts are larger to reduce title/tick overlap on zoom.
    bottom_axis_px = int(
        max(
            int(BOTTOM_AXIS_PX),
            int(round(float(STYLE["tick_font_size_px"]) * 2.4 + float(STYLE["axis_title_font_size_px"]) * 1.6)),
        )
    )
    est_width_px = int(figure_width_px) if not use_auto_width else int(AUTO_WIDTH_ESTIMATE_PX)
    legend_h_full = _estimate_legend_height_px(int(n_traces), est_width_px, int(legend_entrywidth))
    legend_h = min(int(WEB_LEGEND_MAX_HEIGHT_PX), int(legend_h_full))
    total_height = int(plot_height) + int(TOP_MARGIN_PX) + int(bottom_axis_px) + int(legend_h)
    legend_y = -float(bottom_axis_px) / float(max(1, int(plot_height)))

    # Y-axis overlap fix: keep bottom legend behavior, but grow left margin with font sizes
    # so y tick labels and y title don't collide after zoom.
    left_margin_px = int(
        max(
            int(LEFT_MARGIN_PX),
            int(round(float(STYLE["tick_font_size_px"]) * 4.4 + float(STYLE["axis_title_font_size_px"]) * 1.6)),
        )
    )

    fig.update_layout(
        autosize=bool(use_auto_width),
        height=total_height,
        # Keep zoom/pan on Streamlit reruns; include file id in the key so loading a new file resets zoom.
        uirevision=str(uirevision_key),
        font=dict(
            **font_base,
            size=int(STYLE["base_font_size_px"]),
        ),
        margin=dict(
            l=left_margin_px,
            r=RIGHT_MARGIN_PX,
            t=TOP_MARGIN_PX,
            b=int(bottom_axis_px) + int(legend_h),
        ),
        margin_autoexpand=False,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=legend_y,
            xanchor="center",
            x=0.5,
            entrywidth=int(legend_entrywidth),
            entrywidthmode="pixels",
            font=dict(**font_base, size=int(STYLE["legend_font_size_px"])),
        ),
    )
    if not use_auto_width:
        fig.update_layout(width=int(figure_width_px), autosize=False)

    x_title = "Harmonic number n = f / f_base"
    y_title_txt = str(y_title)
    if bool(STYLE.get("bold_axis_titles", True)):
        x_title = f"<b>{x_title}</b>"
        y_title_txt = f"<b>{y_title_txt}</b>"

    axis_title_font = dict(**font_base, size=int(STYLE["axis_title_font_size_px"]))
    tick_font = dict(**font_base, size=int(STYLE["tick_font_size_px"]))

    x_title_standoff = STYLE.get("xaxis_title_standoff_px")
    if x_title_standoff is None:
        x_title_standoff = int(max(10, round(float(STYLE["tick_font_size_px"]) * 1.1)))
    else:
        x_title_standoff = int(x_title_standoff)

    y_title_standoff = STYLE.get("yaxis_title_standoff_px")
    if y_title_standoff is None:
        y_title_standoff = int(max(10, round(float(STYLE["tick_font_size_px"]) * 1.1)))
    else:
        y_title_standoff = int(y_title_standoff)
    fig.update_xaxes(
        title_text=x_title,
        tick0=1,
        dtick=1,
        title_font=axis_title_font,
        tickfont=tick_font,
        automargin=True,
        title_standoff=x_title_standoff,
    )
    fig.update_yaxes(
        title_text=y_title_txt,
        title_font=axis_title_font,
        tickfont=tick_font,
        automargin=True,
        title_standoff=y_title_standoff,
    )


def build_plot_spline(df: Optional[pd.DataFrame], cases: List[str], f_base: float, plot_height: int, y_title: str,
                      smooth: float, enable_spline: bool, legend_entrywidth: int, strip_location_suffix: bool,
                      use_auto_width: bool, figure_width_px: int, case_colors: Dict[str, str], uirevision_key: str
                      ) -> Tuple[go.Figure, Optional[pd.Series]]:
    traces, f_series = make_spline_traces(df, cases, f_base, y_title, smooth, enable_spline, strip_location_suffix, case_colors)
    fig = go.Figure(data=traces)
    apply_common_layout(fig, plot_height, y_title, legend_entrywidth, len(traces), use_auto_width, figure_width_px, uirevision_key=uirevision_key)
    return fig, f_series


def build_x_over_r_spline(df_r: Optional[pd.DataFrame], df_x: Optional[pd.DataFrame], cases: List[str], f_base: float,
                          plot_height: int, seq_label: str, smooth: float, legend_entrywidth: int,
                          enable_spline: bool,
                          strip_location_suffix: bool, use_auto_width: bool, figure_width_px: int,
                          case_colors: Dict[str, str], uirevision_key: str
                          ) -> Tuple[go.Figure, Optional[pd.Series], int, int]:
    fig = go.Figure()
    xr_dropped = 0
    xr_total = 0
    f_series = None
    eps = 1e-9
    TraceCls = go.Scatter if enable_spline else go.Scattergl
    if df_r is not None and df_x is not None:
        cd, r_map = prepare_sheet_arrays(df_r)
        _cd2, x_map = prepare_sheet_arrays(df_x)
        n = cd / float(f_base)
        both = [c for c in cases if (c in r_map and c in x_map)]
        f_series = df_r["Frequency (Hz)"]
        for case in both:
            r = r_map[case]
            x = x_map[case]
            denom_ok = np.abs(r) >= eps
            bad = (~denom_ok) | np.isnan(r) | np.isnan(x)
            y = np.where(denom_ok, x / r, np.nan)
            xr_dropped += int(np.count_nonzero(bad))
            xr_total += int(r.size)
            color = case_colors.get(case)
            tr = TraceCls(
                x=n,
                y=y,
                customdata=cd,
                mode="lines",
                name=display_case_name(case) if strip_location_suffix else str(case),
                meta={"legend_color": color},
                line=dict(color=color),
                hovertemplate=(
                    "Case=%{fullData.name}<br>f=%{customdata:.1f} Hz<br>X/R=%{y}<extra></extra>"
                ),
            )
            if enable_spline and isinstance(tr, go.Scatter):
                tr.update(line=dict(shape="spline", smoothing=float(smooth), simplify=False, color=color))
            fig.add_trace(tr)
    y_title = "X1/R1 (unitless)" if seq_label == "Positive" else "X0/R0 (unitless)"
    apply_common_layout(fig, plot_height, y_title, legend_entrywidth, len(fig.data), use_auto_width, figure_width_px, uirevision_key=uirevision_key)
    return fig, f_series, xr_dropped, xr_total


def _render_client_png_download(
    filename: str,
    scale: int,
    button_label: str,
    plot_height: int,
    legend_entrywidth: int,
    plot_index: int,
):
    dom_id = hashlib.sha1(f"{filename}|{scale}|{plot_height}|{legend_entrywidth}|{plot_index}".encode("utf-8")).hexdigest()[:12]
    html = f"""
    <div id="exp-{dom_id}">
      <button id="btn-{dom_id}" style="padding:6px 10px; font-size: 0.9rem; cursor:pointer;">
        {button_label}
      </button>
      <div id="plot-{dom_id}" style="width:1px; height:1px; position:absolute; left:-99999px; top:-99999px;"></div>
    </div>
    <script>
      const scale = {int(scale)};
      const plotHeight = {int(plot_height)};
      const topMargin = {int(TOP_MARGIN_PX)};
      const bottomAxis = {int(BOTTOM_AXIS_PX)};
      const legendPad = {int(LEGEND_PADDING_PX)};
      const legendEntryWidth = {int(legend_entrywidth)};
      const plotIndex = {int(plot_index)};
      const filename = {json.dumps(filename)};
      const fallbackLegendFontSize = {int(STYLE["legend_font_size_px"])};

      async function doExport() {{
        try {{
          const Plotly = window.parent?.Plotly;
          if (!Plotly) return;
          const plots = window.parent?.document?.querySelectorAll?.("div.js-plotly-plot");
          if (!plots || plots.length <= plotIndex) return;
          const gd = plots[plotIndex];
          if (!gd) return;

          const r = gd.getBoundingClientRect();
          const widthPx = Math.floor(r.width || 0);
          if (!widthPx) return;

          const legendFontSize =
            gd?._fullLayout?.legend?.font?.size ||
            gd?._fullLayout?.font?.size ||
            fallbackLegendFontSize;
          // Legend row height: keep tight to avoid bottom whitespace.
          const legendRowH = Math.ceil(legendFontSize * 1.25);
      const legendFontFamily = {json.dumps(STYLE["font_family"])};
      const legendFontColor = {json.dumps(STYLE["font_color"])};

      const leftMarginBase = {int(LEFT_MARGIN_PX)};
      const rightMargin = {int(RIGHT_MARGIN_PX)};
      const tickFontSize = {int(STYLE["tick_font_size_px"])};
      const axisTitleFontSize = {int(STYLE["axis_title_font_size_px"])};
      const leftMarginPx = Math.max(leftMarginBase, Math.round(tickFontSize * 4.4 + axisTitleFontSize * 1.6));

          const data = Array.isArray(gd.data) ? gd.data : [];
          const data2 = data.map((tr) => {{
            const t = Object.assign({{}}, tr);
            if (t.type === "scattergl") t.type = "scatter";
            return t;
          }});
          const legendItems = [];
          for (const tr of data2) {{
            const name = tr && tr.name ? String(tr.name) : "";
            if (!name) continue;
            const color =
              (tr.meta && tr.meta.legend_color) ? tr.meta.legend_color :
              (tr.line && tr.line.color) ? tr.line.color :
              (tr.marker && tr.marker.color) ? tr.marker.color :
              "#444";
            legendItems.push({{name, color}});
          }}

          const usableW = Math.max(1, widthPx - leftMarginPx - rightMargin);

          // Estimate needed entry width using canvas text measurement to avoid overlap for long names.
          let maxTextW = 0;
          try {{
            const canvas = document.createElement("canvas");
            const ctx = canvas.getContext("2d");
            if (ctx) {{
              ctx.font = legendFontSize + "px " + legendFontFamily;
              for (const it of legendItems) {{
                const w = ctx.measureText(it.name).width || 0;
                if (w > maxTextW) maxTextW = w;
              }}
            }}
          }} catch (e) {{}}

          const sampleLinePx = Math.max(18, Math.round(1.8 * legendFontSize));
          const sampleGapPx = Math.max(6, Math.round(0.6 * legendFontSize));
          const textPadPx = Math.max(8, Math.round(0.8 * legendFontSize));
          const neededEntryPx = Math.ceil(sampleLinePx + sampleGapPx + maxTextW + textPadPx);
          const entryPx = Math.max(1, Math.max(legendEntryWidth, neededEntryPx));

          const cols = Math.max(1, Math.floor(usableW / entryPx));
          const rows = Math.ceil(legendItems.length / cols);
          // Total legend area in bottom margin.
          // Add a small tail so the last row doesn't look cramped, but avoid large blank space.
          const legendH = (rows * legendRowH) + legendPad + Math.ceil(0.35 * legendFontSize);

          const newHeight = plotHeight + topMargin + bottomAxis + legendH;
          const newMarginB = bottomAxis + legendH;

          const container = document.getElementById("plot-{dom_id}");
          if (!container) return;
          container.style.width = widthPx + "px";
          container.style.height = newHeight + "px";

          const baseLayout = Object.assign({{}}, gd.layout || {{}});
          baseLayout.width = widthPx;
          baseLayout.height = newHeight;
          baseLayout.autosize = false;
          baseLayout.margin = Object.assign({{}}, baseLayout.margin || {{}});
          baseLayout.margin.t = topMargin;
          baseLayout.margin.l = leftMarginPx;
          baseLayout.margin.r = rightMargin;
          baseLayout.margin.b = newMarginB;
          // Disable Plotly legend and draw a manual legend in the bottom margin so it never scrolls/clips.
          baseLayout.showlegend = false;
          for (const tr of data2) {{
            tr.showlegend = false;
          }}

          const ann = Array.isArray(baseLayout.annotations) ? baseLayout.annotations.slice() : [];
          const shp = Array.isArray(baseLayout.shapes) ? baseLayout.shapes.slice() : [];

          // Spread columns across the available width to avoid side whitespace and tight columns.
          const colW = usableW / cols;
          const xPadPx = Math.max(0, Math.min(12, Math.floor(colW * 0.06)));

          for (let i = 0; i < legendItems.length; i++) {{
            const row = Math.floor(i / cols);
            const col = i % cols;
            const x0 = (col * colW + xPadPx) / usableW;
            const x1 = x0 + (sampleLinePx / usableW);
            const y = -(bottomAxis + legendPad + (row + 0.6) * legendRowH) / Math.max(1, plotHeight);

            shp.push({{
              type: "line",
              xref: "paper",
              yref: "paper",
              x0, x1,
              y0: y, y1: y,
              line: {{color: legendItems[i].color, width: 2}}
            }});

            ann.push({{
              xref: "paper",
              yref: "paper",
              x: x1 + (sampleGapPx / usableW),
              y,
              xanchor: "left",
              yanchor: "middle",
              showarrow: false,
              align: "left",
              text: legendItems[i].name,
              font: {{size: legendFontSize, family: legendFontFamily, color: legendFontColor}}
            }});
          }}

          baseLayout.annotations = ann;
          baseLayout.shapes = shp;

          await Plotly.newPlot(container, data2, baseLayout, {{displayModeBar: false, staticPlot: true}});
          const url = await Plotly.toImage(container, {{format: "png", width: widthPx, height: newHeight, scale}});
          const a = document.createElement("a");
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          a.remove();
        }} catch (e) {{
        }} finally {{
          try {{
            const container = document.getElementById("plot-{dom_id}");
            if (container) {{
              container.innerHTML = "";
              container.style.width = "1px";
              container.style.height = "1px";
            }}
          }} catch (e) {{}}
        }}
      }}

      document.getElementById("btn-{dom_id}").addEventListener("click", doExport);
    </script>
    """
    components.html(html, height=70)


def _get_query_params() -> Dict[str, List[str]]:
    # Streamlit renamed query param APIs over time; support both.
    try:
        qp = st.query_params  # type: ignore[attr-defined]
        out: Dict[str, List[str]] = {}
        for k in qp.keys():
            out[str(k)] = [str(x) for x in qp.get_all(k)]
        return out
    except Exception:
        try:
            qp = st.experimental_get_query_params()
            return {str(k): [str(x) for x in v] for k, v in qp.items()}
        except Exception:
            return {}


def _apply_zoom_from_query_params(fig: go.Figure, plot_index: int, data_id: str) -> None:
    qp = _get_query_params()
    if not qp:
        return
    if qp.get("zoom_file", [""])[0] != str(data_id):
        return

    def _f(key: str) -> Optional[float]:
        raw = qp.get(key, [None])[0]
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    x0 = _f(f"z{plot_index}_x0")
    x1 = _f(f"z{plot_index}_x1")
    y0 = _f(f"z{plot_index}_y0")
    y1 = _f(f"z{plot_index}_y1")

    if x0 is not None and x1 is not None:
        fig.update_xaxes(range=[x0, x1], autorange=False)
    if y0 is not None and y1 is not None:
        fig.update_yaxes(range=[y0, y1], autorange=False)


def _bind_zoom_to_query_params(data_id: str, plot_count: int = 3) -> None:
    # Frontend helper: listen for Plotly relayout events and write axis ranges into the URL query string.
    # On the next Streamlit rerun, Python reads these params and reapplies the ranges before rendering.
    html = f"""
    <div style="display:none"></div>
    <script>
      (function () {{
        const parentWin = window.parent || window;
        const installTag = "fs_sweep_zoom_qp_v4";
        try {{
          if (parentWin.__fsSweepZoomQPInstalled === installTag && typeof parentWin.__fsSweepZoomQPKick === "function") {{
            parentWin.__fsSweepZoomQPKick();
            return;
          }}
          parentWin.__fsSweepZoomQPInstalled = installTag;
        }} catch (e) {{}}

        const plotCount = {int(plot_count)};
        const dataId = {json.dumps(str(data_id))};

        function setParam(params, k, v) {{
          if (v === null || v === undefined) params.delete(k);
          else params.set(k, String(v));
        }}

        function applyParams(idx, xr, yr) {{
          try {{
            const url = new URL(parentWin.location.href);
            const p = url.searchParams;
            p.set("zoom_file", dataId);
            setParam(p, "z" + idx + "_x0", xr ? xr[0] : null);
            setParam(p, "z" + idx + "_x1", xr ? xr[1] : null);
            setParam(p, "z" + idx + "_y0", yr ? yr[0] : null);
            setParam(p, "z" + idx + "_y1", yr ? yr[1] : null);
            parentWin.history.replaceState({{}}, "", url.toString());
          }} catch (e) {{}}
        }}

        function bind(gd, idx) {{
          if (!gd) return;
          gd.on?.("plotly_relayout", function (evt) {{
            if (!evt || typeof evt !== "object") return;

            let xr = null;
            let yr = null;
            if (evt["xaxis.range[0]"] != null && evt["xaxis.range[1]"] != null) {{
              xr = [evt["xaxis.range[0]"], evt["xaxis.range[1]"]];
            }}
            if (evt["yaxis.range[0]"] != null && evt["yaxis.range[1]"] != null) {{
              yr = [evt["yaxis.range[0]"], evt["yaxis.range[1]"]];
            }}
            if (evt["xaxis.autorange"] === true) xr = null;
            if (evt["yaxis.autorange"] === true) yr = null;

            applyParams(idx, xr, yr);
          }});
        }}

        function getPlots() {{
          const out = [];
          try {{
            const direct = parentWin.document?.querySelectorAll?.("div.js-plotly-plot") || [];
            for (const el of direct) out.push(el);
          }} catch (e) {{}}
          try {{
            const iframes = parentWin.document?.querySelectorAll?.("iframe") || [];
            for (const fr of iframes) {{
              try {{
                const doc = fr.contentWindow?.document;
                const inner = doc?.querySelectorAll?.("div.js-plotly-plot") || [];
                for (const el of inner) out.push(el);
              }} catch (e) {{}}
            }}
          }} catch (e) {{}}
          return out;
        }}

        function syncOnce() {{
          try {{
            const plots = getPlots();
            const n = Math.min(plotCount, plots.length);
            for (let i = 0; i < n; i++) {{
              // Force rebind: Streamlit can re-init Plotly inside the same DOM node and drop handlers.
              try {{ plots[i]?.removeAllListeners?.("plotly_relayout"); }} catch (e) {{}}
              bind(plots[i], i);
            }}
          }} catch (e) {{}}
        }}

        function kick() {{
          let tries = 0;
          (function tick() {{
            syncOnce();
            tries += 1;
            if (tries < 30) parentWin.setTimeout(tick, 100);
          }})();
        }}

        try {{
          parentWin.__fsSweepZoomQPKick = kick;
        }} catch (e) {{}}

        kick();
      }})();
    </script>
    """
    components.html(html, height=0)




def main():
    st.title("FS Sweep Visualizer (Spline)")

    # Data source
    default_path = "FS_sweep.xlsx"
    st.sidebar.header("Data Source")
    up = st.sidebar.file_uploader("Upload Excel", type=["xlsx"], help="If empty, loads 'FS_sweep.xlsx' from this folder.")
    data_id = "unknown"
    try:
        if up is not None:
            data = load_fs_sweep_xlsx(up)
            try:
                b = up.getvalue()
                data_id = hashlib.sha1(b).hexdigest()[:10]
            except Exception:
                data_id = f"upload:{getattr(up, 'name', 'file')}"
        elif os.path.exists(default_path):
            data = load_fs_sweep_xlsx(default_path)
            try:
                data_id = f"local:{int(os.path.getmtime(default_path))}"
            except Exception:
                data_id = "local"
            st.sidebar.info(f"Loaded local file: {default_path}")
        else:
            st.warning("Upload an Excel file or place 'FS_sweep.xlsx' here.")
            st.stop()
    except Exception as e:
        st.error(f"Failed to load Excel: {e}")
        st.stop()

    # Controls
    st.sidebar.header("Controls")
    seq_label = st.sidebar.radio("Sequence", ["Positive", "Zero"], index=0)
    seq = ("R1", "X1") if seq_label == "Positive" else ("R0", "X0")
    base_label = st.sidebar.radio("Base frequency", ["50 Hz", "60 Hz"], index=0)
    f_base = 50.0 if base_label.startswith("50") else 60.0
    plot_height = st.sidebar.slider("Plot area height (px)", min_value=100, max_value=1000, value=400, step=25)
    use_auto_width = st.sidebar.checkbox("Auto width (fit container)", value=True)
    figure_width_px = DEFAULT_FIGURE_WIDTH_PX
    if not use_auto_width:
        figure_width_px = st.sidebar.slider("Figure width (px)", min_value=800, max_value=2200, value=DEFAULT_FIGURE_WIDTH_PX, step=50)

    enable_spline = st.sidebar.checkbox("Spline (slow)", value=False)
    smooth = float(DEFAULT_SPLINE_SMOOTHING)
    if enable_spline:
        prev_smooth = st.session_state.get("spline_smoothing", float(DEFAULT_SPLINE_SMOOTHING))
        try:
            prev_smooth_f = float(prev_smooth)
        except Exception:
            prev_smooth_f = float(DEFAULT_SPLINE_SMOOTHING)
        prev_smooth_f = max(0.0, min(1.3, prev_smooth_f))
        smooth = st.sidebar.slider(
            "Spline smoothing",
            min_value=0.0,
            max_value=1.3,
            value=prev_smooth_f,
            step=0.05,
            key="spline_smoothing",
        )

    # Legend/Export controls
    st.sidebar.header("Legend & Export")
    auto_legend_entrywidth = st.sidebar.checkbox("Auto legend column width", value=True)
    legend_entrywidth = 180
    if not auto_legend_entrywidth:
        legend_entrywidth = st.sidebar.slider("Legend column width (px)", min_value=50, max_value=300, value=180, step=10)

    # Keep the download buttons visually within the "Legend & Export" section,
    # but fill their contents later once figures are built.
    download_area = st.sidebar.container()

    download_config = {
        "toImageButtonOptions": {
            "format": "png",
            "filename": "plot",
            "scale": int(EXPORT_IMAGE_SCALE),
        }
    }

    # Cases / filters
    df_r = data.get(seq[0])
    df_x = data.get(seq[1])
    if df_r is None and df_x is None:
        st.error(f"Missing sheets for sequence '{seq_label}' ({seq[0]}/{seq[1]}).")
        st.stop()
    all_cases = sorted(list({*list_case_columns(df_r), *list_case_columns(df_x)}))
    filtered_cases, part_labels = build_filters_for_case_parts(all_cases)
    if not filtered_cases:
        st.warning("No cases after filtering. Adjust filters.")
        st.stop()

    strip_location_suffix = False
    if part_labels and part_labels[-1] == "Location":
        loc_key = f"case_part_{len(part_labels)}_ms"
        selected_disp = st.session_state.get(loc_key, [])
        selected_raw = ["" if s == "<empty>" else s for s in selected_disp]
        strip_location_suffix = len(selected_raw) == 1

    if auto_legend_entrywidth:
        display_names = [display_case_name(c) if strip_location_suffix else str(c) for c in filtered_cases]
        max_len = max((len(n) for n in display_names), default=12)
        legend_font_px = int(STYLE["legend_font_size_px"])
        approx_char_px = max(7, int(round(0.60 * float(legend_font_px))))
        base_px = max(44, int(round(3.5 * float(legend_font_px))))  # symbol + padding inside a legend item

        # Only used to cap export legend columns; use the configured width when available.
        est_width_px = int(figure_width_px)
        usable_w = max(1, int(est_width_px) - int(LEFT_MARGIN_PX) - int(RIGHT_MARGIN_PX))
        desired = int(max_len) * int(approx_char_px) + int(base_px)
        legend_entrywidth = _clamp_int(desired, 50, min(900, usable_w))
        if legend_entrywidth >= int(usable_w * 0.95):
            legend_entrywidth = usable_w

    case_colors = cached_clustered_case_colors(tuple(filtered_cases))

    # Harmonic decorations
    show_harmonics = st.sidebar.checkbox("Show harmonic lines", value=True)
    bin_width_hz = st.sidebar.number_input("Bin width (Hz)", min_value=0.0, value=0.0, step=1.0, help="0 disables tolerance bands")

    # Build plots
    r_title = "R1 (\u03A9)" if seq_label == "Positive" else "R0 (\u03A9)"
    x_title = "X1 (\u03A9)" if seq_label == "Positive" else "X0 (\u03A9)"
    zoom_key = f"zoom:{data_id}"

    fig_r, f_r = build_plot_spline(
        df_r,
        filtered_cases,
        f_base,
        plot_height,
        r_title,
        smooth,
        enable_spline,
        legend_entrywidth,
        strip_location_suffix,
        use_auto_width,
        figure_width_px,
        case_colors,
        uirevision_key=f"{zoom_key}:r",
    )
    fig_x, f_x = build_plot_spline(
        df_x,
        filtered_cases,
        f_base,
        plot_height,
        x_title,
        smooth,
        enable_spline,
        legend_entrywidth,
        strip_location_suffix,
        use_auto_width,
        figure_width_px,
        case_colors,
        uirevision_key=f"{zoom_key}:x",
    )
    fig_xr, f_xr, xr_dropped, xr_total = build_x_over_r_spline(
        df_r,
        df_x,
        filtered_cases,
        f_base,
        plot_height,
        seq_label,
        smooth,
        legend_entrywidth,
        enable_spline,
        strip_location_suffix,
        use_auto_width,
        figure_width_px,
        case_colors,
        uirevision_key=f"{zoom_key}:xr",
    )

    f_refs = [s for s in [f_r, f_x, f_xr] if s is not None]
    n_lo, n_hi = compute_common_n_range(f_refs, f_base)
    harm_shapes = build_harmonic_shapes(n_lo, n_hi, f_base, show_harmonics, bin_width_hz)
    for fig in (fig_r, fig_x, fig_xr):
        fig.update_xaxes(range=[n_lo, n_hi])
        if harm_shapes:
            fig.update_layout(shapes=(fig.layout.shapes + harm_shapes) if fig.layout.shapes else harm_shapes)

    # Restore zoom (overrides the default x-range above). Note: indices must match plot order on the page.
    _apply_zoom_from_query_params(fig_x, plot_index=0, data_id=data_id)
    _apply_zoom_from_query_params(fig_r, plot_index=1, data_id=data_id)
    _apply_zoom_from_query_params(fig_xr, plot_index=2, data_id=data_id)

    # Render
    st.subheader(f"Sequence: {seq_label} | Base: {int(f_base)} Hz")
    if xr_total > 0 and xr_dropped > 0:
        st.caption(f"X/R: dropped {xr_dropped} of {xr_total} points where |R| < 1e-9 or data missing.")

    export_scale = int(EXPORT_IMAGE_SCALE)
    with download_area:
        st.subheader("Download (Full Legend)")
        st.caption("Browser PNG download (temporarily expands the on-page chart legend, then downloads).")
        cols = st.columns(3)
        with cols[0]:
            _render_client_png_download(
                filename="X_full_legend.png",
                scale=export_scale,
                button_label="X PNG",
                plot_height=plot_height,
                legend_entrywidth=legend_entrywidth,
                plot_index=0,
            )
        with cols[1]:
            _render_client_png_download(
                filename="R_full_legend.png",
                scale=export_scale,
                button_label="R PNG",
                plot_height=plot_height,
                legend_entrywidth=legend_entrywidth,
                plot_index=1,
            )
        with cols[2]:
            _render_client_png_download(
                filename="X_over_R_full_legend.png",
                scale=export_scale,
                button_label="X/R PNG",
                plot_height=plot_height,
                legend_entrywidth=legend_entrywidth,
                plot_index=2,
            )

    st.plotly_chart(fig_x, use_container_width=bool(use_auto_width), config=download_config, key="plot_x")
    st.markdown("<div style='height:36px'></div>", unsafe_allow_html=True)
    st.plotly_chart(fig_r, use_container_width=bool(use_auto_width), config=download_config, key="plot_r")
    st.markdown("<div style='height:36px'></div>", unsafe_allow_html=True)
    st.plotly_chart(fig_xr, use_container_width=bool(use_auto_width), config=download_config, key="plot_xr")

    # Persist zoom/pan by writing axis ranges into URL query params on relayout.
    _bind_zoom_to_query_params(data_id=data_id, plot_count=3)


if __name__ == "__main__":
    main()
