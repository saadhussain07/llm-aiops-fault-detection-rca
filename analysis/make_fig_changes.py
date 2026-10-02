"""Figure: verdict changes vs the live first vote, NOTE vs control cycles.
Counts from first_vote_compare.py (Table 5 of the manuscript)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SC = ["S1", "S2", "S3", "S4", "S5"]
# (changed, total) per scenario: NOTE, control ; None = no such cycles
DATA = {
    "E1: gpt-oss-120b, NOTE removed": {
        "note": [(0,25),(17,20),(0,29),(0,12),(1,10)],
        "ctrl": [(3,33),(12,12),None,(0,36),None]},
    "E2: gpt-oss-20b": {
        "note": [(0,25),(0,20),(0,29),(0,12),(0,10)],
        "ctrl": [(3,33),(5,12),None,(0,48),None]},
    "E2: qwen3.8-27b": {
        "note": [(1,25),(0,20),(0,29),(0,12),(3,10)],
        "ctrl": [(26,33),(12,12),None,(3,48),None]},
}
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3dd"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5,
                     "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                     "xtick.color": INK, "ytick.color": MUTED})
fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.6), sharey=True)
w = 0.36
for ax, (title, d) in zip(axes, DATA.items()):
    for i, s in enumerate(SC):
        for off, key, col, hatch in ((-w/2-0.01, "note", BLUE, None),
                                     (w/2+0.01, "ctrl", ORANGE, "////")):
            v = d[key][i]
            if v is None:
                continue
            k, n = v
            h = k / n
            ax.bar(i + off, max(h, 0.004), w, color=col, edgecolor="white",
                   linewidth=0.8, hatch=hatch, zorder=2)
            ax.text(i + off, h + 0.03, f"{k}/{n}", ha="center", va="bottom",
                    fontsize=6.2, color=INK, rotation=90)
    ax.set_title(title, fontsize=8.5, color=INK, pad=6)
    ax.set_xticks(range(len(SC)), SC)
    ax.set_ylim(0, 1.32)
    ax.set_yticks([0, .25, .5, .75, 1.0], ["0", ".25", ".50", ".75", "1"])
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(length=0)
axes[0].set_ylabel("share of verdicts changed")
from matplotlib.patches import Patch
fig.legend(handles=[Patch(facecolor=BLUE, label="NOTE cycles (annotated input)"),
                    Patch(facecolor=ORANGE, hatch="////", edgecolor="white",
                          label="control cycles (no annotation)")],
           loc="lower center", ncol=2, frameon=False, fontsize=8,
           bbox_to_anchor=(0.5, -0.02))
fig.tight_layout(rect=(0, 0.08, 1, 1))
fig.savefig("fig_changes.pdf")
fig.savefig("fig_changes.png", dpi=200)
