// Glossary for the mission pages: known terms in the page text get a dotted underline, and
// hovering one shows a short explainer (with a Wikipedia link where that helps).
//
// Text is marked automatically, including text the page adds later (rocket summaries, the
// flight log, results...).  Some places are left alone on purpose: buttons (a click must
// still press the button), form fields and dropdown options (they can't hold markup), and
// containers marked data-no-glossary (e.g. telemetry that redraws every half second).

const WIKI = 'https://en.wikipedia.org/wiki/';

// [terms that show it, title, explanation, Wikipedia article (optional)]
const ENTRIES = [
  // Orbits and their parts
  [['LEO', 'low Earth orbit', 'Low Earth orbit'], 'LEO: low Earth orbit',
    'An orbit close to Earth, lower than about 2,000 km. The International Space Station and most satellites fly here. One trip around takes about 90 minutes.', 'Low_Earth_orbit'],
  [['MEO', 'medium Earth orbit', 'Medium Earth orbit'], 'MEO: medium Earth orbit',
    'Orbits between low Earth orbit and geostationary orbit, from about 2,000 km to 35,786 km up. GPS satellites fly here.', 'Medium_Earth_orbit'],
  [['GTO', 'geostationary transfer orbit', 'Geostationary transfer orbit'], 'GTO: geostationary transfer orbit',
    'A stretched-out orbit whose high point reaches geostationary height. Rockets often leave satellites here, and the satellite then uses its own engine to get to geostationary orbit.', 'Geostationary_transfer_orbit'],
  [['GEO', 'geostationary', 'Geostationary'], 'GEO: geostationary orbit',
    'A circular orbit 35,786 km above the equator. A satellite there goes around once a day, the same as Earth spins, so it seems to hang still over one spot.', 'Geostationary_orbit'],
  [['SSO', 'sun-synchronous', 'Sun-synchronous'], 'Sun-synchronous orbit',
    'A nearly polar orbit that slowly turns to keep the same angle to the Sun. The satellite passes over each place at the same local time every day, which is handy for taking pictures.', 'Sun-synchronous_orbit'],
  [['Molniya'], 'Molniya orbit',
    'A long, tilted orbit that spends most of its time high over the far north. Russia uses it for satellites that serve places too far north for geostationary satellites.', 'Molniya_orbit'],
  [['polar orbit', 'Polar orbit'], 'Polar orbit',
    'An orbit that passes over or near both poles (inclination near 90°). As Earth turns underneath, the satellite gets to see the whole planet.', 'Polar_orbit'],
  [['parking orbit', 'Parking orbit'], 'Parking orbit',
    'A low, temporary orbit a spacecraft waits in before its next big burn.', 'Parking_orbit'],
  [['perigee', 'Perigee', 'apogee', 'Apogee'], 'Perigee and apogee',
    'The lowest point (perigee) and highest point (apogee) of an orbit around Earth. For a circular orbit they are the same.', 'Apsis'],
  [['perilune', 'Perilune', 'apolune', 'Apolune'], 'Perilune and apolune',
    'The lowest point (perilune) and highest point (apolune) of an orbit around the Moon.', 'Apsis'],
  [['periapsis', 'Periapsis', 'apoapsis', 'Apoapsis', 'apsis'], 'Periapsis and apoapsis',
    'The lowest and highest points of any orbit, whatever it goes around.', 'Apsis'],
  [['inclination', 'Inclination'], 'Inclination',
    'How tilted an orbit is compared to the equator. 0° goes around the equator, 90° passes over the poles, and more than 90° means going around "backwards" (against the planet\'s spin).', 'Orbital_inclination'],
  [['sphere of influence'], 'Sphere of influence',
    'The region around a planet or moon where its gravity matters most to a spacecraft. Near the Moon it is about 66,000 km across in every direction.', 'Sphere_of_influence_(astrodynamics)'],
  [['B-plane'], 'B-plane',
    'An imaginary target board placed across a spacecraft\'s path as it nears a planet or moon. Aiming at a spot on it decides how close the craft passes and how its orbit will be tilted.'],

  // Mission steps
  [['TLI', 'trans-lunar injection', 'Trans-lunar injection'], 'TLI: trans-lunar injection',
    'The big engine burn that sends a spacecraft from Earth orbit toward the Moon. From low Earth orbit it adds about 3.1 km/s.', 'Trans-lunar_injection'],
  [['LOI', 'lunar orbit insertion', 'Lunar orbit insertion'], 'LOI: lunar orbit insertion',
    'A braking burn near the Moon so its gravity captures the spacecraft into orbit instead of letting it fly past.', 'Orbit_insertion'],
  [['TEI', 'trans-Earth injection', 'Trans-Earth injection'], 'TEI: trans-Earth injection',
    'The burn that sends a spacecraft out of lunar orbit and back toward Earth.', 'Trans-Earth_injection'],
  [['MCC', 'course correction', 'Course correction', 'course corrections', 'Course corrections'], 'Course correction',
    'A small burn partway through a long coast to fix small aiming errors, so the spacecraft arrives exactly where planned.', 'Orbital_maneuver'],
  [['DOI', 'descent orbit insertion', 'Descent orbit insertion'], 'Descent orbit insertion',
    'A small burn that lowers the low point of the orbit to about 15 km above the Moon, setting up the landing.'],
  [['PDI', 'powered descent', 'Powered descent'], 'Powered descent',
    'The main landing burn: the engine fires to slow the lander from orbital speed (about 1.7 km/s) all the way down to a gentle touchdown.', 'Apollo_Lunar_Module'],
  [['high gate', 'High gate'], 'High gate',
    'A checkpoint about 1 km above the landing site where the hard braking ends and the slower final approach begins.'],
  [['gravity turn', 'Gravity turn'], 'Gravity turn',
    'How rockets climb: after a small tip-over (the pitch kick), gravity slowly bends the path from straight up toward sideways, which wastes less fuel and keeps air loads low.', 'Gravity_turn'],
  [['pitch kick', 'Pitch kick', 'Pitch-over', 'pitch-over'], 'Pitch kick',
    'A small tilt a rocket makes just after liftoff to start its gravity turn.', 'Gravity_turn'],
  [['fairing', 'Fairing'], 'Payload fairing',
    'The nose cone that protects the payload while the rocket flies through the air. It splits open and falls away once the rocket is high enough.', 'Payload_fairing'],
  [['aerobraking'], 'Aerobraking',
    'Dipping through the top of a planet\'s air to slow down using drag instead of rocket fuel.', 'Aerobraking'],
  [['rendezvous'], 'Rendezvous',
    'Two spacecraft meeting up in orbit, for example so a lander can dock with the ship it left.', 'Space_rendezvous'],
  [['tanker'], 'Tanker flight',
    'A launch that carries no cargo, only spare propellant, so it can be moved into another spacecraft in orbit.', 'Orbital_propellant_depot'],

  // Measures
  [['Isp', 'specific impulse'], 'Isp: specific impulse',
    'How efficiently an engine uses propellant, measured in seconds. Higher is better: the engine gets more push out of each kilogram it burns.', 'Specific_impulse'],
  [['Δv', 'delta-v'], 'Δv (delta-v)',
    'How much a spacecraft can change its speed with the propellant it carries. Each part of a mission "costs" some delta-v, a bit like a fuel budget.', 'Delta-v'],
  [['thrust-to-weight'], 'Thrust-to-weight ratio',
    'Engine push divided by the vehicle\'s weight. It must be above 1 to lift off; higher means a faster climb.', 'Thrust-to-weight_ratio'],
  [['kN'], 'kN: kilonewton',
    'A unit of force (push). 1 kN is about the weight of 100 kg on Earth.', 'Newton_(unit)'],
  [['gravity losses'], 'Gravity losses',
    'Fuel spent just holding a rocket up against gravity while it climbs or lands, instead of speeding it up.', 'Gravity_drag'],

  // Places and organizations
  [['LC-39A'], 'LC-39A',
    'Launch Complex 39A at NASA\'s Kennedy Space Center in Florida. Apollo moon rockets and Space Shuttles launched from it, and SpaceX uses it today.', 'Kennedy_Space_Center_Launch_Complex_39A'],
  [['Cape Canaveral'], 'Cape Canaveral',
    'The stretch of Florida coast where most American rockets launch. Launching east from there gets a boost from Earth\'s spin.', 'Cape_Canaveral'],
  [['ISS'], 'ISS: International Space Station',
    'The large crewed space station orbiting about 420 km up, tilted 51.6° to the equator.', 'International_Space_Station'],
  [['GPS'], 'GPS: Global Positioning System',
    'The US navigation satellites, orbiting about 20,180 km up. Phones use their signals to work out where they are.', 'Global_Positioning_System'],
  [['Galileo'], 'Galileo', 'Europe\'s satellite navigation system, similar to GPS.', 'Galileo_(satellite_navigation)'],
  [['GLONASS'], 'GLONASS', 'Russia\'s satellite navigation system, similar to GPS.', 'GLONASS'],
  [['Starlink'], 'Starlink', 'SpaceX\'s network of thousands of internet satellites in low Earth orbit.', 'Starlink'],
  [['NASA'], 'NASA', 'The United States\' space agency.', 'NASA'],
  [['ULA'], 'ULA: United Launch Alliance',
    'An American rocket company (a Boeing and Lockheed Martin partnership) that builds the Atlas V and Vulcan rockets.', 'United_Launch_Alliance'],
  [['Apollo 11'], 'Apollo 11', 'The first crewed Moon landing, in July 1969, at Tranquility Base.', 'Apollo_11'],
  [['Apollo 15'], 'Apollo 15', 'The 1971 Moon landing near the Apennine mountains and Hadley Rille; the first to use a lunar rover.', 'Apollo_15'],
  [['Apollo 17'], 'Apollo 17', 'The last Apollo Moon landing, in December 1972, in the Taurus-Littrow valley.', 'Apollo_17'],
  [["Chang'e 4"], "Chang'e 4", 'The Chinese lander that made the first soft landing on the Moon\'s far side, in 2019.', "Chang'e_4"],
  [['Shackleton'], 'Shackleton crater',
    'A crater at the Moon\'s south pole. Its floor never sees sunlight and may hold ice, which makes the area a popular landing target.', 'Shackleton_(crater)'],

  // Vehicles, stages and engines
  [['SLS'], 'SLS: Space Launch System', 'NASA\'s large rocket for the Artemis Moon missions.', 'Space_Launch_System'],
  [['ICPS'], 'ICPS: Interim Cryogenic Propulsion Stage',
    'The upper stage on the first version of SLS. It does the trans-lunar injection burn for the Orion capsule.', 'Interim_Cryogenic_Propulsion_Stage'],
  [['Delta Cryogenic Second Stage'], 'Delta Cryogenic Second Stage',
    'The hydrogen-fueled upper stage of the Delta IV rockets.', 'Delta_Cryogenic_Second_Stage'],
  [['Centaur', 'Centaur V'], 'Centaur', 'A long-serving American hydrogen-fueled upper stage, flown on Atlas V and Vulcan.', 'Centaur_(rocket_stage)'],
  [['Super Heavy'], 'Super Heavy', 'The giant first stage (booster) of SpaceX\'s Starship rocket, with 33 Raptor engines.', 'SpaceX_Super_Heavy'],
  [['Starship'], 'Starship', 'SpaceX\'s fully reusable rocket in development. The Starship upper stage can also be a Moon lander (Starship HLS).', 'SpaceX_Starship'],
  [['HLS', 'Starship HLS'], 'HLS: Human Landing System',
    'A spacecraft that carries astronauts from lunar orbit down to the Moon and back up. NASA picked a version of Starship for this.', 'Starship_HLS'],
  [['Lunar Module', 'LM'], 'Lunar Module (LM)',
    'The Apollo Moon lander. Its bottom half (descent stage) did the landing and stayed behind; the top half (ascent stage) took the crew back to orbit.', 'Apollo_Lunar_Module'],
  [['CSM', 'service module'], 'Service module',
    'The part of a crew spacecraft that holds its main engine, propellant and power, like the Apollo CSM or Orion\'s service module.', 'Apollo_command_and_service_module'],
  [['Orion'], 'Orion', 'NASA\'s crew capsule for the Artemis Moon missions.', 'Orion_(spacecraft)'],
  [['Blue Moon'], 'Blue Moon', 'Blue Origin\'s Moon lander, in development.', 'Blue_Moon_(spacecraft)'],
  [['Space Shuttle'], 'Space Shuttle', 'NASA\'s reusable spaceplane, flown from 1981 to 2011.', 'Space_Shuttle'],
  [['BE-4'], 'BE-4', 'Blue Origin\'s methane-burning engine, used on New Glenn and Vulcan.', 'BE-4'],
  [['BE-3U'], 'BE-3U', 'Blue Origin\'s hydrogen-burning upper-stage engine, used on New Glenn.', 'BE-3'],
  [['BE-7'], 'BE-7', 'Blue Origin\'s small hydrogen-burning landing engine for the Blue Moon lander.', 'BE-7'],
  [['RL10', 'RL10B-2', 'RL10C-1'], 'RL10',
    'A small, very efficient hydrogen-burning engine first flown in 1962 and still used on upper stages today.', 'RL10'],
  [['RS-25'], 'RS-25', 'The Space Shuttle\'s main engine, now used on SLS.', 'RS-25'],
  [['RD-180'], 'RD-180', 'A Russian-built kerosene engine that powers the Atlas V first stage.', 'RD-180'],
  [['Raptor', 'Raptors', 'Raptor Vacuum'], 'Raptor',
    'SpaceX\'s methane-burning engine for Starship and Super Heavy. The Vacuum version has a bigger nozzle for use in space.', 'SpaceX_Raptor'],
  [['Merlin', 'Merlin 1D', 'Merlin 1D Vacuum'], 'Merlin', 'SpaceX\'s kerosene-burning engine, used on Falcon 9.', 'SpaceX_Merlin'],
  [['J-2'], 'J-2', 'The hydrogen-burning engine of the Saturn V\'s upper stages, which sent Apollo to the Moon.', 'Rocketdyne_J-2'],
  [['AJ10', 'AJ10-137', 'AJ10-190'], 'AJ10', 'A family of simple, reliable engines used on Apollo\'s service module, the Space Shuttle and Orion.', 'AJ10'],
  [['OMS', 'OMS-E'], 'OMS: Orbital Maneuvering System',
    'The Space Shuttle\'s engines for changing orbit. Spare ones (OMS-E) now fly on Orion.', 'Space_Shuttle_Orbital_Maneuvering_System'],
  [['DPS'], 'DPS: Descent Propulsion System',
    'The throttleable landing engine of the Apollo Lunar Module.', 'Descent_Propulsion_System'],
  [['APS'], 'APS: Ascent Propulsion System',
    'The engine that launched the Apollo Lunar Module\'s top half back up from the Moon.', 'Ascent_Propulsion_System'],
  [['CECE'], 'CECE', 'A NASA test version of the RL10 that could throttle down very low, as a landing engine needs.', 'RL10'],
  [['GEM 63XL', 'GEM 63'], 'GEM 63', 'Solid rocket boosters strapped to Atlas V and Vulcan for extra push at liftoff.', 'GEM_63'],
  [['AJ-60A'], 'AJ-60A', 'Solid rocket boosters used on the Atlas V.', 'AJ-60A'],

  // Propellants
  [['LH2'], 'LH2: liquid hydrogen',
    'Hydrogen cooled to −253 °C so it becomes liquid. A very efficient fuel, but bulky and hard to keep cold.', 'Liquid_hydrogen'],
  [['LOX'], 'LOX: liquid oxygen', 'Oxygen cooled until liquid. Most rockets burn their fuel with it.', 'Liquid_oxygen'],
  [['CH4'], 'CH4: methane', 'Methane, burned as rocket fuel with liquid oxygen (Raptor, BE-4).', 'Methane'],
  [['RP-1'], 'RP-1', 'A highly refined kerosene used as rocket fuel.', 'RP-1'],
  [['MMH'], 'MMH: monomethylhydrazine',
    'A fuel that ignites by itself when it touches its oxidizer, which makes engines simple and reliable.', 'Monomethylhydrazine'],
  [['N2O4'], 'N2O4: dinitrogen tetroxide', 'An oxidizer used with hydrazine fuels; they ignite on contact.', 'Dinitrogen_tetroxide'],
  [['Aerozine 50'], 'Aerozine 50', 'A hydrazine fuel blend used by the Apollo engines.', 'Aerozine_50'],
];

// Places left alone: buttons, form controls and links, and anything marked data-no-glossary.
const SKIP = 'button, select, option, input, textarea, a, script, style, .term, [data-no-glossary], .glossary-box';

const BY_TERM = new Map();
for (const [terms, title, text, wiki] of ENTRIES) {
  for (const t of terms) BY_TERM.set(t, { title, text, wiki });
}
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const PATTERN = new RegExp(`(?<![\\w-])(${[...BY_TERM.keys()].sort((a, b) => b.length - a.length)
  .map(escape).join('|')})(?![\\w])`, 'g');

const STYLE = `
.term { text-decoration: underline dotted; text-underline-offset: 3px; cursor: help; }
.glossary-box {
  position: fixed; z-index: 1000; max-width: 320px; padding: 10px 12px; border-radius: 10px;
  background: #141a2b; color: #dfe6f3; border: 1px solid rgba(140, 160, 200, 0.35);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45); font: 13px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.glossary-box strong { display: block; margin-bottom: 4px; color: #ffb547; font-size: 13px; }
.glossary-box p { margin: 0; }
.glossary-box a { display: inline-block; margin-top: 6px; color: #6ea8ff; }
.glossary-box[hidden] { display: none; }
`;

function markText(node) {
  const text = node.nodeValue;
  PATTERN.lastIndex = 0;
  if (!PATTERN.test(text)) return;
  PATTERN.lastIndex = 0;
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(PATTERN)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const span = document.createElement('span');
    span.className = 'term';
    span.dataset.term = m[1];
    span.textContent = m[1];
    frag.append(span);
    last = m.index + m[1].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  node.replaceWith(frag);
}

function markWithin(root) {
  if (root.nodeType === Node.TEXT_NODE) {
    if (root.parentElement && !root.parentElement.closest(SKIP)) markText(root);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE || root.closest(SKIP)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(markText);
}

export function installGlossary(root = document.body) {
  const style = document.createElement('style');
  style.textContent = STYLE;
  document.head.append(style);

  const box = document.createElement('div');
  box.className = 'glossary-box';
  box.setAttribute('role', 'tooltip');
  box.hidden = true;
  document.body.append(box);

  let hideTimer = null;
  let current = null;
  const show = (el) => {
    const e = BY_TERM.get(el.dataset.term);
    if (!e) return;
    clearTimeout(hideTimer);
    if (current !== el) {
      current = el;
      box.replaceChildren();
      const title = document.createElement('strong');
      title.textContent = e.title;
      const p = document.createElement('p');
      p.textContent = e.text;
      box.append(title, p);
      if (e.wiki) {
        const a = document.createElement('a');
        a.href = WIKI + e.wiki;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'Read more on Wikipedia ↗';
        box.append(a);
      }
    }
    box.hidden = false;
    // Below the term if there's room, otherwise above; kept inside the window.
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left), window.innerWidth - b.width - 8);
    const below = r.bottom + 6;
    const top = below + b.height < window.innerHeight - 8 ? below : Math.max(8, r.top - b.height - 6);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  };
  const hideSoon = () => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      box.hidden = true;
      current = null;
    }, 250);  // long enough to move the pointer into the box and click its link
  };

  document.addEventListener('mouseover', (ev) => {
    const term = ev.target.closest?.('.term');
    if (term) show(term);
    else if (ev.target.closest?.('.glossary-box')) clearTimeout(hideTimer);
  });
  document.addEventListener('mouseout', (ev) => {
    const from = ev.target.closest?.('.term, .glossary-box');
    const to = ev.relatedTarget?.closest?.('.term, .glossary-box');
    if (from && from !== to) hideSoon();
  });

  markWithin(root);
  // Mark text the page adds or changes later.
  new MutationObserver((records) => {
    for (const rec of records) {
      if (rec.type === 'characterData') markWithin(rec.target);
      for (const n of rec.addedNodes) markWithin(n);
    }
    if (current && !current.isConnected) {  // the term under the pointer was redrawn
      box.hidden = true;
      current = null;
    }
  }).observe(root, { childList: true, subtree: true, characterData: true });
}
