// Extract date time
var doorOpen = document.getElementsByClassName('event-info-door')[1].children[0];
if (doorOpen) doorOpen.remove();

var date = document.getElementsByClassName('event-info-date')[0].textContent.trim().split('.').reverse().join('-');
var time = document.getElementsByClassName('event-info-door')[1].textContent.trim().split('.').reverse().join('-');
var datetime = `${date}T${time}:00+02:00`;

// Extract artists
var artist = document.getElementsByTagName('h1')[0].textContent.trim();
var supports = Array(...document.getElementsByTagName('h2'))
    .filter(h2 => h2.classList.contains('text-center'))
    .flatMap(h2 => h2.textContent.split('+'))
    .map(title => title.trim())
    .filter(title => title);

var allArtists = [artist, ...supports];

// Extract SeeTickets link
var seeticketsLink = Array(...document.getElementsByClassName("dropdown-item"))
    .filter(item => item.nodeName === 'A')
    .filter(a => a.href.startsWith('https://www.seetickets.com'));

var seetickets = "";
if (seeticketsLink.length > 0) {
    seetickets = seeticketsLink[0].href.substring('https://www.seetickets.com'.length);
}

// Slug helper (handles accents and special chars)
function toSlug(name) {
    return name.toLowerCase()
        .normalize('NFD').replace(/[̀-ͯ]/g, '')
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '');
}

// Build file path (relative to repo root)
var dtd = new Date(datetime);
var directory = `content/events/${dtd.getFullYear()}`;
directory += `/${(dtd.getMonth() + 1).toString().padStart(2, '0')}`;
directory += `/${dtd.getDate().toString().padStart(2, '0')}`;

var filename = allArtists.map(toSlug).join('-');

// Generate shell commands: resolve existing artist ID or create stub + UUID
var artistResolutions = allArtists.map((name, i) => {
    var slug = toSlug(name);
    var safeName = name.replace(/"/g, '\\"').replace(/%/g, '%%');
    return `ARTIST_${i}=$(
  if [ -f "content/artists/${slug}/index.md" ]; then
    grep '^id:' "content/artists/${slug}/index.md" | sed 's/^id:[[:space:]]*"//;s/"$//'
  else
    _UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    mkdir -p "content/artists/${slug}"
    printf -- '---\\nid: "%s"\\ntitle: "${safeName}"\\ncover: ""\\ntodo:\\n  - Add cover\\n  - Add socials\\n  - Add description\\n---\\n' "$_UUID" > "content/artists/${slug}/index.md"
    echo "$_UUID"
  fi
)`;
}).join('\n\n');

var artistsList = allArtists.map((_, i) => `  - "$ARTIST_${i}"`).join('\n');

copy(`${artistResolutions}

mkdir -p "${directory}" && cat > "${directory}/${filename}.md" <<EOF
---
date: ${datetime}
artists:
${artistsList}
locationId: "592a1212-36b9-48c2-9bfe-ebaa09957bde"
tickets:
  seetickets: "${seetickets}"
---
EOF`);
