"""
xq_screen.py — 台股多策略選股掃描（仿 XQ 全球贏家）
策略：海龜突破、起漲點、強勢收盤、KD低檔金叉、MACD雙共振、均線多頭排列、量增價漲
籌碼：外資連買、法人買超（FinMind）
輸出：result_screen.csv + daily/YYYY-MM-DD.md

用法：
  python xq_screen.py                  # 完整掃描（約 15~25 分鐘）
  python xq_screen.py --no-inst        # 跳過 FinMind，純技術面（約 15 分鐘）
  python xq_screen.py --top-only       # 只分析上市前500大（約 8 分鐘）
  python xq_screen.py --append         # 不覆蓋今日已存在的 MD，改為附加

安裝：
  pip install yfinance pandas twstock tqdm requests
"""

import os, sys, time, warnings, threading, argparse, io

# Windows terminal UTF-8 fix
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import pandas as pd
import numpy as np
import yfinance as yf
import twstock
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ── 股票名稱查詢表（twstock） ────────────────────────────────────
CODE_NAME: dict[str, str] = {}
for _code, _info in twstock.codes.items():
    if _info.type == "股票" and _code.isdigit() and len(_code) == 4:
        CODE_NAME[_code] = _info.name
# ────────────────────────────────────────────────────────────────

# ── 設定區 ─────────────────────────────────────────────────────
PERIOD       = "6mo"
MAX_WORKERS  = 8          # 太高會被 Yahoo 封鎖，建議 6~10
MIN_VOL_張   = 300        # 最低成交量（張），過濾流動性差的股票
MIN_PRICE    = 10         # 最低股價

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
INST_LOOKBACK = 14        # 法人資料回溯天數
# ────────────────────────────────────────────────────────────────

_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════
# 1. 股票清單
# ════════════════════════════════════════════════════════════════

def get_tw_stock_list(top_only: bool = False) -> list[tuple[str, str]]:
    """回傳 [(代碼, Yahoo ticker), ...]，上市+上櫃一般股（排除ETF/權證）"""
    result = []
    for code, info in twstock.codes.items():
        if info.type != "股票":
            continue
        if info.market not in ("上市", "上櫃"):
            continue
        if not (code.isdigit() and len(code) == 4):
            continue
        suffix = ".TW" if info.market == "上市" else ".TWO"
        result.append((code, code + suffix))

    if top_only:
        # 只保留上市（.TW），通常流動性較好
        result = [(c, t) for c, t in result if t.endswith(".TW")][:500]

    return result


# ════════════════════════════════════════════════════════════════
# 2. 技術指標計算（單支股票）
# ════════════════════════════════════════════════════════════════

def fetch_and_calc(ticker_info: tuple) -> dict | None:
    """下載 OHLCV + 計算所有技術指標，回傳 dict 或 None"""
    code, ticker = ticker_info
    try:
        df = yf.download(ticker, period=PERIOD, progress=False, auto_adjust=True)
        if df.empty or len(df) < 65:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        c = df["Close"].squeeze().astype(float)
        h = df["High"].squeeze().astype(float)
        l = df["Low"].squeeze().astype(float)
        o = df["Open"].squeeze().astype(float)
        v = df["Volume"].squeeze().astype(float)

        # ── 均線 ──
        ma5  = c.rolling(5).mean()
        ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(60).mean()

        # ── RSI(14) ──
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        # ── KD 隨機指標(14,3,3) ──
        low14  = l.rolling(14).min()
        high14 = h.rolling(14).max()
        rsv    = (c - low14) / (high14 - low14 + 1e-9) * 100
        K      = rsv.ewm(com=2, adjust=False).mean()
        D      = K.ewm(com=2, adjust=False).mean()

        # ── MACD 日線(12,26,9) ──
        ema12  = c.ewm(span=12, adjust=False).mean()
        ema26  = c.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        sig    = macd.ewm(span=9, adjust=False).mean()
        hist   = macd - sig

        # ── MACD 週線（resample → 重算） ──
        df_w  = df.resample("W").agg({"Close": "last"}).dropna()
        cw    = df_w["Close"].squeeze().astype(float)
        if len(cw) >= 30:
            ema12w = cw.ewm(span=12, adjust=False).mean()
            ema26w = cw.ewm(span=26, adjust=False).mean()
            macdw  = ema12w - ema26w
            sigw   = macdw.ewm(span=9, adjust=False).mean()
            histw  = macdw - sigw
            hist_w      = float(histw.iloc[-1])
            hist_w_prev = float(histw.iloc[-2]) if len(histw) > 1 else 0.0
            macd_w_bull = hist_w > 0 and hist_w > hist_w_prev
        else:
            macd_w_bull = False
            hist_w = hist_w_prev = 0.0

        # ── 量 ──
        vol_ma5  = v.rolling(5).mean()
        vol_ma20 = v.rolling(20).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma5.iloc[-1] + 1e-9))

        # ── 當日 K 線型態 ──
        day_range = float(h.iloc[-1] - l.iloc[-1]) or 0.01
        close_pos  = (float(c.iloc[-1]) - float(l.iloc[-1])) / day_range * 100
        upper_shad = (float(h.iloc[-1]) - float(c.iloc[-1])) / day_range * 100
        chg_pct    = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100

        # ── 20日最高 H（不含今日，海龜用） ──
        high20_prev = float(h.rolling(20).max().iloc[-2])

        # ── 起漲點：均線糾結度 ──
        ma_max = ma5.combine(ma20, max).combine(ma60, max)
        ma_min = ma5.combine(ma20, min).combine(ma60, min)
        tangle_pct = float((ma_max.iloc[-1] - ma_min.iloc[-1]) / (ma_min.iloc[-1] + 1e-9) * 100)
        ma5_rising = float(ma5.iloc[-1]) > float(ma5.iloc[-2])

        # ── 明日強勢股：上影線 vs 實體 ──
        body = abs(float(c.iloc[-1]) - float(o.iloc[-1])) or 0.01
        upper_shadow_abs = float(h.iloc[-1]) - float(c.iloc[-1])

        # 用近20日收盤序列計算 hash，供去重使用
        series_hash = hash(tuple(round(float(x), 2) for x in c.iloc[-20:]))

        return {
            "code":        code,
            "name":        CODE_NAME.get(code, ""),
            "series_hash": series_hash,
            "close":       round(float(c.iloc[-1]), 2),
            "close_prev":  round(float(c.iloc[-2]), 2),
            "chg_pct":     round(chg_pct, 2),
            "vol_張":      int(v.iloc[-1] / 1000),
            "vol_ratio":   round(vol_ratio, 2),
            "vol_ma20_張": round(float(vol_ma20.iloc[-1] / 1000), 0),
            # 均線
            "ma5":         round(float(ma5.iloc[-1]), 2),
            "ma10":        round(float(ma10.iloc[-1]), 2),
            "ma20":        round(float(ma20.iloc[-1]), 2),
            "ma60":        round(float(ma60.iloc[-1]), 2),
            "ma20_prev":   round(float(ma20.iloc[-2]), 2),
            "ma5_prev":    round(float(ma5.iloc[-2]), 2),
            # RSI
            "rsi":         round(float(rsi.iloc[-1]), 1),
            # KD
            "K":           round(float(K.iloc[-1]), 1),
            "D":           round(float(D.iloc[-1]), 1),
            "K_prev":      round(float(K.iloc[-2]), 1),
            "D_prev":      round(float(D.iloc[-2]), 1),
            # MACD 日
            "macd":        round(float(macd.iloc[-1]), 4),
            "signal":      round(float(sig.iloc[-1]), 4),
            "hist":        round(float(hist.iloc[-1]), 4),
            "hist_prev":   round(float(hist.iloc[-2]), 4),
            # MACD 週
            "hist_w":      round(hist_w, 4),
            "hist_w_prev": round(hist_w_prev, 4),
            "macd_w_bull": macd_w_bull,
            # 突破
            "high20_prev": round(high20_prev, 2),
            # 起漲點額外指標
            "tangle_pct":  round(tangle_pct, 2),   # 均線糾結度%
            "ma5_rising":  ma5_rising,               # MA5向上
            "ma_max":      round(float(ma_max.iloc[-1]), 2),
            # 明日強勢股額外
            "body":             round(body, 2),
            "upper_shadow_abs": round(upper_shadow_abs, 2),
            # K線型態
            "close_pos":   round(close_pos, 1),
            "upper_shad":  round(upper_shad, 1),
            "is_limit_up": chg_pct >= 9.5,
        }
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 3. 策略篩選函數（每個 return True = 命中）
# ════════════════════════════════════════════════════════════════

def _base_ok(s: dict) -> bool:
    """基本流動性過濾"""
    return s["vol_張"] >= MIN_VOL_張 and s["close"] >= MIN_PRICE


def screen_turtle(s: dict) -> bool:
    """🐢 海龜突破（XQ邏輯）：收盤突破前20日最高H（不含今日）+ 5日均量>1000張"""
    return (
        _base_ok(s)
        and s["close"] > s["high20_prev"]   # Close > Highest(High[1], 20)
        and s["vol_ma20_張"] > 1000          # Average(Volume,5) > VolFilter
    )


def screen_breakout(s: dict) -> bool:
    """🚀 起漲點（XQ邏輯）：均線糾結突破 OR 箱型突破，均需量比≥1.5x

    策略A：均線糾結(<2%) + 今日突破所有均線 + MA5向上
    策略B：收盤突破前20日H（箱型高點）
    共同：Volume > 5日均量 * 1.5
    """
    vol_ok = s["vol_ratio"] >= 1.5  # Volume > Average(Volume,5) * 1.5

    # 策略A：均線糾結突破
    cond_a = (
        s["tangle_pct"] < 2.0           # 三均線差距<2%（糾結）
        and s["close"] > s["ma_max"]     # 今日突破所有均線
        and s["ma5_rising"]              # MA5向上
    )

    # 策略B：箱型突破（收盤突破前20日最高H）
    cond_b = s["close"] > s["high20_prev"]

    return _base_ok(s) and vol_ok and (cond_a or cond_b)


def screen_strong_close(s: dict) -> bool:
    """💪 明日強勢股（XQ邏輯）：
    condition1: 漲幅 > 3%
    condition2: 量比 >= 1.5x
    condition3: 收盤 > 前20日最高H（創近期新高）
    condition4: 收盤 > MA60（在季線之上）
    condition5: 上影線 < 實體 * 0.5（收盤強勢，留影不長）
    """
    return (
        _base_ok(s)
        and s["chg_pct"] > 3.0                              # condition1
        and s["vol_ratio"] >= 1.5                           # condition2
        and s["close"] > s["high20_prev"]                   # condition3
        and s["close"] > s["ma60"]                          # condition4
        and s["upper_shadow_abs"] < s["body"] * 0.5         # condition5
    )


def screen_kd_golden(s: dict) -> bool:
    """📈 KD低檔金叉：前日K<D且K<40，今日K>D（低檔反彈）"""
    return (
        _base_ok(s)
        and s["K_prev"] < s["D_prev"]
        and s["K_prev"] < 40
        and s["K"] > s["D"]
        and s["rsi"] < 70
    )


def screen_macd_resonance(s: dict) -> bool:
    """🔥 MACD雙共振：日線柱放大（紅柱增加）+ 週線柱也向上"""
    return (
        _base_ok(s)
        and s["hist"] > 0
        and s["hist"] > s["hist_prev"]
        and s["macd"] > s["signal"]
        and s["macd_w_bull"]
    )


def screen_ma_aligned(s: dict) -> bool:
    """📊 均線多頭排列：MA5>MA10>MA20>MA60 全排好 + 股價在MA5上"""
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
    "💪強勢收盤":     screen_strong_close,
    "📈KD低檔金叉":   screen_kd_golden,
    "🔥MACD雙共振":   screen_macd_resonance,
    "📊均線多頭排列": screen_ma_aligned,
    "💥量增價漲":     screen_volume_surge,
}


# ════════════════════════════════════════════════════════════════
# 4. FinMind 法人資料（批次，只對候選股呼叫）
# ════════════════════════════════════════════════════════════════

def fetch_institutional(codes: list[str]) -> dict:
    """
    批次抓三大法人近14日資料
    回傳 {code: {"外資3日":x, "外資10日":x, "投信3日":x, "連買日":x}}
    """
    if not FINMIND_TOKEN or not codes:
        return {}

    start = (datetime.today() - timedelta(days=INST_LOOKBACK)).strftime("%Y-%m-%d")
    result = {}

    for code in tqdm(codes, desc="法人資料", ncols=60, leave=False):
        try:
            r = requests.get(FINMIND_URL, params={
                "dataset":    "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id":    code,
                "start_date": start,
                "token":      FINMIND_TOKEN,
            }, timeout=10)
            data = r.json().get("data", [])
            if not data:
                continue

            df = pd.DataFrame(data)
            df["net"] = df["buy"].astype(float) - df["sell"].astype(float)

            row = {}
            for name, key in [("外資及陸資", "外資"), ("投信", "投信")]:
                sub = df[df["name"] == name].sort_values("date")
                if sub.empty:
                    row[f"{key}3日"] = 0
                    row[f"{key}10日"] = 0
                    if key == "外資":
                        row["連買日"] = 0
                else:
                    nets = sub["net"].tolist()
                    row[f"{key}3日"]  = int(sub["net"].tail(3).sum() / 1000)
                    row[f"{key}10日"] = int(sub["net"].tail(10).sum() / 1000)
                    if key == "外資":
                        consec = 0
                        for n in reversed(nets):
                            if n > 0:
                                consec += 1
                            else:
                                break
                        row["連買日"] = consec

            result[code] = row
            time.sleep(0.25)   # rate limit
        except Exception:
            continue

    return result


# ════════════════════════════════════════════════════════════════
# 5. MD 報告輸出
# ════════════════════════════════════════════════════════════════

def write_md(df_cross: pd.DataFrame, strategy_lists: dict, today: str, inst: dict):
    os.makedirs("daily", exist_ok=True)
    path = f"daily/{today}.md"

    lines = [
        f"# {today} 策略選股報告\n",
        f"> 執行時間：{datetime.now().strftime('%H:%M')}  "
        f"策略數：{len(STRATEGIES)}  "
        f"命中股票：{len(df_cross)} 檔\n",
        "---\n",
    ]

    # ── 多策略重疊（≥2）──
    top = df_cross[df_cross["策略數"] >= 2].head(25)
    lines += [
        f"## ⭐ 多策略重疊候選（≥2 策略，共 {len(top)} 檔）\n",
        "| 代碼 | 名稱 | 收盤 | 漲幅% | RSI | K | 量比 | 收盤位置% | 策略數 | 策略組合 | 外資3日 | 連買 |",
        "|------|------|------|-------|-----|---|------|---------|--------|---------|---------|------|",
    ]
    for _, r in top.iterrows():
        code = r["代碼"]
        inst_net  = f"{inst.get(code,{}).get('外資3日','-')}張" if code in inst else "-"
        inst_cons = f"{inst.get(code,{}).get('連買日','-')}日" if code in inst else "-"
        stars = "⭐" * int(r["策略數"])
        lines.append(
            f"| {code} | {r['名稱']} | {r['收盤']} | {r['漲幅%']}% | {r['RSI']} | {r['K']} | "
            f"{r['量比']}x | {r['收盤位置%']}% | {stars} | {r['策略']} | {inst_net} | {inst_cons} |"
        )

    lines += ["\n---\n"]

    # ── 各策略 Top10 ──
    for name, lst in strategy_lists.items():
        if not lst:
            continue
        top10 = sorted(lst, key=lambda x: x["vol_ratio"], reverse=True)[:10]
        lines += [
            f"## {name}（{len(lst)} 檔，依量比排序 Top10）\n",
            "| 代碼 | 名稱 | 收盤 | 漲幅% | RSI | K | 量比 | 外資3日 |",
            "|------|------|------|-------|-----|---|------|---------|",
        ]
        for s in top10:
            c = s["code"]
            inst_net = f"{inst.get(c,{}).get('外資3日','-')}張" if c in inst else "-"
            lines.append(
                f"| {c} | {s['name']} | {s['close']} | {s['chg_pct']}% | {s['rsi']} | {s['K']} | {s['vol_ratio']}x | {inst_net} |"
            )
        lines.append("")

    lines += [
        "---\n",
        f"*資料來源：yfinance + FinMind  |  工具：xq_screen.py  |  日期：{today}*\n",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"  → daily/{today}.md 已寫入")


# ════════════════════════════════════════════════════════════════
# 6. 主程式
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="台股多策略選股掃描")
    parser.add_argument("--no-inst",   action="store_true", help="跳過 FinMind 法人資料")
    parser.add_argument("--top-only",  action="store_true", help="只掃上市前500大（加快速度）")
    parser.add_argument("--append",    action="store_true", help="不覆蓋已存在的 MD")
    args = parser.parse_args()

    today = datetime.today().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  台股策略選股  xq_screen.py  {today}")
    print(f"{'='*60}\n")

    # 1. 股票清單
    print("📋 載入股票清單...")
    all_stocks = get_tw_stock_list(top_only=args.top_only)
    print(f"   共 {len(all_stocks)} 檔\n")

    # 2. 並行技術面掃描
    print(f"🔍 技術面掃描（{MAX_WORKERS} 線程）...")
    t0 = time.time()
    tech_data = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_calc, s): s for s in all_stocks}
        for f in tqdm(as_completed(futures), total=len(futures), ncols=70):
            r = f.result()
            if r:
                tech_data.append(r)
    print(f"   有效資料：{len(tech_data)} 檔  ({time.time()-t0:.0f}秒)")

    # 去重：yfinance 對無效 ticker 有時回傳相鄰代碼資料（ghost ticker），
    # 造成多支股票擁有完全相同的20日價格序列。
    # 策略：計算每個 hash 出現次數，只有出現 ≥3 次的才是明顯的 ghost cluster，
    # 保留第一筆，移除後續。出現 1~2 次視為正常（兩支股票歷史相似的極端情況）。
    from collections import Counter
    hash_count = Counter(s["series_hash"] for s in tech_data)
    seen_fp: set = set()
    dedup_data = []
    for s in tech_data:
        fp = s["series_hash"]
        if hash_count[fp] < 3:
            dedup_data.append(s)        # 出現1~2次：無條件保留
        elif fp not in seen_fp:
            seen_fp.add(fp)
            dedup_data.append(s)        # 出現≥3次：只保留第一筆
    removed = len(tech_data) - len(dedup_data)
    if removed:
        print(f"   去重後：{len(dedup_data)} 檔（移除 {removed} 筆重複）")
    tech_data = dedup_data
    print()

    # 3. 策略篩選
    print("🎯 執行策略篩選...")
    strategy_lists = {name: [] for name in STRATEGIES}
    hit_map = {}   # code -> {"hits":[], "data":{}}

    for s in tech_data:
        hits = [name for name, fn in STRATEGIES.items() if fn(s)]
        if hits:
            hit_map[s["code"]] = {"hits": hits, "data": s}
            for name in hits:
                strategy_lists[name].append(s)

    print("   各策略命中：")
    for name, lst in strategy_lists.items():
        print(f"     {name}: {len(lst)} 檔")

    # 4. 整理交叉重疊表
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

    df_cross = pd.DataFrame(rows).sort_values(
        ["策略數", "量比"], ascending=[False, False]
    ).reset_index(drop=True)

    df_cross.to_csv("result_screen.csv", index=False, encoding="utf-8-sig")
    print(f"\n   重疊≥2策略：{len(df_cross[df_cross['策略數']>=2])} 檔")
    print(f"   result_screen.csv 已寫入\n")

    # 5. FinMind 法人資料（只對重疊≥2的候選）
    inst = {}
    if not args.no_inst and FINMIND_TOKEN:
        cands = df_cross[df_cross["策略數"] >= 2]["代碼"].tolist()[:60]
        print(f"🏛️  抓法人資料（{len(cands)} 支候選）...")
        inst = fetch_institutional(cands)
        print(f"   完成 {len(inst)} 支\n")
    elif not FINMIND_TOKEN and not args.no_inst:
        print("⚠️  未設定 FINMIND_TOKEN，跳過法人資料（export FINMIND_TOKEN=xxx）\n")

    # 6. 輸出 MD
    write_md(df_cross, strategy_lists, today, inst)

    # 7. 終端摘要
    print(f"\n{'='*60}")
    print(f"  ✅ 完成！{today}")
    print(f"  多策略重疊 Top5：")
    for _, r in df_cross.head(5).iterrows():
        print(f"    {r['代碼']}  {r['收盤']}  {r['策略數']}策略  {r['策略']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
