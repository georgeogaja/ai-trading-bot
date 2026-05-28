# 🤖 George's Autonomous Trading Bot
## Complete Setup Guide

---

## What This Bot Does

This is a fully autonomous AI trading agent that:

1. **Builds its own watchlist** by scanning financial news daily and identifying trending sectors
2. **Scans for A+ setups** using George's exact ThinkorSwim strategy (RSI, ADX, EMA, patterns)
3. **Places real options trades** via Alpaca API (paper or live)
4. **Monitors stop losses** every 30 minutes during market hours
5. **Learns from mistakes** — tracks every error and self-adjusts thresholds
6. **Generates weekly reports** every Saturday with full P/L, win rate, and lessons

---

## Quick Start (10 Minutes)

### Step 1: Install Python
Download Python 3.10+ from python.org

### Step 2: Create project folder and install dependencies
```bash
cd george_trading_bot
pip install -r requirements.txt
```

### Step 3: Set up API keys
```bash
copy .env.example .env
# Edit .env with your actual API keys
```

**Get your keys:**
- Alpaca (paper): https://app.alpaca.markets/paper-trading → API Keys
- Claude API: https://console.anthropic.com/ → API Keys
- Discord webhook: https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks (optional)

### Step 4: Run in paper mode (ALWAYS START HERE)
```bash
python orchestrator.py
```

### Step 5: Run a single scan to test
```bash
python orchestrator.py --scan
```

### Step 6: Generate a test report
```bash
python orchestrator.py --report
```

---

## Project Structure

```
george_trading_bot/
│
├── orchestrator.py          ← MAIN CONTROLLER (run this)
├── config.py                ← ALL strategy settings
├── requirements.txt         ← Python dependencies
├── .env.example             ← Environment variable template (do not commit)
├── alpaca_broker.py         ← Alpaca paper broker abstraction
├── market_intelligence.py    ← News scanning + watchlist building
├── strategy_engine.py        ← George's ThinkorSwim signal logic
├── trade_executor.py         ← Alpaca order placement
└── learning_engine.py        ← Mistake tracking + weekly reports
│
├── database/
│   ├── db.py                ← SQLite database functions
│   └── trading_bot.db       ← Auto-created on first run
│
├── reports/
│   └── weekly_report_*.txt  ← Auto-generated weekly reports
│
└── logs/
    └── trading_bot_*.log    ← Daily log files
```

---

## Daily Schedule (All times CT)

| Time | Task |
|------|------|
| 8:00 AM | Macro briefing + news scan + watchlist update |
| 10:00 AM | Full watchlist A+ signal scan + trade placement |
| 10:30 AM – 3:30 PM | Position monitor every 30 minutes |
| 4:30 PM | After-hours news + earnings scan |
| Saturday 9:00 AM | Weekly performance report |
| Sunday 8:00 PM | Watchlist rebuild for next week |

---

## Account Rules (Hardcoded — Cannot Be Changed By Bot)

| Rule | Value |
|------|-------|
| Total capital | $10,000 |
| Max per trade | 20% = $2,000 |
| Cash reserve (always kept) | 30% = $3,000 |
| Max open positions | 5 |
| Max contracts per position | 1 |
| Max OTM strike | 15% |
| RSI hard rejection | >= 70 |
| No-trade window | First 30 min after open |

---

## The Three Phases

### Phase 1: PAPER TRADING (First 30+ days)
```
PAPER_TRADING=true in .env
ALPACA_BASE_URL=https://paper-api.alpaca.markets
Bot trades with fake money only
Review performance weekly
DO NOT go live until win rate > 55%
```

### Phase 2: LIVE — SMALL SIZE (Days 31-60)
```
PAPER_TRADING=false
Reduce max_per_trade_pct to 0.10 (10% = $1,000)
Monitor every single trade manually
Full size only after 60 days proven performance
```

### Phase 3: FULL AUTOMATION
```
Standard 20% position sizing
Weekly report replaces daily manual review
Trust the system — it knows George's rules
```

---

## Weekly Report Format

Every Saturday, the bot generates a report including:

- **Executive Summary** — P/L, win rate, key lesson
- **Trade Performance Table** — every trade with entry/exit/P/L
- **What Worked** — patterns and sectors that produced wins
- **What Failed** — honest breakdown of losses
- **Mistake Analysis** — top errors and corrections made
- **Open Positions Review** — thesis check for each open trade
- **Watchlist Changes** — why stocks were added/removed
- **Strategy Adjustments** — what the bot changed based on mistakes
- **Next Week Focus** — top 3 setups to watch
- **Full Account Metrics** — value, return, win rate, profit factor

---

## George's Strategy (Built Into The Bot)

### A+ Setup — All 6 Required
1. Pattern confirmed (falling wedge, cup/handle, RSI divergence, bull flag)
2. RSI between 40–65 (REJECTED if RSI >= 70)
3. Price above key support
4. Catalyst within 30 days
5. Macro GREEN or YELLOW
6. ADX above 20

### Strike Selection
- ATM to max 10% OTM
- Target delta: 0.45–0.60
- Expiry: 75–90 days
- Order: Limit at bid/ask midpoint

### Exit Rules
- Stop: Close below defined stock price level
- Target 1: 50% option gain
- Target 2: 100% option gain
- Never sell options in after-hours

---

## Alpaca Options Setup

Before the bot can trade options, you need Level 2 options approval on Alpaca:
1. Log into alpaca.markets
2. Go to Account → Trading → Options
3. Apply for Level 2 (covers buying calls and puts)
4. Approval usually takes 1–2 business days

## Alpaca Paper-Only Architecture

This bot is designed to use Alpaca paper trading by default. The following variables are required in `.env`:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_BASE_URL=https://paper-api.alpaca.markets`
- `PAPER_TRADING=true`

If you manually set `PAPER_TRADING=false`, the bot will allow live execution, but live mode is not recommended until you have proven performance in paper trading.

The bot now includes:
- account balance checks
- buying power validation
- option order placement via Alpaca paper API
- open position tracking
- stop loss and take profit auto exits in paper trading
- Discord notifications for trade entries and exits
- a daily kill switch for max loss and max trades

---

## Support

The bot logs everything to `logs/trading_bot_YYYY-MM-DD.log`
Check this file if anything seems wrong.

For code issues, use Claude Code:
```bash
claude  # In your project directory
```
Then describe what is not working.
