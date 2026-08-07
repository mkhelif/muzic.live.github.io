/**
 * API to fetch venues information
 */

import { badRequest, isUuid, json, notFound } from "../http";
import type { Context } from "../types";

export interface Venue {
    id: string;
    slug: string;
    path: string;
    title: string;
    city: string | null;
    country: string | null;
    city_id: string | null;
    country_id: string | null;
    latitude: number | null;
    longitude: number | null;
}

/**
 * GET /api/venues/:id
 */
export async function get({ env, params }: Context): Promise<Response> {
    const id = params.id ?? "";
    if (!isUuid(id)) return badRequest("id must be a UUID");

    const venue = await env.DB.prepare(`
            SELECT id, slug, path, title, city, country, city_id, country_id, latitude, longitude
            FROM venue
            WHERE id = ?1
        `).bind(id).first<Venue>();
    return venue ? json(venue) : notFound("venue");
}
