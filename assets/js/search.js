const loading = import("/pagefind/pagefind.js");

const OPTIONS = {
    excerptLength: 150
};

// Lowercase + strip diacritics so "josman" matches "Josman" and "medine" matches "Médine".
function normalizeText(value) {
    return (value || '')
        .normalize('NFD')
        .replace(/[̀-ͯ]/g, '')
        .toLowerCase()
        .trim();
}

window.addEventListener('DOMContentLoaded', () => loading.then(pagefind => {
    // Elements
    const searchInput = document.getElementById('search-input');
    const searchResultsContainer = document.getElementById('search-results-container');
    const searchResultsClose = document.getElementById('search-results-close');
    const searchResults = document.getElementById('search-results');

    // Position elements
    const position = searchInput.getBoundingClientRect();
    searchResults.style = `margin-top: ${position.top + position.height}px !important`;

    // Bind events
    searchResults.addEventListener('click', e => e.stopPropagation());
    searchResultsContainer.addEventListener('click', hideResults);
    searchResultsClose.addEventListener('click', hideResults);

    searchInput.addEventListener('keyup', () => {
        const term = (searchInput.value || '').trim();
        if (term === '') {
            hideResults();
        } else {
            debounce(() => {
                pagefind.search(term, OPTIONS)
                    .then(showResults)
                    .catch(hideResults);
            }, 300)();
        }
    });

    // Results handling
    function clearResults() {
        while (searchResults.firstChild) {
            searchResults.removeChild(searchResults.lastChild);
        }
    }

    function addResult(data) {
        const element = document.createElement('div');
        element.id = data.url;
        element.classList.add('pb-2', 'px-3', 'px-sm-0');
        element.appendChild(createCoverElement(data.meta.image, data.meta.image_alt, data.url));
        element.appendChild(createTitleElement(data.meta.title, data.url));
        element.appendChild(createContentElement(data));
        searchResults.appendChild(element);
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

    function showResults(search) {
        const term = normalizeText(searchInput.value || '');

        // Load every result's data, then order title matches first while keeping
        // Pagefind's relevance order as a tie-breaker.
        Promise.all(search.results.map((result, index) => result.data().then(data => ({ data, index }))))
            .then(items => {
                items.sort((a, b) => {
                    const diff = titleScore(b.data.meta.title, term) - titleScore(a.data.meta.title, term);
                    return diff !== 0 ? diff : a.index - b.index;
                });

                // Update results
                clearResults();
                items.forEach(item => addResult(item.data));

                // Show results
                searchResultsContainer.classList.remove('d-none');
                searchResultsContainer.classList.add('d-block');
                document.body.style.overflowY = 'hidden';
            })
            .catch(hideResults);
    }

    function hideResults() {
        searchResultsContainer.classList.remove('d-block');
        searchResultsContainer.classList.add('d-none');
        document.body.style.overflowY = '';
    }
}));

function createCoverElement(src, alt, url) {
    const image = document.createElement('img');
    image.src = src;
    image.alt = alt;
    image.setAttribute('width', '100%');
    image.classList.add('rounded-1');

    const link = document.createElement('a');
    link.href = url;
    link.classList.add('h-100');
    link.appendChild(image);

    const cover = document.createElement('div');
    cover.style.width = '250px';
    cover.classList.add('float-sm-start', 'd-flex', 'align-content-center', 'me-sm-3', 'my-2', 'mx-auto');
    cover.appendChild(link);
    return cover;
}

function createTitleElement(value, src) {
    const link = document.createElement('a');
    link.href = src;
    link.innerText = value;

    const title = document.createElement('h3');
    title.appendChild(link);
    return title;
}

function createContentElement(data) {
    const content = document.createElement('div');
    content.classList.add('small', 'text-body-tertiary');

    // Meta-data
    if (data.meta.venues) {
        content.innerText = data.meta.venues;
    }

    if (data.meta.date) {
        if (content.innerText) {
            content.innerText += ', ';
        }
        content.appendChild(createDateElement(data.meta.date));
    }

    const rank = createRankElement(data.meta.rank);
    if (rank) {
        content.appendChild(rank);
    }

    // Content
    const excerpt = document.createElement('div');
    excerpt.classList.add('pt-1', 'text-muted', 'text-justify', 'text-sm-start');
    excerpt.innerText = data.content.substring(0, Math.min(data.content.length, 1000)).trim() + '…';
    content.appendChild(excerpt);

    // Read more
    const link = document.createElement('a');
    link.href = data.url;
    link.innerText = 'Lire la suite';

    const more = document.createElement('p');
    more.classList.add('text-end');
    more.style.clear = 'both';
    more.appendChild(link);
    content.appendChild(more);

    return content;
}

function createRankElement(value) {
    if (isNaN(value)) {
        return null;
    }

    const rank = document.createElement('span');
    rank.classList.add('ms-2', 'text-primary');

    value = Number(value);
    for (let i = 0 ; i < 5 ; i++) {
        const icon = document.createElement('i');
        if (value > i) {
            if (value < i + 1) {
                icon.classList.add('fa', 'fa-star-half-stroke');
            } else {
                icon.classList.add('fa', 'fa-star');
            }
        } else {
            icon.classList.add('far', 'fa-star');
        }
        rank.appendChild(icon);
    }
    return rank;
}

function createDateElement(value) {
    const date = document.createElement('time');
    date.classList.add('text-body');
    date.innerText = value;
    return date;
}
