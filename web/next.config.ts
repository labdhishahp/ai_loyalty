import type { NextConfig } from "next";

/**
 * The frontend deploys as its OWN Vercel project, with `web/` as the root
 * directory. The Python API is a separate project — see docs/deployment.md.
 * Earlier comments here and in .env.example claimed both shipped from one
 * project, which contradicted the runbook and would have sent BACKEND_URL to
 * the wrong origin.
 *
 * The browser never calls the API directly, in development or production. It
 * calls the route handler at /api/proxy/*, which holds the shared secret and
 * forwards server-side. That is why there is no CORS configuration here and
 * no NEXT_PUBLIC_ variable pointing at the API.
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
};

export default config;
