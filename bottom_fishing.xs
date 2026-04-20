{ ============================================================
  抄底選股腳本 (bottom_fishing.xs)
  策略：跌深超賣 + 止跌訊號 + 籌碼支撐
  適用情境：大盤系統性下跌後，尋找反彈潛力股
  ============================================================ }

PARAM
  LookbackHigh := 60;   { 從高點回落的回望天數 }
  RSI_Period   := 14;
  MA_Short     := 5;
  MA_Mid       := 20;
  MA_Long      := 60;

VAR
  HighestPeak  : Series;
  DropPct      : Series;
  RSI_Val      : Series;
  MA5          : Series;
  MA20         : Series;
  MA60         : Series;
  VolMA5       : Series;
  LowerShadow  : Series;
  BodySize     : Series;

BEGIN
  { === 均線 === }
  MA5  := MA(CLOSE, MA_Short);
  MA20 := MA(CLOSE, MA_Mid);
  MA60 := MA(CLOSE, MA_Long);

  { === 量能 === }
  VolMA5 := MA(VOL, 5);

  { === RSI === }
  RSI_Val := RSI(CLOSE, RSI_Period);

  { === 從近 N 日高點跌幅 === }
  HighestPeak := HHV(HIGH, LookbackHigh);
  DropPct     := (HighestPeak - CLOSE) / HighestPeak * 100;

  { === K棒型態：下影線長度 / 實體大小 === }
  LowerShadow := MIN(OPEN, CLOSE) - LOW;
  BodySize    := ABS(CLOSE - OPEN);

  { ============================================================
    第一層：跌深條件
    - 從高點回落超過 30%
    - 股價在季線之下（空頭格局）
    - RSI 超賣（< 35）
    ============================================================ }
  COND1 := DropPct >= 30;
  COND2 := CLOSE < MA60;
  COND3 := RSI_Val < 35;

  { ============================================================
    第二層：止跌訊號（擇一成立即可）
    A. 長下影線：下影線 > 實體 * 2，且下影線 > 1%
    B. 量縮後放量：今日量 > 昨日量 * 1.5，且昨日量 < 前日量
    C. 收盤 > 開盤（今日收紅）且量能放大
    ============================================================ }
  SHADOW_A := (LowerShadow > BodySize * 2) AND (LowerShadow / CLOSE * 100 > 1);
  VOLUME_B := (VOL > VOL[1] * 1.5) AND (VOL[1] < VOL[2]);
  RED_C    := (CLOSE > OPEN) AND (VOL > VolMA5 * 1.2);

  COND4 := SHADOW_A OR VOLUME_B OR RED_C;

  { ============================================================
    第三層：基本條件過濾（避開地雷股）
    - 成交量 > 1000 張（流動性）
    - 股價 > 10 元（避開雞蛋水餃股）
    - 非漲跌停（今日無異常）
    ============================================================ }
  COND5 := VOL > 1000;
  COND6 := CLOSE > 10;
  COND7 := (CLOSE / CLOSE[1] - 1) * 100 < 9.5;   { 非漲停 }
  COND8 := (CLOSE / CLOSE[1] - 1) * 100 > -9.5;  { 非跌停 }

  { ============================================================
    最終輸出
    ============================================================ }
  OUTPUT := COND1 AND COND2 AND COND3 AND COND4
            AND COND5 AND COND6 AND COND7 AND COND8;
END
