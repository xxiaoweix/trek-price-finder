from pathlib import Path

import pytest

from bike_price_alerts.brands.trek.config import ConfigError, load_config


def test_rejects_untrusted_public_url(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
version: 2
locally:
  company_id: 3480
  center_lat: 39
  center_lng: -98
  radius_miles: 100
  page_limit: 100
scan:
  default_profile: test
  profiles:
    test:
      coverage: points
      points:
        - {label: Test, lat: 39, lng: -98, distance_diag: 100}
watches:
  - key: bad
    canonical_name: Bad
    aliases: [Bad]
    allowed_sizes: ["M"]
    baseline_usd: 1
    public_urls:
      - source: bikeexchange
        url: https://evil.example/item
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must use https"):
        load_config(config)


def test_rejects_non_positive_baseline(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
version: 2
locally:
  company_id: 3480
  center_lat: 39
  center_lng: -98
  radius_miles: 100
scan:
  default_profile: test
  profiles:
    test:
      coverage: points
      points:
        - {label: Test, lat: 39, lng: -98, distance_diag: 100}
watches:
  - key: bad
    canonical_name: Bad
    aliases: [Bad]
    allowed_sizes: ["M"]
    baseline_usd: 0
    locally:
      variants:
        - {upc: "123456789012", product_id: 1, style: "1", size: "M"}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="greater than zero"):
        load_config(config)


def test_loads_valid_widget_point():
    config = load_config("tests/fixtures/parser-config.yaml")

    point = config.scan_profile("canary").points[0]
    assert point.label == "Denver, CO"
    assert point.lat == pytest.approx(39.7525)
    assert point.distance_diag == 100


def test_example_config_loads():
    config = load_config("examples/trek.yaml")

    assert config.locally_company_id == 3480
    assert config.scan_profile("conus").coverage == "conus"
    assert config.watches[0].key == "example_bike"
    assert "Example Bike" in config.watches[0].aliases
