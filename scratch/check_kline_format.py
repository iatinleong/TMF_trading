import json
from pathlib import Path
import pandas as pd

p = Path("data/kline_cache/TM2609.json")
data = json.loads(p.read_text(encoding="utf-8"))

print(f"Data type: {type(data)}")
if isinstance(data, dict):
    print("Keys count:", len(data))
    for k, v in list(data.items())[-20:]:
        ts = int(k)
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Taipei")
        print(f"Key={k} ({dt}) -> val={v}")
elif isinstance(data, list):
    print("List items count:", len(data))
    for item in data[-20:]:
        print(item)
