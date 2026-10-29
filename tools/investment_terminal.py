#!/usr/bin/env python3
"""Fetch EastMoney public market data, calculate local indicators, and render a 400x300 BWR card."""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
FONT_DIR = ROOT / "fonts"
CACHE = DATA_DIR / "investment-terminal-cache.json"
CARDS = DATA_DIR / "investment_cards.json"
EASTMONEY = "https://push2.eastmoney.com/api/qt/ulist.np/get"
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MACRO = "https://datacenter.eastmoney.com/api/data/get"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MOVE_CSV = "https://convex-trading-production.up.railway.app/metrics/move-index/data.csv"
TZ = ZoneInfo("Asia/Shanghai")
WIDTH, HEIGHT = 400, 300
BLACK, WHITE, RED = (0, 0, 0), (255, 255, 255), (255, 0, 0)


def _get_json(url: str, params: dict[str, Any], *, retries: int = 3) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Unexpected EastMoney response")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"EastMoney request failed: {last_error}")


def _daily_csv(url: str, series: str | None = None) -> list[tuple[date, float]]:
    params = {"id": series} if series else None
    response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    response.raise_for_status()
    rows: list[tuple[date, float]] = []
    for row in csv.DictReader(line for line in response.text.splitlines() if line and not line.startswith("#")):
        day_raw = row.get("observation_date") or row.get("date")
        value_raw = row.get(series) if series else row.get("value_bp")
        if not day_raw or not value_raw or value_raw in (".", "NA", "null"):
            continue
        try:
            rows.append((date.fromisoformat(day_raw), float(value_raw)))
        except ValueError:
            continue
    if not rows:
        raise RuntimeError(f"No valid observations from {series or 'MOVE'} data")
    return rows


def fetch_eastmoney() -> dict[str, Any]:
    """Fetch U.S. and China market data, then calculate local market context."""
    today = datetime.now(TZ).date()
    start = today - timedelta(days=11 * 366)
    hist = _get_json(
        KLINE,
        {
            "secid": "100.NDX100",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": today.strftime("%Y%m%d"),
            "lmt": "3000",
        },
    )
    raw_rows = ((hist.get("data") or {}).get("klines") or [])
    rows: list[tuple[date, float, float]] = []
    for item in raw_rows:
        parts = item.split(",")
        if len(parts) < 9:
            continue
        try:
            rows.append((date.fromisoformat(parts[0]), float(parts[2]), float(parts[8])))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda row: row[0])
    if len(rows) < 260:
        raise RuntimeError(f"EastMoney returned too little NDX100 history: {len(rows)} rows")

    time.sleep(1.2)
    cn_hist = _get_json(
        KLINE,
        {
            "secid": "1.000300",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": today.strftime("%Y%m%d"),
            "lmt": "3000",
        },
    )
    cn_raw_rows = ((cn_hist.get("data") or {}).get("klines") or [])
    cn_rows: list[tuple[date, float, float]] = []
    for item in cn_raw_rows:
        parts = item.split(",")
        if len(parts) < 9:
            continue
        try:
            cn_rows.append((date.fromisoformat(parts[0]), float(parts[2]), float(parts[8])))
        except (TypeError, ValueError):
            continue
    cn_rows.sort(key=lambda row: row[0])
    if len(cn_rows) < 260:
        raise RuntimeError(f"EastMoney returned too little CSI300 history: {len(cn_rows)} rows")

    cutoff = today - timedelta(days=365)
    latest_day, latest_close, daily_change_pct = rows[-1]
    previous_close = rows[-2][1]
    year_rows = [r for r in rows if r[0] >= cutoff]
    year_window = year_rows[-252:]
    year_high = max(r[1] for r in year_window)
    drawdown = latest_close / year_high - 1

    sma200 = statistics.fmean(r[1] for r in rows[-200:])
    ten_year_cutoff = today - timedelta(days=365 * 10)
    drawdowns: list[float] = []
    # Each daily drawdown uses a 252-session trailing high, including the current session.
    for i, (day, close, _change) in enumerate(rows):
        if day < ten_year_cutoff or i < 251:
            continue
        high = max(r[1] for r in rows[i - 251 : i + 1])
        drawdowns.append(close / high - 1)
    if not drawdowns:
        raise RuntimeError("Unable to calculate the 10-year drawdown distribution")
    drawdown_rank = 100 * sum(value <= drawdown for value in drawdowns) / len(drawdowns)

    # EastMoney quote IDs: VIX (167.VIX), US 10Y (171.US10Y), CSI300 (1.000300), USD/CNH (133.USDCNH).
    time.sleep(1.2)
    quote_payload = _get_json(
        EASTMONEY,
        {
            "fltt": "2",
            "secids": "100.NDX100,100.SPX,167.VIX,171.US10Y,1.000300,120.USDCNYC",
            "fields": "f2,f3,f4,f12,f14,f124",
            "_": int(time.time() * 1000),
        },
    )
    diff = ((quote_payload.get("data") or {}).get("diff") or [])
    if isinstance(diff, dict):
        diff = list(diff.values())
    quotes = {str(item.get("f12")): item for item in diff if isinstance(item, dict)}
    required = {"NDX100", "SPX", "VIX", "US10Y", "000300", "USDCNYC"}
    if not required.issubset(quotes):
        raise RuntimeError(f"EastMoney quote response missing: {sorted(required - quotes.keys())}")

    time.sleep(1.2)
    rates_payload = _get_json(
        MACRO,
        {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "token": "894050c76af8597a853f5b408b759f5d",
            "p": "1",
            "ps": "20",
            "pageNo": "1",
            "pageNum": "1",
        },
    )
    rate_rows = ((rates_payload.get("result") or {}).get("data") or [])
    rate_row = next(
        (row for row in rate_rows if row.get("EMM00166466") is not None and row.get("EMG00001310") is not None),
        None,
    )
    if not rate_row:
        raise RuntimeError("EastMoney did not return matched China/US 10Y yields")

    # Public daily macro series. FRED graph CSV is unauthenticated; MOVE CSV is CC BY 4.0.
    time.sleep(1.0)
    real_yield_rows = _daily_csv(FRED_CSV, "DFII10")
    hy_spread_rows = _daily_csv(FRED_CSV, "BAMLH0A0HYM2")
    vix_history_rows = _daily_csv(FRED_CSV, "VIXCLS")
    move_rows = _daily_csv(MOVE_CSV)

    def quote_time(item: dict[str, Any]) -> str:
        stamp = item.get("f124")
        if stamp:
            return datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(TZ).isoformat(timespec="minutes")
        return ""

    cn_quote = quotes["000300"]
    cn_quote_stamp = quote_time(cn_quote)
    cn_quote_day = datetime.fromisoformat(cn_quote_stamp).date() if cn_quote_stamp else today
    cn_live_close = float(cn_quote["f2"])
    cn_completed = [row for row in cn_rows if row[0] < cn_quote_day]
    if len(cn_completed) < 251:
        raise RuntimeError("EastMoney returned too little completed CSI300 history")

    cn_current_window = [row[1] for row in cn_completed[-251:]] + [cn_live_close]
    cn_year_high = max(cn_current_window)
    cn_drawdown = cn_live_close / cn_year_high - 1
    cn_sma200 = statistics.fmean([row[1] for row in cn_completed[-199:]] + [cn_live_close])
    cn_ten_year_cutoff = cn_quote_day - timedelta(days=365 * 10)
    cn_drawdowns: list[float] = []
    for i, (day, close, _change) in enumerate(cn_completed):
        if day < cn_ten_year_cutoff or i < 251:
            continue
        high = max(row[1] for row in cn_completed[i - 251 : i + 1])
        cn_drawdowns.append(close / high - 1)
    if not cn_drawdowns:
        raise RuntimeError("Unable to calculate CSI300 10-year drawdown distribution")
    cn_drawdown_rank = 100 * sum(value <= cn_drawdown for value in cn_drawdowns) / len(cn_drawdowns)
    china_10y = float(rate_row["EMM00166466"])
    us_10y_close = float(rate_row["EMG00001310"])
    rate_day = str(rate_row["SOLAR_DATE"]).split(" ")[0]
    ndx_dma_pct = (latest_close / sma200 - 1) * 100

    def percentile(series: list[tuple[date, float]], current_day: date, current_value: float, window: int = 252) -> float:
        sample = [value for day, value in series if day <= current_day][-window:]
        if not sample:
            raise RuntimeError("Not enough observations to calculate percentile")
        return 100 * sum(value <= current_value for value in sample) / len(sample)

    # MOVE is a separately displayed signal; its free source can lag, so it does
    # not determine the as-of date for the four-leg stress score.
    macro_series = {"real_yield": real_yield_rows, "hy_spread": hy_spread_rows, "vix": vix_history_rows}
    common_day = min(max(day for day, _ in series) for series in macro_series.values())
    common_values = {key: next((value for day, value in reversed(series) if day <= common_day), None) for key, series in macro_series.items()}
    common_ndx_index = next((i for i in range(len(rows) - 1, -1, -1) if rows[i][0] <= common_day), None)
    if any(value is None for value in common_values.values()) or common_ndx_index is None:
        raise RuntimeError("Unable to align inputs for cross-asset stress calculation")
    common_ndx = rows[common_ndx_index][1]
    common_ndx_high = max(row[1] for row in rows[max(0, common_ndx_index - 251):common_ndx_index + 1])
    common_ndx_dd = common_ndx / common_ndx_high - 1
    common_ndx_dd_series: list[tuple[date, float]] = []
    for i, (day, close, _change) in enumerate(rows[:common_ndx_index + 1]):
        if i >= 251:
            high = max(item[1] for item in rows[i - 251:i + 1])
            common_ndx_dd_series.append((day, close / high - 1))
    pcts = {
        "vix": percentile(vix_history_rows, common_day, float(common_values["vix"])),
        "hy_spread": percentile(hy_spread_rows, common_day, float(common_values["hy_spread"])),
        "real_yield": percentile(real_yield_rows, common_day, float(common_values["real_yield"])),
        "ndx_drawdown": percentile(common_ndx_dd_series, common_day, common_ndx_dd),
    }
    stress_flags = {name: value >= 80 for name, value in pcts.items()}
    stress_score = sum(stress_flags.values())
    stress_label = "CALM" if stress_score == 0 else "WATCH" if stress_score == 1 else "ELEVATED" if stress_score == 2 else "HIGH"
    real_latest_day, real_latest_value = real_yield_rows[-1]
    hy_latest_day, hy_latest_value = hy_spread_rows[-1]
    move_latest_day, move_latest_value = move_rows[-1]
    move_is_stale = (today - move_latest_day).days > 7
    vix_pctl = percentile(vix_history_rows, vix_history_rows[-1][0], vix_history_rows[-1][1])

    return {
        "source": "EastMoney public quote and K-line endpoints",
        "retrieved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "nasdaq100": {
            "code": "NDX100",
            "close": round(latest_close, 2),
            "previous_close": round(previous_close, 2),
            "daily_change_pct": round(daily_change_pct, 2),
            "date": latest_day.isoformat(),
            "quote_time": quote_time(quotes["NDX100"]),
            "dma200_pct": round(ndx_dma_pct, 2),
            "dma200": round(sma200, 2),
        },
        "us10y": {
            "code": "US10Y",
            "yield_pct": float(quotes["US10Y"]["f2"]),
            "change_pct": float(quotes["US10Y"].get("f3") or 0),
            "quote_time": quote_time(quotes["US10Y"]),
        },
        "vix": {
            "code": "VIX",
            "value": float(quotes["VIX"]["f2"]),
            "change_pct": float(quotes["VIX"].get("f3") or 0),
            "quote_time": quote_time(quotes["VIX"]),
        },
        "quotes": {code: {"f2": float(item.get("f2") or 0), "f3": float(item.get("f3") or 0)} for code, item in quotes.items()},
        "macro": {
            "us10y_real": {"value_pct": real_latest_value, "date": real_latest_day.isoformat(), "source": "FRED DFII10"},
            "hy_spread": {"oas_pct": hy_latest_value, "date": hy_latest_day.isoformat(), "source": "FRED BAMLH0A0HYM2"},
            "move": {"value": move_latest_value, "date": move_latest_day.isoformat(), "stale": move_is_stale, "source": "Convex MOVE CSV CC BY 4.0"},
            "vix_percentile_1y": round(vix_pctl),
            "ndx_drawdown_percentile_10y": round(drawdown_rank),
        },
        "cross_asset_stress": {
            "as_of": common_day.isoformat(),
            "score": stress_score,
            "out_of": 4,
            "label": stress_label,
            "percentiles": {key: round(value) for key, value in pcts.items()},
            "flags": stress_flags,
            "method": "one point for VIX, HY OAS, 10Y real yield, or NDX 52-week drawdown at or above its trailing 1-year 80th percentile",
        },
        "csi300": {
            "code": "000300",
            "close": round(cn_live_close, 2),
            "daily_change_pct": float(cn_quote.get("f3") or 0),
            "quote_time": cn_quote_stamp,
        },
        "usd_cnh": {
            "code": "USDCNYC",
            "rate": float(quotes["USDCNYC"]["f2"]),
            "change_pct": float(quotes["USDCNYC"].get("f3") or 0),
            "quote_time": quote_time(quotes["USDCNYC"]),
        },
        "china_10y": {
            "yield_pct": china_10y,
            "date": rate_day,
        },
        "treasury_spread_eod": {
            "us_10y": us_10y_close,
            "cn_10y": china_10y,
            "spread_ppt": round(us_10y_close - china_10y, 4),
            "date": rate_day,
        },
        "indicators": {
            "year_high": round(year_high, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "sma200": round(sma200, 2),
            "trend": "ABOVE" if latest_close >= sma200 else "BELOW",
            "drawdown_percentile_10y": round(drawdown_rank),
            "drawdown_sample_days": len(drawdowns),
            "drawdown_rank_meaning": "share of 10y daily drawdowns at least as deep as today",
        },
        "china_indicators": {
            "year_high": round(cn_year_high, 2),
            "drawdown_pct": round(cn_drawdown * 100, 2),
            "sma200": round(cn_sma200, 2),
            "trend": "ABOVE" if cn_live_close >= cn_sma200 else "BELOW",
            "drawdown_percentile_10y": round(cn_drawdown_rank),
            "drawdown_sample_days": len(cn_drawdowns),
        },
    }


def load_snapshot(refresh: bool) -> dict[str, Any]:
    if refresh:
        try:
            snapshot = fetch_eastmoney()
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            return snapshot
        except Exception as exc:
            if not CACHE.exists():
                raise
            print(f"EastMoney refresh failed; using last successful cache: {exc}", file=sys.stderr)
    if not CACHE.exists():
        raise RuntimeError(f"No market cache at {CACHE}; run with network access first")
    return json.loads(CACHE.read_text(encoding="utf-8"))


def _font(px: int) -> ImageFont.FreeTypeFont:
    if px <= 12:
        path, size = FONT_DIR / "spleen-6x12.bdf", 12
    elif px <= 16:
        path, size = FONT_DIR / "spleen-8x16.bdf", 16
    else:
        path, size = FONT_DIR / "spleen-12x24.bdf", 24
    return ImageFont.truetype(str(path), size)


def _fit_text(draw: ImageDraw.ImageDraw, s: str, px: int, max_width: int) -> ImageFont.FreeTypeFont:
    font = _font(px)
    if draw.textbbox((0, 0), s, font=font)[2] <= max_width:
        return font
    return _font(12)


def _wrap(text: str, max_chars: int) -> list[str]:
    words = text.upper().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _today_card(now: datetime) -> dict[str, Any]:
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    anchor = date(2026, 10, 29)
    card = cards[(now.date() - anchor).days % len(cards)]
    return card


def render(snapshot: dict[str, Any], out_path: Path, *, now: datetime | None = None) -> None:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    img = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    def txt(x: int, y: int, value: str, px: int = 12, color: tuple[int, int, int] = BLACK, anchor: str | None = None) -> None:
        font = _font(px)
        box = draw.textbbox((0, 0), value, font=font)
        width = box[2] - box[0]
        if anchor and "r" in anchor:
            x -= width
        elif anchor and "m" in anchor:
            x -= width // 2
        draw.text((x, y), value, font=font, fill=color)

    ndx = snapshot["nasdaq100"]
    ten = snapshot["us10y"]
    vix = snapshot["vix"]
    ind = snapshot["indicators"]
    csi = snapshot["csi300"]
    fx = snapshot["usd_cnh"]
    cn10 = snapshot["china_10y"]
    cind = snapshot["china_indicators"]
    spread = snapshot["treasury_spread_eod"]
    asof = datetime.fromisoformat(csi["quote_time"]).astimezone(TZ)
    q = snapshot.get("quotes", {})

    macro = snapshot["macro"]
    stress = snapshot["cross_asset_stress"]
    # Two compact columns preserve the selected signals at the panel's 400x300 resolution.
    txt(392, 2, f"UPDATED {asof:%m/%d %H:%M} CST", 12, BLACK, "ra")
    draw.line((8, 19, 392, 19), fill=BLACK, width=1)
    txt(9, 22, "EQUITY / TREND", 16)
    txt(204, 22, "RATES / CREDIT / VOL", 12)
    draw.line((8, 40, 392, 40), fill=BLACK, width=1)
    draw.line((198, 22, 198, 137), fill=BLACK, width=1)

    equity_rows = [
        ("NDX", f"{ndx['close']:,.0f}", f"{ndx['daily_change_pct']:+.2f}%"),
        ("SPX", f"{q['SPX']['f2']:,.0f}", f"{q['SPX']['f3']:+.2f}%"),
        ("CSI300", f"{csi['close']:,.0f}", f"{csi['daily_change_pct']:+.2f}%"),
        ("NDX 52W DD", f"{ind['drawdown_pct']:.1f}%", ""),
        ("NDX vs 200DMA", f"{ndx['dma200_pct']:+.1f}%", ""),
    ]
    for i, (label, value, change) in enumerate(equity_rows):
        y = 43 + i * 18
        if i % 2 == 0:
            draw.rectangle((8, y, 194, y + 16), fill=BLACK)
            fg = WHITE
        else:
            fg = BLACK
        txt(12, y + 1, label, 12, fg)
        txt(145, y + 1, value, 12, fg, "ra")
        txt(192, y + 1, change, 12, (WHITE if fg == WHITE else RED) if change.startswith("-") else fg, "ra")

    rate_rows = [
        ("VIX", f"{vix['value']:.2f}"),
        ("VIX PCT", f"{macro['vix_percentile_1y']}%"),
        ("US10Y", f"{ten['yield_pct']:.2f}%"),
        ("US10Y REAL", f"{macro['us10y_real']['value_pct']:.2f}%"),
        ("HY SPREAD", f"{macro['hy_spread']['oas_pct']:.2f}%"),
        ("MOVE STALE" if macro["move"]["stale"] else "MOVE", f"{macro['move']['value']:.1f}"),
    ]
    for i, (label, value) in enumerate(rate_rows):
        y = 43 + i * 16
        if i % 2 == 1:
            draw.rectangle((202, y, 392, y + 15), fill=BLACK)
            fg = WHITE
        else:
            fg = BLACK
        txt(207, y + 1, label, 12, fg)
        txt(388, y + 1, value, 12, fg, "ra")

    draw.line((8, 138, 392, 138), fill=BLACK, width=1)
    fx_time = datetime.fromisoformat(fx["quote_time"]).astimezone(TZ)
    txt(10, 141, f"USD/CNY FIX  {fx['rate']:.4f}  {fx['change_pct']:+.2f}%", 12)
    txt(390, 141, f"FIX {fx_time:%H:%M} CST", 12, BLACK, "ra")
    draw.line((8, 158, 392, 158), fill=BLACK, width=1)
    txt(9, 161, "CROSS-ASSET STRESS", 16, RED)
    label_color = RED if stress["score"] >= 2 else BLACK
    txt(391, 160, f"{stress['score']}/{stress['out_of']}  {stress['label']}", 16, label_color, "ra")
    draw.rectangle((8, 182, 392, 266), fill=BLACK)
    components = [
        ("VIX", stress["percentiles"]["vix"], stress["flags"]["vix"]),
        ("HY OAS", stress["percentiles"]["hy_spread"], stress["flags"]["hy_spread"]),
        ("REAL YLD", stress["percentiles"]["real_yield"], stress["flags"]["real_yield"]),
        ("NDX DD", stress["percentiles"]["ndx_drawdown"], stress["flags"]["ndx_drawdown"]),
    ]
    for i, (label, pct, alert) in enumerate(components):
        x = 12 + i * 95
        txt(x, 190, label, 12, WHITE)
        txt(x, 211, f"{pct}P", 24, WHITE)
        txt(x, 246, "STRESS" if alert else "NORMAL", 12, WHITE)
        if i < 3:
            draw.line((x + 84, 190, x + 84, 257), fill=(160, 160, 160), width=1)
    txt(10, 269, f"AS OF {stress['as_of']} · 1 PT IF 1Y PCTL ≥80", 12)
    draw.line((8, 284, 392, 284), fill=BLACK, width=1)
    txt(9, 287, f"EM · FRED {macro['hy_spread']['date'][5:]} · MOVE {macro['move']['date'][5:]}* · CONVEX CC BY 4.0", 12)

    # Quantize every pixel to the panel's actual black/white/red palette.
    px = img.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y]
            if r > 170 and g < 90 and b < 90:
                px[x, y] = RED
            elif max(r, g, b) < 128:
                px[x, y] = BLACK
            else:
                px[x, y] = WHITE

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch EastMoney market data and render an Investment Intelligence e-paper PNG")
    parser.add_argument("--output", type=Path, default=ROOT / "design" / "investment-intelligence-preview.png")
    parser.add_argument("--no-refresh", action="store_true", help="Render from the last successful local cache")
    args = parser.parse_args()
    snapshot = load_snapshot(refresh=not args.no_refresh)
    render(snapshot, args.output)
    print(f"PNG: {args.output}")
    print(f"DATA: EastMoney | NDX EOD {snapshot['nasdaq100']['date']} | CSI300 {snapshot['csi300']['close']:.2f} ({snapshot['csi300']['daily_change_pct']:+.2f}%) | CN10Y {snapshot['china_10y']['yield_pct']:.4f}% | USD/CNH {snapshot['usd_cnh']['rate']:.4f}")
    print(f"CALC: NDX vs 200D {snapshot['indicators']['trend']} | 1Y drawdown {snapshot['indicators']['drawdown_pct']:+.2f}% | 10Y rank {snapshot['indicators']['drawdown_percentile_10y']}th")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
