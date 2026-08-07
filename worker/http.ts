/**
 * Response helpers shared by every endpoint.
 */

export function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json; charset=utf-8" },
    });
}

export const notFound = (what: string): Response => json({ error: `${what} not found` }, 404);
export const badRequest = (message: string): Response => json({ error: message }, 400);

export function tooManyRequests(retryAfter = 60): Response {
    const response = json({ error: "rate limit exceeded" }, 429);
    response.headers.set("Retry-After", String(retryAfter));
    return response;
}

// Every entity is keyed by a UUID, so anything else cannot match a row —
// checking the shape turns an enumeration sweep into a free 400.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const isUuid = (value: string): boolean => UUID.test(value);

/** Read a bounded positive integer from the query string. */
export function limitOf(url: URL, fallback = 50, max = 200): number {
    const value = parseInt(url.searchParams.get("limit") || String(fallback), 10);
    return Number.isFinite(value) && value > 0 ? Math.min(value, max) : fallback;
}
