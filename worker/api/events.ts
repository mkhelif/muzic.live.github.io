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
    venue: string | null;
    venue_path: string | null;
    city: string | null;
}

type Query = (env: Env, id: string, limit: number) => Promise<D1Result<Event>>;

const PERIOD:string = "'-3 days'";

/** Concerts an artist plays. Index: event_artist(artist_id). */
const byArtist: Query = async (env, artistId, limit) =>
    env.DB.prepare(`
        SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, v.title AS venue, v.path AS venue_path, v.city
        FROM event_artist ea
        JOIN event e ON e.id = ea.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE ea.artist_id = ?1
        AND e.starts_at >= datetime('now', ${PERIOD})
        ORDER BY e.starts_at ASC
        LIMIT ?2
    `).bind(artistId, limit).all<Event>();

/** Concerts held at a venue. Index: event(venue_id, starts_at). */
const byVenue: Query = async (env, venueId, limit) =>
    env.DB.prepare(
        `SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, v.title AS venue, v.path AS venue_path, v.city
           FROM event e
      LEFT JOIN venue v ON v.id = e.venue_id
          WHERE e.venue_id = ?1
            AND e.starts_at >= datetime('now', ${PERIOD})
       ORDER BY e.starts_at ASC
          LIMIT ?2`,
    ).bind(venueId, limit).all<Event>();

/** Concerts belonging to a festival. Index: event(festival_id, starts_at). */
const byFestival: Query = async (env, festivalId, limit) =>
    env.DB.prepare(
        `SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, v.title AS venue, v.path AS venue_path, v.city
           FROM event e
      LEFT JOIN venue v ON v.id = e.venue_id
          WHERE e.festival_id = ?1
            AND e.starts_at >= datetime('now', ${PERIOD})
       ORDER BY e.starts_at ASC
          LIMIT ?2`,
    ).bind(festivalId, limit).all<Event>();

/**
 * Concerts anywhere in a city. Index: venue(city_id).
 *
 * The join is inner, not left: a concert with no venue cannot be in a city.
 */
const byCity: Query = async (env, cityId, limit) =>
    env.DB.prepare(
        `SELECT e.id, e.path, e.starts_at, e.ticket_url, e.cancelled, v.title AS venue, v.path AS venue_path, v.city
           FROM event e
           JOIN venue v ON v.id = e.venue_id
          WHERE v.city_id = ?1
            AND e.starts_at >= datetime('now', ${PERIOD})
       ORDER BY e.starts_at ASC
          LIMIT ?2`,
    ).bind(cityId, limit).all<Event>();

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

    const { results } = await QUERIES[name](env, id, limitOf(url));
    return json({ [name]: id, events: results });
}
