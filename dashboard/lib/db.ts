import postgres from "postgres";

// One small pool reused across requests; sslmode comes from the URL (Neon/Supabase need require).
// prepare:false keeps it working through poolers (Neon "-pooler" hosts, Supabase pgbouncer).
const globalForDb = globalThis as unknown as { sql?: ReturnType<typeof postgres> };

export function getSql() {
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error("DATABASE_URL is not set");
  return (globalForDb.sql ??= postgres(url, { max: 1, idle_timeout: 20, connect_timeout: 10, prepare: false }));
}
