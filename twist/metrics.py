"""Forecast statistics and Derek's skill score.

Same definitions as tidewatch-asa/asa/metrics.py (statistics, skill_score), so
the diagnostic tool's numbers match the ASA tables and decks:

    S = 0.25 [R2 + e^(-|Bias|/sd) + e^(-MAE/sd) + e^(-RMSE/sd)]

sd is the population standard deviation of the observations in the window.
Bias and errors are forecast minus observed.
"""
import numpy as np


def statistics(obs, fc, t_hours=None):
    """R2, bias, MAE, RMSE, SD of obs, skill, peak height and timing error.

    obs, fc: paired arrays (ft). t_hours: matching times in hours (for the
    peak timing error), optional.
    """
    x = np.asarray(obs, float)
    y = np.asarray(fc, float)
    n = len(x)
    out = {"N": n}
    if n < 3:
        return out
    err = y - x
    r = np.corrcoef(x, y)[0, 1] if x.std() > 0 and y.std() > 0 else np.nan
    slope, intercept = np.polyfit(x, y, 1)
    sd = float(x.std(ddof=0))
    out.update(
        R2=float(r * r) if np.isfinite(r) else None,
        Bias=float(err.mean()),
        MAE=float(np.abs(err).mean()),
        RMSE=float(np.sqrt((err ** 2).mean())),
        SDobs=sd,
        Slope=float(slope),
        Intercept=float(intercept),
        PeakObs=float(x.max()),
        PeakFc=float(y.max()),
        PeakError=float(y.max() - x.max()),
    )
    if t_hours is not None:
        t = np.asarray(t_hours, float)
        out["PeakTimeError_h"] = float(t[int(np.argmax(y))] - t[int(np.argmax(x))])
    out["Skill"] = skill(out)
    return out


def skill(s):
    sd = s.get("SDobs")
    if not sd or sd <= 0 or s.get("R2") is None:
        return None
    comp = [min(max(s["R2"], 0.0), 1.0)] + [float(np.exp(-abs(s[k]) / sd)) for k in ("Bias", "MAE", "RMSE")]
    return float(np.mean(comp))
