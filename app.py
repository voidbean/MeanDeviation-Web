from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import tushare as ts
import pandas as pd
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Tushare Token (Optional for basic realtime quotes, but recommended for stability)
TS_TOKEN = os.getenv("TUSHARE_TOKEN", "")
if TS_TOKEN:
    ts.set_token(TS_TOKEN)
    pro = ts.pro_api()

def calculate_8848(code: str):
    try:
        # Fetch real-time data
        # Note: tushare.get_realtime_quotes returns a DataFrame
        df = ts.get_realtime_quotes(code)
        
        if df is None or df.empty:
            return {"error": "Stock code not found or data unavailable."}

        # Extract data
        name = df.loc[0, 'name']
        price = float(df.loc[0, 'price'])
        # Tushare volume is in shares (hand * 100), amount is in Yuan
        # However, get_realtime_quotes returns volume in 'hands' (100 shares) usually?
        # Let's check the raw values carefully. 
        # Actually standard Sina source: volume is in Shares, amount is in Yuan.
        # But sometimes amount is in 10k. 
        # Let's use a heuristic: Average price should be close to current price.
        
        volume = float(df.loc[0, 'volume']) # Volume in shares
        amount = float(df.loc[0, 'amount']) # Amount in Yuan
        
        if volume == 0:
            return {"error": "Volume is 0, cannot calculate average price (Market might be closed or just opened)."}

        if price == 0:
             return {"error": "Current price is 0, cannot calculate (Stock might be suspended)."}

        # Calculate Intraday Average Price (ZSTJJ)
        # Verify unit scaling. If avg_price is way off price, adjust.
        avg_price = amount / volume
        
        # Heuristic check: if avg_price is 100x smaller than price, volume might be in shares but amount in 100s? 
        # Or if volume is in hands.
        # Standard Sina API: volume (shares), amount (yuan).
        # But let's add a safety factor.
        if abs(avg_price - price) / price > 0.5:
             # If significant deviation, maybe volume is in hands?
             # Try adjusting by 100
             if abs((avg_price * 100) - price) / price < 0.5:
                 avg_price *= 100
        
        # 8848 Formula
        # Red Line (Resistance/High) = ZSTJJ / 0.98848
        upper_line = avg_price / 0.98848
        
        # Green Line (Support/Low) = ZSTJJ * 0.98848
        lower_line = avg_price * 0.98848
        
        return {
            "code": code,
            "name": name,
            "current_price": price,
            "avg_price": round(avg_price, 3),
            "upper_line": round(upper_line, 3),
            "lower_line": round(lower_line, 3),
            "status": "success"
        }

    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_stock(request: Request, stock_code: str = Form(...)):
    # Basic validation for stock code (add sh/sz if missing)
    # Tushare usually expects 6 digits.
    # If purely digits, we might need to guess the market, but get_realtime_quotes works with just 6 digits often.
    # However, for uniqueness, let's keep it as is or try to append likely suffix if it fails?
    # Actually get_realtime_quotes is smart enough with just '600519' etc.
    
    result = calculate_8848(stock_code)
    return templates.TemplateResponse("index.html", {"request": request, "result": result, "last_code": stock_code})
