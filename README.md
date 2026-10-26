# Trek Price Finder

Finds the cheapest in-stock Trek bikes at US dealers.

Latest prices for ten popular Trek families:
https://xxiaoweix.github.io/trek-price-finder/

It does not buy anything, and it does not need an account. A full US scan is
slow. The tool waits when a site asks it to slow down.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
bike-price-alerts validate-trek-config
```

Python 3.11 or newer. On Windows, activate `.venv\Scripts\activate` and run
the same commands.

## Scan any Trek model

`scan` looks the model up in Trek's public catalog, then checks shops.

Put the generation in the model name (`Gen 5`). Put a model year in the name
when you want that year (`2026`). `--size` and `--color` are optional. Color
matches the color name, ignoring case and punctuation. Run the command once
per model. A short name that matches several bikes will ask you to be more
specific; one specific name can still cover more than one matching Trek
product, up to four.

`--profile conus` is the contiguous United States. That is the scan that can
report the cheapest verified in-stock price. It takes a long time. `canary`
checks one area around Denver, CO, and is only a sample. `regional`
checks seven cities. A sample is not a US low.

```bash
bike-price-alerts scan --model "Marlin 5 Gen 3" --size 'M (29" wheel)' --profile canary --max-cells 1
bike-price-alerts scan --model "Domane AL 2 Gen 4" --size 52 --profile canary --max-cells 1
bike-price-alerts scan --model "Fuel EX 5 Gen 7" --size M --profile conus
```

The last lines of output are JSON. `lowest` is the cheapest verified in-stock
offer from the scan that just finished: price, shop, city, state, size, color,
and link. `lowest` is empty when the scan found no verified in-stock price.
`coverage_complete` is true only when that profile finished.

Each scan writes `data/scan.sqlite3` and a report under `reports/scan/`.
Those files stay on your machine.

`examples/trek.yaml` holds the map profiles. Its one bike is a placeholder
named Example Bike. A real model name does not use that placeholder.

## A saved price file

`config/trek/top-sellers.yaml` is a fixed list of Trek US bikes, with a public
web page cited for each family. `price-board` scans that list and writes
`site/data/prices.json`.

```bash
bike-price-alerts price-board --profile canary --max-cells 1 --out site/data/prices.json
bike-price-alerts price-board --profile conus --out site/data/prices.json
```

Repeat `--watch SOME_KEY` to scan one or more bikes from that file instead of
the whole list. Keys are the `key:` lines in the catalog.

`site/index.html` is a static page. Open it from the `site/` folder after the
JSON file exists. The page reads only that file. The published page refreshes
weekly via GitHub Actions. Filters on the page are
model, generation, size, color, and state. It does not contact Trek or Locally.

## Configuration

| File | What it is |
| --- | --- |
| `examples/trek.yaml` | Company id and the canary, regional, and conus map profiles |
| `config/trek/top-sellers.yaml` | The bikes `price-board` scans, with the public page each family came from |

`TRACKER_ENABLED=false` makes every command exit before any network request.
`LOCALLY_API_TOKEN` is optional and unset by default. The normal scan uses
public pages and does not need it.
