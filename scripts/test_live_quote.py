import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Load environment
base_dir = Path(__file__).resolve().parent.parent
load_dotenv(base_dir / ".env")

try:
    from backend.broker.capital_futures import CapitalFuturesBroker, Environment
except ImportError:
    import sys
    sys.path.append(str(base_dir))
    from backend.broker.capital_futures import CapitalFuturesBroker, Environment

def main():
    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    env_str = os.getenv("CAPITAL_ENVIRONMENT", "2")  # Default to TEST (2)
    try:
        environment = int(env_str)
    except ValueError:
        environment = 2

    if not user_id or not password:
        print("【錯誤】請先在 .env 檔案中設定 CAPITAL_USER_ID 與 CAPITAL_PASSWORD")
        return

    print(f"正在以帳號 {user_id} 連線至環境 {environment} (0=正式, 2=測試)...")
    
    try:
        broker = CapitalFuturesBroker(environment=environment)
        broker.login(user_id, password)
        print("【成功】登入完成！")
        
        product = os.getenv("LIVE_PRODUCT_CODE", "TMFR1")
        print(f"正在連線報價主機並訂閱商品 {product}...")
        
        # 訂閱商品（會呼叫 enter_monitor() -> 等待 3003 商品檔下載完成 -> 訂閱 Tick）
        broker.subscribe_quote(product)
        
        print("\n=== 開始接收即時報價，程式將持續運行 30 秒 ===")
        print("請觀察是否有包含 [OnConnection] kind=3003(商品檔下載完成) 的日誌與 Tick 輸出：\n")
        
        for _ in range(60):
            broker.pump_events(0.5)
            
    except Exception as exc:
        print(f"【連線失敗/異常】: {exc}")

if __name__ == "__main__":
    main()
