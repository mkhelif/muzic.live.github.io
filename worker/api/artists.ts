/**
 * API to fetch artists (bands and people) information.
 */

import { badRequest, isUuid, json, notFound } from "../http";
import type { Context } from "../types";

export interface Artist {
    id: string;
    slug: string;
    title: string;
    type: string | null;
    musicbrainz: string | null;
}

interface Social {
    provider: string;
    value: string;
}

export interface Membership {
    id: string;
    slug: string;
    title: string;
    roles: string | null;
    start: number | null;
    end: number | null;
}

/**
 * GET /api/artists/:id
 */
export async function get({ env, params }: Context): Promise<Response> {
    const id = params.id ?? "";
    if (!isUuid(id)) return badRequest("id must be a UUID");

    const artist = await env.DB.prepare(`
            SELECT id, slug, title, type, musicbrainz
            FROM artist
            WHERE id = ?1
        `).bind(id).first<Artist>();
    if (!artist) return notFound("artist");

    // Three small indexed queries rather than one join: a band with 40 members
    // would otherwise repeat the artist row 40 times, and D1 bills rows read.
    const [socials, members, bands] = await Promise.all([
        env.DB.prepare(`
            SELECT provider, value
            FROM artist_social
            WHERE artist_id = ?1
        `).bind(id).all<Social>(),
        env.DB.prepare(`
            SELECT a.id, a.slug, a.title, m.roles, m.start, m.end
            FROM membership m JOIN artist a ON a.id = m.person_id
            WHERE m.band_id = ?1 ORDER BY a.title
        `).bind(id).all<Membership>(),
        env.DB.prepare(`
            SELECT a.id, a.slug, a.title, m.roles, m.start, m.end
            FROM membership m JOIN artist a ON a.id = m.band_id
            WHERE m.person_id = ?1 ORDER BY a.title
        `).bind(id).all<Membership>(),
    ]);

    return json({
        ...artist,
        socials: Object.fromEntries(socials.results.map((s) => [s.provider, s.value])),
        members: members.results, // for band
        member_of: bands.results, // for person
    });
}
