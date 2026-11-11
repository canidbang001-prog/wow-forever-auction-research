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
