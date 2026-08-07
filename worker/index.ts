/**
 * Muzic.Live — JSON API over D1.
 */

import { ROUTES } from "./routes";
import { tooManyRequests } from "./http";
import type { Env, Handler } from "./types";

// How long the edge may serve a cached response
const CACHE_SECONDS: Record<number, number> = {
    200: 86400,
    404: 3600,
};

export default {
    async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
        const url = new URL(request.url);

        // Routes matching
        const route = resolve(url.pathname);
        if (!route) {
            return fetch(request);
        }

        // Cache lookup
        const cached = await caches.default.match(request);
        if (cached) {
            return cached;
        }

        // Rate limit
        const client = request.headers.get("CF-Connecting-IP") ?? "unknown";
        const { success } = await env.RATE_LIMIT.limit({ key: client });
        if (!success) {
            return tooManyRequests();
        }

        // Process request
        let response: Response;
        try {
            response = await route.handler({ env, url, params: route.params });
        } catch (error) {
            console.error(error);
            return fetch(request);
        }

        // Cache response
        const ttl = CACHE_SECONDS[response.status];
        if (ttl) {
            response.headers.set("Cache-Control", `public, max-age=${ttl}`);
            ctx.waitUntil(caches.default.put(request, response.clone()));
        }
        return response;
    },
} satisfies ExportedHandler<Env>;

interface Match {
    handler: Handler;
    params: Record<string, string>;
}

/**
 * Find the route matching a pathname.
 *
 * Patterns use `:name` for a segment capture, e.g. "/api/artists/:id".
 * Returns `{ handler, params }` or null.
 */
function resolve(pathname: string): Match | null {
    const segments = pathname.replace(/\/+$/, "").split("/").filter(Boolean);

    for (const [pattern, handler] of Object.entries(ROUTES)) {
        const expected = pattern.split("/").filter(Boolean);
        if (expected.length !== segments.length) {
            continue;
        }

        const params: Record<string, string> = {};
        const ok = expected.every((part, index) => {
            const segment = segments[index];
            if (segment === undefined) {
                return false;
            }
            if (part.startsWith(":")) {
                params[part.slice(1)] = decodeURIComponent(segment);
                return true;
            }
            return part === segment;
        });
        if (ok) {
            return { handler, params };
        }
    }
    return null;
}
