// 腳本名稱: 海龜交易法_突破進場系統
// 適用頻率: 日
// 說明: 著名的海龜交易法則兩個系統的進場邏輯
// 系統1 (短線): 突破過去 20 天最高價 (適合短線爆發)
// 系統2 (長線): 突破過去 55 天最高價 (適合長線趨勢)

// 1. 定義參數
input: SystemType(1, "系統設定: 1=20日突破, 2=55日突破");
input: VolFilter(1000, "成交量濾網(至少大於N張)");

// 2. 決定突破天數 (系統1用20天, 系統2用55天)
var: Period(20);
if SystemType = 2 then Period = 55 else Period = 20;

// 3. 計算過去 N 天的最高價 (不含今日)
// 使用 High[1] 代表從昨天開始往前算 Period 天
value1 = Highest(High[1], Period);

// 4. 設定進場條件
// 條件A: 今日收盤價 突破 過去 N 天的最高價
condition1 = Close > value1;

// 條件B: 成交量濾網 (避免太冷門的股票)
condition2 = Average(Volume, 5) > VolFilter;

// 5. 過濾重複訊號 (選用)
// 真正的海龜法則在「上一次訊號獲利」時會略過這次訊號
// 但在選股腳本中，我們先單純找出符合突破的股票即可

// 6. 執行篩選
if condition1 and condition2 then ret=1;

// 7. 輸出欄位
outputfield(1, Close, 2, "收盤價");
outputfield(2, value1, 2, "突破前高價");
outputfield(3, Period, 0, "系統天數");
outputfield(4, (Close-Close[1])/Close[1]*100, 2, "漲跌幅(%)");
