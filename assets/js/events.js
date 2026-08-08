/*
 * Upcoming concerts, fetched from /api/events.
 *
 * The listing used to be built into the page by Hugo. It now comes from D1 at
 * runtime, so the site stays static while the catalogue keeps growing. The
 * markup below mirrors _partials/events/list-entry.html — if one changes, the
 * other has to follow.
 *
 * The container is rendered hidden and stays hidden unless there is at least
 * one concert: a page with nothing coming up shows nothing, as before.
 */
(() => {
    const DATE = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long", year: "numeric" });
    const TIME = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });

    /* Titles come from our own database, but an artist called "AC/DC & <Co>"
       would still break the markup. */
    const escape = (value) => String(value ?? "").replace(
        /[&<>"']/g,
        (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]),
    );

    const link = (href, label) => `<a href="${escape(href)}">${escape(label)}</a>`;

    /* "Les Docks, Lausanne" — the venue then its city, as venues/link.html does.
       The city path is the first two segments of the venue path. */
    function place(event) {
        if (!event.venue_path) {
            return escape(event.venue ?? "");
        }
        const parts = [link(`/venues/${event.venue_path}/`, event.venue)];
        if (event.city) {
            parts.push(link(`/venues/${event.venue_path.split("/").slice(0, 2).join("/")}/`, event.city));
        }
        return parts.join(", ");
    }

    function bill(event) {
        const names = (event.lineup ?? []).map((a) => link(`/artists/${a.slug}/`, a.title)).join(", ");
        return event.festival_slug
            ? `${link(`/festivals/${event.festival_slug}/`, event.festival)} : ${names}`
            : names;
    }

    function status(event) {
        if (event.full) return "text-warning";
        if (event.cancelled) return "text-danger text-decoration-line-through";
        return "text-primary";
    }

    function row(event, index) {
        const padding = index === 0 ? "pb-2" : "py-2";
        const date = new Date(event.starts_at);
        const tickets = event.ticket_url
            ? `<a target="_blank" rel="noopener norefferer" href="${escape(event.ticket_url)}">
                   <i class="fa fa-cart-shopping me-1"></i>Billets</a>`
            : "";

        return `
<tr class="${index === 0 ? "" : "border-top"} border-primary" style="--bs-border-opacity: 0.25;">
    <td class="${padding} text-nowrap align-middle">
        <p class="d-flex mb-0 pt-1 align-items-center">
            <i class="fa fa-2xs text-muted fa-clock ms-1"></i>
            <small class="text-white ms-2">
                <time datetime="${escape(event.starts_at.slice(0, 10))}">${escape(DATE.format(date))}</time>
                <br>
                <small><small class="text-white-50">${escape(TIME.format(date))}</small></small>
            </small>
        </p>
    </td>

    <td class="${padding} text-white-50 align-middle">
        <p class="mb-0 d-flex">
            <i class="fa fa-xs text-muted fa-music ms-1 mt-2"></i>
            <span class="ms-2 fw-lighter">${bill(event)}</span>
        </p>
        <p class="mb-0 d-flex align-items-center">
            <i class="fa fa-xs text-muted fa-map-marker-alt ms-1"></i>
            <small class="ms-2 opacity-50"><small>${place(event)}</small></small>
        </p>
    </td>

    <td class="${padding} text-nowrap text-center align-middle ${status(event)}">${tickets}</td>
</tr>`;
    }

    async function load(container, query) {
        const response = await fetch(`/api/events?${query}`);
        if (!response.ok) {
            /* Nothing to show and nothing to explain to a reader: leave the
               block hidden, and leave a trace for us. */
            console.error(`events: /api/events?${query} -> ${response.status}`);
            return;
        }

        const { events } = await response.json();
        if (events?.length) {
            return;
        }

        container.querySelector("tbody").innerHTML = events.map(row).join("");
        container.hidden = false;
    }

    function start() {
        const container = document.getElementById("events");
        const query = container?.dataset.events;
        if (!query) {
            return;
        }
        load(container, query).catch((error) => console.error("events:", error));
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
})();
