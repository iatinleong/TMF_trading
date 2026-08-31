import json
from pathlib import Path
import pandas as pd

p = Path("data/kline_cache/TM2609.json")
data = json.loads(p.read_text(encoding="utf-8"))

print(f"Total bars in cache: {len(data)}")
phantom = []
for k, v in data.items():
    ts = int(k)
    dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Taipei")
    weekday = dt.dayofweek
    hour = dt.hour
    minute = dt.minute
    # Valid close times:
    # Day: Mon-Fri 09:45, 10:45, 11:45, 12:45, 13:45
    # Night: Mon-Fri 16:00..23:00, Tue-Sat 00:00..05:00
    is_valid_day = (0 <= weekday <= 4) and (hour in (9, 10, 11, 12, 13) and minute == 45)
    is_valid_night_pm = (0 <= weekday <= 4) and (16 <= hour <= 23 and minute == 0)
    is_valid_night_am = ((1 <= weekday <= 5) and (0 <= hour <= 5 and minute == 0))
    if not (is_valid_day or is_valid_night_pm or is_valid_night_am):
        phantom.append((k, str(dt), v))

print(f"Found {len(phantom)} phantom bars:")
for k, dt_str, v in phantom:
    print(f"Key={k} ({dt_str}) -> {v}")
