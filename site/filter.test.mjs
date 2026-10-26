import assert from "node:assert/strict";
import test from "node:test";

import {
  choiceValues,
  compareSize,
  emptyState,
  familyOf,
  filterOffers,
  groupOffers,
  isStale,
  matchOffers,
  paginate,
  parseState,
  pruneSelections,
  resultLabel,
  serializeState,
  showLowestSeen,
  sortGroups,
} from "./filter.mjs";

const offer = (fields) => ({
  generation: "Gen 3",
  size: "M",
  color: "Miami Green",
  city: "Town",
  state: "CA",
  lowest_seen_usd: fields.price_usd,
  ...fields,
});

const offers = [
  offer({ model: "Marlin 5", price_usd: "900.00", retailer: "Higher", upc: "1" }),
  offer({ model: "Marlin 5", price_usd: "800.00", retailer: "Mead's", upc: "1" }),
  offer({ model: "Marlin 5", price_usd: "800.00", retailer: "Acme", upc: "1", state: "OR" }),
  offer({ model: "Marlin 5", size: "S", price_usd: "799.00", retailer: "Zed", upc: "3" }),
  offer({
    model: "Fuel EX 5",
    generation: "Gen 7",
    color: "Wild Mushroom",
    price_usd: "2999.99",
    retailer: "Village",
    state: "NY",
    upc: "2",
  }),
  offer({ model: "Domane AL 2", generation: null, size: "56", price_usd: "1099.99", retailer: "Spoke", state: "NY", upc: "4" }),
];

test("family is the leading word, with two-word families kept whole", () => {
  assert.equal(familyOf("Marlin 5"), "Marlin");
  assert.equal(familyOf("Fuel EX 9.8 XT Di2"), "Fuel EX");
  assert.equal(familyOf("Fuel EXe 9.7"), "Fuel EXe");
  assert.equal(familyOf("Top Fuel 8"), "Top Fuel");
  assert.equal(familyOf("Madone SLR 9 AXS - No. 76 ICON"), "Madone");
  assert.equal(familyOf("  Checkpoint   ALR 5 "), "Checkpoint");
  assert.equal(familyOf(""), "");
  assert.equal(familyOf(undefined), "");
});

test("filters by single values, cheapest first, ties by store", () => {
  const matched = filterOffers(offers, { model: "Marlin 5", generation: "Gen 3", size: "M", color: "Miami Green" });
  assert.deepEqual(matched.map((o) => o.retailer), ["Acme", "Mead's", "Higher"]);
  assert.equal(filterOffers(offers, {}).length, offers.length);
  assert.equal(filterOffers(offers, { state: "WY" }).length, 0);
});

test("multi-select matches any value within a filter and all filters together", () => {
  assert.equal(matchOffers(offers, { state: ["CA", "OR"] }).length, 4);
  assert.equal(matchOffers(offers, { state: ["CA", "OR"], size: ["S"] }).length, 1);
  assert.equal(matchOffers(offers, { family: ["Fuel EX", "Domane"] }).length, 2);
  assert.equal(matchOffers(offers, { family: [] }).length, offers.length);
  assert.equal(matchOffers(offers, { generation: ["Gen 3"] }).length, 4);
});

test("cascading options ignore their own filter and sort sizes by fit", () => {
  const query = { model: ["Marlin 5"], size: ["S"] };
  assert.deepEqual(choiceValues(matchOffers(offers, query, "size"), "size"), ["S", "M"]);
  assert.deepEqual(choiceValues(offers, "family"), ["Domane", "Fuel EX", "Marlin"]);
  assert.deepEqual(choiceValues(offers, "generation"), ["Gen 3", "Gen 7"]);
  assert.deepEqual(["XL", "M", "56", "XS (27.5\" wheel)", "50", "ML"].sort(compareSize), [
    "50",
    "56",
    "XS (27.5\" wheel)",
    "M",
    "ML",
    "XL",
  ]);
});

test("pruning only drops later bike filters that no longer match", () => {
  const query = { family: ["Marlin", "Fuel EX"], model: ["Fuel EX 5"], size: ["S", "M"], state: ["CA", "NY"] };
  const pruned = pruneSelections(offers, query, "model");
  assert.deepEqual(pruned.family, ["Marlin", "Fuel EX"]);
  assert.deepEqual(pruned.model, ["Fuel EX 5"]);
  assert.deepEqual(pruned.size, ["M"]);
  assert.deepEqual(pruned.state, ["CA", "NY"]);
  assert.deepEqual(pruneSelections(offers, { family: ["Marlin"], size: ["56"] }, "size").family, ["Marlin"]);
  assert.deepEqual(pruneSelections(offers, { model: ["Marlin 5"], size: ["56"] }, "state").size, ["56"]);
});

test("groups one row per configuration with the cheapest store first", () => {
  const groups = groupOffers(offers);
  assert.equal(groups.length, 4);
  const marlinM = groups.find((g) => g.best.model === "Marlin 5" && g.best.size === "M");
  assert.equal(marlinM.best.retailer, "Acme");
  assert.deepEqual(marlinM.offers.map((o) => o.retailer), ["Acme", "Mead's", "Higher"]);
});

test("sorts groups by any column in either direction", () => {
  const groups = groupOffers(offers);
  const prices = (list) => list.map((g) => g.best.price_usd);
  assert.deepEqual(prices(sortGroups(groups)), ["799.00", "800.00", "1099.99", "2999.99"]);
  assert.deepEqual(prices(sortGroups(groups, { key: "price", dir: "desc" })), ["2999.99", "1099.99", "800.00", "799.00"]);
  assert.deepEqual(
    sortGroups(groups, { key: "model", dir: "asc" }).map((g) => g.best.model),
    ["Domane AL 2", "Fuel EX 5", "Marlin 5", "Marlin 5"],
  );
  assert.deepEqual(
    sortGroups(groups, { key: "size", dir: "asc" }).map((g) => g.best.size),
    ["56", "S", "M", "M"],
  );
  assert.deepEqual(
    sortGroups(groups, { key: "place", dir: "desc" }).map((g) => g.best.state),
    ["OR", "NY", "NY", "CA"],
  );
  assert.equal(sortGroups(groups, { key: "bogus", dir: "asc" })[0].best.price_usd, "799.00");
});

test("paginates and clamps the page", () => {
  const items = Array.from({ length: 120 }, (_, i) => i);
  const first = paginate(items, 1, 50);
  assert.deepEqual([first.items.length, first.pages, first.page], [50, 3, 1]);
  const last = paginate(items, 99, 50);
  assert.deepEqual([last.items.length, last.page, last.start], [20, 3, 100]);
  assert.equal(paginate(items, 1, 7).perPage, 50);
  assert.deepEqual([paginate([], 3, 25).page, paginate([], 3, 25).pages], [1, 1]);
});

test("lowest seen shows only when a cheapest offer was cheaper before", () => {
  assert.equal(showLowestSeen(groupOffers(offers)), false);
  const history = [...offers, offer({ model: "Roscoe 7", price_usd: "1500", lowest_seen_usd: "1299.99", retailer: "R" })];
  assert.equal(showLowestSeen(groupOffers(history)), true);
});

test("URL state round-trips multi-select values with commas and quotes", () => {
  const state = emptyState();
  state.model = ["Marlin 5", "Domane AL 2"];
  state.size = ['XS (27.5" wheel)'];
  state.color = ["Red, White & Blue"];
  state.sort = { key: "model", dir: "desc" };
  state.page = 3;
  state.perPage = 100;
  const search = serializeState(state);
  assert.equal(search.includes("Red%2C%20White%20%26%20Blue"), true);
  assert.deepEqual(parseState(search), state);
  assert.equal(serializeState(emptyState()), "");
});

test("URL parsing accepts old single-value links and ignores junk", () => {
  const state = parseState("?model=Marlin+5&state=CA,NY,,CA&sort=-nope&page=-4&per=7&x=1&color=%E0%A4%A");
  assert.deepEqual(state.model, ["Marlin 5"]);
  assert.deepEqual(state.state, ["CA", "NY"]);
  assert.deepEqual(state.sort, { key: "price", dir: "asc" });
  assert.equal(state.page, 1);
  assert.equal(state.perPage, 50);
  assert.deepEqual(state.color, []);
});

test("data older than eight days is stale", () => {
  const now = Date.parse("2026-10-25T12:00:00Z");
  assert.equal(isStale("2026-10-20T00:00:00Z", now), false);
  assert.equal(isStale("2026-10-16T00:00:00Z", now), true);
  assert.equal(isStale("not a date", now), true);
});

test("result label counts configurations and offers and names states", () => {
  assert.equal(resultLabel(0, 0, ["RI"]), "No in-stock price in RI");
  assert.equal(resultLabel(0, 0, []), "No in-stock offers match these filters");
  assert.equal(resultLabel(575, 24259, []), "575 configurations from 24,259 in-stock offers");
  assert.equal(resultLabel(1, 1, ["RI", "CT", "MA"]), "1 configuration from 1 in-stock offer in RI, CT or MA");
});
