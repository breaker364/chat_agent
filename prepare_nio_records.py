import json
import datetime

# 读取原始数据
with open('nio_stock_data.json', 'r', encoding='utf-8') as f:
    stock_data = json.load(f)

# 转换为飞书多维表格记录格式（直接包含字段值）
records = []
for item in stock_data:
    # 解析日期字符串为datetime对象
    date_str = item['date']
    date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    
    # 转换为毫秒时间戳（UTC时间，飞书多维表格日期字段使用时间戳）
    timestamp_ms = int(date_obj.timestamp() * 1000)
    
    record = {
        "日期": timestamp_ms,
        "开盘价": item['open'],
        "最高价": item['high'],
        "最低价": item['low'],
        "收盘价": item['close'],
        "成交量": item['volume']
    }
    records.append(record)

# 保存到临时文件
with open('tmp/nio_records.json', 'w', encoding='utf-8') as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

print(f"已转换 {len(records)} 条记录")
print(f"示例记录：{json.dumps(records[0], ensure_ascii=False, indent=2)}")