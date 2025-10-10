// Extract date time
var doorOpen = document.getElementsByClassName('event-info-door')[1].children[0];
if (doorOpen) doorOpen.remove();

var date = document.getElementsByClassName('event-info-date')[0].textContent.trim().split('.').reverse().join('-');
var time = document.getElementsByClassName('event-info-door')[1].textContent.trim().split('.').reverse().join('-');
var datetime = `${date}T${time}:00+02:00`;

// Extract artists
var artist = document.getElementsByTagName('h1')[0].textContent;
var supports = Array(...document.getElementsByTagName('h2'))
    .filter(h2 => h2.classList.contains('text-center'))
    .flatMap(h2 => h2.textContent.split('+'))
    .map(title => title.replace('+', ''))
    .map(title => title.trim())
    .filter(title => title);

var supportsList = "";
supports.forEach(artist => supportsList += `
  - ${artist}`);

// Extract SeeTickets link
var seeticketsLink = Array(...document.getElementsByClassName("dropdown-item"))
    .filter(item => item.nodeName === 'A')
    .filter(a => a.href.startsWith('https://www.seetickets.com'));

var seetickets = "";
if (seeticketsLink.length > 0) {
    seetickets = seeticketsLink[0].href.substring('https://www.seetickets.com'.length);
}

// Build file path
var dtd = new Date(datetime);
var directory = `${dtd.getFullYear()}`;
directory += `/${(dtd.getMonth() + 1).toString().padStart(2, '0')}`;
directory += `/${dtd.getDate().toString().padStart(2, '0')}`;

var filename = Array(artist, ...supports)
    .map(name => name.toLowerCase())
    .map(name => name.replaceAll(' ', '-'))
    .join('-')
;

// Generate content
copy(`cat <<EOF > "${directory}/${filename}.md"
---
eventDate: "${datetime}"
artists:
  - ${artist}${supportsList}
locations:
  - Les Docks
tickets:
  seetickets: "${seetickets}"
---
EOF`);
