const loading = import("/pagefind/pagefind.js");

const OPTIONS = {
    excerptLength: 30
};

// Categories in display order, with their label and thumbnail shape.
const CATEGORIES = {
    'artists': 'Artistes',
    'albums': 'Albums',
    'festivals': 'Festivals',
    'reports': 'Chroniques',
    'news': 'Actualités',
    'venues': 'Salles',
};

const MAX_PER_GROUP = 6;

// Lowercase + strip diacritics so "josman" matches "Josman" and "medine" matches "Médine".
function normalizeText(value) {
    return (value || '')
        .normalize('NFD')
        .replace(/[̀-ͯ]/g, '')
        .toLowerCase()
        .trim();
}

window.addEventListener('DOMContentLoaded', () => loading.then(pagefind => {
    const modal = document.getElementById('search-blackbox');
    const trigger = document.getElementById('search-open');
    const input = document.getElementById('search-input');
    const results = document.getElementById('search-results');

    let activeIndex = -1;   // index of the highlighted result in `rows`
    let rows = [];          // flat list of rendered result <a> elements

    /**
     * Open / close
     */
    function openModal() {
        modal.classList.remove('d-none');
        modal.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        input.focus();
        input.select();
    }

    function closeModal() {
        modal.classList.add('d-none');
        modal.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
    }

    function isOpen() {
        return !modal.classList.contains('d-none');
    }

    if (trigger) {
        trigger.addEventListener('click', openModal);
    }

    modal.querySelectorAll('[data-search-close]').forEach(el => {
        el.addEventListener('click', closeModal);
    });

    /**
     * Keystrokes and events
     */
    document.addEventListener('keydown', e => {
        const typing = /^(input|textarea|select)$/i.test((e.target.tagName || '')) || e.target.isContentEditable;
        if (!isOpen()) {
            if ((e.key === '/' || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k')) && !typing) {
                e.preventDefault();
                openModal();
            }
            return;
        }

        if (e.key === 'Escape') {
            e.preventDefault();
            closeModal();
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            move(1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            move(-1);
        } else if (e.key === 'Enter') {
            const target = rows[activeIndex] || rows[0];
            if (target) {
                e.preventDefault();
                window.location.href = target.href;
            }
        }
    });

    /**
     * Search
     */
    input.addEventListener('input', () => {
        const term = (input.value || '').trim();
        if (term === '') {
            renderEmpty();
        } else {
            pagefind.debouncedSearch(term, OPTIONS)
                .then(showResults)
                .catch((e) => {
                    console.log(e);
                    renderMessage('Une erreur est survenue.');
                });
        }
    });

    function showResults(search) {
        const term = normalizeText(input.value || '');

        Promise.all(search.results.map((result, index) => result.data().then(data => ({ data, index }))))
            .then(items => {
                // Bucket every result by its category filter.
                const buckets = {};
                Object.keys(CATEGORIES).forEach(key => (buckets[key] = []));

                items.forEach(item => {
                    const category = (item.data.filters && item.data.filters.section && item.data.filters.section[0]) || '';
                    if (buckets[category]) {
                        buckets[category].push(item);
                    }
                });

                // Title matches float to the top of each bucket; Pagefind order breaks ties.
                Object.keys(buckets).forEach(key => {
                    buckets[key].sort((a, b) => {
                        const diff = titleScore(b.data.meta.title, term) - titleScore(a.data.meta.title, term);
                        return diff !== 0 ? diff : a.index - b.index;
                    });
                });

                render(buckets);
            })
            .catch(() => renderMessage('Une erreur est survenue.'));
    }

    // Rank a result by how well the query matches its title:
    // 3 = exact title, 2 = title starts with the term, 1 = title contains it, 0 = body only.
    function titleScore(title, term) {
        const normalized = normalizeText(title);
        if (!normalized || !term) return 0;
        if (normalized === term) return 3;
        if (normalized.startsWith(term)) return 2;
        if (normalized.includes(term)) return 1;
        return 0;
    }

    // --- Rendering ----------------------------------------------------------
    function clear() {
        while (results.firstChild) results.removeChild(results.lastChild);
        rows = [];
        activeIndex = -1;
    }

    function renderEmpty() {
        renderMessage('Tapez pour rechercher un artiste, un album, un festival, une salle, une chronique…');
    }

    function renderMessage(message) {
        clear();
        const p = document.createElement('p');
        p.className = 'text-center text-secondary small';
        p.textContent = message;
        results.appendChild(p);
    }

    function render(buckets) {
        clear();

        let total = 0;
        Object.entries(CATEGORIES).forEach(([key, label]) => {
            const items = (buckets[key] || []).slice(0, MAX_PER_GROUP);
            if (items.length === 0) {
                return;
            }

            results.appendChild(createCategoryLabel(label));

            items.forEach(item => {
                const row = createRow(item.data);
                results.appendChild(row);
                rows.push(row);
                total++;
            });
        });

        if (total === 0) {
            renderMessage('Aucun résultat.');
            return;
        }

        setActive(0);
    }

    function createCategoryLabel(category) {
        const label = document.createElement('div');
        label.className = 'small text-primary font-monospace text-uppercase mt-2';
        label.textContent = category;
        return label;
    }

    function createRow(data) {
        const link = document.createElement('a');
        link.className = 'd-flex align-items-center gap-3 p-2 rounded text-decoration-none';
        link.href = data.url;
        link.setAttribute('role', 'option');

        // Thumbnail
        const thumb = document.createElement('div');
        thumb.className = 'overflow-hidden flex-shrink-0 bg-secondary bg-opacity-25 rounded-1';
        thumb.style.width = '42px';
        thumb.style.height = '42px';
        if (data.meta.image) {
            const img = document.createElement('img');
            img.src = data.meta.image;
            img.alt = data.meta.image_alt || data.meta.title || '';
            img.loading = 'lazy';
            img.decoding = 'async';
            img.className = 'w-100 h-100 object-fit-cover';
            thumb.appendChild(img);
        }
        link.appendChild(thumb);

        // Title + meta
        const body = document.createElement('div');
        body.className = 'd-flex flex-column flex-grow-1';
        body.style.minWidth = '0';

        const title = document.createElement('span');
        title.className = 'text-light text-truncate';
        title.textContent = data.meta.title || data.url;
        body.appendChild(title);
        link.appendChild(body);

        link.addEventListener('mousemove', () => setActive(rows.indexOf(link)));
        return link;
    }

    // --- Keyboard highlight -------------------------------------------------
    function setActive(index) {
        if (rows.length === 0) {
            return;
        }
        if (activeIndex >= 0 && rows[activeIndex]) {
            toggleActive(rows[activeIndex], false);
        }
        activeIndex = Math.max(0, Math.min(index, rows.length - 1));
        const row = rows[activeIndex];
        toggleActive(row, true);
        row.scrollIntoView({ block: 'nearest' });
    }

    function toggleActive(row, on) {
        row.classList.toggle('bg-primary', on);
        row.classList.toggle('bg-opacity-10', on);
        const enter = row.querySelector('[data-enter]');
        if (enter) {
            enter.classList.toggle('invisible', !on);
        }
    }

    function move(delta) {
        if (rows.length === 0) {
            return;
        }
        let next = activeIndex + delta;
        if (next < 0) {
            next = rows.length - 1;
        }
        if (next > rows.length - 1) {
            next = 0;
        }
        setActive(next);
    }

    renderEmpty();
}));
