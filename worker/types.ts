/**
 * Shared types: the bindings, and what a handler receives.
 */

// Bindings declared in "wrangler.toml"
export interface Env {
    DB: D1Database;
    RATE_LIMIT: RateLimit;
}

// Everything a handler gets; `params` holds the `:name` captures
export interface Context {
    env: Env;
    url: URL;
    params: Record<string, string>;
}

export type Handler = (context: Context) => Promise<Response>;
