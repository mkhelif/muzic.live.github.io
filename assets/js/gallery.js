// Lightweight, dependency-free photo gallery lightbox.
// Thumbnails live in a `.gallery` container; each is an <a> linking to the
// full-size image. Clicking opens an overlay with prev/next navigation,
// scoped to the gallery that was clicked.
(function () {
    let overlay, imgEl, captionEl, creditEl, items = [], idx = 0;

    function build() {
        overlay = document.createElement('div');
        overlay.className = 'gallery-lightbox';
        overlay.innerHTML =
            '<button class="gallery-lightbox__close" type="button" aria-label="Fermer">&times;</button>' +
            '<button class="gallery-lightbox__nav gallery-lightbox__prev" type="button" aria-label="Image précédente"><i class="fa fa-caret-left"></i></button>' +
            '<figure class="gallery-lightbox__figure">' +
                '<img class="gallery-lightbox__img" alt="">' +
                '<figcaption class="gallery-lightbox__caption"></figcaption>' +
                '<div class="gallery-lightbox__credit"></div>' +
            '</figure>' +
            '<button class="gallery-lightbox__nav gallery-lightbox__next" type="button" aria-label="Image suivante"><i class="fa fa-caret-right"></i></button>';
        document.body.appendChild(overlay);

        imgEl = overlay.querySelector('.gallery-lightbox__img');
        captionEl = overlay.querySelector('.gallery-lightbox__caption');
        creditEl = overlay.querySelector('.gallery-lightbox__credit');

        overlay.querySelector('.gallery-lightbox__close').addEventListener('click', close);
        overlay.querySelector('.gallery-lightbox__prev').addEventListener('click', (e) => { e.stopPropagation(); show(idx - 1); });
        overlay.querySelector('.gallery-lightbox__next').addEventListener('click', (e) => { e.stopPropagation(); show(idx + 1); });
        overlay.addEventListener('click', (e) => {
            // Close when clicking the backdrop (anything but the image itself).
            if (e.target.tagName !== 'IMG' && !e.target.closest('.gallery-lightbox__nav')) {
                close();
            }
        });
        document.addEventListener('keydown', (e) => {
            if (!overlay.classList.contains('is-open')) {
                return;
            }
            if (e.key === 'Escape') {
                close();
            } else if (e.key === 'ArrowLeft') {
                show(idx - 1);
            } else if (e.key === 'ArrowRight') {
                show(idx + 1);
            }
        });
    }

    function show(i) {
        idx = (i + items.length) % items.length;
        imgEl.src = items[idx].full;
        imgEl.alt = items[idx].alt || '';
        captionEl.textContent = (idx + 1) + ' / ' + items.length;
    }

    function open(list, start, credit) {
        if (!overlay) {
            build();
        }
        items = list;
        if (credit && credit.name) {
            const safeName = credit.name.replace(/</g, '&lt;').replace(/>/g, '&gt;');
            creditEl.innerHTML = credit.url
                ? '&copy; Photos / <a href="' + encodeURI(credit.url) + '" target="_blank" rel="noreferrer noopener">' + safeName + '</a>'
                : '&copy; Photos / ' + safeName;
        } else {
            creditEl.innerHTML = '';
        }
        overlay.classList.toggle('is-single', items.length <= 1);
        show(start);
        overlay.classList.add('is-open');
        document.body.classList.add('gallery-lock');
    }

    function close() {
        overlay.classList.remove('is-open');
        document.body.classList.remove('gallery-lock');
        imgEl.removeAttribute('src');
    }

    document.addEventListener('click', function (e) {
        const link = e.target.closest('.gallery a');
        if (!link) {
            return;
        }
        e.preventDefault();
        const gallery = link.closest('.gallery');
        const links = Array.prototype.slice.call(gallery.querySelectorAll('a'));
        const list = links.map((a) => {
            const im = a.querySelector('img');
            return {
                full: a.getAttribute('href'),
                alt: im ? im.alt : ''
            };
        });
        const credit = { name: gallery.dataset.creditName, url: gallery.dataset.creditUrl };
        open(list, links.indexOf(link), credit);
    });
})();
