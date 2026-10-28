"""Renders the MADA dashboard (fault-injection timeline) and console screenshots to PNG."""
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, NullFormatter
from .core import console

INK, MUTED, ACC, RED, GREEN, ORANGE = "#1f2430", "#8a94a6", "#2f6fdd", "#c0392b", "#2e8b57", "#d2691e"


def snap(path, title):
    """Export everything printed on the console since the last snap as a terminal-style PNG."""
    import cairosvg   # only needed for screenshots
    svg = console.export_svg(title=title, clear=True)
    svg = re.sub(r"font-family:[^;\"]*", "font-family: DejaVu Sans Mono", svg)
    svg = svg.replace("Fira Code", "DejaVu Sans Mono")
    cairosvg.svg2png(bytestring=svg.encode(), write_to=path, output_width=1700)
    return path


def fault_timeline(series, faults, events, path):
    t0 = series[0]["t"]
    ts = [(r["t"] - t0) / 60 for r in series]
    fig, axes = plt.subplots(3, 1, figsize=(13, 8.2), sharex=True, gridspec_kw={"height_ratios": [1.2, 1.2, 1]})
    fig.patch.set_facecolor("white")
    panels = [("p95_oltp_ms", "OLTP p95 latency (ms)", ACC, True), ("p95_olap_ms", "OLAP p95 latency (ms)", ORANGE, True)]
    for ax, (k, lab, col, log) in zip(axes, panels):
        ax.plot(ts, [r.get(k, None) for r in series], color=col, lw=1.4)
        ax.set_ylabel(lab, fontsize=10, color=INK)
        if log:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
            ax.yaxis.set_minor_formatter(NullFormatter())
    ax = axes[2]
    ax.plot(ts, [r.get("connections", 0) for r in series], color=MUTED, lw=1.2, label="connections")
    ax.plot(ts, [r.get("lock_waiting", 0) for r in series], color=RED, lw=1.2, label="sessions waiting on locks")
    ax.plot(ts, [r.get("dead:orders", 0) / 10000 for r in series], color=GREEN, lw=1.2, label="dead rows in orders (×10k)")
    ax.legend(loc="upper left", fontsize=8.5, frameon=False, ncol=3)
    ax.set_xlabel("minutes since start of the fault run", fontsize=10)
    for f in faults:
        a, b = (f["inject_t"] - t0) / 60, (f.get("end_t", f["inject_t"] + 60) - t0) / 60
        for axx in axes:
            axx.axvspan(a, b, color="#f1c6c1", alpha=0.35, lw=0)
        axes[0].text(a + 0.05, 0.93, f["key"], fontsize=9, color=RED, fontweight="bold", transform=axes[0].get_xaxis_transform())
    for e in events:
        if e["kind"] in ("alert", "security") and e["t"] >= series[0]["t"]:
            for axx in axes:
                axx.axvline((e["t"] - t0) / 60, color=ORANGE, lw=0.9, ls="--")
        if e["kind"] == "applied" and e["t"] >= series[0]["t"]:
            for axx in axes:
                axx.axvline((e["t"] - t0) / 60, color=GREEN, lw=1.1)
    for axx in axes:
        axx.spines[["top", "right"]].set_visible(False)
        axx.tick_params(labelsize=9, colors=INK)
        axx.grid(axis="y", color="#e6e8ec", lw=0.7)
    fig.suptitle("MADA dashboard · fault-injection run   (red bands: fault active · orange dashed: alert · green: fix applied)",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
