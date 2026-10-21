import { NextRequest, NextResponse } from "next/server";

// HTTP Basic auth gate (any username, password = DASHBOARD_PASSWORD). Fails closed if the password is unset,
// because this page shows trading results.
export function middleware(req: NextRequest) {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) return new NextResponse("Set DASHBOARD_PASSWORD to enable the dashboard.", { status: 503 });

  const header = req.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    try {
      const given = atob(header.slice(6)).split(":").slice(1).join(":");
      if (given.length === password.length && [...given].reduce((d, c, i) => d | (c.charCodeAt(0) ^ password.charCodeAt(i)), 0) === 0) {
        return NextResponse.next();
      }
    } catch {
      /* malformed header: fall through to the challenge */
    }
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="DERIVUSNA dashboard"' },
  });
}

export const config = { matcher: "/((?!_next/static|_next/image|favicon.ico).*)" };
