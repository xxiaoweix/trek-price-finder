from bike_price_alerts.brands.trek.price_board import (
    build_dataset,
    display_model,
    publication_blockers,
)


def test_display_model_drops_brand_and_generation():
    assert display_model("Trek Marlin 5 Gen 3") == "Marlin 5"
    assert display_model("Trek Fuel EX 5 Gen 7") == "Fuel EX 5"


def test_dataset_keeps_verified_stock_and_refuses_a_canary_national_claim():
    dataset = build_dataset(
        [
            {
                "canonical_name": "Trek Marlin 5 Gen 3",
                "generation": "Gen 3",
                "size": 'M (29" wheel)',
                "color": "Miami Green",
                "sale_price_usd": "900.00",
                "in_stock": True,
                "size_verified": True,
                "stock_verified": True,
                "retailer": "Higher",
                "city": "Chicago",
                "state": "IL",
                "url": "https://trek.locally.com/higher",
            },
            {
                "canonical_name": "Trek Marlin 5 Gen 3",
                "generation": "Gen 3",
                "size": 'M (29" wheel)',
                "color": "Miami Green",
                "sale_price_usd": "800.00",
                "in_stock": True,
                "size_verified": True,
                "stock_verified": True,
                "retailer": "Mead's Bike Shop",
                "city": "Sterling",
                "state": "IL",
                "url": "https://trek.locally.com/low",
            },
            {
                "canonical_name": "Trek Marlin 5 Gen 3",
                "sale_price_usd": "1000.00",
                "in_stock": False,
                "size_verified": True,
                "stock_verified": True,
                "retailer": "Gone",
                "url": "https://trek.locally.com/gone",
            },
        ],
        updated_at="2026-10-24T18:00:00+00:00",
        profile="canary",
        complete=True,
    )

    assert dataset["nationwide"] is False
    assert dataset["offer_count"] == 2
    assert dataset["offers"][0]["price_usd"] == "800.00"
    assert dataset["offers"][0]["model"] == "Marlin 5"
    assert dataset["offers"][0]["generation"] == "Gen 3"
    assert dataset["offers"][0]["city"] == "Sterling"
    assert "year" not in dataset["offers"][0]
    assert "not a nationwide low" in dataset["note"]
    assert dataset["skipped_offer_count"] == 0


def _healthy(**overrides):
    gate = {
        "cells": {"complete": 100, "subdivided": 4},
        "offers": {"complete": 1000, "failed": 1, "ignored": 2},
        "by_model": {"slash": {"complete": 4, "failed": 1}},
        "by_state": {"KS": {"complete": 40, "failed": 1, "ignored": 2}},
        "missing_states": [],
        "unpriced_stocked_pairs": 1,
        "priced_offer_count": 1000,
        "require_conus_states": True,
    }
    gate.update(overrides)
    return gate


def test_one_failed_offer_is_skipped_and_does_not_block_publication():
    """Wichita Slash 9.8 was 1 failed offer out of 24,278 attempts."""

    wichita = _healthy(
        cells={"complete": 5643, "subdivided": 342},
        offers={"complete": 24259, "failed": 1, "ignored": 18},
        by_model={"slash_9_8_xt_di2_gen_6_52582": {"complete": 4, "failed": 1}},
        by_state={"KS": {"complete": 548, "failed": 1, "ignored": 18}},
        unpriced_stocked_pairs=1,
        priced_offer_count=24259,
    )

    assert publication_blockers(**wichita) == []
    dataset = build_dataset(
        [
            {
                "canonical_name": "Slash 9.8 XT Di2 Gen 6",
                "generation": "Gen 6",
                "size": "M",
                "color": "Black",
                "sale_price_usd": "4999.99",
                "in_stock": True,
                "size_verified": True,
                "stock_verified": True,
                "retailer": "Other Shop",
                "city": "Austin",
                "state": "TX",
                "url": "https://trek.locally.com/ok",
                "upc": "197216206521",
            }
        ],
        updated_at="2026-10-25T07:35:56+00:00",
        profile="conus",
        complete=True,
        skipped_offers=[
            {
                "model_key": "slash_9_8_xt_di2_gen_6_52582",
                "upc": "197216206514",
                "store_id": 58437,
                "retailer": "Bicycle X-Change Shops",
                "city": "Wichita",
                "state": "KS",
                "reason": "isolated store 58437 returned a non-target size",
            }
        ],
    )

    assert dataset["nationwide"] is True
    assert dataset["offer_count"] == 1
    assert dataset["skipped_offer_count"] == 1
    assert dataset["skipped_offers"][0]["upc"] == "197216206514"
    assert dataset["offers"][0]["upc"] == "197216206521"
    assert "1 offer was skipped" in dataset["note"]


def test_widespread_offer_failures_block_publication():
    blockers = publication_blockers(
        **_healthy(offers={"complete": 90, "failed": 10}, unpriced_stocked_pairs=10)
    )

    assert any("above 1% of attempted offers" in blocker for blocker in blockers)


def test_a_model_or_state_that_only_fails_blocks_publication():
    blockers = publication_blockers(
        **_healthy(
            by_model={
                "slash": {"complete": 4, "failed": 1},
                "madone_slr": {"failed": 3},
            },
            by_state={"KS": {"complete": 10, "failed": 1}, "WY": {"failed": 2}},
        )
    )

    assert any(blocker.startswith("model madone_slr failed outright") for blocker in blockers)
    assert any(blocker.startswith("state WY failed outright") for blocker in blockers)


def test_unfinished_coverage_a_missing_state_or_an_empty_file_still_blocks():
    assert any(
        "map coverage failed outright" in blocker
        for blocker in publication_blockers(**_healthy(cells={"failed": 8}, priced_offer_count=1))
    )
    assert any(
        "coverage did not finish" in blocker
        for blocker in publication_blockers(
            **_healthy(cells={"complete": 10, "planned": 2})
        )
    )
    assert any(
        "missing states: WY" in blocker
        for blocker in publication_blockers(**_healthy(missing_states=["WY"]))
    )
    assert any(
        "no verified prices" in blocker
        for blocker in publication_blockers(**_healthy(priced_offer_count=0, offers={"ignored": 4}, unpriced_stocked_pairs=0))
    )
    assert any(
        "never priced" in blocker
        for blocker in publication_blockers(**_healthy(unpriced_stocked_pairs=6))
    )
