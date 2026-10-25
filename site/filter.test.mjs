import assert from "node:assert/strict";
import test from "node:test";

import { choiceValues, filterOffers, resultLabel } from "./filter.mjs";

const offers = [
  {
    model: "Marlin 5",
    generation: "Gen 3",
    size: "M",
    color: "Miami Green",
    price_usd: "900.00",
    retailer: "Higher",
    state: "CA",
    upc: "1",
  },
  {
    model: "Marlin 5",
    generation: "Gen 3",
    size: "M",
    color: "Miami Green",
    price_usd: "800.00",
    retailer: "Mead's",
    state: "CA",
    upc: "1",
  },
  {
    model: "Fuel EX 5",
    generation: "Gen 7",
    size: "M",
    color: "Wild Mushroom",
    price_usd: "2999.99",
    retailer: "Village",
    state: "NY",
    upc: "2",
  },
];

test("filters by model, generation, size, and color, cheapest first", () => {
  const matched = filterOffers(offers, {
    model: "Marlin 5",
    generation: "Gen 3",
    size: "M",
    color: "Miami Green",
  });

  assert.deepEqual(matched.map((offer) => offer.retailer), ["Mead's", "Higher"]);
  assert.deepEqual(choiceValues(offers, "model"), ["Fuel EX 5", "Marlin 5"]);
});

test("an empty filter keeps every offer", () => {
  assert.equal(filterOffers(offers, {}).length, 3);
});

test("a state keeps only that state's offers, cheapest first", () => {
  const matched = filterOffers(offers, { state: "CA", model: "Marlin 5" });

  assert.deepEqual(matched.map((offer) => offer.retailer), ["Mead's", "Higher"]);
  assert.deepEqual(choiceValues(offers, "state"), ["CA", "NY"]);
  assert.equal(filterOffers(offers, { state: "WY" }).length, 0);
});

test("the empty label names the selected state", () => {
  assert.equal(resultLabel(0, "RI"), "No in-stock price in RI");
  assert.equal(resultLabel(2, "RI"), "2 in-stock offers in RI, cheapest first");
  assert.equal(resultLabel(1, ""), "1 in-stock offer, cheapest first");
  assert.equal(resultLabel(0, ""), "No in-stock offers match these filters");
});
