PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 ok INTEGER CHECK(ok IN (0,1)), note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS realms (realm_id INTEGER PRIMARY KEY, names TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 item_id INTEGER PRIMARY KEY, name TEXT,
 purchase_price INTEGER CHECK(purchase_price >= 0), sell_price INTEGER CHECK(sell_price >= 0),
 class_id INTEGER, class_name TEXT, subclass_id INTEGER, subclass_name TEXT,
 binding TEXT, updated_at TEXT, cache_error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('api','auctionator')),
 realm_id INTEGER NOT NULL REFERENCES realms(realm_id), fetched_at TEXT NOT NULL,
 last_modified TEXT NOT NULL, n_auctions INTEGER CHECK(n_auctions >= 0),
 UNIQUE(source, realm_id, last_modified)
);
CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(source,realm_id,fetched_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS prices (
 snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(item_id),
 min_price INTEGER NOT NULL CHECK(min_price > 0),
 median_price INTEGER CHECK(median_price > 0),
 quantity INTEGER CHECK(quantity >= 0), n_auctions INTEGER CHECK(n_auctions >= 0),
 observed_at TEXT, PRIMARY KEY(snapshot_id,item_id)
);
CREATE INDEX IF NOT EXISTS price_item ON prices(item_id,snapshot_id);
CREATE TABLE IF NOT EXISTS auctionator_imports (
 id INTEGER PRIMARY KEY, realm_id INTEGER NOT NULL REFERENCES realms(realm_id),
 snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), fetched_at TEXT NOT NULL,
 file_mtime_ns TEXT NOT NULL, content_sha256 TEXT NOT NULL, parser_version INTEGER NOT NULL,
 latest_count INTEGER NOT NULL, daily_count INTEGER NOT NULL, skipped_count INTEGER NOT NULL,
 UNIQUE(realm_id,content_sha256,parser_version)
);
CREATE TABLE IF NOT EXISTS auctionator_daily (
 import_id INTEGER NOT NULL REFERENCES auctionator_imports(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(item_id), scan_day INTEGER NOT NULL,
 observed_at TEXT NOT NULL, low_price INTEGER CHECK(low_price > 0),
 high_price INTEGER CHECK(high_price > 0), max_quantity INTEGER CHECK(max_quantity >= 0),
 PRIMARY KEY(import_id,item_id,scan_day),
 CHECK(low_price IS NULL OR high_price IS NULL OR low_price <= high_price)
);
CREATE INDEX IF NOT EXISTS auctionator_daily_item ON auctionator_daily(item_id,scan_day);
CREATE TABLE IF NOT EXISTS recipes (
 recipe_id INTEGER PRIMARY KEY, name TEXT NOT NULL, profession TEXT NOT NULL,
 skill_tier TEXT NOT NULL, crafted_item_id INTEGER REFERENCES items(item_id),
 crafted_qty INTEGER CHECK(crafted_qty > 0), note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reagents (
 recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(item_id), qty INTEGER NOT NULL CHECK(qty > 0),
 PRIMARY KEY(recipe_id,item_id)
);
CREATE TABLE IF NOT EXISTS vendor_items (
 item_id INTEGER PRIMARY KEY REFERENCES items(item_id),
 vendor_price INTEGER NOT NULL CHECK(vendor_price >= 0), source TEXT NOT NULL, note TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS latest_prices AS
WITH ranked AS (
 SELECT s.*, ROW_NUMBER() OVER (
   PARTITION BY source,realm_id ORDER BY fetched_at DESC,id DESC) AS rank
 FROM snapshots s
)
SELECT s.id AS snapshot_id,s.source,s.realm_id,s.fetched_at,s.last_modified,
 p.item_id,COALESCE(i.name,'item#'||p.item_id) AS name,
 p.min_price,p.median_price,p.quantity,p.n_auctions,p.observed_at
FROM ranked s JOIN prices p ON p.snapshot_id=s.id
JOIN items i ON i.item_id=p.item_id WHERE s.rank=1;

-- Blizzard WoW Forever auction houses. Retail snapshots.source stays api/auctionator.
CREATE TABLE IF NOT EXISTS blizzard_houses (
 house_id INTEGER NOT NULL, ruleset INTEGER NOT NULL, name TEXT,
 side TEXT NOT NULL CHECK(side IN ('horde','alliance','blackwater','unknown')),
 updated_at TEXT NOT NULL, PRIMARY KEY(house_id, ruleset)
);
CREATE TABLE IF NOT EXISTS blizzard_snapshots (
 id INTEGER PRIMARY KEY, house_id INTEGER NOT NULL, ruleset INTEGER NOT NULL,
 fetched_at TEXT NOT NULL, fetched_epoch INTEGER NOT NULL CHECK(fetched_epoch >= 0),
 auctions_last_modified TEXT, commodities_last_modified TEXT,
 n_auctions INTEGER NOT NULL CHECK(n_auctions >= 0),
 n_commodities INTEGER NOT NULL CHECK(n_commodities >= 0),
 n_priced INTEGER NOT NULL CHECK(n_priced >= 0),
 FOREIGN KEY(house_id, ruleset) REFERENCES blizzard_houses(house_id, ruleset)
);
CREATE INDEX IF NOT EXISTS blizzard_snapshot_house
 ON blizzard_snapshots(ruleset, house_id, fetched_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS blizzard_snapshot_modified
 ON blizzard_snapshots(house_id, ruleset, auctions_last_modified, commodities_last_modified)
 WHERE auctions_last_modified IS NOT NULL AND commodities_last_modified IS NOT NULL;
CREATE TABLE IF NOT EXISTS blizzard_checks (
 id INTEGER PRIMARY KEY, house_id INTEGER NOT NULL, ruleset INTEGER NOT NULL,
 checked_at TEXT NOT NULL, auctions_last_modified TEXT, commodities_last_modified TEXT,
 stored INTEGER NOT NULL CHECK(stored IN (0,1)),
 snapshot_id INTEGER REFERENCES blizzard_snapshots(id), note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS blizzard_check_house ON blizzard_checks(ruleset, house_id, checked_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS blizzard_item_prices (
 snapshot_id INTEGER NOT NULL REFERENCES blizzard_snapshots(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(item_id),
 min_unit_price INTEGER NOT NULL CHECK(min_unit_price > 0),
 median_unit_price INTEGER NOT NULL CHECK(median_unit_price > 0),
 p25_unit_price INTEGER NOT NULL CHECK(p25_unit_price > 0),
 quantity INTEGER NOT NULL CHECK(quantity >= 0),
 listing_count INTEGER NOT NULL CHECK(listing_count >= 0),
 PRIMARY KEY(snapshot_id, item_id)
);
CREATE INDEX IF NOT EXISTS blizzard_price_item ON blizzard_item_prices(item_id, snapshot_id);
CREATE TABLE IF NOT EXISTS blizzard_listings (
 snapshot_id INTEGER NOT NULL REFERENCES blizzard_snapshots(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('auction','commodity')),
 listing_id INTEGER NOT NULL, item_id INTEGER NOT NULL REFERENCES items(item_id),
 unit_price INTEGER NOT NULL CHECK(unit_price > 0),
 quantity INTEGER NOT NULL CHECK(quantity > 0), time_left TEXT NOT NULL,
 PRIMARY KEY(snapshot_id, kind, listing_id)
);
