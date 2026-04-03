"""
xq_screen.py v2 — 台股多策略選股掃描（TWSE / TPEX OpenAPI）
資料來源：
  - TWSE OpenAPI（上市，免費無限制）
  - TPEX OpenAPI（上櫃，免費無限制）
  - FinMind（法人籌碼，候選股用）
策略：海龜突破、起漲點、明日強勢股、KD低檔金叉、MACD雙共振、均線多頭排列、量增價漲
輸出：result_screen.csv + daily/YYYY-MM-DD.md

用法：
  python xq_screen.py                 # 完整掃描 + 法人資料（需 FINMIND_TOKEN）
  python xq_screen.py --no-inst       # 跳過法人資料
  python xq_screen.py --days 30       # 只抓近 30 個交易日（較快，指標精度略降）
"""

import os, sys, io, time, warnings, argparse
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings("ignore")

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── 設定 ────────────────────────────────────────────────────────
TRADING_DAYS   = 65        # 歷史天數（MA60 需要 ≥ 60）
MIN_VOL_張     = 300       # 最低成交量門檻
MIN_PRICE      = 10        # 最低股價

FINMIND_TOKEN  = os.getenv("FINMIND_TOKEN", "")
FINMIND_URL    = "https://api.finmindtrade.com/api/v4/data"

TWSE_TODAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_TODAY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_stk_daily_trading_info"
TWSE_HIST_URL  = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
TPEX_HIST_URL  = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
# ────────────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════
# 1. TWSE/TPEX 取今日合法代碼清單
# ════════════════════════════════════════════════════════════════

def _to_float(s: str) -> float | None:
    s = str(s).strip().replace(",", "").replace("+", "")
    try:
        return float(s) if s not in ("--", "", "X", "除權", "除息", "除權息") else None
    except ValueError:
        return None


def fetch_valid_codes() -> dict[str, dict]:
    """
    從 TWSE + TPEX 取今日最新全市場快照，
    回傳 {code: {name, close, volume_張, chg_pct, market}}
    這些代碼來自官方，完全沒有 ghost ticker 問題。
    """
    result = {}

    # ── TWSE 上市 ──
    try:
        r = SESSION.get(TWSE_TODAY_URL, timeout=15)
        for row in r.json():
            code = str(row.get("Code", "")).strip()
            if not (code.isdigit() and len(code) == 4):
                continue
            c = _to_float(row.get("ClosingPrice", ""))
            if c is None or c < MIN_PRICE:
                continue
            vol = int(str(row.get("TradeVolume", "0")).replace(",", "") or 0) // 1000
            chg = _to_float(row.get("Change", "0")) or 0.0
            result[code] = {
                "name":    str(row.get("Name", "")).strip(),
                "close":   c,
                "vol_張":  vol,
                "chg_pct": round(chg / (c - chg) * 100, 2) if (c - chg) else 0,
                "market":  "TW",
            }
        print(f"  TWSE 上市：{len(result)} 支")
    except Exception as e:
        print(f"  TWSE 失敗：{e}")

    # ── TPEX 上櫃 ──
    tpex_count = 0
    try:
        r = SESSION.get(TPEX_TODAY_URL, timeout=15)
        for row in r.json():
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            if not (code.isdigit() and len(code) == 4):
                continue
            c = _to_float(row.get("Close", ""))
            if c is None or c < MIN_PRICE:
                continue
            vol = int(str(row.get("TradingShares", "0")).replace(",", "") or 0) // 1000
            chg = _to_float(row.get("Change", "0")) or 0.0
            result[code] = {
                "name":    str(row.get("CompanyName", "")).strip(),
                "close":   c,
                "vol_張":  vol,
                "chg_pct": round(chg / (c - chg) * 100, 2) if (c - chg) else 0,
                "market":  "TWO",
            }
            tpex_count += 1
        print(f"  TPEX 上櫃：{tpex_count} 支")
    except Exception as e:
        print(f"  TPEX 失敗（可能今日無資料）：{e}")

    return result


# ════════════════════════════════════════════════════════════════
# 2. yfinance 抓歷史（對已驗證代碼，無 ghost ticker）
# ════════════════════════════════════════════════════════════════

def fetch_yf_history(code: str, market: str, period: str = "6mo") -> pd.DataFrame | None:
    """對已知合法代碼抓 yfinance 歷史，不會有 ghost ticker"""
    import yfinance as yf
    ticker = f"{code}.{market}"
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        # 統一小寫欄名，重置 index 產生 "date" 欄
        df.columns = [c.lower() for c in df.columns]
        df = df.reset_index()
        df = df.rename(columns={"Date": "date", "index": "date", "datetime": "date"})
        return df
    except Exception:
        return None


def build_history(valid_codes: dict, n_workers: int = 8) -> dict[str, pd.DataFrame]:
    """
    對 TWSE/TPEX 驗證過的代碼，用 yfinance 批次抓歷史。
    因代碼來自官方，完全不會有 ghost ticker。
    """
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from tqdm import tqdm

    # 初篩：量 > 300張（減少 yfinance 呼叫次數）
    candidates = {
        code: info for code, info in valid_codes.items()
        if info["vol_張"] >= MIN_VOL_張
    }
    print(f"  初篩後候選：{len(candidates)} 支（量>{MIN_VOL_張}張）")

    result = {}
    items = list(candidates.items())

    def _fetch(item):
        code, info = item
        df = fetch_yf_history(code, info["market"])
        if df is None:
            return None
        df["name"] = info["name"]
        return code, df

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(_fetch, item): item for item in items}
        for f in tqdm(as_completed(futures), total=len(futures), ncols=70):
            r = f.result()
            if r:
                code, df = r
                result[code] = df

    return result


# ════════════════════════════════════════════════════════════════
# 2. 技術指標計算
# ════════════════════════════════════════════════════════════════

def calc_indicators(code: str, df: pd.DataFrame) -> dict | None:
    try:
        if len(df) < 5:
            return None

        c = df["close"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        o = df["open"].astype(float)
        v = df["volume"].astype(float)

        # 均線
        ma5  = c.rolling(5).mean()
        ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(min(60, len(c))).mean()

        # RSI(14)
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        # KD(14)
        low14  = l.rolling(14).min()
        high14 = h.rolling(14).max()
        rsv    = (c - low14) / (high14 - low14 + 1e-9) * 100
        K      = rsv.ewm(com=2, adjust=False).mean()
        D      = K.ewm(com=2, adjust=False).mean()

        # MACD 日線(12,26,9)
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        sig   = macd.ewm(span=9, adjust=False).mean()
        hist  = macd - sig

        # MACD 週線
        tmp = df.copy()
        tmp["date_dt"] = pd.to_datetime(tmp["date"])
        cw = tmp.set_index("date_dt")["close"].astype(float).resample("W").last().dropna()
        if len(cw) >= 10:
            hw = (cw.ewm(span=12, adjust=False).mean() - cw.ewm(span=26, adjust=False).mean())
            hw -= hw.ewm(span=9, adjust=False).mean()
            hist_w      = float(hw.iloc[-1])
            hist_w_prev = float(hw.iloc[-2]) if len(hw) > 1 else 0.0
        else:
            hist_w = hist_w_prev = 0.0
        macd_w_bull = hist_w > 0 and hist_w > hist_w_prev

        # 量
        vol_ma5  = v.rolling(5).mean()
        vol_ma20 = v.rolling(20).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma5.iloc[-1] + 1e-9))

        # 20日最高 H（不含今日，對應 XQ Highest(High[1], 20)）
        high20_prev = float(h.rolling(20).max().iloc[-2]) if len(h) >= 21 else float(h.max())

        # 均線糾結度（起漲點用）
        ma_max = ma5.combine(ma20, max).combine(ma60, max)
        ma_min = ma5.combine(ma20, min).combine(ma60, min)
        tangle_pct = float((ma_max.iloc[-1] - ma_min.iloc[-1]) / (ma_min.iloc[-1] + 1e-9) * 100)

        # K 線型態
        day_range        = float(h.iloc[-1] - l.iloc[-1]) or 0.01
        close_pos        = (float(c.iloc[-1]) - float(l.iloc[-1])) / day_range * 100
        upper_shad       = (float(h.iloc[-1]) - float(c.iloc[-1])) / day_range * 100
        body             = abs(float(c.iloc[-1]) - float(o.iloc[-1])) or 0.01
        upper_shadow_abs = float(h.iloc[-1]) - float(c.iloc[-1])
        chg_pct          = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 else 0

        return {
            "code":             code,
            "name":             df["name"].iloc[-1],
            "close":            round(float(c.iloc[-1]), 2),
            "close_prev":       round(float(c.iloc[-2]), 2),
            "chg_pct":          round(chg_pct, 2),
            "vol_張":           int(v.iloc[-1] / 1000),
            "vol_ratio":        round(vol_ratio, 2),
            "vol_ma20_張":      round(float(vol_ma20.iloc[-1] / 1000), 0),
            "ma5":              round(float(ma5.iloc[-1]), 2),
            "ma10":             round(float(ma10.iloc[-1]), 2),
            "ma20":             round(float(ma20.iloc[-1]), 2),
            "ma60":             round(float(ma60.iloc[-1]), 2),
            "ma20_prev":        round(float(ma20.iloc[-2]), 2),
            "ma5_prev":         round(float(ma5.iloc[-2]), 2),
            "ma5_rising":       float(ma5.iloc[-1]) > float(ma5.iloc[-2]),
            "ma_max":           round(float(ma_max.iloc[-1]), 2),
            "rsi":              round(float(rsi.iloc[-1]), 1),
            "K":                round(float(K.iloc[-1]), 1),
            "D":                round(float(D.iloc[-1]), 1),
            "K_prev":           round(float(K.iloc[-2]), 1),
            "D_prev":           round(float(D.iloc[-2]), 1),
            "macd":             round(float(macd.iloc[-1]), 4),
            "signal":           round(float(sig.iloc[-1]), 4),
            "hist":             round(float(hist.iloc[-1]), 4),
            "hist_prev":        round(float(hist.iloc[-2]), 4),
            "hist_w":           round(hist_w, 4),
            "hist_w_prev":      round(hist_w_prev, 4),
            "macd_w_bull":      macd_w_bull,
            "high20_prev":      round(high20_prev, 2),
            "tangle_pct":       round(tangle_pct, 2),
            "close_pos":        round(close_pos, 1),
            "upper_shad":       round(upper_shad, 1),
            "body":             round(body, 2),
            "upper_shadow_abs": round(upper_shadow_abs, 2),
            "is_limit_up":      chg_pct >= 9.5,
        }
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 3. 策略篩選（對齊 XQ .xs 邏輯）
# ════════════════════════════════════════════════════════════════

def _base_ok(s: dict) -> bool:
    return s["vol_張"] >= MIN_VOL_張 and s["close"] >= MIN_PRICE


def screen_turtle(s: dict) -> bool:
    """🐢 海龜突破 (turtle_breakout.xs)
    Close > Highest(High[1], 20)  +  Average(Volume,5) > 1000張
    """
    return (
        _base_ok(s)
        and s["close"] > s["high20_prev"]
        and s["vol_ma20_張"] > 1000
    )


def screen_breakout(s: dict) -> bool:
    """🚀 起漲點 (breakout_start.xs)
    策略A：均線糾結(<2%) + 今日突破所有均線 + MA5向上
    策略B：收盤突破前20日最高H
    共同：Volume > 5日均量 × 1.5
    """
    vol_ok = s["vol_ratio"] >= 1.5
    cond_a = (
        s["tangle_pct"] < 2.0
        and s["close"] > s["ma_max"]
        and s["ma5_rising"]
    )
    cond_b = s["close"] > s["high20_prev"]
    return _base_ok(s) and vol_ok and (cond_a or cond_b)


def screen_strong_close(s: dict) -> bool:
    """💪 明日強勢股 (bullish_next_day.xs)
    漲幅>3% + 量比≥1.5x + 收盤創近期新高 + 季線上 + 上影線<實體0.5
    """
    return (
        _base_ok(s)
        and s["chg_pct"] > 3.0
        and s["vol_ratio"] >= 1.5
        and s["close"] > s["high20_prev"]
        and s["close"] > s["ma60"]
        and s["upper_shadow_abs"] < s["body"] * 0.5
    )


def screen_kd_golden(s: dict) -> bool:
    """📈 KD低檔金叉：前日K<D且K<40，今日K>D"""
    return (
        _base_ok(s)
        and s["K_prev"] < s["D_prev"]
        and s["K_prev"] < 40
        and s["K"] > s["D"]
        and s["rsi"] < 70
    )


def screen_macd_resonance(s: dict) -> bool:
    """🔥 MACD雙共振：日線紅柱放大 + 週線紅柱向上"""
    return (
        _base_ok(s)
        and s["hist"] > 0
        and s["hist"] > s["hist_prev"]
        and s["macd"] > s["signal"]
        and s["macd_w_bull"]
    )


def screen_ma_aligned(s: dict) -> bool:
    """📊 均線多頭排列：MA5>MA10>MA20>MA60 + 股價在MA5上"""
    return (
        _base_ok(s)
        and s["ma5"] > s["ma10"] > s["ma20"] > s["ma60"]
        and s["close"] > s["ma5"]
    )


def screen_volume_surge(s: dict) -> bool:
    """💥 量增價漲：量比≥2x + 收盤位置≥60% + 非漲停"""
    return (
        _base_ok(s)
        and s["vol_ratio"] >= 2.0
        and s["close_pos"] >= 60
        and not s["is_limit_up"]
        and s["chg_pct"] > 0
        and s["vol_張"] >= 500
    )


STRATEGIES = {
    "🐢海龜突破":     screen_turtle,
    "🚀起漲點":       screen_breakout,
    "💪明日強勢股":   screen_strong_close,
    "📈KD低檔金叉":   screen_kd_golden,
    "🔥MACD雙共振":   screen_macd_resonance,
    "📊均線多頭排列": screen_ma_aligned,
    "💥量增價漲":     screen_volume_surge,
}


# ════════════════════════════════════════════════════════════════
# 4. FinMind 法人資料
# ════════════════════════════════════════════════════════════════

def fetch_institutional(codes: list[str]) -> dict:
    if not FINMIND_TOKEN or not codes:
        return {}
    start = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    result = {}
    for code in codes:
        try:
            r = requests.get(FINMIND_URL, params={
                "dataset":    "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id":    code,
                "start_date": start,
                "token":      FINMIND_TOKEN,
            }, timeout=10)
            df = pd.DataFrame(r.json().get("data", []))
            if df.empty:
                continue
            df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
            row = {}
            for name, key in [("外資及陸資", "外資"), ("投信", "投信")]:
                sub = df[df["name"] == name].sort_values("date")
                row[f"{key}3日"]  = int(sub["net"].tail(3).sum() / 1000) if not sub.empty else 0
                if key == "外資" and not sub.empty:
                    consec = 0
                    for n in reversed(sub["net"].tolist()):
                        if n > 0:
                            consec += 1
                        else:
                            break
                    row["連買日"] = consec
            result[code] = row
            time.sleep(0.25)
        except Exception:
            continue
    return result


# ════════════════════════════════════════════════════════════════
# 5. MD 報告輸出
# ════════════════════════════════════════════════════════════════

def write_md(df_cross: pd.DataFrame, strategy_lists: dict, today: str, inst: dict):
    os.makedirs("daily", exist_ok=True)
    lines = [
        f"# {today} 策略選股報告\n",
        f"> 執行時間：{datetime.now().strftime('%H:%M')}  策略數：{len(STRATEGIES)}  "
        f"命中：{len(df_cross)} 支  重疊≥2：{len(df_cross[df_cross['策略數']>=2])} 支\n",
        "---\n",
        f"## ⭐ 多策略重疊候選（≥2 策略）\n",
        "| 代碼 | 名稱 | 收盤 | 漲幅% | RSI | K | 量比 | 收盤位置% | 策略 | 外資3日 | 連買 |",
        "|------|------|------|-------|-----|---|------|---------|------|---------|------|",
    ]
    top = df_cross[df_cross["策略數"] >= 2].head(25)
    for _, r in top.iterrows():
        code = r["代碼"]
        inet = f"{inst.get(code,{}).get('外資3日','-')}張" if code in inst else "-"
        icon = f"{inst.get(code,{}).get('連買日','-')}日" if code in inst else "-"
        stars = "⭐" * int(r["策略數"])
        lines.append(
            f"| {code} | {r['名稱']} | {r['收盤']} | {r['漲幅%']}% | {r['RSI']} | {r['K']} | "
            f"{r['量比']}x | {r['收盤位置%']}% | {stars} {r['策略']} | {inet} | {icon} |"
        )
    lines += ["\n---\n"]

    for name, lst in strategy_lists.items():
        if not lst:
            continue
        top10 = sorted(lst, key=lambda x: x["vol_ratio"], reverse=True)[:10]
        lines += [
            f"## {name}（{len(lst)} 支，依量比 Top10）\n",
            "| 代碼 | 名稱 | 收盤 | 漲幅% | RSI | K | 量比 | 外資3日 |",
            "|------|------|------|-------|-----|---|------|---------|",
        ]
        for s in top10:
            c = s["code"]
            inet = f"{inst.get(c,{}).get('外資3日','-')}張" if c in inst else "-"
            lines.append(
                f"| {c} | {s['name']} | {s['close']} | {s['chg_pct']}% | "
                f"{s['rsi']} | {s['K']} | {s['vol_ratio']}x | {inet} |"
            )
        lines.append("")

    lines += [
        "---\n",
        f"*資料來源：TWSE + TPEX OpenAPI + FinMind  |  工具：xq_screen.py v2  |  {today}*\n",
    ]
    with open(f"daily/{today}.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  → daily/{today}.md 寫入完成")


# ════════════════════════════════════════════════════════════════
# 6. 主程式
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-inst", action="store_true", help="跳過法人資料")
    parser.add_argument("--days",    type=int, default=TRADING_DAYS, help="歷史天數（預設65）")
    args = parser.parse_args()

    today = datetime.today().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  台股策略選股  xq_screen.py v2  {today}")
    print(f"  資料來源：TWSE + TPEX OpenAPI（官方，免費）")
    print(f"{'='*60}\n")

    # 1. 取得官方股票清單（無 ghost ticker）
    print("取得官方股票清單...")
    valid_codes = fetch_valid_codes()
    print(f"  上市+上櫃合計：{len(valid_codes)} 支\n")

    # 2. 下載歷史資料（只對官方代碼，無 ghost ticker）
    print("下載歷史資料（yfinance，官方代碼）...")
    history = build_history(valid_codes)

    # 2. 計算技術指標
    print("計算技術指標...")
    tech_data = []
    for code, df in history.items():
        s = calc_indicators(code, df)
        if s:
            tech_data.append(s)
    # 去重：若多支股票的收盤/漲幅/RSI/K 完全相同 → ghost ticker，只保留代碼最小那支
    seen_fp: dict[tuple, str] = {}
    deduped = []
    for s in tech_data:
        fp = (s["close"], s["chg_pct"], round(s["rsi"], 1), round(s["K"], 1))
        if fp not in seen_fp:
            seen_fp[fp] = s["code"]
            deduped.append(s)
    ghost_removed = len(tech_data) - len(deduped)
    tech_data = deduped
    print(f"  有效：{len(tech_data)} 支（移除 ghost {ghost_removed} 支）\n")

    # 3. 策略篩選
    print("執行策略篩選...")
    strategy_lists = {name: [] for name in STRATEGIES}
    hit_map = {}
    for s in tech_data:
        hits = [name for name, fn in STRATEGIES.items() if fn(s)]
        if hits:
            hit_map[s["code"]] = {"hits": hits, "data": s}
            for name in hits:
                strategy_lists[name].append(s)

    print("  各策略命中：")
    for name, lst in strategy_lists.items():
        print(f"    {name}: {len(lst)} 支")

    # 4. 整理交叉表
    rows = []
    for code, v in hit_map.items():
        s = v["data"]
        rows.append({
            "代碼":      code,
            "名稱":      s["name"],
            "收盤":      s["close"],
            "漲幅%":     s["chg_pct"],
            "RSI":       s["rsi"],
            "K":         s["K"],
            "量比":      s["vol_ratio"],
            "收盤位置%": s["close_pos"],
            "策略數":    len(v["hits"]),
            "策略":      " | ".join(v["hits"]),
        })
    if not rows:
        print("  無任何股票命中任何策略，結束。")
        return
    df_cross = pd.DataFrame(rows).sort_values(
        ["策略數", "量比"], ascending=[False, False]
    ).reset_index(drop=True)
    df_cross.to_csv("result_screen.csv", index=False, encoding="utf-8-sig")
    print(f"\n  重疊≥2：{len(df_cross[df_cross['策略數']>=2])} 支")
    print(f"  result_screen.csv 寫入完成\n")

    # 5. 法人資料
    inst = {}
    if not args.no_inst and FINMIND_TOKEN:
        cands = df_cross[df_cross["策略數"] >= 2]["代碼"].tolist()[:60]
        print(f"抓法人資料（{len(cands)} 支）...")
        inst = fetch_institutional(cands)
        print(f"  完成 {len(inst)} 支\n")
    elif not FINMIND_TOKEN and not args.no_inst:
        print("⚠ 未設定 FINMIND_TOKEN，跳過法人（set FINMIND_TOKEN=xxx）\n")

    # 6. 輸出 MD
    write_md(df_cross, strategy_lists, today, inst)

    # 7. 終端摘要
    print(f"\n{'='*60}")
    print(f"  完成！{today}")
    print(f"  多策略重疊 Top5：")
    for _, r in df_cross.head(5).iterrows():
        print(f"    {r['代碼']} {r['名稱']:8s}  {r['收盤']:>8.2f}  {r['策略數']}策略  {r['策略']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
