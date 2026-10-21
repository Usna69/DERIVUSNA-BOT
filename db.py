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
create table if not exists lab_runs (
  id bigserial primary key,
  ts timestamptz not null default now(),
  symbol text not null, tf_seconds int not null, strategy text not null,
  filters text not null default 'none', stop_fill text not null default 'level',
  spread_pct numeric, candles int, days numeric,
  trades int not null, win_rate numeric, mean_r numeric, ci_lo numeric, ci_hi numeric,
  first_r numeric, second_r numeric, verdict text, random_better numeric
);
create unique index if not exists lab_runs_key on lab_runs (symbol, tf_seconds, strategy, filters, stop_fill);
create table if not exists positions (
  mode text not null, ticket bigint not null, symbol text not null, direction text not null,
  volume numeric, price_open numeric, sl numeric, tp numeric, profit numeric,
  opened_at timestamptz, updated_at timestamptz not null default now(),
  primary key (mode, ticket)
);
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


def positions_sync(mode, rows):
    """Replace this bot's open-position rows with `rows` (list of dicts). Best effort, never raises."""
    if not DB_URL:
        return
    try:
        import psycopg
        with psycopg.connect(DB_URL, connect_timeout=10, autocommit=True) as conn:
            _ensure_schema(conn)
            with conn.transaction():
                conn.execute("delete from positions where mode=%s", (mode,))
                for r in rows:
                    conn.execute("insert into positions (mode, ticket, symbol, direction, volume, price_open, sl, tp, profit, opened_at)"
                                 " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                 (mode, r["ticket"], r["symbol"], r["direction"], r["volume"], r["price_open"], r["sl"], r["tp"],
                                  r["profit"], r.get("opened_at")))
    except Exception as e:                           # noqa: BLE001 - must never raise into the bot
        _warn(e)


LAB_COLS = ("symbol", "tf_seconds", "strategy", "filters", "stop_fill", "spread_pct", "candles", "days", "trades", "win_rate",
            "mean_r", "ci_lo", "ci_hi", "first_r", "second_r", "verdict", "random_better")


def lab_publish(runs):
    """Upsert lab results (one row per symbol/timeframe/strategy/filters/stop-fill; newest wins). Raises on failure."""
    import psycopg
    if not DB_URL:
        raise SystemExit("DATABASE_URL is not set in this window.")
    cols = ", ".join(LAB_COLS)
    marks = ", ".join(["%s"] * len(LAB_COLS))
    update = ", ".join(f"{c}=excluded.{c}" for c in LAB_COLS[5:]) + ", ts=now()"
    sql = (f"insert into lab_runs ({cols}) values ({marks}) "
           f"on conflict (symbol, tf_seconds, strategy, filters, stop_fill) do update set {update}")
    with psycopg.connect(DB_URL, connect_timeout=15, autocommit=True) as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.executemany(sql, [tuple(r[c] for c in LAB_COLS) for r in runs])
    return len(runs)


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
