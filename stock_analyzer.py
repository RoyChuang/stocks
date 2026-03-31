"""
台股每日選股分析工具 v3
資料來源：FinMind（三大法人/融資券/月營收/當沖/台指期）+ yfinance（全球市場/技術指標）

安裝：pip install yfinance pandas requests
用法：
  python stock_analyzer.py 3715 2419 1905
  python stock_analyzer.py --file stocks.txt
  python stock_analyzer.py --global-only
"""
import argparse, sys, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings('ignore')

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import yfinance as yf
import pandas as pd
import requests

# ── 常數 ──────────────────────────────────────────────────────
FINMIND   = 'https://api.finmindtrade.com/api/v4/data'
FINMIND_H = {'User-Agent': 'Mozilla/5.0'}

SECTOR_MAP = {
    '2313':'PCB','3715':'PCB','4958':'PCB','2355':'PCB',
    '4906':'PCB','6269':'PCB','3037':'PCB','2404':'PCB',
    '2330':'半導體','3105':'半導體','2303':'半導體','2379':'半導體',
    '2419':'網通','3034':'網通',
    '5871':'金融','2882':'金融','2886':'金融','2884':'金融',
    '1905':'紙業','1909':'紙業',
    '1528':'機械','1582':'機械',
    '2484':'電子',
}

GLOBAL_TICKERS = {
    '^VIX':'VIX恐慌指數', '^GSPC':'S&P500', 'QQQ':'Nasdaq ETF',
    'TSM':'台積電ADR', 'EWT':'台灣ETF',
    'GC=F':'黃金期貨', 'CL=F':'原油期貨',
    '^TNX':'10Y美債殖利率', 'NVDA':'NVIDIA',
}


# ── 工具 ──────────────────────────────────────────────────────
def days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime('%Y-%m-%d')

def fm(dataset: str, data_id: str = '', start: str = '', **kw) -> list:
    """FinMind API 統一呼叫，失敗回傳空 list"""
    try:
        params = {'dataset': dataset}
        if data_id: params['data_id'] = data_id
        if start:   params['start_date'] = start
        params.update(kw)
        r = requests.get(FINMIND, params=params, headers=FINMIND_H, timeout=10)
        return r.json().get('data', [])
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# 台指期夜盤（自動抓，不需手動輸入）
# ══════════════════════════════════════════════════════════════
def _is_night_session() -> bool:
    """台灣時間 15:00~05:00 = 夜盤進行中"""
    h = datetime.now().hour
    return h >= 15 or h < 5


def _fetch_realtime_futures() -> dict:
    """
    夜盤進行中時，從台期所官方 API 取即時台指期近月報價。
    POST https://mis.taifex.com.tw/futures/api/getQuoteList
    """
    try:
        url = 'https://mis.taifex.com.tw/futures/api/getQuoteList'
        body = {'MarketType': '1', 'SymbolType': 'F', 'Sym': 'TX'}
        r = requests.post(url, json=body, headers=FINMIND_H, timeout=8)
        quotes = r.json()['RtData']['QuoteList']
        # 取近月：有成交量、排除現貨列（TXF-P）、取量最大
        active = [q for q in quotes
                  if q.get('CLastPrice') and q['SymbolID'] != 'TXF-P'
                  and q.get('CTotalVolume')]
        if not active:
            return {}
        q = max(active, key=lambda x: int(x['CTotalVolume']))
        last    = float(q['CLastPrice'])
        ref     = float(q['CRefPrice'])
        spread  = round(last - ref)
        sper    = round((last - ref) / ref * 100, 2)
        return {
            'date':       datetime.now().strftime('%Y-%m-%d'),
            'close':      last,
            'spread':     spread,
            'spread_per': sper,
            'open':       float(q['COpenPrice'] or last),
            'high':       float(q['CHighPrice'] or last),
            'low':        float(q['CLowPrice']  or last),
            'volume':     int(q['CTotalVolume']),
            'source':     '台期所即時',
        }
    except Exception:
        return {}


def fetch_tw_night_futures() -> dict:
    """
    自動取得台指期夜盤資料：
    - 夜盤進行中（15:00~05:00）→ 優先抓 Yahoo Finance 即時價
    - 夜盤結束後 → 從 FinMind 取已完成的夜盤收盤
    回傳 {'date','close','spread','spread_per','open','high','low','volume','source'}
    """
    if _is_night_session():
        result = _fetch_realtime_futures()
        if result:
            return result
        # 即時抓失敗時提示，再 fall back FinMind 的前一完成夜盤
        print('  ⚠️ 即時夜盤抓取失敗，改用 FinMind 前次夜盤（非即時）')

    rows = fm('TaiwanFuturesDaily', 'TX', days_ago(7))
    night = [d for d in rows
             if d['trading_session'] == 'after_market'
             and len(str(d['contract_date'])) == 6
             and d['volume'] > 0]
    if not night:
        return {}
    latest_date = max(d['date'] for d in night)
    same_day    = [d for d in night if d['date'] == latest_date]
    latest      = max(same_day, key=lambda x: x['volume'])
    src = 'FinMind(前夜盤)' if _is_night_session() else 'FinMind'
    return {
        'date':       latest['date'],
        'close':      latest['close'],
        'spread':     latest['spread'],
        'spread_per': latest['spread_per'],
        'open':       latest['open'],
        'high':       latest['max'],
        'low':        latest['min'],
        'volume':     latest['volume'],
        'source':     src,
    }


# ══════════════════════════════════════════════════════════════
# 全球市場
# ══════════════════════════════════════════════════════════════
def fetch_global_market() -> float:
    """印出全球市場概況，回傳 VIX 值"""
    print('\n' + '='*64)
    print('全球市場 + 台指期夜盤')
    print('='*64)

    # ── 美股日線 ──
    rows = []
    for ticker, name in GLOBAL_TICKERS.items():
        try:
            df = yf.download(ticker, period='5d', interval='1d',
                             progress=False, auto_adjust=True, multi_level_index=False)
            if df.empty or len(df) < 2: continue
            c = df['Close']
            latest, prev, first = c.iloc[-1], c.iloc[-2], c.iloc[0]
            rows.append({'名稱':name, '最新':round(latest,2),
                         '日漲跌%':round((latest-prev)/prev*100,2),
                         '週漲跌%':round((latest-first)/first*100,2)})
        except Exception:
            pass
    if rows:
        print(pd.DataFrame(rows).set_index('名稱').to_string())

    # ── 美股盤後 ──
    print('\n── 美股盤後')
    for ticker, name in {'^GSPC':'S&P500','QQQ':'Nasdaq','NVDA':'NVIDIA','TSM':'台積電ADR'}.items():
        try:
            df = yf.download(ticker, period='2d', interval='1m',
                             progress=False, auto_adjust=True,
                             multi_level_index=False, prepost=True)
            if df.empty: continue
            c    = df['Close'].dropna()
            last = float(c.iloc[-1])
            reg  = next((float(c[ts]) for ts in reversed(c.index)
                         if ts.hour == 16 and ts.minute == 0), float(c.iloc[-2]))
            chg  = (last - reg) / reg * 100
            sym  = '▲' if chg >= 0 else '▼'
            print(f'  {name:12s}  收盤:{reg:8.2f}  盤後:{last:8.2f}  {sym}{chg:+.2f}%')
        except Exception:
            pass

    # ── 台指期夜盤（自動抓）──
    night = fetch_tw_night_futures()
    src_label = night.get('source', 'FinMind') if night else 'FinMind'
    print(f'\n── 台指期夜盤（{src_label}）')
    gap_pct = 0.0
    if night:
        s = night['spread']
        p = night['spread_per']
        sym = '▲' if s >= 0 else '▼'
        print(f'  日期: {night["date"]}  近月合約')
        print(f'  開:{night["open"]:,.0f}  高:{night["high"]:,.0f}  '
              f'低:{night["low"]:,.0f}  收:{night["close"]:,.0f}')
        print(f'  漲跌: {sym}{s:+.0f} 點  ({p:+.2f}%)  量:{night["volume"]:,}口')
        gap_pct = p

        if gap_pct <= -3:
            print(f'  ⛔ 重大跳空下跌，強烈建議不入場')
        elif gap_pct <= -1.5:
            print(f'  🔴 顯著跳空，不追，等反彈確認')
        elif gap_pct <= -0.5:
            print(f'  🟡 小幅低開，觀察量能確認')
        elif gap_pct >= 1.5:
            print(f'  🟡 高開，追高需謹慎')
        else:
            print(f'  🟢 平開，正常操作')
    else:
        print('  （夜盤非交易時段或資料尚未更新）')

    # ── VIX ──
    vix_now = 20.0
    try:
        vdf = yf.download('^VIX', period='1mo', interval='1d',
                          progress=False, auto_adjust=True, multi_level_index=False)
        vix_now = float(vdf['Close'].iloc[-1])
        vix_avg = float(vdf['Close'].mean())
        lvl = ('🔴 高恐慌(>30) — 保守操作，倉位減半'  if vix_now >= 30 else
               '🟡 中度恐慌(20-30) — 謹慎操作'        if vix_now >= 20 else
               '🟢 低恐慌(<20) — 正常操作')
        print(f'\nVIX: {vix_now:.2f}  (月均 {vix_avg:.2f})  {lvl}')
    except Exception:
        pass

    return vix_now, gap_pct


# ══════════════════════════════════════════════════════════════
# FinMind 籌碼資料（批次一次抓完）
# ══════════════════════════════════════════════════════════════
def fetch_institutional(codes: list) -> dict:
    """
    回傳 {code: {'foreign':淨買賣股, 'trust':淨買賣股, 'dealer':淨買賣股}}
    """
    result = defaultdict(lambda: {'foreign':0,'trust':0,'dealer':0,'total':0})
    start  = days_ago(5)
    for code in codes:
        rows = fm('TaiwanStockInstitutionalInvestorsBuySell', code, start)
        # 取最新一天
        if not rows: continue
        latest_date = max(r['date'] for r in rows)
        for r in rows:
            if r['date'] != latest_date: continue
            net = r['buy'] - r['sell']
            n   = r['name']
            if 'Foreign_Investor' in n:
                result[code]['foreign'] += net
            elif 'Investment_Trust' in n:
                result[code]['trust']   += net
            elif 'Dealer' in n:
                result[code]['dealer']  += net
        result[code]['total'] = (result[code]['foreign'] +
                                 result[code]['trust']   +
                                 result[code]['dealer'])
    return dict(result)


def fetch_margin(codes: list) -> dict:
    """
    回傳 {code: {'margin_today':融資餘額, 'margin_chg':增減, 'short_today':融券, 'short_chg':增減}}
    """
    result = {}
    start  = days_ago(5)
    for code in codes:
        rows = fm('TaiwanStockMarginPurchaseShortSale', code, start)
        if not rows: continue
        r = sorted(rows, key=lambda x: x['date'])[-1]
        result[code] = {
            'margin_today': r['MarginPurchaseTodayBalance'],
            'margin_chg':   r['MarginPurchaseTodayBalance'] - r['MarginPurchaseYesterdayBalance'],
            'short_today':  r['ShortSaleTodayBalance'],
            'short_chg':    r['ShortSaleTodayBalance']   - r['ShortSaleYesterdayBalance'],
        }
    return result


def fetch_revenue(codes: list) -> dict:
    """
    回傳 {code: {'rev_m':本月億, 'yoy':%,'mom':%}}
    """
    result = {}
    start  = days_ago(400)
    for code in codes:
        rows = fm('TaiwanStockMonthRevenue', code, start)
        if not rows: continue
        rows = sorted(rows, key=lambda x: (x['revenue_year'], x['revenue_month']))
        if len(rows) < 2: continue
        cur  = rows[-1]
        prev = rows[-2]
        # 找去年同月
        yoy_row = next((r for r in rows
                        if r['revenue_year']  == cur['revenue_year'] - 1
                        and r['revenue_month'] == cur['revenue_month']), None)
        rev_m = cur['revenue'] / 1e8
        mom   = (cur['revenue'] - prev['revenue']) / prev['revenue'] * 100 if prev['revenue'] else None
        yoy   = ((cur['revenue'] - yoy_row['revenue']) / yoy_row['revenue'] * 100
                 if yoy_row and yoy_row['revenue'] else None)
        result[code] = {
            'rev_m': round(rev_m, 2),
            'yoy':   round(yoy, 1) if yoy is not None else None,
            'mom':   round(mom, 1) if mom is not None else None,
            'month': f"{cur['revenue_year']}/{cur['revenue_month']:02d}",
        }
    return result


def fetch_day_trade(codes: list) -> dict:
    """回傳 {code: 當沖比例%}"""
    result = {}
    start  = days_ago(5)
    for code in codes:
        dt_rows    = fm('TaiwanStockDayTrading', code, start)
        price_rows = fm('TaiwanStockPrice',      code, start)
        if not dt_rows or not price_rows: continue
        dt    = sorted(dt_rows,    key=lambda x: x['date'])[-1]
        price = sorted(price_rows, key=lambda x: x['date'])[-1]
        if dt['date'] != price['date']:  continue
        total_vol = price['Trading_Volume']
        if total_vol:
            result[code] = round(dt['Volume'] / total_vol * 100, 1)
    return result


# ══════════════════════════════════════════════════════════════
# 技術指標（yfinance）
# ══════════════════════════════════════════════════════════════
def calc_technical(code: str) -> dict | None:
    try:
        df = yf.download(code + '.TW', period='6mo', interval='1d',
                         progress=False, auto_adjust=True, multi_level_index=False)
        if df is None or df.empty: return None
        c, h, l, v = df['Close'], df['High'], df['Low'], df['Volume']

        ma5, ma10, ma20 = (c.rolling(n).mean().iloc[-1] for n in (5,10,20))

        ema12, ema26 = c.ewm(span=12).mean(), c.ewm(span=26).mean()
        macd  = ema12 - ema26
        sig   = macd.ewm(span=9).mean()
        hist  = macd - sig
        macd_golden = bool(macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2])
        macd_bull   = bool(macd.iloc[-1] > sig.iloc[-1])

        rsv = (c - l.rolling(14).min()) / (h.rolling(14).max() - l.rolling(14).min()) * 100
        K   = rsv.ewm(com=2).mean()
        D   = K.ewm(com=2).mean()
        kd_golden = bool(K.iloc[-1] > D.iloc[-1] and K.iloc[-2] <= D.iloc[-2])

        delta = c.diff()
        rsi   = float((100 - 100/(1 + delta.clip(lower=0).rolling(14).mean() /
                                      (-delta.clip(upper=0)).rolling(14).mean())).iloc[-1])

        tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        close     = float(c.iloc[-1])
        vol_ratio = float(v.iloc[-1]) / float(v.tail(20).mean())

        bb_mid   = float(c.rolling(20).mean().iloc[-1])
        bb_std   = float(c.rolling(20).std().iloc[-1])
        bb_pos   = (close - (bb_mid - 2*bb_std)) / (4*bb_std) * 100

        # ── 日K型態 ──
        o_today = float(df['Open'].iloc[-1])
        h_today = float(h.iloc[-1])
        l_today = float(l.iloc[-1])
        day_range = h_today - l_today
        if day_range > 0:
            close_pos    = round((close   - l_today) / day_range * 100, 1)  # 0=收在最低 100=收在最高
            upper_shadow = round((h_today - close)   / day_range * 100, 1)  # 上影線佔比
            lower_shadow = round((close   - l_today) / day_range * 100, 1)  # 下影線佔比（同 close_pos）
        else:
            close_pos = upper_shadow = lower_shadow = 50.0
        prev_close   = float(c.iloc[-2])
        up_pct_today = (close - prev_close) / prev_close * 100
        is_limit_up  = up_pct_today >= 9.5  # 漲停（台股漲幅上限約 10%）

        if   close_pos >= 80:                      k_pattern = '強勢收盤'
        elif close_pos <= 20:                      k_pattern = '弱勢收盤'
        elif upper_shadow >= 60 and not is_limit_up: k_pattern = '長上影線'
        elif lower_shadow >= 60:                   k_pattern = '長下影線'
        else:                                      k_pattern = '中性'

        return {
            'code': code, 'close': round(close,2),
            'ma5':round(float(ma5),2), 'ma10':round(float(ma10),2), 'ma20':round(float(ma20),2),
            'macd':round(float(macd.iloc[-1]),3), 'hist':round(float(hist.iloc[-1]),3),
            'macd_golden':macd_golden, 'macd_bull':macd_bull,
            'hist_pos':bool(hist.iloc[-1]>0), 'hist_expand':bool(abs(hist.iloc[-1])>abs(hist.iloc[-2])),
            'K':round(float(K.iloc[-1]),1), 'D':round(float(D.iloc[-1]),1),
            'kd_golden':kd_golden, 'RSI':round(rsi,1), 'ATR14':round(atr,2),
            'vol_ratio':round(vol_ratio,2),
            'support':round(float(l.tail(20).min()),2), 'resist':round(float(h.tail(20).max()),2),
            'bb_pos':round(bb_pos,1),
            'ma_bull':bool(close > float(ma5) > float(ma10) > float(ma20)),
            'close_pos':close_pos, 'upper_shadow':upper_shadow,
            'is_limit_up':is_limit_up, 'k_pattern':k_pattern,
        }
    except Exception as e:
        print(f'  [{code}] 技術面失敗: {e}')
        return None


# ══════════════════════════════════════════════════════════════
# 評分
# ══════════════════════════════════════════════════════════════
def score_stock(t, inst, marg, dt_pct, rev) -> tuple[int, list]:
    score, notes = 0, []

    # 技術面（最高 20 分）
    if t['macd_golden']:    score+=5; notes.append('MACD黃金交叉+5')
    elif t['macd_bull']:    score+=3; notes.append('MACD多頭+3')
    if t['hist_pos']:       score+=2; notes.append('紅柱+2')
    if t['hist_expand']:    score+=1; notes.append('柱擴張+1')
    if t['kd_golden']:      score+=3; notes.append('KD黃金交叉+3')
    elif t['K']>t['D']:     score+=2; notes.append('K>D+2')
    if 50<=t['RSI']<=70:    score+=2; notes.append('RSI健康+2')
    elif t['RSI']>70:                 notes.append('⚠️RSI超買')
    if t['vol_ratio']>=3:   score+=3; notes.append(f'爆量{t["vol_ratio"]:.1f}x+3')
    elif t['vol_ratio']>=2: score+=2; notes.append(f'大量{t["vol_ratio"]:.1f}x+2')
    elif t['vol_ratio']>=1.5:score+=1;notes.append(f'量增{t["vol_ratio"]:.1f}x+1')
    if t['ma_bull']:        score+=2; notes.append('均線多頭排列+2')
    if t['bb_pos']>80:                notes.append(f'⚠️近布林上軌({t["bb_pos"]:.0f}%)')
    elif t['bb_pos']>50:    score+=1; notes.append('布林偏多+1')

    # 三大法人（最高 8 分）
    if inst:
        fk = inst['foreign']//1000
        tk = inst['trust']//1000
        tot = inst['total']//1000
        if fk>0:   score+=3; notes.append(f'外資+{fk:,}張+3')
        elif fk<0:            notes.append(f'⚠️外資{fk:,}張')
        if tk>0:   score+=3; notes.append(f'投信+{tk:,}張+3')
        elif tk<0:            notes.append(f'⚠️投信{tk:,}張')
        if tot>0:  score+=2; notes.append(f'三大法人合計+{tot:,}張+2')

    # 融資融券（+2 或 -2 分）
    if marg:
        mc = marg['margin_chg']//1000
        if mc < -500:   score+=2; notes.append(f'融資減{mc:,}張(籌碼乾淨)+2')
        elif mc > 2000: score-=2; notes.append(f'⚠️融資暴增+{mc:,}張-2')
        sc = marg['short_chg']//1000
        if sc > 500:    score+=1; notes.append(f'融券增{sc:,}張(潛在軋空)+1')

    # 當沖比例（高比例提示）
    if dt_pct:
        if dt_pct > 60: notes.append(f'⚠️當沖比{dt_pct:.0f}%(投機偏高)')
        elif dt_pct > 40: notes.append(f'當沖比{dt_pct:.0f}%')

    # 月營收（最高 5 分）
    if rev:
        yoy = rev.get('yoy')
        mom = rev.get('mom')
        if yoy is not None:
            if yoy>=30:    score+=3; notes.append(f'營收YoY+{yoy:.0f}%+3')
            elif yoy>=10:  score+=2; notes.append(f'營收YoY+{yoy:.0f}%+2')
            elif yoy>=0:   score+=1; notes.append(f'營收YoY+{yoy:.0f}%+1')
            else:                    notes.append(f'⚠️營收YoY{yoy:.0f}%')
        if mom and mom>=10: score+=2; notes.append(f'營收MoM+{mom:.0f}%+2')

    # 日K型態（最高 +3，最低 -3）
    cp  = t.get('close_pos', 50.0)
    ush = t.get('upper_shadow', 50.0)
    lim = t.get('is_limit_up', False)
    if lim:
        # 漲停：用量比區分是否真正鎖板
        vr = t.get('vol_ratio', 0)
        if vr >= 5:
            notes.append(f'⚠️漲停大量({vr:.1f}x)，恐曾開板，隔日慎追')
        else:
            score += 1; notes.append(f'漲停({vr:.1f}x)+1')
    else:
        if   cp >= 80: score+=2; notes.append(f'強勢收盤({cp:.0f}%)+2')
        elif cp >= 60: score+=1; notes.append(f'收盤偏強({cp:.0f}%)+1')
        elif cp <= 20: score-=2; notes.append(f'⚠️弱勢收盤({cp:.0f}%)−2')
        elif cp <= 40: score-=1; notes.append(f'⚠️收盤偏弱({cp:.0f}%)−1')
        if ush >= 60:  score-=1; notes.append(f'⚠️長上影線({ush:.0f}%)−1')

    return score, notes


# ══════════════════════════════════════════════════════════════
# 入場點位
# ══════════════════════════════════════════════════════════════
def entry_points(t, vix) -> str:
    c, atr = t['close'], t['ATR14']
    sl_m   = 2.5 if vix>=30 else (2.0 if vix>=20 else 1.5)
    tp1_m  = 2.5 if vix>=30 else 3.0
    tp2_m  = 4.0 if vix>=30 else 5.0
    stop   = round(c - atr*sl_m, 2)
    t1     = round(c + atr*tp1_m, 2)
    t2     = round(c + atr*tp2_m, 2)
    rr     = round((t1-c)/(c-stop), 2) if c!=stop else 0
    return (
        f'  ATR={atr}  布林={t["bb_pos"]:.0f}%\n'
        f'  保守入場: {round(c*.99,2)}~{round(c*.97,2)}  (等拉回)\n'
        f'  積極入場: {c}~{round(c*1.02,2)}  (開平追量)\n'
        f'  止損: {stop}  (-{round((c-stop)/c*100,1)}%)  [ATR×{sl_m}]\n'
        f'  目標1: {t1}  (+{round((t1-c)/c*100,1)}%)  風報比{rr}:1\n'
        f'  目標2: {t2}  (+{round((t2-c)/c*100,1)}%)\n'
        f'  支撐:{t["support"]}  壓力:{t["resist"]}'
    )


def kelly_position(score, vix) -> str:
    p    = min(0.40 + score/35*0.25, 0.65)
    b    = 2.0
    f    = max((b*p-(1-p))/b * 0.5, 0)
    adj  = 0.5 if vix>=30 else (0.75 if vix>=20 else 1.0)
    pct  = round(min(f*adj*100, 25), 1)
    tag  = '🔴建議觀望' if pct<3 else f'建議倉位 {pct}%'
    return f'{tag}  (勝率估{p:.0%}, 半凱利×VIX係數{adj})'


def sector_warning(codes):
    from collections import Counter
    cnt = Counter(SECTOR_MAP.get(c,'其他') for c in codes)
    print('\n產業集中度：')
    for sec, n in cnt.most_common():
        stocks = [c for c in codes if SECTOR_MAP.get(c,'其他')==sec]
        warn = '  ⚠️同產業過多' if n>=3 else ''
        print(f'  {sec}: {n}支 ({", ".join(stocks)}){warn}')


# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stocks', nargs='*')
    ap.add_argument('--file',  '-f')
    ap.add_argument('--global-only', action='store_true')
    ap.add_argument('--no-revenue',  action='store_true', help='跳過月營收（較快）')
    args = ap.parse_args()

    vix, gap_pct = fetch_global_market()

    if args.global_only: return

    codes = list(args.stocks)
    if args.file:
        with open(args.file) as f:
            codes += [l.strip() for l in f if l.strip()]
    codes = list(dict.fromkeys(codes))
    if not codes:
        print('\n請提供股票代號'); return

    # ── 批次抓籌碼 ──
    print(f'\n{"="*64}\n抓取籌碼資料（FinMind）...')
    print('  三大法人...', end='', flush=True)
    inst_all = fetch_institutional(codes); print(f' {len(inst_all)}筆')
    print('  融資融券...',  end='', flush=True)
    marg_all = fetch_margin(codes);       print(f' {len(marg_all)}筆')
    print('  當沖比例...',  end='', flush=True)
    dt_all   = fetch_day_trade(codes);    print(f' {len(dt_all)}筆')
    rev_all  = {}
    if not args.no_revenue:
        print('  月營收...',    end='', flush=True)
        rev_all = fetch_revenue(codes);   print(f' {len(rev_all)}筆')

    # ── 技術分析 ──
    print(f'\n{"="*64}\n分析 {len(codes)} 支股票技術面...')
    results = []
    for code in codes:
        print(f'  {code} ...', end='', flush=True)
        t = calc_technical(code)
        if not t: print(' 失敗'); continue
        inst = inst_all.get(code, {})
        marg = marg_all.get(code, {})
        dt   = dt_all.get(code, 0)
        rev  = rev_all.get(code, {})
        score, notes = score_stock(t, inst, marg, dt, rev)
        print(f' 評分:{score:2d}  RSI:{t["RSI"]}  K:{t["K"]}  '
              f'外資:{"有" if inst else "-"}  融資:{"有" if marg else "-"}  '
              f'當沖:{"有" if dt else "-"}  營收:{"有" if rev else "-"}')
        results.append({**t, 'score':score, 'notes':', '.join(notes),
                         '_inst':inst, '_marg':marg, '_dt':dt, '_rev':rev})

    if not results: print('無資料'); return
    results.sort(key=lambda x: (-x['score'], -x['vol_ratio']))

    # ── 摘要表 ──
    print(f'\n{"="*64}\n綜合評分排名')
    print('='*64)
    rows = []
    for r in results:
        inst = r['_inst']; marg = r['_marg']; rev = r['_rev']
        rows.append({
            '代碼':r['code'], '收盤':r['close'], 'RSI':r['RSI'], 'K':r['K'],
            '量比':r['vol_ratio'],
            'K線型態':r.get('k_pattern','-'),
            '收盤位置':f'{r.get("close_pos","-"):.0f}%' if isinstance(r.get('close_pos'), float) else '-',
            '上影%':f'{r.get("upper_shadow","-"):.0f}%' if isinstance(r.get('upper_shadow'), float) else '-',
            '外資(張)':f'{inst["foreign"]//1000:+,}' if inst else '-',
            '投信(張)':f'{inst["trust"]//1000:+,}'   if inst else '-',
            '融資增減':f'{marg["margin_chg"]//1000:+,}' if marg else '-',
            '當沖%':f'{r["_dt"]:.0f}%' if r['_dt'] else '-',
            '營收YoY':f'{rev["yoy"]}%' if rev and rev.get("yoy") else '-',
            '評分':r['score'],
        })
    print(pd.DataFrame(rows).to_string(index=False))

    sector_warning([r['code'] for r in results])

    # ── Top 5 入場點 ──
    print(f'\n{"="*64}\n入場點位 + 倉位建議 Top5  [VIX={vix:.1f}]')
    print('='*64)
    for i, r in enumerate(results[:5], 1):
        rev = r['_rev']
        rev_str = (f'  營收({rev["month"]}): {rev["rev_m"]:.1f}億  '
                   f'YoY:{rev["yoy"]:+.1f}%  MoM:{rev["mom"]:+.1f}%'
                   if rev and rev.get('yoy') is not None else '')
        print(f'\n#{i}  {r["code"]}  收盤:{r["close"]}  '
              f'評分:{r["score"]}  量比:{r["vol_ratio"]}x')
        print(entry_points(r, vix))
        print(f'  {kelly_position(r["score"], vix)}')
        if rev_str: print(rev_str)
        print(f'  {r["notes"][:130]}')

    # ── 開盤情境 ──
    print(f'\n{"="*64}\n開盤情境指引  [夜盤:{gap_pct:+.1f}%  VIX:{vix:.1f}]')
    print('='*64)
    top = results[0]
    c   = top['close']
    if gap_pct <= -3:
        print('  ⛔ 夜盤重跌 → 開盤不入場，等指數止跌回穩後再評估')
    elif gap_pct <= -1.5:
        print(f'  🔴 夜盤跌 {gap_pct:.1f}% → 等開盤確認量縮止跌再進')
    else:
        print(f'  大盤開低>-1.5%     → 不入場，等確認')
        print(f'  大盤開平 ±0.5%     → 追 #{1} {top["code"]} 積極入場區')
    print(f'  個股開低回 {round(c*.97,1)} 以下 → 等量能確認再接')
    print(f'  個股跳空>+5%       → 不追，等回測')
    if vix >= 30:
        print(f'\n  ⚠️  VIX={vix:.1f} 高恐慌 — 所有倉位縮減50%，止損嚴格執行')

    # ── 存檔 ──
    save = [{k:v for k,v in r.items() if not k.startswith('_')} for r in results]
    pd.DataFrame(save).to_csv('d:/Code/stocks/result_technical.csv',
                               index=False, encoding='utf-8-sig')
    print('\n結果已存至: d:/Code/stocks/result_technical.csv')


if __name__ == '__main__':
    main()
