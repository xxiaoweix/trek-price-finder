import { choiceValues, filterOffers, resultLabel } from "./filter.mjs";

const filters = ["model", "generation", "size", "color", "state"];
const labels = {
  model: "Model",
  generation: "Gen",
  size: "Size",
  color: "Color",
  state: "State",
};
const blanks = {
  model: "Any",
  generation: "Any",
  size: "Any",
  color: "Any",
  state: "All states",
};
const query = { model: "", generation: "", size: "", color: "", state: "" };

function money(value) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(value));
}

function place(offer) {
  return [offer.city, offer.state].filter(Boolean).join(", ");
}

function shown(value) {
  const text = value == null ? "" : String(value).trim();
  return text || "—";
}

function withCurrent(values, current) {
  if (!current || values.includes(current)) return values;
  return [...values, current].sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
}

function fillSelect(select, values, current, blankLabel) {
  const shown = withCurrent(values, current);
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = blankLabel;
  select.replaceChildren(blank);
  for (const value of shown) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  select.value = current && shown.includes(current) ? current : "";
}

function poolFor(offers, except) {
  return offers.filter((offer) => {
    if (except !== "model" && query.model && offer.model !== query.model) return false;
    if (except !== "generation" && query.generation && offer.generation !== query.generation) return false;
    if (except !== "size" && query.size && String(offer.size) !== query.size) return false;
    if (except !== "color" && query.color && offer.color !== query.color) return false;
    if (except !== "state" && query.state && offer.state !== query.state) return false;
    return true;
  });
}

function render(dataset) {
  const matched = filterOffers(dataset.offers, query);
  for (const key of filters) {
    fillSelect(
      document.getElementById(key),
      choiceValues(poolFor(dataset.offers, key), key),
      query[key],
      blanks[key],
    );
  }
  const summary = document.getElementById("result-count");
  summary.textContent = resultLabel(matched.length, query.state);
  const table = document.getElementById("offers");
  table.hidden = matched.length === 0;
  const body = table.querySelector("tbody");
  body.replaceChildren();
  for (const offer of matched) {
    const row = document.createElement("tr");
    const link = document.createElement("a");
    link.href = offer.url;
    link.textContent = offer.retailer || "Store";
    link.rel = "noreferrer";
    const store = document.createElement("td");
    store.className = "store";
    store.append(link);
    const cells = [
      ["price", money(offer.price_usd)],
      ["model", shown(offer.model)],
      ["gen", shown(offer.generation)],
      ["size", shown(offer.size)],
      ["color", shown(offer.color)],
      store,
      ["place", place(offer) || "—"],
      ["seen", offer.lowest_seen_usd ? money(offer.lowest_seen_usd) : "—"],
    ];
    for (const cell of cells) {
      if (cell instanceof HTMLElement) {
        row.append(cell);
        continue;
      }
      const td = document.createElement("td");
      td.className = cell[0];
      td.textContent = cell[1];
      row.append(td);
    }
    body.append(row);
  }
}

function formatUpdated(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `${new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(parsed)} UTC`;
}

async function main() {
  const response = await fetch("data/prices.json");
  if (!response.ok) throw new Error(`prices.json returned ${response.status}`);
  const dataset = await response.json();
  const params = new URLSearchParams(window.location.search);
  for (const key of filters) {
    const value = params.get(key);
    if (value) query[key] = value;
  }
  document.getElementById("updated").textContent = `Updated ${formatUpdated(dataset.updated_at)}`;
  document.getElementById("note").textContent = dataset.note || "";
  const form = document.getElementById("filters");
  for (const key of filters) {
    const label = document.createElement("label");
    label.textContent = labels[key];
    const select = document.createElement("select");
    select.id = key;
    select.addEventListener("change", () => {
      query[key] = select.value;
      if (key === "model") {
        query.generation = "";
        query.size = "";
        query.color = "";
      } else if (key === "generation") {
        query.size = "";
        query.color = "";
      } else if (key === "size") {
        query.color = "";
      }
      render(dataset);
    });
    label.append(select);
    form.append(label);
  }
  render(dataset);
}

main().catch((error) => {
  document.getElementById("note").textContent = `Could not load prices: ${error.message}`;
});
