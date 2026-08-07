/**
 * API to fetch concerts
 */

import { json, badRequest, isUuid, limitOf } from "../http";
import type { Context, Env } from "../types";

export interface Event {
    id: string;
    path: string;
    starts_at: string;
    ticket_url: string | null;
    cancelled: number;
    full: number;
    venue: string | null;
    venue_path: string | null;
    city: string | null;
    festival: string | null;
    festival_slug: string | null;
}

/** One name on the bill. */
export interface EventArtist {
    id: string;
    slug: string;
    title: string;
}

type Query = (env: Env, id: string, limit: number) => Promise<D1Result<Event>>;

const PERIOD: string = "'-3 days'";

/**
 * Concerts an artist plays, plus those of the bands they belong to.
 */
const byArtist: Query = async (env, artistId, limit) =>
    env.DB.prepare(`
        SELECT DISTINCT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, e."full", v.title AS venue, v.path AS venue_path, v.city, f.title AS festival, f.slug AS festival_slug
        FROM event_artist ea
        JOIN event e ON e.id = ea.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        LEFT JOIN festival f ON f.id = e.festival_id
        WHERE ea.artist_id IN (
            SELECT ?1
            UNION
            SELECT band_id FROM membership WHERE person_id = ?1
        )
        AND e.starts_at >= datetime('now', ${PERIOD})
        ORDER BY e.starts_at ASC
        LIMIT ?2
    `).bind(artistId, limit).all<Event>();

/** Concerts held at a venue. Index: event(venue_id, starts_at). */
const byVenue: Query = async (env, venueId, limit) =>
    env.DB.prepare(`
        SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, e."full", v.title AS venue, v.path AS venue_path, v.city, f.title AS festival, f.slug AS festival_slug
        FROM event e
        LEFT JOIN venue v ON v.id = e.venue_id
        LEFT JOIN festival f ON f.id = e.festival_id
        WHERE e.venue_id = ?1
        AND e.starts_at >= datetime('now', ${PERIOD})
        ORDER BY e.starts_at ASC
        LIMIT ?2
    `).bind(venueId, limit).all<Event>();

/** Concerts belonging to a festival. Index: event(festival_id, starts_at). */
const byFestival: Query = async (env, festivalId, limit) =>
    env.DB.prepare(`
        SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, e."full", v.title AS venue, v.path AS venue_path, v.city, f.title AS festival, f.slug AS festival_slug
        FROM event e
        LEFT JOIN venue v ON v.id = e.venue_id
        LEFT JOIN festival f ON f.id = e.festival_id
        WHERE e.festival_id = ?1
        AND e.starts_at >= datetime('now', ${PERIOD})
        ORDER BY e.starts_at ASC
        LIMIT ?2
    `).bind(festivalId, limit).all<Event>();

/**
 * Concerts anywhere in a city. Index: venue(city_id).
 *
 * The venue join is inner, not left: a concert with no venue cannot be in a city.
 */
const byCity: Query = async (env, cityId, limit) =>
    env.DB.prepare(`
        SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, e."full", v.title AS venue, v.path AS venue_path, v.city, f.title AS festival, f.slug AS festival_slug
        FROM event e
        JOIN venue v ON v.id = e.venue_id
        LEFT JOIN festival f ON f.id = e.festival_id
        WHERE v.city_id = ?1
        AND e.starts_at >= datetime('now', ${PERIOD})
        ORDER BY e.starts_at ASC
        LIMIT ?2
    `).bind(cityId, limit).all<Event>();

/**
 * The bill of each concert, in one round trip. Index: event_artist PK.
 *
 * A separate query rather than a join on the listing: joining would repeat every
 * event row once per performer, and D1 bills rows read.
 */
async function lineups(env: Env, eventIds: string[]): Promise<Map<string, EventArtist[]>> {
    const byEvent = new Map<string, EventArtist[]>();
    if (eventIds.length === 0) {
        return byEvent;
    }

    const placeholders = eventIds.map((_, index) => `?${index + 1}`).join(", ");
    const { results } = await env.DB.prepare(`
        SELECT ea.event_id, a.id, a.slug, a.title
        FROM event_artist ea
        JOIN artist a ON a.id = ea.artist_id
        WHERE ea.event_id IN (${placeholders})
        ORDER BY a.title
    `).bind(...eventIds).all<EventArtist & { event_id: string }>();

    for (const { event_id, ...performer } of results) {
        byEvent.set(event_id, [...(byEvent.get(event_id) ?? []), performer]);
    }
    return byEvent;
}

/** Which query answers which parameter. */
const QUERIES = {
    artist: byArtist,
    venue: byVenue,
    festival: byFestival,
    city: byCity,
};

type Target = keyof typeof QUERIES;

const TARGETS = Object.keys(QUERIES) as Target[];

/**
 * GET /api/events
 *   ?artist=<uuid>
 *   ?venue=<uuid>
 *   ?city=<uuid>
 *   ?festival=<uuid>
 */
export async function list({ env, url }: Context): Promise<Response> {
    const name = TARGETS.find((key) => url.searchParams.get(key));
    if (!name) {
        return badRequest(`one of ${TARGETS.join(", ")} is required`);
    }

    // Reject anything that cannot be a key before touching D1: a sweep over
    // made-up ids then costs a 400, not a billed row read.
    const id = url.searchParams.get(name) ?? "";
    if (!isUuid(id)) {
        return badRequest(`${name} must be a UUID`);
    }

    const withBands = name === "artist" && url.searchParams.get("with_bands") === "1";
    const query = withBands ? byArtistWithBands : QUERIES[name];

    const { results } = await query(env, id, limitOf(url));
    const bills = await lineups(env, results.map((event) => event.id));

    return json({
        [name]: id,
        events: results.map((event) => ({ ...event, lineup: bills.get(event.id) ?? [] })),
    });
}
