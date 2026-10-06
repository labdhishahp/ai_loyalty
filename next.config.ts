import type { NextConfig } from "next";

/**
 * ONE Vercel project serves both halves from this directory: Next.js builds
 * from app/, lib/ and components/, and vercel.json turns api/index.py into a
 * Python function with /api/* and /mcp/* rewritten to it. One production URL,
 * one set of environment variables.
 *
 * The browser never calls the API directly, in development or production. It
 * calls the route handler at /bff/*, which holds the shared secret and
 * forwards server-side. That is why there is no CORS configuration here and
 * no NEXT_PUBLIC_ variable pointing at the API.
 *
 * The proxy lives at /bff rather than under app/api/ because the Python
 * function already owns /api/* in this project; two owners for one prefix is
 * a routing question nobody should have to answer at deploy time.
 */

// NEXT_PUBLIC_* are compiled into the bundle at BUILD time, not read at
// runtime. Absent during the build, the app ships with sign-in permanently
// disabled and setting them afterwards changes nothing until the next build --
// a failure that looks like a bug in the auth code rather than a missing
// variable. Refusing the build is the only moment this can still be cheap to
// fix, so a production build without them stops here.
const required = ["NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_ANON_KEY"];
if (process.env.VERCEL_ENV === "production") {
  const missing = required.filter((name) => !process.env[name]);
  if (missing.length > 0) {
    throw new Error(
      `Missing at build time: ${missing.join(", ")}. These are compiled into ` +
        `the browser bundle, so setting them after the build has no effect. ` +
        `Add them in the frontend project's Environment Variables and rebuild.`,
    );
  }
}

const config: NextConfig = {
  reactStrictMode: true,

  // /mcp and /mcp/* are rewritten to the Python function, and Starlette's
  // app.mount("/mcp", ...) redirects /mcp to /mcp/. Next.js normalises the
  // other way by default, 308-ing /mcp/ back to /mcp -- so the two bounced the
  // request between them until the client gave up (50 redirects, observed).
  // Neither side is wrong on its own; they just disagree, and only one of them
  // can own the answer for a path the other is serving.
  //
  // This hands trailing-slash normalisation to whatever serves the route. Next
  // pages are unaffected: they are reached without trailing slashes anyway.
  skipTrailingSlashRedirect: true,
};

export default config;
