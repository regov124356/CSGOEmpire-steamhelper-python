-- Adds a freshness timestamp to item_prices.
-- update_items_prices() now sets price_updated_at = SYSUTCDATETIME() (UTC) on
-- every successful refresh. The bidding/consumer project should treat prices
-- whose price_updated_at is NULL or older than its freshness threshold as stale.
--
-- Run once against the trading_steam database.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.item_prices') AND name = 'price_updated_at'
)
BEGIN
    ALTER TABLE dbo.item_prices ADD price_updated_at DATETIME2 NULL;
END;
