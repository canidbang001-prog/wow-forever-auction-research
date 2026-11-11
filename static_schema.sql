CREATE TABLE IF NOT EXISTS static_documents (
 id INTEGER PRIMARY KEY, url TEXT NOT NULL, provider TEXT NOT NULL,
 game_version TEXT NOT NULL DEFAULT 'forever', game_build TEXT, fetched_at TEXT NOT NULL,
 sha256 TEXT NOT NULL, cache_path TEXT NOT NULL, http_status INTEGER NOT NULL,
 verification TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(url,sha256)
);
CREATE TABLE IF NOT EXISTS static_queue (
 url TEXT PRIMARY KEY, provider TEXT NOT NULL, kind TEXT NOT NULL,
 entity_id INTEGER, priority INTEGER NOT NULL DEFAULT 50,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 checked_at TEXT, retry_at TEXT, error TEXT, document_id INTEGER REFERENCES static_documents(id)
);
CREATE TABLE IF NOT EXISTS static_coverage (
 url TEXT NOT NULL REFERENCES static_queue(url), relation_kind TEXT NOT NULL,
 status TEXT NOT NULL, expected INTEGER, observed INTEGER,
 note TEXT NOT NULL, checked_at TEXT NOT NULL,
 PRIMARY KEY(url,relation_kind)
);
CREATE TABLE IF NOT EXISTS static_facts (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES static_documents(id),
 kind TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
 object_type TEXT, object_id INTEGER, label TEXT, quantity INTEGER CHECK(quantity>0),
 verification TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
 fact_key TEXT NOT NULL, UNIQUE(document_id,fact_key)
);
CREATE TABLE IF NOT EXISTS static_api_pages (
 url TEXT PRIMARY KEY REFERENCES static_queue(url),
 document_id INTEGER NOT NULL REFERENCES static_documents(id),
 collection TEXT NOT NULL, page_offset INTEGER NOT NULL,
 total INTEGER NOT NULL, observed INTEGER NOT NULL, ids TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS static_fact_object ON static_facts(object_type,object_id,kind);
CREATE TABLE IF NOT EXISTS item_seeds (
 item_id INTEGER NOT NULL REFERENCES items(item_id), reason TEXT NOT NULL,
 source_url TEXT NOT NULL, discovered_at TEXT NOT NULL,
 PRIMARY KEY(item_id,reason,source_url)
);
CREATE TABLE IF NOT EXISTS recipe_mappings (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES static_documents(id),
 recipe_item_id INTEGER REFERENCES items(item_id), craft_spell_id INTEGER NOT NULL,
 output_item_id INTEGER REFERENCES items(item_id), blizzard_recipe_id INTEGER,
 verification TEXT NOT NULL, output_qty INTEGER CHECK(output_qty>0),
 UNIQUE(document_id,recipe_item_id,craft_spell_id,output_item_id)
);
CREATE TABLE IF NOT EXISTS character_offer_checks (
 offer_id INTEGER NOT NULL REFERENCES vendor_items(offer_id), character_key TEXT NOT NULL,
 eligibility TEXT NOT NULL CHECK(eligibility IN ('unknown','eligible','ineligible')),
 checked_at TEXT NOT NULL, note TEXT NOT NULL,
 PRIMARY KEY(offer_id,character_key)
);
CREATE VIEW IF NOT EXISTS current_static_facts AS
 SELECT f.*,d.url AS source_url,d.provider,d.game_version,d.game_build,d.fetched_at
 FROM static_facts f JOIN static_documents d ON d.id=f.document_id WHERE d.active=1;
CREATE VIEW IF NOT EXISTS current_vendor_offers AS
 SELECT v.*,d.game_build,d.fetched_at AS source_fetched_at FROM vendor_items v
 LEFT JOIN static_documents d ON d.id=v.document_id
 WHERE v.active=1 AND (v.document_id IS NULL OR d.active=1);
