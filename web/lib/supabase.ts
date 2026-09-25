/**
 * The browser's Supabase client, used for one thing only: signing in and
 * holding the session.
 *
 * It never reads L-Mart data directly. Every business request still goes
 * through /api/proxy to the Python API, which verifies the token and decides
 * what the caller may do. Letting the browser query the database would move
 * authorisation into the client, where it is a suggestion rather than a rule.
 */

import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

// Absent locally means "auth not configured yet"; the app still runs using the
// service key, which can investigate but cannot approve anything.
export const authConfigured = Boolean(url && anon);

export const supabase = authConfigured
  ? createClient(url!, anon!, { auth: { persistSession: true } })
  : null;
