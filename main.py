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
# Thêm "import math" vào đầu file main.py nếu chưa có
import math

@app.get("/api/prices")
async def get_prices(tickers: str = ""):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return {}

    cache_key = "prices:" + ",".join(sorted(ticker_list))
    if _is_fresh(cache_key):
        return _cache[cache_key]["data"]

    prices = {}
    try:
        # Tải dữ liệu 5 ngày gần nhất để đảm bảo có giá trị đóng cửa
        data = yf.download(ticker_list, period="5d", progress=False, threads=True)
        
        if not data.empty:
            for t in ticker_list:
                try:
                    # Lấy toàn bộ cột giá đóng cửa của mã đó, bỏ qua các ô trống (NaN)
                    series = data["Close"][t].dropna()
                    if not series.empty:
                        val = float(series.iloc[-1])
                        # Kiểm tra chắc chắn giá trị là số thực mới lưu vào
                        if math.isfinite(val):
                            prices[t] = round(val, 2)
                except Exception:
                    continue
    except Exception:
        pass

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
            tickers = configs["mag7"]
            for t in tickers:
                try:
                    ticker_obj = yf.Ticker(t)
                    info = ticker_obj.info
                    hist = ticker_obj.history(period=hist_period)
                    current_price = round(float(hist["Close"].iloc[-1]), 2) if not hist.empty else 0
                    if valid_period == "1w" and not hist.empty and len(hist) >= 5:
                        change = round(((hist["Close"].iloc[-1] / hist["Close"].iloc[-5]) - 1) * 100, 2)
                    elif valid_period == "1d" and not hist.empty and len(hist) >= 2:
                        change = round(((hist["Close"].iloc[-1] / hist["Close"].iloc[-2]) - 1) * 100, 2)
                    else:
                        change = round(info.get("regularMarketChangePercent", 0), 2)
                    results.append({
                        "name": t,
                        "size": round(info.get("marketCap", 1e9) / 1e9),
                        "change": change,
                        "price": current_price,
                    })
                except Exception:
                    results.append({"name": t, "size": 100, "change": 0, "price": 0})
        elif category == "sectors":
            for ticker, name in configs["sectors"].items():
                try:
                    hist = yf.Ticker(ticker).history(period=hist_period)
                    if not hist.empty and len(hist) >= 2:
                        if valid_period == "1w" and len(hist) >= 5:
                            change = round(((hist["Close"].iloc[-1] / hist["Close"].iloc[-5]) - 1) * 100, 2)
                        else:
                            change = round(((hist["Close"].iloc[-1] / hist["Close"].iloc[-2]) - 1) * 100, 2)
                        results.append({"name": name, "size": 1000, "change": change})
                except Exception:
                    pass
        else:
            return []
    except Exception:
        pass

    if results:
        _cache[cache_key] = {"data": results, "_ts": dt.datetime.now().timestamp()}
    return results


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
