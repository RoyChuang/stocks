// 腳本名稱: 強勢爆量突破_尋找明日續漲股
// 適用頻率: 日 (建議在選股中心使用)
// 說明: 尋找今日強勢上漲、爆量且突破區間的股票，這類股票慣性強，明日續漲或開高機率較大。

// 1. 定義參數
input: Length(20, "突破天數(設20為月高, 60為季高)");
input: VolRatio(1.5, "爆量倍數(今日量/5日均量)");
input: MinRise(3, "今日最小漲幅(%)");

// 2. 計算指標
value1 = Average(Volume, 5);       // 5日均量
value2 = Highest(High[1], Length); // 過去N天(不含今日)的最高價

// 3. 設定篩選條件
// 條件A: 今日漲幅夠強 (大於設定%)
condition1 = Close > Close[1] * (1 + MinRise/100);

// 條件B: 成交量放大 (大於5日均量的 N 倍)
condition2 = Volume > value1 * VolRatio;

// 條件C: 收盤價創近期新高 (突破壓力區)
condition3 = Close > value2;

// 條件D: 股價在季線之上 (確認是多頭趨勢，非空頭反彈)
condition4 = Close > Average(Close, 60);

// 條件E: 收盤價接近最高價 (表示收盤買盤強勢，留上影線不長)
// (High - Close) < (Close - Open) * 0.5  -> 上影線長度小於實體的一半
condition5 = (High - Close) < (Close - Open) * 0.5;

// 4. 執行篩選
if condition1 and condition2 and condition3 and condition4 and condition5 then ret=1;

// 5. 輸出欄位 (選股中心顯示用資料)
outputfield(1, Close, 2, "收盤價");
outputfield(2, Volume, 0, "成交量");
outputfield(3, (Close-Close[1])/Close[1]*100, 2, "漲跌幅(%)");
outputfield(4, value1, 0, "5日均量");
