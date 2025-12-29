// 腳本名稱: 綜合評分選股系統
// 適用頻率: 日 (建議在選股中心使用)
// 說明: 整合技術面+籌碼面+動能指標，為每檔股票計算綜合評分 (滿分100分)
//       分數越高，隔日續漲機率越大

// ========================================
// 1. 參數設定
// ========================================
input: BreakoutDays(20, "突破天數");
input: VolRatio(1.5, "爆量倍數");
input: MinScore(60, "最低通過分數");

// ========================================
// 2. 基礎數據計算
// ========================================

// 均線
value1 = Average(Close, 5);    // 5MA
value2 = Average(Close, 20);   // 20MA (月線)
value3 = Average(Close, 60);   // 60MA (季線)

// 成交量
value4 = Average(Volume, 5);   // 5日均量
value5 = Average(Volume, 20);  // 20日均量

// 價格區間
value6 = Highest(High[1], BreakoutDays);  // N日最高價
value7 = Lowest(Low[1], BreakoutDays);    // N日最低價

// ========================================
// 3. 技術指標計算
// ========================================

// RSI (14日)
value10 = RSI(Close, 14);

// KD 指標 (9,3,3)
value11 = Stochastic(9, 3);  // K值
value12 = SlowD(9, 3);       // D值

// MACD (12, 26, 9)
value13 = MACD(Close, 12, 26);      // DIF
value14 = XAverage(value13, 9);     // MACD (DEA)
value15 = value13 - value14;        // 柱狀體 (OSC)

// ========================================
// 4. 評分系統 (滿分100分)
// ========================================
variable: TotalScore(0);
TotalScore = 0;

// --- A. 趨勢分數 (25分) ---

// A1. 收盤價 > 季線 (+10分) - 多頭格局
if Close > value3 then TotalScore = TotalScore + 10;

// A2. 均線多頭排列 5MA > 20MA > 60MA (+10分)
if value1 > value2 and value2 > value3 then TotalScore = TotalScore + 10;

// A3. 股價站上月線 (+5分)
if Close > value2 then TotalScore = TotalScore + 5;

// --- B. 突破分數 (25分) ---

// B1. 收盤創N日新高 (+15分) - 海龜突破核心
if Close > value6 then TotalScore = TotalScore + 15;

// B2. 突破盤整區間 (收盤 > 區間高點*0.98) (+5分)
if Close > value6 * 0.98 then TotalScore = TotalScore + 5;

// B3. 收盤接近當日最高 (上影線短) (+5分)
if High > 0 and (High - Close) < (High - Low) * 0.2 then TotalScore = TotalScore + 5;

// --- C. 量能分數 (20分) ---

// C1. 成交量 > 5日均量 * 1.5 (+10分) - 爆量確認
if Volume > value4 * VolRatio then TotalScore = TotalScore + 10;

// C2. 成交量 > 20日均量 (+5分)
if Volume > value5 then TotalScore = TotalScore + 5;

// C3. 量增價漲 (+5分)
if Volume > Volume[1] and Close > Close[1] then TotalScore = TotalScore + 5;

// --- D. 動能指標分數 (20分) ---

// D1. RSI 在強勢區間 (50-70) (+5分) - 強勢但未過熱
if value10 > 50 and value10 < 70 then TotalScore = TotalScore + 5;

// D2. KD 黃金交叉或 K > D (+5分)
if value11 > value12 then TotalScore = TotalScore + 5;

// D3. MACD 柱狀翻正或擴大 (+5分)
if value15 > 0 then TotalScore = TotalScore + 5;

// D4. MACD DIF > 0 (多方格局) (+5分)
if value13 > 0 then TotalScore = TotalScore + 5;

// --- E. K線型態分數 (10分) ---

// E1. 今日紅K (+5分)
if Close > Open then TotalScore = TotalScore + 5;

// E2. 今日漲幅 > 3% (+5分)
if Close > Close[1] * 1.03 then TotalScore = TotalScore + 5;

// ========================================
// 5. 篩選條件
// ========================================

// 基本門檻: 分數 >= 設定值
if TotalScore >= MinScore then ret = 1;

// ========================================
// 6. 輸出欄位
// ========================================
outputfield(1, TotalScore, 0, "綜合評分");
outputfield(2, Close, 2, "收盤價");
outputfield(3, (Close-Close[1])/Close[1]*100, 2, "漲跌幅(%)");
outputfield(4, Volume, 0, "成交量");
outputfield(5, value10, 1, "RSI(14)");
outputfield(6, value11, 1, "K值");
outputfield(7, value15, 2, "MACD柱");

// ========================================
// 7. 評分說明
// ========================================
// 90-100分: ⭐⭐⭐ 極強勢，可積極操作
// 75-89分:  ⭐⭐  強勢股，標準進場
// 60-74分:  ⭐    觀望，等待更好時機
// <60分:    ❌    不符合條件，暫不考慮
