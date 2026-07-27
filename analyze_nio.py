import json
import datetime

# 读取原始数据
with open('nio_stock_data.json', 'r', encoding='utf-8') as f:
    stock_data = json.load(f)

# 按日期排序（从旧到新）
stock_data.sort(key=lambda x: x['date'])

# 计算基本统计指标
close_prices = [item['close'] for item in stock_data]
high_prices = [item['high'] for item in stock_data]
low_prices = [item['low'] for item in stock_data]
volumes = [item['volume'] for item in stock_data]

# 价格统计
first_close = close_prices[0]
last_close = close_prices[-1]
price_change = last_close - first_close
price_change_pct = (price_change / first_close) * 100
highest_price = max(high_prices)
lowest_price = min(low_prices)
avg_close = sum(close_prices) / len(close_prices)

# 波动性分析（标准差）
import statistics
close_std = statistics.stdev(close_prices)

# 支撑阻力位（简单方法：近期高低点）
recent_high = max(high_prices[-10:])  # 最近10天最高点
recent_low = min(low_prices[-10:])   # 最近10天最低点

# 成交量分析
avg_volume = sum(volumes) / len(volumes)
max_volume = max(volumes)
min_volume = min(volumes)

# 趋势分析（简单移动平均）
def simple_moving_average(data, window):
    if len(data) < window:
        return None
    return sum(data[-window:]) / window

sma_5 = simple_moving_average(close_prices, 5)
sma_10 = simple_moving_average(close_prices, 10)
sma_20 = simple_moving_average(close_prices, 20)

# 生成报告
report = f"""# 蔚来(NIO)股价分析报告

## 概述
本报告基于蔚来(NIO)最近30个交易日（从{stock_data[0]['date']}到{stock_data[-1]['date']}）的股价数据进行分析。

## 价格表现
- **起始价格**: {first_close:.2f} 美元
- **最新价格**: {last_close:.2f} 美元
- **价格变化**: {price_change:.2f} 美元 ({price_change_pct:.2f}%)
- **期间最高价**: {highest_price:.2f} 美元
- **期间最低价**: {lowest_price:.2f} 美元
- **平均收盘价**: {avg_close:.2f} 美元
- **价格波动标准差**: {close_std:.2f} 美元

## 技术分析
### 支撑阻力位
- **近期阻力位**: {recent_high:.2f} 美元（最近10天最高点）
- **近期支撑位**: {recent_low:.2f} 美元（最近10天最低点）
- **关键阻力位**: {max(high_prices):.2f} 美元（30天最高点）
- **关键支撑位**: {min(low_prices):.2f} 美元（30天最低点）

### 移动平均线
- **5日移动平均**: {sma_5:.2f} 美元
- **10日移动平均**: {sma_10:.2f} 美元
- **20日移动平均**: {sma_20:.2f} 美元
- **当前价格 vs 5日均线**: {"高于" if last_close > sma_5 else "低于"} ({((last_close - sma_5) / sma_5 * 100):.2f}%)
- **当前价格 vs 20日均线**: {"高于" if last_close > sma_20 else "低于"} ({((last_close - sma_20) / sma_20 * 100):.2f}%)

## 成交量分析
- **平均成交量**: {avg_volume:,.0f} 股
- **最高成交量**: {max_volume:,.0f} 股
- **最低成交量**: {min_volume:,.0f} 股
- **成交量趋势**: {"放量" if volumes[-1] > avg_volume else "缩量"} (最新成交量 {volumes[-1]:,.0f} vs 平均 {avg_volume:,.0f})

## 风险指标
- **波动率**: {close_std/avg_close*100:.2f}% (收盘价标准差/平均收盘价)
- **价格区间**: {highest_price - lowest_price:.2f} 美元 ({(highest_price - lowest_price)/lowest_price*100:.2f}%)
- **平均日波动**: {sum([item['high'] - item['low'] for item in stock_data])/len(stock_data):.2f} 美元

## 市场情绪
- **趋势判断**: {"上升" if price_change > 0 else "下降"}趋势
- **动量分析**: {"正向动量" if last_close > sma_5 and sma_5 > sma_10 else "负向动量或整理"}
- **成交量确认**: {"成交量支持价格走势" if (price_change > 0 and volumes[-1] > avg_volume) or (price_change < 0 and volumes[-1] > avg_volume) else "成交量与价格走势背离"}

## 投资建议（仅供参考）
1. **短期关注**: {recent_high:.2f} 美元阻力位和 {recent_low:.2f} 美元支撑位
2. **中期趋势**: {"看涨" if last_close > sma_20 else "看跌"}，关注20日均线支撑/阻力
3. **风险提示**: 当前波动率为 {close_std/avg_close*100:.2f}%，属于{"高" if close_std/avg_close*100 > 3 else "中等" if close_std/avg_close*100 > 2 else "低"}波动性

## 数据表格
详见飞书多维表格中的完整数据。

---
*报告生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*数据来源: 雅虎财经*
"""

# 保存报告到文件
with open('tmp/nio_analysis_report.md', 'w', encoding='utf-8') as f:
    f.write(report)

print("分析报告已生成：tmp/nio_analysis_report.md")
print(f"报告长度: {len(report)} 字符")