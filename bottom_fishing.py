"""
台股抄底選股工具（多線程加速版）
資料來源：Yahoo Finance (yfinance) + twstock
執行前安裝：pip install yfinance pandas twstock tqdm
"""

import warnings
import threading
import pandas as pd
import yfinance as yf
import twstock
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

# ── 設定區 ─────────────────────────────────────────────────────
PERIOD          = "6mo"     # 抓取的歷史資料長度
LOOKBACK_HIGH   = 60        # 計算高點的回望天數
RSI_PERIOD      = 14
MA_LONG         = 60        # 季線
DROP_MIN        = 0.20      # 跌深版：從高點至少跌 20%
DROP_REL_MIN    = 0.10      # 相對強勢版：至少跌 10%
DROP_REL_MAX    = 0.50      # 相對強勢版：最多跌 50%
REL_LOOKBACK    = 20        # 相對強勢比較天數（近 20 日）
MIN_VOL         = 1000      # 最低成交量（張）
MIN_PRICE       = 10        # 最低股價
MAX_WORKERS     = 10        # 並行線程數（太高會被 Yahoo 封鎖）
# ──────────────────────────────────────────────────────────────

_print_lock = threading.Lock()


def get_tw_stock_list() -> list[str]:
    """取得所有上市 + 上櫃一般股票代碼（排除權證、ETF、TDR）"""
    codes = []
    for code, info in twstock.codes.items():
        if info.type != "股票":
            continue
        if info.market not in ("上市", "上櫃"):
            continue
        # 只保留 4 位數字的一般股票（排除權證 6 碼、ETF 00xxx、TDR 9xxxx）
        if not (code.isdigit() and len(code) == 4):
            continue
        suffix = ".TW" if info.market == "上市" else ".TWO"
        codes.append(code + suffix)
    return codes


def fetch_ohlcv(ticker: str) -> pd.DataFrame | None:
    """下載單一股票 OHLCV 資料"""
    try:
        df = yf.download(ticker, period=PERIOD, progress=False, auto_adjust=True)
        if df.empty or len(df) < MA_LONG + 5:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        return df
    except Exception:
        return None


def calc_indicators(df: pd.DataFrame) -> pd.Series:
    """計算技術指標，回傳最新一天的數值"""
    close = df["Close"].squeeze()
    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()
    open_ = df["Open"].squeeze()
    vol   = df["Volume"].squeeze()

    ma60     = close.rolling(MA_LONG).mean()
    vol_ma5  = vol.rolling(5).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = 100 - (100 / (1 + rs))

    highest  = high.rolling(LOOKBACK_HIGH).max()
    drop_pct = (highest - close) / highest

    lower_shadow = open_.combine(close, min) - low
    body_size    = (close - open_).abs()

    # 近 20 日個股報酬率（用於相對強勢比較）
    ret_20d = (close.iloc[-1] - close.iloc[-REL_LOOKBACK]) / close.iloc[-REL_LOOKBACK]

    return pd.Series({
        "close"       : close.iloc[-1],
        "open"        : open_.iloc[-1],
        "vol"         : vol.iloc[-1],
        "vol_ma5"     : vol_ma5.iloc[-1],
        "ma60"        : ma60.iloc[-1],
        "rsi"         : rsi.iloc[-1],
        "drop_pct"    : drop_pct.iloc[-1],
        "ret_20d"     : ret_20d,
        "lower_shadow": lower_shadow.iloc[-1],
        "body_size"   : body_size.iloc[-1],
        "vol_prev1"   : vol.iloc[-2],
        "vol_prev2"   : vol.iloc[-3],
        "close_prev1" : close.iloc[-2],
    })


def screen_deep_bottom(s: pd.Series) -> bool:
    """跌深抄底篩選（高風險、訊號明確）"""
    c1 = s["drop_pct"] >= DROP_MIN
    c2 = s["close"] < s["ma60"]
    c3 = s["rsi"] < 40

    shadow_ok = (s["lower_shadow"] > s["body_size"] * 2) and \
                (s["lower_shadow"] / s["close"] * 100 > 1)
    vol_ok    = (s["vol"] > s["vol_prev1"] * 1.5) and (s["vol_prev1"] < s["vol_prev2"])
    red_ok    = (s["close"] > s["open"]) and (s["vol"] > s["vol_ma5"] * 1.2)
    c4 = shadow_ok or vol_ok or red_ok

    c5  = s["vol"] / 1000 > MIN_VOL
    c6  = s["close"] > MIN_PRICE
    chg = (s["close"] / s["close_prev1"] - 1) * 100
    c7  = -9.5 < chg < 9.5

    return all([c1, c2, c3, c4, c5, c6, c7])


def screen_relative_strength(s: pd.Series, twii_ret_20d: float) -> bool:
    """相對強勢抄底篩選（低風險、提前佈局）
    twii_ret_20d：大盤近 20 日報酬率（負數代表下跌）
    """
    import math
    if math.isnan(twii_ret_20d):
        return False  # 大盤資料無效，跳過此篩選
    # 個股近 20 日報酬 - 大盤近 20 日報酬（正 = 抗跌）
    rel_str = s["ret_20d"] - twii_ret_20d

    c1  = rel_str > 0.03            # 比大盤多漲（或少跌）3% 以上
    c2  = DROP_REL_MIN <= s["drop_pct"] <= DROP_REL_MAX  # 從高點仍有 10-50% 修正
    c3  = s["ma60"] * 0.85 <= s["close"] <= s["ma60"] * 1.10  # 季線附近（放寬到 ±15%）
    c4  = s["vol"] >= s["vol_ma5"] * 0.7
    c5  = 30 <= s["rsi"] <= 60
    c6  = s["vol"] / 1000 > 300
    c7  = s["close"] > 15
    chg = (s["close"] / s["close_prev1"] - 1) * 100
    c8  = chg > -9.5

    return all([c1, c2, c3, c4, c5, c6, c7, c8])


def get_twii_stats() -> tuple[float, float]:
    """取得大盤：(從高點跌幅, 近 20 日報酬率)，帶重試避免 rate limit"""
    for attempt in range(4):
        try:
            if attempt > 0:
                time.sleep(10 * attempt)
            df    = yf.download("^TWII", period=PERIOD, progress=False, auto_adjust=True)
            if df.empty:
                continue
            close    = df["Close"].squeeze()
            highest  = close.rolling(LOOKBACK_HIGH).max()
            drop     = float((highest.iloc[-1] - close.iloc[-1]) / highest.iloc[-1])
            ret_20d  = float((close.iloc[-1] - close.iloc[-REL_LOOKBACK]) / close.iloc[-REL_LOOKBACK])
            return drop, ret_20d
        except Exception:
            continue
    print("[警告] 無法取得大盤資料，相對強勢篩選將停用")
    return 0.0, float("nan")


def analyze_ticker(ticker: str, twii_ret_20d: float) -> dict | None:
    """下載 + 計算 + 篩選單一股票，回傳結果 dict 或 None"""
    df = fetch_ohlcv(ticker)
    if df is None:
        return None
    try:
        s    = calc_indicators(df)
        code = ticker.replace(".TWO", "").replace(".TW", "")
        row  = {
            "代號"      : code,
            "收盤"      : round(float(s["close"]), 1),
            "RSI"       : round(float(s["rsi"]), 1),
            "跌幅(高點)": f"{float(s['drop_pct']):.1%}",
            "20日報酬"  : f"{float(s['ret_20d']):.1%}",
            "季線"      : round(float(s["ma60"]), 1),
            "成交量(張)": int(s["vol"] / 1000),
            "_deep"     : screen_deep_bottom(s),
            "_rel"      : screen_relative_strength(s, twii_ret_20d),
        }
        return row
    except Exception:
        return None


def main():
    print("=== 台股抄底選股工具（多線程加速版）===\n")

    print("取得股票清單...")
    tickers = get_tw_stock_list()
    print(f"共 {len(tickers)} 檔\n")

    print("取得大盤資料...")
    twii_drop, twii_ret_20d = get_twii_stats()
    print(f"加權指數從高點下跌：{twii_drop:.1%}，近 20 日報酬：{twii_ret_20d:.1%}\n")

    deep_results = []
    rel_results  = []

    print(f"開始並行分析（{MAX_WORKERS} 線程）...\n")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_ticker, t, twii_ret_20d): t for t in tickers}
        pbar    = tqdm(as_completed(futures), total=len(futures), ncols=70)
        for future in pbar:
            result = future.result()
            if result is None:
                continue
            is_deep = result.pop("_deep")
            is_rel  = result.pop("_rel")
            if is_deep:
                deep_results.append(result.copy())
            if is_rel:
                rel_results.append(result.copy())

    # ── 輸出 ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("【跌深抄底】篩選結果（高風險，等大盤止跌再進場）")
    print("=" * 60)
    if deep_results:
        df_out = pd.DataFrame(deep_results).sort_values("RSI")
        print(df_out.to_string(index=False))
        df_out.to_csv("result_deep.csv", index=False, encoding="utf-8-sig")
    else:
        print("目前無符合條件（止跌訊號尚未出現，繼續等待）")

    print("\n" + "=" * 60)
    print("【相對強勢抄底】篩選結果（低風險，可先建立觀察清單）")
    print("=" * 60)
    if rel_results:
        df_out = pd.DataFrame(rel_results).sort_values("RSI")
        print(df_out.to_string(index=False))
        df_out.to_csv("result_relative.csv", index=False, encoding="utf-8-sig")
    else:
        print("目前無符合條件")

    print("\n結果已存至 result_deep.csv / result_relative.csv")


if __name__ == "__main__":
    main()
