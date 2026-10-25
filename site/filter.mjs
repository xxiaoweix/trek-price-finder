export function choiceValues(offers, key) {
  return [...new Set(offers.map((offer) => offer[key]).filter((value) => value != null && value !== ""))]
    .map(String)
    .sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
}

export function filterOffers(offers, query) {
  const selected = offers.filter((offer) => {
    if (query.state && offer.state !== query.state) return false;
    if (query.model && offer.model !== query.model) return false;
    if (query.generation && offer.generation !== query.generation) return false;
    if (query.size && String(offer.size) !== query.size) return false;
    if (query.color && offer.color !== query.color) return false;
    return true;
  });
  return selected.sort((left, right) => {
    const price = Number(left.price_usd) - Number(right.price_usd);
    if (price !== 0) return price;
    return String(left.retailer || "").localeCompare(String(right.retailer || ""));
  });
}

export function resultLabel(count, state) {
  if (!count) {
    return state ? `No in-stock price in ${state}` : "No in-stock offers match these filters";
  }
  const offers = count === 1 ? "offer" : "offers";
  const where = state ? ` in ${state}` : "";
  return `${count} in-stock ${offers}${where}, cheapest first`;
}
