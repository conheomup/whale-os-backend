"""
main.py — The Whale OS v2.0 Backend API
Run: uvicorn main:app --reload --port 8000
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from fredapi import Fred
import yfinance as yf
import datetime as dt
import math
import asyncio
from concurrent.futures import ThreadPoolExecutor

# ── Hàm bảo vệ chống lỗi NaN từ yfinance ──
def safe_float(val, fallback=0.0):
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return fallback
        return f
    except Exception:
        return fallback

load_dotenv()

app = FastAPI(title="The Whale OS API", version="2.0")

# Cho phép Frontend (Vite) truy cập vào Backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Khởi tạo FRED
api_key = os.getenv("FRED_API_KEY")
fred = Fred(api_key=api_key) if api_key else None

# ── Simple in-memory cache ─────────────────────────────────────
_cache = {}
CACHE_TTL = 900  # 15 phút


def _is_fresh(key: str) -> bool:
    if key not in _cache:
        return False
    ts = _cache[key].get("_ts", 0)
    return (dt.datetime.now().timestamp() - ts) < CACHE_TTL


# ── Health check ───────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": dt.datetime.now().isoformat()}


# ── VIX (full period data) ─────────────────────────────────────
@app.get("/api/vix")
async def get_vix():
    if _is_fresh("vix"):
        return _cache["vix"]["data"]
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1mo")
        if hist.empty:
            return {"error": "VIX data unavailable"}
        result = {
            "current": round(float(hist["Close"].iloc[-1]), 2),
            "day": round(float(hist["Close"].iloc[-2]), 2) if len(hist) >= 2 else None,
            "week": round(float(hist["Close"].iloc[-5]), 2) if len(hist) >= 5 else None,
            "month": round(float(hist["Close"].iloc[0]), 2),
            "midTerm": round(float(hist["Close"].iloc[-5:].mean()), 2) if len(hist) >= 5 else round(float(hist["Close"].iloc[-1]), 2),
        }
        _cache["vix"] = {"data": result, "_ts": dt.datetime.now().timestamp()}
        return result
    except Exception as e:
        return {"error": str(e)}


# ── Fed Funds Rate ─────────────────────────────────────────────
@app.get("/api/fed-rate")
async def get_fed_rate():
    if _is_fresh("fed"):
        return _cache["fed"]["data"]
    try:
        if fred:
            data = fred.get_series("FEDFUNDS")
            current = round(float(data.iloc[-1]), 2)
            prev = round(float(data.iloc[-2]), 2) if len(data) >= 2 else current
        else:
            current = 4.50
            prev = 4.75
        result = {"current": current, "prev": prev, "source": "FRED" if fred else "fallback"}
        _cache["fed"] = {"data": result, "_ts": dt.datetime.now().timestamp()}
        return result
    except Exception as e:
        return {"current": 4.50, "prev": 4.75, "source": "fallback", "error": str(e)}


# ── Live prices (batch) ───────────────────────────────────────

# --- Cấu hình bộ xử lý song song ---
executor = ThreadPoolExecutor(max_workers=10)

def fetch_single_price(ticker):
    """Hàm phụ tải giá 1 mã (chạy trên luồng riêng)"""
    try:
        t_obj = yf.Ticker(ticker)
        hist = t_obj.history(period="2d")
        if not hist.empty:
            price = safe_float(hist["Close"].iloc[-1], None)
            if price is not None:
                return ticker, round(price, 2)
    except Exception:
        pass
    return ticker, None

@app.get("/api/prices")
async def get_prices(tickers: str = ""):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list: return {}

    cache_key = f"prices:{','.join(sorted(ticker_list))}"
    if _is_fresh(cache_key):
        return _cache[cache_key]["data"]

    # Chạy song song tất cả các mã cùng lúc
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(executor, fetch_single_price, t) for t in ticker_list]
    results = await asyncio.gather(*tasks)
    
    # Gom kết quả
    prices = {ticker: price for ticker, price in results if price is not None}
    if prices:
        _cache[cache_key] = {"data": prices, "_ts": dt.datetime.now().timestamp()}
    return prices

# ── Heatmap data ───────────────────────────────────────────────
@app.get("/api/heatmap/{category}")
async def get_heatmap(category: str, period: str = "1d"):
    """Market heatmap. category = mag7 | sectors, period = 1d | 1w"""
    valid_period = period if period in ("1d", "1w") else "1d"
    hist_period = "5d" if valid_period == "1d" else "1mo"
    cache_key = f"heatmap:{category}:{valid_period}"
    if _is_fresh(cache_key):
        return _cache[cache_key]["data"]

    configs = {
        "mag7": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "TSLA"],
        "sectors": {
            "XLK": "Tech", "XLV": "Health", "XLF": "Finance", "XLE": "Energy",
            "XLY": "Consumer", "XLI": "Industrial", "XLU": "Utilities", "XLRE": "Real Estate",
        },
    }

    results = []
    try:
        if category == "mag7":
            for t in configs["mag7"]:
                try:
                    hist = yf.Ticker(t).history(period=hist_period)
                    # Kiểm tra và dùng dropna() để vứt bỏ các ngày bị lỗi NaN
                    if not hist.empty and "Close" in hist:
                        closes = hist["Close"].dropna() 
                        if len(closes) >= 2:
                            p1 = float(closes.iloc[-1])
                            p_prev = float(closes.iloc[-5]) if valid_period == "1w" and len(closes) >= 5 else float(closes.iloc[-2])
                            change = round(((p1 / p_prev) - 1) * 100, 2) if p_prev > 0 else 0
                            
                            results.append({
                                "name": t, "size": 1000, 
                                "change": change, "price": round(p1, 2)
                            })
                            continue
                except Exception:
                    pass
                # Nếu API lỗi hoàn toàn, trả về 0 để không bị sập
                results.append({"name": t, "size": 100, "change": 0, "price": 0})
                    
        elif category == "sectors":
            for ticker, name in configs["sectors"].items():
                try:
                    hist = yf.Ticker(ticker).history(period=hist_period)
                    if not hist.empty and "Close" in hist:
                        closes = hist["Close"].dropna() 
                        if len(closes) >= 2:
                            p1 = float(closes.iloc[-1])
                            p_prev = float(closes.iloc[-5]) if valid_period == "1w" and len(closes) >= 5 else float(closes.iloc[-2])
                            change = round(((p1 / p_prev) - 1) * 100, 2) if p_prev > 0 else 0
                            results.append({"name": name, "size": 1000, "change": change})
                except Exception:
                    pass
    except Exception:
        pass

    if results:
        _cache[cache_key] = {"data": results, "_ts": dt.datetime.now().timestamp()}
    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
