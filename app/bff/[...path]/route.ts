/**
 * Server-side proxy to the Python API.
 *
 * The shared secret lives here and only here. The browser calls /api/proxy/...
 * with no credentials; this handler adds the key and forwards. That is the
 * whole reason it exists -- a key in client code is a public key.
 *
 * BACKEND_API_KEY and BACKEND_URL are deliberately NOT prefixed NEXT_PUBLIC_:
 * that prefix is what compiles a value into the browser bundle.
 *
 * The frontend and the Python API ship as ONE Vercel project, so the API is this
 * same deployment. Being a route handler rather than a rewrite is what keeps the
 * key server-side, and it is also why the browser needs no CORS grant.
 */

import { NextRequest } from "next/server";

// Where to forward. One project means the Python API is THIS deployment, and
// VERCEL_URL is the host Vercel assigns it -- so nothing needs configuring in
// the normal case. BACKEND_URL stays as an override for pointing at an API
// deployed elsewhere, and the localhost default covers `next dev` against a
// separately running uvicorn.
const SELF = process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : "";
const BACKEND = process.env.BACKEND_URL || SELF || "http://127.0.0.1:8000";
const KEY = process.env.BACKEND_API_KEY ?? "";

async function forward(request: NextRequest, path: string[]) {
  if (!BACKEND) {
    return Response.json(
      {
        detail:
          "No API origin resolved: neither VERCEL_URL nor BACKEND_URL is set.",
      },
      { status: 500 },
    );
  }

  const search = request.nextUrl.search;
  const target = `${BACKEND}/api/${path.join("/")}${search}`;

  // A signed-in browser sends its Supabase access token; forward it and the
  // API resolves a named person with a role. Otherwise fall back to the shared
  // service key, which is deliberately weaker -- it may read and draft, but the
  // API refuses it any approval or execution.
  const session = request.headers.get("authorization");

  const init: RequestInit = {
    method: request.method,
    headers: {
      "Content-Type": "application/json",
      ...(session ? { Authorization: session } : KEY ? { "X-API-Key": KEY } : {}),
    },
    // An agent turn can take tens of seconds; no client-side timeout shorter
    // than the server's own limits.
    cache: "no-store",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }

  try {
    const response = await fetch(target, init);
    const body = await response.text();
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    // A backend that is not running is the most common local failure, and
    // "fetch failed" tells nobody anything useful.
    return Response.json(
      {
        detail:
          `Could not reach the API at ${BACKEND}. Is it running? ` +
          `(${error instanceof Error ? error.message : String(error)})`,
      },
      { status: 502 },
    );
  }
}

export async function GET(r: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return forward(r, (await ctx.params).path);
}
export async function POST(r: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return forward(r, (await ctx.params).path);
}
