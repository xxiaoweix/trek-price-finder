import {
  FILTER_KEYS,
  PAGE_SIZES,
  choiceValues,
  groupOffers,
  isStale,
  ageInDays,
  matchOffers,
  paginate,
  parseState,
  pruneSelections,
  resultLabel,
  serializeState,
  showLowestSeen,
  sortGroups,
} from "./filter.mjs";

const labels = {
  family: "Family",
  model: "Model",
  generation: "Gen",
  size: "Size",
  color: "Color",
  state: "State",
};
const blanks = { state: "All states" };
const columns = ["price", "model", "generation", "size", "color", "store", "place", "seen"];
const columnLabels = {
  price: "Price",
  model: "Model",
  generation: "Gen",
  size: "Size",
  color: "Color",
  store: "Store",
  place: "City, State",
  seen: "Lowest seen (US)",
};

let state = parseState(window.location.search);
const expanded = new Set();
const $ = (id) => document.getElementById(id);
const moneyFormat = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function money(value) {
  return value == null || value === "" ? "—" : moneyFormat.format(Number(value));
}

function shown(value) {
  const text = value == null ? "" : String(value).trim();
  return text || "—";
}

function place(offer) {
  return [offer.city, offer.state].filter(Boolean).join(", ") || "—";
}

function element(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name in node && name !== "list") node[name] = value;
    else node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

function cell(column, text, extra = []) {
  return element("td", { className: column, "data-label": columnLabels[column] }, [text, ...extra]);
}

function storeLink(offer) {
  return element("a", { href: offer.url, rel: "noreferrer", textContent: offer.retailer || "Store" });
}

// ---- Filters ---------------------------------------------------------------

function buildFilters(dataset) {
  const list = $("filter-list");
  for (const key of FILTER_KEYS) {
    const summary = element("summary", {}, [
      element("span", { className: "filter-name", textContent: labels[key] }),
      element("span", { className: "filter-value", id: `${key}-value` }),
    ]);
    const options = element("fieldset", { className: "options", id: `${key}-options` }, [
      element("legend", { className: "visually-hidden", textContent: labels[key] }),
    ]);
    const details = element("details", { className: "multi", id: `${key}-filter` }, [summary, options]);
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      for (const other of list.querySelectorAll("details[open]")) if (other !== details) other.open = false;
    });
    options.addEventListener("change", (event) => {
      const box = event.target;
      if (!(box instanceof HTMLInputElement)) return;
      const values = new Set(state[key]);
      if (box.checked) values.add(box.value);
      else values.delete(box.value);
      state = { ...pruneSelections(dataset.offers, { ...state, [key]: [...values] }, key), page: 1 };
      render(dataset, { keepFocus: `${key}\u0000${box.value}` });
    });
    list.append(element("div", { className: "filter" }, [details]));
  }
  $("clear").addEventListener("click", () => {
    for (const key of FILTER_KEYS) state[key] = [];
    state.page = 1;
    for (const open of list.querySelectorAll("details[open]")) open.open = false;
    render(dataset);
  });
  document.addEventListener("click", (event) => {
    for (const open of list.querySelectorAll("details[open]")) {
      if (!open.contains(event.target)) open.open = false;
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = list.querySelector("details[open]");
    if (!open) return;
    open.open = false;
    open.querySelector("summary").focus();
  });
}

function renderFilters(dataset, keepFocus) {
  for (const key of FILTER_KEYS) {
    const selected = new Set(state[key]);
    const available = choiceValues(matchOffers(dataset.offers, state, key), key);
    const values = [...available, ...state[key].filter((value) => !available.includes(value))];
    const fieldset = $(`${key}-options`);
    const legend = fieldset.querySelector("legend");
    const boxes = values.map((value) =>
      element("label", { className: "option" }, [
        element("input", { type: "checkbox", value, checked: selected.has(value) }),
        element("span", { textContent: value }),
      ]),
    );
    if (!boxes.length) boxes.push(element("p", { className: "empty", textContent: "No options" }));
    fieldset.replaceChildren(legend, ...boxes);
    const count = state[key].length;
    $(`${key}-value`).textContent =
      count === 0 ? blanks[key] || "Any" : count === 1 ? state[key][0] : `${count} selected`;
    $(`${key}-filter`).classList.toggle("active", count > 0);
  }
  $("clear").hidden = !FILTER_KEYS.some((key) => state[key].length);
  if (keepFocus) {
    const [key, value] = keepFocus.split("\u0000");
    const box = [...$(`${key}-options`).querySelectorAll("input")].find((input) => input.value === value);
    box?.focus();
  }
}

// ---- Table -----------------------------------------------------------------

function buildHeaders(dataset) {
  for (const th of document.querySelectorAll("#offers th[data-sort]")) {
    const key = th.dataset.sort;
    const button = element("button", { type: "button", className: "sort" }, [
      th.textContent,
      element("span", { className: "arrow", "aria-hidden": "true" }),
    ]);
    button.addEventListener("click", () => {
      const same = state.sort.key === key;
      state.sort = { key, dir: same && state.sort.dir === "asc" ? "desc" : "asc" };
      state.page = 1;
      render(dataset);
    });
    th.replaceChildren(button);
  }
}

function renderHeaders() {
  for (const th of document.querySelectorAll("#offers th[data-sort]")) {
    const active = th.dataset.sort === state.sort.key;
    th.setAttribute("aria-sort", active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none");
    th.querySelector(".arrow").textContent = active ? (state.sort.dir === "asc" ? "▲" : "▼") : "";
  }
}

function groupRow(group, dataset) {
  const { best, offers } = group;
  const isOpen = expanded.has(group.key);
  const storeContent = element("span", { className: "store-content" }, [storeLink(best)]);
  const store = cell("store", storeContent);
  if (offers.length > 1) {
    const more = offers.length - 1;
    const id = `stores-${group.key.replace(/[^a-z0-9]/gi, "-")}`;
    const toggle = element("button", {
      type: "button",
      className: "more",
      textContent: isOpen ? "Hide other stores" : `+${more} more ${more === 1 ? "store" : "stores"}`,
      "aria-expanded": String(isOpen),
      "aria-controls": id,
    });
    toggle.addEventListener("click", () => {
      if (expanded.has(group.key)) expanded.delete(group.key);
      else expanded.add(group.key);
      render(dataset, { keepToggle: group.key });
    });
    toggle.dataset.group = group.key;
    storeContent.append(element("br"), toggle);
  }
  const row = element("tr", { className: isOpen ? "group open" : "group" }, [
    cell("price", money(best.price_usd)),
    cell("model", shown(best.model)),
    cell("generation", shown(best.generation)),
    cell("size", shown(best.size)),
    cell("color", shown(best.color)),
    store,
    cell("place", place(best)),
    cell("seen", money(best.lowest_seen_usd)),
  ]);
  const rows = [row];
  if (isOpen) {
    const id = `stores-${group.key.replace(/[^a-z0-9]/gi, "-")}`;
    offers.slice(1).forEach((offer, index) => {
      const sub = element("tr", { className: "store-row" }, [
        cell("price", money(offer.price_usd)),
        element("td", { className: "spacer", colSpan: 4 }),
        cell("store", storeLink(offer)),
        cell("place", place(offer)),
        cell("seen", money(offer.lowest_seen_usd)),
      ]);
      if (index === 0) sub.id = id;
      rows.push(sub);
    });
  }
  return rows;
}

function renderPager(info) {
  $("pager").hidden = info.total === 0;
  $("prev").disabled = info.page <= 1;
  $("next").disabled = info.page >= info.pages;
  const last = Math.min(info.start + info.perPage, info.total);
  $("page-label").textContent = `Page ${info.page} of ${info.pages} · ${info.start + 1}–${last} of ${info.total}`;
  $("per-page").value = String(info.perPage);
}

function buildPager(dataset) {
  const select = $("per-page");
  select.replaceChildren(...PAGE_SIZES.map((size) => element("option", { value: String(size), textContent: String(size) })));
  select.addEventListener("change", () => {
    state.perPage = Number(select.value);
    state.page = 1;
    render(dataset);
  });
  const go = (delta) => {
    state.page += delta;
    render(dataset);
    $("result-count").scrollIntoView({ block: "start" });
  };
  $("prev").addEventListener("click", () => go(-1));
  $("next").addEventListener("click", () => go(1));
}

function render(dataset, { keepFocus = "", keepToggle = "" } = {}) {
  const matched = matchOffers(dataset.offers, state);
  const groups = sortGroups(groupOffers(matched), state.sort);
  const info = paginate(groups, state.page, state.perPage);
  state.page = info.page;
  state.perPage = info.perPage;

  renderFilters(dataset, keepFocus);
  renderHeaders();
  $("result-count").textContent = resultLabel(groups.length, matched.length, state.state);
  const table = $("offers");
  table.hidden = groups.length === 0;
  table.classList.toggle("hide-seen", !showLowestSeen(groups));
  table.querySelector("tbody").replaceChildren(...info.items.flatMap((group) => groupRow(group, dataset)));
  if (keepToggle) {
    [...table.querySelectorAll("button.more")].find((button) => button.dataset.group === keepToggle)?.focus();
  }
  renderPager(info);
  const search = serializeState(state);
  if (search !== window.location.search) {
    window.history.replaceState(null, "", `${window.location.pathname}${search}${window.location.hash}`);
  }
}

// ---- Header ----------------------------------------------------------------

function formatUpdated(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `${new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(parsed)} UTC`;
}

function renderFreshness(dataset) {
  $("updated").textContent = `Updated ${formatUpdated(dataset.updated_at)}`;
  $("freshness").hidden = false;
  if (isStale(dataset.updated_at)) {
    const age = ageInDays(dataset.updated_at);
    const days = age === null ? "" : ` ${Math.floor(age)} days ago`;
    $("stale").textContent =
      `These prices were last updated${days}, and the weekly scan has not refreshed them since. ` +
      "Treat them as out of date and check the store page.";
    $("stale").hidden = false;
  }
}

async function main() {
  const response = await fetch("data/prices.json");
  if (!response.ok) throw new Error(`prices.json returned ${response.status}`);
  const dataset = await response.json();
  renderFreshness(dataset);
  $("note").textContent = dataset.note || "";
  buildFilters(dataset);
  buildHeaders(dataset);
  buildPager(dataset);
  render(dataset);
}

main().catch((error) => {
  $("note").textContent = `Could not load prices: ${error.message}`;
});
