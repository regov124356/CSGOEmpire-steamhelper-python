# SteamTrading

Automated CS2 skin trading on **CSGOEmpire**, priced off **CSFloat** resale value.
Three cooperating subsystems buy auctions below their resale value, accept the
resulting Steam trades, and keep a live price reference in the database.

## How it makes money

1. **PriceService** computes, for each tracked item, our max Empire price
   (`price_empire`) from CSFloat's market (lowest listing + top buy order, minus
   CSFloat's 2% fee, converted to Empire coins via `divider`). Stored in the DB.
2. **BiddingBot** listens to Empire's auction websocket and bids on items up to
   `price_empire`. Winning below our valuation = margin.
3. **TradeBot** accepts the incoming Steam trade offers for those wins, confirms
   receipt on Empire, disputes trades about to expire, and records purchases.

## Processes

Run as **two separate processes** (same machine, same Empire key):

```bash
pip install -r requirements.txt
# one-time DB migration:
#   run migrations/001_add_price_updated_at.sql against trading_steam

python main.py            # TradeBot (accept/dispute) + PriceService (CSFloat prices)
python main.py --bidder   # the above + BiddingBot (auction websocket bidder)
```

`--bidder` runs everything in one process so TradeBot and BiddingBot share a
single Empire client and therefore one rate-limit window. Ctrl+C with `--bidder`
active stops the bidder and keeps TradeBot running for 33 min (to receive a late
trade offer — sellers have up to 30 min to send); a second Ctrl+C quits at once.

Prompts to select a user from `config.json`.

## Modules

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point: wires DB/Telegram/clients, runs TradeBot + PriceService (+ BiddingBot with `--bidder`); handles graceful bidder shutdown |
| `config_loader.py` | Shared config load + user selection |
| `steam_client.py` | `SteamClient` — Steam only (login, trade offers); wraps steampy |
| `csgoempire_client.py` | `CSGOEmpireClient` — Empire REST API + token-bucket rate limit + retry |
| `csfloatclient.py` | `CSFloatClient` — CSFloat REST API + adaptive (header-driven) rate limit + retry |
| `trade_bot.py` | `TradeBot` — buying logic: match/accept Steam offers, mark received, dispute, record |
| `price_service.py` | `PriceService` — CSFloat price math + continuous DB refresh loop |
| `bidding_bot.py` | `BiddingBot` — Empire auction websocket + bid strategy |
| `db.py` | `DB` — async (off-loop) SQL Server data layer |
| `telegram.py` | `Telegram` — notifications |
| `logger.py` | Shared logger (file + console) |
| `migrations/` | SQL schema migrations |

Layering: thin API clients (no business logic) ← logic/orchestration layers ←
entry points. Pricing logic lives in PriceService, not the client; bidding logic
in BiddingBot, not the client; etc.

## External APIs

| API | Host | Auth | Rate limit |
|-----|------|------|------------|
| Empire REST | `csgoempire.io/api/v2` | `Authorization: Bearer <key>` | global 120 req / 60s per key (docs conflict: README says /10s — we use safer /60s) + per-endpoint caps (bid 20/10s, trades 3/10s, …); any 429 = 60s block |
| Empire WS | `wss://trade.csgoempire.com` (namespace `/trade`, path `/s/`) | `identify` with `socket_token`/`socket_signature` from `/metadata/socket` | — |
| CSFloat REST | `csfloat.com/api/v1` | `Authorization: <key>` | per-endpoint, via `x-ratelimit-*` headers: `/listings` 200/h, `/listings/{id}/buy-orders` 20/60s |

Notes:
- Empire REST domain is `.io` (chosen); the websocket host is `trade.csgoempire.com`.
- Empire does **not** send `X-RateLimit-*` headers — the token bucket is the only
  proactive guard there.
- CSFloat client paces requests as `(reset - now) / remaining` from headers, so it
  self-tunes and never 429s. With ~45 items each refreshes roughly every ~14 min
  (bound by `/listings` 200/h).
- `place_bid(fail_fast_429=True)` makes bids raise immediately on 429 instead of
  blocking ~60s (auctions are time-sensitive).

## Database (`trading_steam`, SQL Server)

- `items` (id, market_hash_name)
- `item_prices` (item_id, price_empire, price_float, **price_updated_at**) — written
  by PriceService; consumed by BiddingBot. `price_updated_at` is UTC; treat NULL or
  stale rows as not-fresh.
- `sellers`, `purchased_skins` — written by TradeBot on each accepted buy.
- Stored procs: `AddSeller`, `AddPurchasedSkin`.

## Pricing math

```
price_float  = floor( buy_order            if listing < 100
                      else (listing + buy_order) / 2 )      # USD cents
price_empire = floor( floor(price_float * 0.98) / divider ) # Empire coincents
# inverse, used when recording a buy:
float_value  = round( (price_empire * divider) / 0.98, 2 )
```

`divider` (config) is the Empire-coin → USD rate; `0.98` is CSFloat's 2% fee
(`CSFLOAT_FEE`).

## Config

`config.json` (git-ignored — contains live secrets):
```json
{
  "users": [{ "username", "steam": {...}, "empire": {"api_key"}, "float": {"api_key"} }],
  "db": { "user", "password", "host", "database" },
  "telegram": { "token", "chat_id" },
  "divider": 0.123
}
```

## Known design notes

- TradeBot's dispute / mark-received calls pass `item_id` on the path (the docs
  label it `tradeoffer_id`) — matches the working setup.
- TradeBot and BiddingBot run in one process (`main.py --bidder`) and share a
  single `CSGOEmpireClient`, so Empire's limits are enforced across both. The
  client has a global token bucket (120/60s — docs conflict 60s vs 10s, we pick
  the safer 60s) plus per-endpoint buckets
  (`ENDPOINT_LIMITS`: bid 18/10s, trades 2/10s —
  just under Empire's documented caps). A request takes its endpoint slot then a
  global slot. All buckets are priority-aware (3 tiers): TradeBot money actions
  (`mark_as_received`/`dispute_trade`/`create_withdrawal`) > bids > polling.
- A TEMP `[ratelimit-debug]` log fires on every 429 (in `csgoempire_client.py`,
  marked `TEMP(429-bug)`) to confirm the burst source — remove once verified.
