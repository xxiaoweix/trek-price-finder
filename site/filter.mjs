// Pure page logic: filtering, grouping, sorting, paging, and URL state.
// No DOM access here so everything can be tested with `node --test`.

export const FILTER_KEYS = ["family", "model", "generation", "size", "color", "state"];
export const SORT_KEYS = ["price", "model", "generation", "size", "color", "store", "place", "seen"];
export const PAGE_SIZES = [25, 50, 100];
export const DEFAULT_SORT = { key: "price", dir: "asc" };
export const DEFAULT_PER_PAGE = 50;
export const STALE_AFTER_DAYS = 8;

const TWO_WORD_FAMILIES = new Set(["Fuel EX", "Fuel EXe", "Top Fuel", "Dual Sport"]);
const LETTER_SIZES = ["XXS", "XS", "S", "P", "M", "ML", "MG", "L", "G", "XL", "XG", "XXL"];
const collator = new Intl.Collator("en-US", { numeric: true, sensitivity: "base" });

// Trek family is the leading word of the model name ("Marlin 5" -> "Marlin"),
// except a few two-word families such as "Fuel EX 9.8 XT" -> "Fuel EX".
export function familyOf(model) {
  const words = String(model || "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "";
  const two = words.slice(0, 2).join(" ");
  return TWO_WORD_FAMILIES.has(two) ? two : words[0];
}

export function valueOf(offer, key) {
  const value = key === "family" ? familyOf(offer.model) : offer[key];
  return value == null ? "" : String(value);
}

export function compareText(left, right) {
  return collator.compare(String(left || ""), String(right || ""));
}

// Letter sizes in fit order (XS < S < M < ...), numeric frame sizes first.
export function compareSize(left, right) {
  const rank = (size) => {
    const text = String(size || "");
    if (/^\d/.test(text)) return [0, Number.parseFloat(text)];
    const index = LETTER_SIZES.indexOf(text.split(" ")[0]);
    return [1, index === -1 ? LETTER_SIZES.length : index];
  };
  const [a, b] = [rank(left), rank(right)];
  return a[0] - b[0] || a[1] - b[1] || compareText(left, right);
}

export function choiceValues(offers, key) {
  const values = new Set();
  for (const offer of offers) {
    const value = valueOf(offer, key);
    if (value) values.add(value);
  }
  return [...values].sort(key === "size" ? compareSize : compareText);
}

function selectedSet(query, key) {
  const value = query[key];
  if (value == null || value === "") return null;
  const list = Array.isArray(value) ? value : [value];
  return list.length ? new Set(list.map(String)) : null;
}

// Keep offers matching every active filter. A filter is a list of allowed
// values (any of them matches); an empty list means "any". `except` skips one
// filter, which is how cascading option lists are built.
export function matchOffers(offers, query, except = "") {
  const active = FILTER_KEYS.filter((key) => key !== except)
    .map((key) => [key, selectedSet(query, key)])
    .filter(([, set]) => set);
  if (!active.length) return offers.slice();
  return offers.filter((offer) => active.every(([key, set]) => set.has(valueOf(offer, key))));
}

function price(offer) {
  return Number(offer.price_usd);
}

export function compareOffers(left, right) {
  return price(left) - price(right) || compareText(left.retailer, right.retailer);
}

// Kept for compatibility: matching offers, cheapest first.
export function filterOffers(offers, query) {
  return matchOffers(offers, query).sort(compareOffers);
}

export function configKey(offer) {
  return [offer.model, offer.generation, offer.size, offer.color].map((v) => (v == null ? "" : String(v))).join("|");
}

// One group per configuration (model + generation + size + color).
// `best` is the cheapest offer (ties by store name); `offers` is cheapest first.
export function groupOffers(offers) {
  const groups = new Map();
  for (const offer of offers) {
    const key = configKey(offer);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(offer);
  }
  return [...groups].map(([key, list]) => {
    list.sort(compareOffers);
    return { key, best: list[0], offers: list };
  });
}

function seenValue(offer) {
  const value = Number(offer.lowest_seen_usd);
  return offer.lowest_seen_usd == null || offer.lowest_seen_usd === "" || Number.isNaN(value) ? Infinity : value;
}

const groupComparers = {
  price: (a, b) => price(a.best) - price(b.best),
  model: (a, b) => compareText(a.best.model, b.best.model),
  generation: (a, b) => compareText(a.best.generation, b.best.generation),
  size: (a, b) => compareSize(a.best.size, b.best.size),
  color: (a, b) => compareText(a.best.color, b.best.color),
  store: (a, b) => compareText(a.best.retailer, b.best.retailer),
  place: (a, b) => compareText(a.best.state, b.best.state) || compareText(a.best.city, b.best.city),
  seen: (a, b) => seenValue(a.best) - seenValue(b.best),
};

// Sort groups by a column; ties fall back to price, then model, size, color.
export function sortGroups(groups, sort = DEFAULT_SORT) {
  const primary = groupComparers[sort.key] || groupComparers.price;
  const sign = sort.dir === "desc" ? -1 : 1;
  const tie = (a, b) =>
    groupComparers.price(a, b) ||
    groupComparers.model(a, b) ||
    groupComparers.generation(a, b) ||
    groupComparers.size(a, b) ||
    groupComparers.color(a, b) ||
    compareText(a.key, b.key);
  return groups.slice().sort((a, b) => sign * primary(a, b) || tie(a, b));
}

export function paginate(items, page, perPage) {
  const per = PAGE_SIZES.includes(Number(perPage)) ? Number(perPage) : DEFAULT_PER_PAGE;
  const pages = Math.max(1, Math.ceil(items.length / per));
  const current = Math.min(Math.max(1, Math.trunc(Number(page)) || 1), pages);
  const start = (current - 1) * per;
  return { items: items.slice(start, start + per), page: current, pages, perPage: per, start, total: items.length };
}

// The "Lowest seen" column only means something once a configuration has been
// cheaper before than its cheapest offer today (after the first scan).
export function showLowestSeen(groups) {
  return groups.some((group) => seenValue(group.best) < price(group.best));
}

// After a filter changes, drop selections further down the bike hierarchy
// (family > model > gen > size > color) that no longer match, so picking a new
// model never leaves the page stuck on a size that model does not come in.
// Earlier filters and the state filter are never changed for the user.
const PRUNE_ORDER = ["family", "model", "generation", "size", "color"];

export function pruneSelections(offers, query, changedKey) {
  const next = { ...query };
  const from = PRUNE_ORDER.indexOf(changedKey);
  if (from === -1) return next;
  for (const key of PRUNE_ORDER.slice(from + 1)) {
    if (!next[key]?.length) continue;
    const available = new Set(choiceValues(matchOffers(offers, next, key), key));
    next[key] = next[key].filter((value) => available.has(value));
  }
  return next;
}

export function emptyState() {
  const state = { sort: { ...DEFAULT_SORT }, page: 1, perPage: DEFAULT_PER_PAGE };
  for (const key of FILTER_KEYS) state[key] = [];
  return state;
}

function decode(text) {
  try {
    return decodeURIComponent(text.replace(/\+/g, " "));
  } catch {
    return "";
  }
}

// Parse "?model=Marlin%205,Domane%20AL%202&sort=-price&page=2&per=25".
// Values are comma-separated; commas inside a value are sent as %2C.
export function parseState(search) {
  const state = emptyState();
  const raw = String(search || "").replace(/^\?/, "");
  for (const pair of raw.split("&")) {
    if (!pair) continue;
    const at = pair.indexOf("=");
    const name = decode(at === -1 ? pair : pair.slice(0, at));
    const value = at === -1 ? "" : pair.slice(at + 1);
    if (FILTER_KEYS.includes(name)) {
      const values = value.split(",").map(decode).map((v) => v.trim()).filter(Boolean);
      state[name] = [...new Set([...state[name], ...values])];
    } else if (name === "sort") {
      const text = decode(value);
      const key = text.replace(/^-/, "");
      if (SORT_KEYS.includes(key)) state.sort = { key, dir: text.startsWith("-") ? "desc" : "asc" };
    } else if (name === "page") {
      state.page = Math.max(1, Math.trunc(Number(decode(value))) || 1);
    } else if (name === "per") {
      const per = Number(decode(value));
      if (PAGE_SIZES.includes(per)) state.perPage = per;
    }
  }
  return state;
}

// Inverse of parseState; defaults are left out to keep links short.
export function serializeState(state) {
  const parts = [];
  for (const key of FILTER_KEYS) {
    const values = state[key] || [];
    if (values.length) parts.push(`${key}=${values.map(encodeURIComponent).join(",")}`);
  }
  const sort = state.sort || DEFAULT_SORT;
  if (sort.key !== DEFAULT_SORT.key || sort.dir !== DEFAULT_SORT.dir) {
    parts.push(`sort=${sort.dir === "desc" ? "-" : ""}${sort.key}`);
  }
  if (state.page > 1) parts.push(`page=${state.page}`);
  if (state.perPage && state.perPage !== DEFAULT_PER_PAGE) parts.push(`per=${state.perPage}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export function ageInDays(updatedAt, now = Date.now()) {
  const time = new Date(updatedAt).getTime();
  if (Number.isNaN(time)) return null;
  return (Number(now) - time) / 86_400_000;
}

export function isStale(updatedAt, now = Date.now(), maxDays = STALE_AFTER_DAYS) {
  const age = ageInDays(updatedAt, now);
  return age === null || age > maxDays;
}

function listStates(states) {
  if (!states?.length) return "";
  if (states.length === 1) return states[0];
  return `${states.slice(0, -1).join(", ")} or ${states[states.length - 1]}`;
}

export function resultLabel(configurations, offers, states = []) {
  const where = states.length ? ` in ${listStates(states)}` : "";
  if (!configurations) {
    return states.length ? `No in-stock price${where}` : "No in-stock offers match these filters";
  }
  const configs = configurations === 1 ? "configuration" : "configurations";
  const count = offers === 1 ? "offer" : "offers";
  const n = (value) => value.toLocaleString("en-US");
  return `${n(configurations)} ${configs} from ${n(offers)} in-stock ${count}${where}`;
}
