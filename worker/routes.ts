import * as events from "./api/events";
import * as artists from "./api/artists";
import * as venues from "./api/venues";
import type { Handler } from "./types";

export const ROUTES: Record<string, Handler> = {
    "/api/events": events.list,
    "/api/artists/:id": artists.get,
    "/api/venues/:id": venues.get,
};
