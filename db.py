"""
Optional Postgres sink for the dashboard. Set DATABASE_URL to enable; unset = bot works exactly as before.
Every function swallows errors (and prints at most one warning per 5 min): a database outage must never
stop the bot or lose a trade, because trades.csv stays the primary log.
"""
import os, threading, time

DB_URL = os.getenv("DATABASE_URL", "")

SCHEMA = """
create table if not exists trades (
  id bigserial primary key,
  ts timestamptz not null default now(),
  mode text not null, symbol text not null, direction text not null,
  stake numeric not null, profit numeric not null, day_pnl numeric not null,
  ask_price numeric, payout_ratio numeric, contract_id bigint
);
create index if not exists trades_ts_idx on trades (ts desc);
create table if not exists events (
  id bigserial primary key,
  ts timestamptz not null default now(),
  mode text not null, level text not null, text text not null
);
create index if not exists events_ts_idx on events (ts desc);
create table if not exists bot_status (
  mode text primary key, symbol text, day_pnl numeric, trades_today int,
  status text, message text, last_seen timestamptz not null default now()
);
"""

_ready = False
_last_warn = 0.0
_schema_lock = threading.Lock()                      # bot threads start together; only one may create the tables


def enabled():
    return bool(DB_URL)


def _warn(e):
    global _last_warn
    if time.time() - _last_warn > 300:
        _last_warn = time.time()
        first = (str(e).strip().splitlines() or [""])[0][:160]
        print(f"database unavailable ({e.__class__.__name__}: {first}); continuing without it")


def _ensure_schema(conn):
    """Create tables once per process. The advisory lock also serialises separate processes (bot + dbinit)."""
    global _ready
    if _ready:
        return
    with _schema_lock:
        if _ready:
            return
        with conn.transaction():
            conn.execute("select pg_advisory_xact_lock(727272)")
            conn.execute(SCHEMA)
        _ready = True


def _exec(sql, params=()):
    if not DB_URL:
        return
    try:
        import psycopg
        with psycopg.connect(DB_URL, connect_timeout=10, autocommit=True) as conn:
            _ensure_schema(conn)
            conn.execute(sql, params)
    except Exception as e:                           # noqa: BLE001 - must never raise into the bot
        _warn(e)


def insert_trade(mode, symbol, direction, stake, profit, day_pnl, ask_price=None, payout_ratio=None, contract_id=None):
    _exec("insert into trades (mode, symbol, direction, stake, profit, day_pnl, ask_price, payout_ratio, contract_id)"
          " values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
          (mode, symbol, direction, stake, profit, day_pnl, ask_price or None, payout_ratio, contract_id))


def event(mode, level, text):
    _exec("insert into events (mode, level, text) values (%s,%s,%s)", (mode, level, text[:500]))


def heartbeat(mode, symbol, day_pnl, trades_today, status, message=""):
    _exec("insert into bot_status (mode, symbol, day_pnl, trades_today, status, message, last_seen)"
          " values (%s,%s,%s,%s,%s,%s, now())"
          " on conflict (mode) do update set symbol=excluded.symbol, day_pnl=excluded.day_pnl,"
          " trades_today=excluded.trades_today, status=excluded.status, message=excluded.message, last_seen=now()",
          (mode, symbol, day_pnl, trades_today, status, message[:200]))


def init():
    """Create the tables now (raises on failure, unlike the bot-side helpers)."""
    import psycopg
    if not DB_URL:
        raise SystemExit("DATABASE_URL is not set in this window.")
    with psycopg.connect(DB_URL, connect_timeout=15, autocommit=True) as conn:
        _ensure_schema(conn)
        n = conn.execute("select count(*) from information_schema.tables where table_name in ('trades','events','bot_status')").fetchone()[0]
    print(f"database connected; {n}/3 tables ready (trades, events, bot_status)")


if __name__ == "__main__":
    init()
