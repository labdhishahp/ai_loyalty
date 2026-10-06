"use client";

/**
 * Who is signed in, and what they may do.
 *
 * Two separate facts, deliberately: Supabase says who you ARE, and the API says
 * what you MAY DO. A signed-in account with no role in ops.user_roles gets an
 * identity and no permissions, which is the safe default when signup is open.
 *
 * Every `can()` here is a rendering decision only. The same scope is checked
 * again server-side on every request, so hiding a button is a courtesy, not a
 * control.
 */

import {
  createContext, useCallback, useContext, useEffect, useState,
} from "react";
import { api, Me } from "@/lib/api";
import { authConfigured, supabase } from "@/lib/supabase";

type SessionState = {
  me: Me | null;
  loading: boolean;
  error: string | null;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  can: (scope: string) => boolean;
};

const Ctx = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me());
      setError(null);
    } catch (e) {
      setMe(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    if (!supabase) return;
    const { data } = supabase.auth.onAuthStateChange(() => refresh());
    return () => data.subscription.unsubscribe();
  }, [refresh]);

  const signIn = async (email: string, password: string) => {
    if (!supabase) throw new Error("Authentication is not configured.");
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw new Error(error.message);
    await refresh();
  };

  const signOut = async () => {
    await supabase?.auth.signOut();
    await refresh();
  };

  return (
    <Ctx.Provider
      value={{
        me, loading, error, signIn, signOut,
        can: (scope) => Boolean(me?.scopes.includes(scope)),
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useSession(): SessionState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSession must be used inside SessionProvider");
  return ctx;
}

export { authConfigured };
