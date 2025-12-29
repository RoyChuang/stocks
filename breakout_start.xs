// 腳本名稱: 起漲點偵測 (均線糾結 + 箱型突破)
// 適用頻率: 日 (建議在選股中心使用)
// 說明:
// 策略1: 均線糾結突破 (短中長均線原本黏在一起，今日突然向上發散)
// 策略2: 箱型整理突破 (股價在一個區間震盪很久，今日放量突破箱頂)

// ======== 參數設定 ========
input: MaTanglePercent(2, "均線糾結容許誤差(%)");
input: BoxLength(20, "箱型觀察天數");
input: VolRatio(1.5, "爆量倍數(今日量/5日均量)");

// ======== 計算指標 ========

// 1. 均線計算 (5, 20, 60MA)
value1 = Average(Close, 5);  
value2 = Average(Close, 20);
value3 = Average(Close, 60);

// 計算均線最大值與最小值，判斷是否糾結
value4 = MaxList(value1, value2, value3);
value5 = MinList(value1, value2, value3);

// 2. 箱型高點計算
value6 = Highest(High[1], BoxLength); // 過去N天(不含今日)的最高價

// ======== 策略邏輯 ========

// 【策略A：均線糾結】
// 條件A1: 三條均線之間的差距很小 ( < 2% )
Condition1 = (value4 - value5) / value5 * 100 < MaTanglePercent;

// 條件A2: 今日股價一舉突破所有均線
Condition2 = Close > value4;

// 條件A3: 均線開始向上 (至少5日線是向上的)
Condition3 = value1 > value1[1];


// 【策略B：箱型突破】
// 條件B1: 今日收盤價突破過去20天的高點
Condition4 = Close > value6;

// 條件B2: 成交量放大 (確認突破有效)
Condition5 = Volume > Average(Volume, 5) * VolRatio;


// ======== 綜合判斷 ========
// 滿足 (均線糾結突破) OR (箱型突破) 且 (成交量放大)
if ( (Condition1 and Condition2 and Condition3) or Condition4 ) and Condition5 then ret = 1;

// ======== 輸出顯示 ========
outputfield(1, Close, 2, "收盤價");
outputfield(2, (value4-value5)/value5*100, 2, "均線糾結度(%)");
outputfield(3, (Close-value6)/value6*100, 2, "突破幅度(%)");
outputfield(4, Volume, 0, "成交量");
