import type { NextConfig } from "next";

const config: NextConfig = {
  // The API runs as Python serverless functions under /api/*, deployed from the
  // same Vercel project. In development it runs separately on :8000, and the
  // proxy route handler bridges the two -- see app/api/proxy/[...path]/route.ts.
  reactStrictMode: true,
};

export default config;
