import json
from pathlib import Path
import pandas as pd

p = Path("data/kline_cache/TM2609.json")
if p.exists():
    data = json.loads(p.read_text(encoding="utf-8"))
    print(f"Total bars: {len(data)}")
    df = pd.DataFrame(data)
    print("Columns:", df.columns.tolist())
    print("First 3 rows:", df.head(3).to_dict(orient="records"))
    print("Last 3 rows:", df.tail(3).to_dict(orient="records"))
    
    # Try datetime / ts / time
    col = "datetime" if "datetime" in df.columns else ("time" if "time" in df.columns else "ts")
    if col in df.columns:
        if isinstance(df[col].iloc[0], (int, float)):
            df["dt"] = pd.to_datetime(df[col], unit="s", utc=True).dt.tz_convert("Asia/Taipei")
        else:
            df["dt"] = pd.to_datetime(df[col])
        
        df["diff_min"] = df["dt"].diff().dt.total_seconds() / 60.0
        
        print("\nLast 30 bars:")
        for _, row in df.tail(30).iterrows():
            print(f"{row['dt']} | diff={row['diff_min']}m | O={row.get('open')} H={row.get('high')} L={row.get('low')} C={row.get('close')} V={row.get('volume')}")
        
        print("\nAll gaps > 65 minutes across whole dataset:")
        gaps = df[df["diff_min"] > 65]
        for _, row in gaps.tail(20).iterrows():
            print(f"Gap at {row['dt']}: diff={row['diff_min']:.1f} minutes ({row['diff_min']/60:.1f} hours)")
else:
    print("File not found")
