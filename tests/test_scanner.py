from dataclasses import replace

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.scanner import scan


class Response:
    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url


class FixtureHTTP:
    def __init__(self, html: str):
        self.html = html

    def get(self, url, *, params=None, headers=None):
        return Response(self.html, url)


def test_public_scan_runs_without_token(monkeypatch, fixture_dir):
    monkeypatch.delenv("LOCALLY_API_TOKEN", raising=False)
    config = load_config("tests/fixtures/parser-config.yaml")
    one_watch = replace(
        config,
        watches=(next(x for x in config.watches if x.key == "domane_al_2_gen_4_80164"),),
        default_scan_profile="conus",
    )
    html = (fixture_dir / "bikeexchange_domane_al_2.html").read_text()
    result = scan(one_watch, FixtureHTTP(html))
    assert result.listings
    assert not result.errors
    assert "enumeration skipped" in result.notes[0]


def test_public_source_failure_is_recorded(monkeypatch):
    monkeypatch.delenv("LOCALLY_API_TOKEN", raising=False)
    config = load_config("tests/fixtures/parser-config.yaml")

    class FailedHTTP:
        def get(self, url, *, params=None, headers=None):
            raise TimeoutError("bounded failure")

    result = scan(config, FailedHTTP())
    assert not result.listings
    widget_scans = sum(
        len(watch.locally.variants)
        for watch in config.watches
        if (
            config.scan_profile().points
            and watch.locally.variants
        )
    )
    assert len(result.errors) == (
        sum(len(watch.public_urls) for watch in config.watches) + widget_scans
    )
