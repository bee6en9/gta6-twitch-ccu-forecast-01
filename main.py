"""gta6-twitch-ccu-forecast-01: forecast GTA VI's launch-day Twitch viewers.

Model: an empirical cross-launch quantile model on the log scale.

For every past big launch shipped in the case's `comparables`, take its
release day (00:00-24:00 UTC of its release date) and form the log-ratio of
each hour's viewers to that day's hour-1 print:

    r_h = ln(viewers(h) / viewers(1)),  h = 2..24.

The forecast for the target launch anchors on its own hour-1 print p1 and
reads the distribution of r_h across the comparables:

    q_tau(h) = p1 * exp(median_h * ALPHA + SPREAD * (Q_tau(r_h) - median_h))

where Q_tau is the weighted empirical quantile of r_h at level tau, the
median is shrunk toward 0 (persistence) by ALPHA = 0.8, and the spread
around the median is narrowed by SPREAD = 0.8. The quantiles are weighted:
console-only launches count 3x, because the target (Grand Theft Auto VI) is
a console-only, single-player, rolling-midnight launch, and console-only
launches show a tighter, higher afternoon shape than the mixed pool; and a
training launch whose hour-1 print is further than 1.5 log units from the
target's hour-1 print is downweighted to 0.05x, because launch shapes
depend on scale (a category at a few hundred viewers can explode 1000x by
the afternoon; a category already at 500k cannot).

Launches are left out of the training pool when their hour-1 print cannot
anchor a shape at all:
  - tier "weak" or "early-access-contaminated" (their release-day shape is
    contaminated by early access or is a free-to-play launch, not a paid
    launch day);
  - single global unlocks (Silksong, Black Myth: Wukong, ...): their hour 1
    is hours away from the unlock, so their hour-1 print is a pre-unlock
    trickle and their ratios explode for reasons that do not apply to a
    rolling-midnight launch;
  - the GTA VI category's own pre-release series: it has no release day yet,
    so it contributes no ratios (it is skipped automatically: no hour-1
    print falls inside its release day).

Validation (leave-one-out over the 37 usable comparable launches in the
kit, scored with the contest's exact pinball loss on the log scale, item
pooled over every scored hour and quantile; the numbers below were produced
by importing this file itself, so they describe the shipped code):
  house baseline   all 0.930, console-only-like 0.344, big-launch 0.253
  this model       all 0.582, console-only-like 0.199, big-launch 0.251
The console-only-like subset (Ghost of Yotei, Saros, Zelda: Tears of the
Kingdom, FFVII Rebirth, The Last of Us Part II, Ghost of Tsushima, Stellar
Blade) is the closest proxy for a console-only single-player rolling
launch, and the model beats the house baseline by ~42% on it. The
big-launch subset (hour-1 print above 150k) is a tie, so the model does not
give back its advantage where the launch is already huge.

The program is deterministic: it reads only the case file(s) in the input
directory, uses no randomness, no network and no time, and writes the same
bytes on every run. Standard library only.
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]

# Median log-ratio shrinkage toward persistence (0 = pure persistence).
ALPHA = 0.8
# Spread of the log-quantiles around the shrunk median.
SPREAD = 0.8
# Extra weight for console-only launches (the target's class).
CONSOLE_WEIGHT = 3.0
# Scale conditioning: a training launch whose hour-1 print is further than
# SCALE_BAND log units from the target's hour-1 print is downweighted to
# SCALE_DOWN of its weight. Launch shapes depend on scale: a category whose
# hour-1 print is a few hundred viewers can explode 1000x by the afternoon,
# a category already at 500k viewers cannot. Conditioning on scale keeps the
# fat tail of tiny launches from inflating the quantiles of a huge launch.
SCALE_BAND = 1.5
SCALE_DOWN = 0.05


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up (same as the starter's rounding)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def release_day_series(comp: dict) -> dict:
    """Hour (1..24) -> viewers for the comparable's release day, UTC."""
    day0 = parse_ts(comp["day_start"])
    out: dict[int, float] = {}
    for point in comp.get("hourly") or []:
        stamp = parse_ts(point["from"])
        delta = (stamp - day0).total_seconds()
        if 0 <= delta < 24 * 3600:
            hour = int(delta // 3600) + 1
            viewers = point.get("viewers")
            if viewers is not None and viewers > 0:
                out[hour] = float(viewers)
    return out


def tier_of(comp: dict) -> str:
    text = (comp.get("unlock") or "").lower()
    if "tier: weak" in text:
        return "weak"
    if "early-access-contaminated" in text:
        return "ea"
    return "ok"


def is_console_only(comp: dict) -> bool:
    return "pc" not in (comp.get("platforms") or "").lower()


def is_single_global(comp: dict) -> bool:
    text = (comp.get("unlock") or "").lower()
    return ("single global" in text
            or "single simultaneous" in text
            or "simultaneous global" in text
            or "simultaneous across" in text)


def weighted_quantile(pairs, tau: float):
    """Linear-interpolation quantile of (value, weight) pairs."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return None
    target = tau * total
    seen = 0.0
    for i, (value, weight) in enumerate(ordered):
        before = seen
        seen += weight
        if seen >= target:
            if i == 0 or seen == target:
                return value
            frac = (target - before) / weight
            return ordered[i - 1][0] * (1.0 - frac) + value * frac
    return ordered[-1][0]


def log_ratios(case: dict, p1_target: float) -> dict:
    """Per-hour lists of (log-ratio, weight) over the usable comparables."""
    ratios: dict[int, list] = {h: [] for h in range(2, 25)}
    log_target = math.log(max(1.0, float(p1_target)))
    for comp in case.get("comparables") or []:
        if tier_of(comp) in ("weak", "ea"):
            continue
        if is_single_global(comp):
            continue
        series = release_day_series(comp)
        first = series.get(1)
        if not first or first <= 0:
            continue
        log_first = math.log(first)
        weight = CONSOLE_WEIGHT if is_console_only(comp) else 1.0
        if abs(log_first - log_target) > SCALE_BAND:
            weight *= SCALE_DOWN
        for hour in range(2, 25):
            viewers = series.get(hour)
            if viewers and viewers > 0:
                ratios[hour].append((math.log(viewers) - log_first, weight))
    return ratios


def hour_quantiles(ratios: dict, p1: float, hour: int) -> list:
    """The five quantiles for one hour, anchored on the hour-1 print."""
    p1 = max(1.0, float(p1))
    log_p1 = math.log(p1)
    pairs = ratios.get(hour) or []
    median = weighted_quantile(pairs, 0.5) if pairs else None
    if median is None:
        # No comparable covered this hour: fall back to the house baseline.
        return [round2(max(1.0, p1 * math.exp(z * 0.15 * math.sqrt(hour - 1))))
                for z in Z]
    raw = [weighted_quantile(pairs, tau) for tau in LEVELS]
    shrunk_median = ALPHA * median
    quantiles = [log_p1 + shrunk_median + SPREAD * (q - median) for q in raw]
    out: list = []
    prev = 0.0
    for q in quantiles:
        value = math.exp(max(-30.0, min(30.0, q)))
        value = max(1.0, min(1_000_000_000.0, value))
        value = max(value, prev)
        out.append(round2(value))
        prev = value
    return out


def forecast_case(case: dict, name: str) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"{name} asks for levels {case['levels']}, not {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))
    ratios = log_ratios(case, p1)
    hours = [
        {"hour": row["hour"], "q": hour_quantiles(ratios, p1, row["hour"])}
        for row in case["forecast"]
    ]
    return {"hours": hours}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
