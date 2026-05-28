"""
agents/market_intelligence.py
Scans financial news, identifies trending sectors, and builds dynamic watchlist.
Uses Claude to understand context and relevance.
"""

import os
import json
import requests
import feedparser
from datetime import datetime, timedelta
from anthropic import Anthropic
from loguru import logger
from config import SECTORS, CORE_WATCHLIST, MACRO
from database.db import add_to_watchlist, log_macro

# Top financial RSS feeds (free, no API key needed)
NEWS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://feeds.finance.yahoo.com/rss/2.0/headline",
    "https://seekingalpha.com/feed.xml",
]

claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def fetch_market_news() -> list:
    """Fetch latest financial news from RSS feeds."""
    all_articles = []

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:  # Top 10 from each feed
                all_articles.append({
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", "")[:500],
                    "date":    entry.get("published", ""),
                    "source":  feed.feed.get("title", feed_url),
                })
        except Exception as e:
            logger.warning(f"News feed error {feed_url}: {e}")

    logger.info(f"📰 Fetched {len(all_articles)} news articles")
    return all_articles


def get_macro_data() -> dict:
    """
    Fetch current macro data from Yahoo Finance via yfinance.
    Returns oil, VIX, 10yr yield, S&P 500.
    """
    try:
        import yfinance as yf

        tickers = {
            "oil":   yf.Ticker("CL=F"),     # WTI Crude Futures
            "vix":   yf.Ticker("^VIX"),
            "yield": yf.Ticker("^TNX"),      # 10-year Treasury
            "sp500": yf.Ticker("^GSPC"),
            "dxy":   yf.Ticker("DX-Y.NYB"),  # Dollar Index
        }

        macro = {}
        for name, ticker in tickers.items():
            try:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    macro[name] = round(float(hist['Close'].iloc[-1]), 2)
            except Exception:
                macro[name] = None

        # Determine macro signal
        oil   = macro.get("oil", 0) or 0
        vix   = macro.get("vix", 0) or 0
        yield_ = macro.get("yield", 0) or 0

        if oil > MACRO["oil_danger_threshold"] or vix > MACRO["vix_crisis"]:
            macro["signal"] = "RED"
        elif oil > MACRO["oil_risk_threshold"] or vix > MACRO["vix_elevated"]:
            macro["signal"] = "YELLOW"
        else:
            macro["signal"] = "GREEN"

        logger.info(f"📊 Macro: Oil=${oil} | VIX={vix} | 10yr={yield_}% | Signal={macro['signal']}")
        return macro

    except Exception as e:
        logger.error(f"Macro data error: {e}")
        return {"signal": "YELLOW", "error": str(e)}


def identify_trending_sectors_and_stocks(news_articles: list) -> dict:
    """
    Uses Claude to analyze news and identify:
    1. Which sectors are trending
    2. Which specific stocks to add to watchlist
    3. Why each addition is justified
    """

    news_text = "\n".join([
        f"- {a['title']}: {a['summary'][:200]}"
        for a in news_articles[:30]
    ])

    sector_definitions = json.dumps(
        {k: v["keywords"] for k, v in SECTORS.items()},
        indent=2
    )

    prompt = f"""
You are George's autonomous trading AI. Your job is to analyze today's financial news
and identify stocks to add to the trading watchlist.

GEORGE'S STRATEGY CONTEXT:
- Trades directional long CALLS and PUTS on US-listed stocks
- Looks for quality stocks that are DOWN 20-30%+ from peaks
- Needs a catalyst within 14-30 days (earnings, product launch, macro event)
- Focuses on: AI infrastructure, semiconductor, enterprise software, energy, fintech
- Avoids: deep OTM options, overbought stocks (RSI > 70), broken businesses
- Account: $10,000 swing options account

SECTOR DEFINITIONS:
{sector_definitions}

CURRENT NEWS (last 24 hours):
{news_text}

CORE WATCHLIST (already tracked):
{', '.join(CORE_WATCHLIST)}

TASK: Analyze the news and return a JSON response with:
1. Top 3 trending sectors and why
2. Up to 10 specific stocks to ADD to watchlist (can include stocks already in core list if news is significant)
3. Up to 3 stocks to REMOVE or REDUCE CONVICTION on based on negative news
4. Macro assessment based on news

RESPOND WITH VALID JSON ONLY:
{{
  "trending_sectors": [
    {{"sector": "SECTOR_NAME", "reason": "why trending", "strength": "HIGH/MEDIUM/LOW"}}
  ],
  "add_to_watchlist": [
    {{
      "symbol": "TICKER",
      "sector": "SECTOR_NAME",
      "reason": "specific reason based on news",
      "catalyst": "what the upcoming catalyst is",
      "headline": "the specific news headline that triggered this",
      "urgency": "HIGH/MEDIUM/LOW"
    }}
  ],
  "reduce_conviction": [
    {{
      "symbol": "TICKER",
      "reason": "why reducing conviction"
    }}
  ],
  "macro_note": "one sentence macro assessment from the news"
}}
"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        result = json.loads(response.content[0].text)
        logger.info(f"🤖 Claude identified {len(result.get('add_to_watchlist', []))} new watchlist candidates")
        return result
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude response as JSON")
        return {"add_to_watchlist": [], "trending_sectors": []}


def run_market_intelligence():
    """
    Main function — runs the full market intelligence cycle:
    1. Fetch news
    2. Get macro data
    3. Identify trending sectors and stocks
    4. Update watchlist in database
    """
    logger.info("🔍 Running market intelligence scan...")

    # 1. Get current news
    articles = fetch_market_news()

    # 2. Get macro data
    macro = get_macro_data()

    # 3. Ask Claude to analyze
    analysis = identify_trending_sectors_and_stocks(articles)

    # 4. Update watchlist based on findings
    added = []
    for stock in analysis.get("add_to_watchlist", []):
        symbol = stock.get("symbol", "").upper()
        if symbol and len(symbol) <= 5:
            add_to_watchlist(
                symbol=symbol,
                source="NEWS",
                sector=stock.get("sector", "UNKNOWN"),
                reason=stock.get("reason", ""),
                headline=stock.get("headline", ""),
            )
            added.append(symbol)

    # 5. Log macro data
    try:
        log_macro({
            "date":           datetime.now().isoformat(),
            "oil_price":      macro.get("oil"),
            "vix":            macro.get("vix"),
            "ten_yr_yield":   macro.get("yield"),
            "sp500":          macro.get("sp500"),
            "macro_signal":   macro.get("signal"),
            "summary":        analysis.get("macro_note", ""),
        })
    except Exception as e:
        logger.warning(f"Macro log error: {e}")

    logger.info(f"✅ Intelligence complete | Added: {added} | Macro: {macro.get('signal')}")

    return {
        "macro":            macro,
        "trending_sectors": analysis.get("trending_sectors", []),
        "added_symbols":    added,
        "analysis":         analysis,
    }
