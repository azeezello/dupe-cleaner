"""Offline reverse geocoding (задача 20, Р4 link 1).

The tests that matter here are the ones aimed at real places from Aziz's
library plus the exonym cases that broke the first two attempts at
choosing a Russian spelling — a synthetic gazetteer of made-up towns would
have passed every one of those attempts.
"""

from __future__ import annotations

import pytest

from dupecleaner.geocode import (
    MAX_REACH_M,
    MIN_REACH_M,
    NEAR_LIMIT_M,
    Gazetteer,
    Place,
    default_gazetteer_path,
    gazetteer_from_geonames,
    load_gazetteer,
    preferred_name,
    write_gazetteer,
)

# Real coordinates and populations, so "does it answer correctly" is a
# question about places that exist rather than about the test's own data.
DUSHANBE = Place("Душанбе", 38.53575, 68.77905, 679400, "TJ")
KRASNODAR = Place("Краснодар", 45.04484, 38.97603, 899541, "RU")
NOVOSIBIRSK = Place("Новосибирск", 55.0415, 82.9346, 1612833, "RU")
AKADEM = Place("Академгородок", 54.83333, 83.1, 20000, "RU")
VLADIVOSTOK = Place("Владивосток", 43.10562, 131.87353, 604901, "RU")


@pytest.fixture
def gazetteer() -> Gazetteer:
    return Gazetteer([DUSHANBE, KRASNODAR, NOVOSIBIRSK, AKADEM, VLADIVOSTOK])


class TestReach:
    def test_reach_grows_with_the_square_root_of_population(self):
        """The whole justification for the reach formula: a settlement's
        area scales with its population, so its radius scales with the
        root. A city ten times the size is not ten times as wide."""
        small = Place("A", 0, 0, 100_000)
        big = Place("B", 0, 0, 1_000_000)
        assert big.reach_m / small.reach_m == pytest.approx(10**0.5, rel=0.01)

    def test_reach_is_clamped_at_both_ends(self):
        assert Place("Хутор", 0, 0, 200).reach_m == MIN_REACH_M
        assert Place("Мегаполис", 0, 0, 40_000_000).reach_m == MAX_REACH_M

    def test_three_sanity_points(self):
        """15 000 ≈ 1.2 km, a million ≈ 10 km, ten million ≈ 32 km — the
        three checks against reality that fix the constant."""
        assert Place("a", 0, 0, 15_000).reach_m == pytest.approx(2_000, abs=900)
        assert Place("b", 0, 0, 1_000_000).reach_m == pytest.approx(10_000, abs=1_500)
        assert Place("c", 0, 0, 10_000_000).reach_m == pytest.approx(32_000, abs=4_000)


class TestLookup:
    def test_real_city_centres_resolve_to_themselves(self, gazetteer):
        for place in (DUSHANBE, KRASNODAR, NOVOSIBIRSK, VLADIVOSTOK):
            match = gazetteer.lookup(place.latitude, place.longitude)
            assert match is not None and match.inside
            assert match.place.name == place.name

    def test_the_vladivostok_event_from_task_16_lands_in_vladivostok(self, gazetteer):
        """The task-16 report records the centroid of a real event —
        43.115, 131.894 — and says out loud that it is Vladivostok. That
        makes it the one end-to-end assertion in this file whose expected
        answer was written down before this module existed."""
        match = gazetteer.lookup(43.115, 131.894)
        assert match is not None and match.place.name == "Владивосток"

    def test_a_big_city_beats_a_small_town_the_point_is_closer_to(self, gazetteer):
        """A photo 6 km from the centre of Novosibirsk is in Novosibirsk,
        even with a 20 000-person settlement nominally nearer in the list —
        this is why the winner is the deepest inside rather than nearest."""
        match = gazetteer.lookup(55.09, 82.95)
        assert match is not None and match.place.name == "Новосибирск"

    def test_a_small_town_keeps_its_own_name_when_the_photo_is_in_it(self, gazetteer):
        match = gazetteer.lookup(AKADEM.latitude, AKADEM.longitude)
        assert match is not None and match.place.name == "Академгородок"

    def test_equal_depth_goes_to_the_bigger_place(self):
        """GeoNames lists a city and its own districts as separate
        entries, so a photo in the middle of both is equally deep inside
        each. Without this tie-break the district wins on a rounding
        error, and a person says «Париж», not «Paris 04»."""
        city = Place("Париж", 48.8534, 2.3488, 2_138_551)
        district = Place("Paris 04", 48.8565, 2.3575, 27_332)
        gazetteer = Gazetteer([district, city])
        match = gazetteer.lookup(48.86, 2.35)
        assert match is not None and match.place.name == "Париж"

    def test_outside_every_reach_is_surroundings_not_a_claim(self, gazetteer):
        """25 km from Dushanbe with nothing else around: near, not in."""
        match = gazetteer.lookup(38.75, 68.78)
        assert match is not None
        assert not match.inside
        assert match.label == "Душанбе (окрестности)"

    def test_the_surroundings_label_needs_no_grammatical_case(self, gazetteer):
        """«окрестности Академгородок» is wrong Russian and «окрестности
        Академгородка» needs morphology; the bracketed form needs
        neither."""
        match = gazetteer.lookup(38.75, 68.78)
        assert not match.label.startswith("окрестности")

    def test_the_middle_of_nowhere_gets_no_name(self, gazetteer):
        assert gazetteer.lookup(0.0, 0.0) is None
        assert gazetteer.lookup(60.0, 100.0) is None

    def test_nothing_is_found_just_past_the_near_limit(self):
        far = Place("Одинокий", 0.0, 0.0, 15_000)
        gazetteer = Gazetteer([far])
        inside_limit = (NEAR_LIMIT_M - 2_000) / 111_320
        assert gazetteer.lookup(inside_limit, 0.0) is not None
        assert gazetteer.lookup((NEAR_LIMIT_M + 5_000) / 111_320, 0.0) is None

    def test_the_grid_wraps_the_antimeridian(self):
        """A place at 179.9°E must be found from 179.95°W, which is 11 km
        away and two grid columns apart only if the wrap is missing."""
        place = Place("Крайний", 0.0, 179.95, 500_000)
        gazetteer = Gazetteer([place])
        assert gazetteer.lookup(0.0, -179.95) is not None

    def test_empty_gazetteer_answers_nothing_rather_than_raising(self):
        assert Gazetteer([]).lookup(45.0, 39.0) is None


class TestPreferredName:
    """The Russian-spelling rule, on the exact cases that broke its two
    predecessors. `alternates` here are real GeoNames alternate-name
    fragments, trimmed to the candidates that matter.
    """

    def test_a_plain_transliteration_is_taken(self):
        assert preferred_name("Krasnodar", ["Краснодар", "Krasnodar"], cyrillic=True) == (
            "Краснодар"
        )

    def test_a_komi_calque_does_not_win_over_the_russian_name(self):
        """Attempt one ("first Cyrillic string wins") returned «Виль
        Сибиркар» here."""
        assert (
            preferred_name(
                "Novosibirsk",
                ["Виль Сибиркар", "Новосибирск", "Новониколаевск", "Новосибирскай"],
                cyrillic=True,
            )
            == "Новосибирск"
        )

    def test_ukrainian_and_pre_1918_spellings_are_filtered_out(self):
        assert (
            preferred_name(
                "Novosibirsk",
                ["Новосибірськ", "Новосибирьскъ", "Новосибирск"],
                cyrillic=True,
            )
            == "Новосибирск"
        )

    def test_an_exonym_still_gets_its_russian_name(self):
        """Attempt two (closest transliteration of the Latin name) failed
        exactly here: «Москох» transliterates closer to "Moscow" than
        «Москва» does, and no spelling reaches the exact-transliteration
        bar, so consensus between the Cyrillic spellings is what has to
        decide.

        The full real list, not a trimmed one — deliberately, because the
        margin is thin (0.391 for «Москва» against 0.379 for «Москова»)
        and a shortened list inverts it. That the rule leans on the real
        multiplicity of spellings is the honest fact about it.
        """
        assert (
            preferred_name(
                "Moscow",
                ["Мæскуы", "Маскав", "Масква", "Москва", "Москова", "Москох",
                 "Москъва", "Мускав", "Муско", "Мәскеу", "Мәскәү"],
                cyrillic=True,
            )
            == "Москва"
        )

    def test_an_exact_transliteration_beats_a_consensus_of_odd_spellings(self):
        """Consensus alone returns «Алмаато» here (0.433 against 0.426 for
        «Алматы»), because the discarded variants happen to resemble each
        other. "Almaty" transliterates to «Алматы» exactly, and consensus
        does not disagree by enough to overrule that."""
        assert (
            preferred_name(
                "Almaty",
                ["Алма-Ата", "Алмаато", "Алмати", "Алматы", "Верный", "Вірний"],
                cyrillic=True,
            )
            == "Алматы"
        )

    def test_consensus_overrules_an_exact_transliteration_when_it_disagrees_loudly(self):
        """«Парис» transliterates to "Paris" exactly and is still not the
        Russian name; «Париж» is 0.12 more agreed-on across the Cyrillic
        spellings, well past the override margin."""
        assert (
            preferred_name(
                "Paris",
                ["Парис", "Париз", "Париж", "Парижь", "Паріж", "Париж ош"],
                cyrillic=True,
            )
            == "Париж"
        )

    def test_a_close_variant_loses_to_the_full_name(self):
        assert (
            preferred_name("Tbilisi", ["Тбилис", "Тбилиси", "Тбилиси ош"], cyrillic=True)
            == "Тбилиси"
        )

    def test_no_russian_candidate_keeps_the_latin_name(self):
        assert preferred_name("Foça", ["Foca", "Φώκαια"], cyrillic=True) == "Foça"

    def test_cyrillic_off_keeps_the_latin_name(self):
        assert preferred_name("Krasnodar", ["Краснодар"], cyrillic=False) == "Krasnodar"


class TestGazetteerFile:
    def test_round_trip_through_the_file(self, tmp_path):
        path = write_gazetteer([DUSHANBE, KRASNODAR], tmp_path / "g.tsv")
        loaded = load_gazetteer(path)
        assert len(loaded) == 2
        assert loaded.lookup(45.04, 38.98).place.name == "Краснодар"

    def test_a_malformed_line_costs_that_line_and_nothing_else(self, tmp_path):
        path = tmp_path / "g.tsv"
        path.write_text(
            "#dupecleaner-gazetteer\tv1\n"
            "Душанбе\t38.53575\t68.77905\t679400\tTJ\n"
            "сломано\tне-число\t68.0\t100\tXX\n"
            "\n"
            "слишком\tмало\n"
            "Краснодар\t45.04484\t38.97603\t899541\tRU\n",
            encoding="utf-8",
        )
        assert len(load_gazetteer(path)) == 2

    def test_geonames_dump_is_parsed_by_column_position(self, tmp_path):
        """A dump row is 19 tab-separated columns; the parser must take the
        name, coordinates and population from the right ones and ignore
        the rest."""
        dump = tmp_path / "cities.txt"
        columns = [""] * 19
        columns[0] = "1221874"
        columns[1] = "Dushanbe"
        columns[2] = "Dushanbe"
        columns[3] = "Душанбе,Dusanbe,Stalinabad"
        columns[4] = "38.53575"
        columns[5] = "68.77905"
        columns[8] = "TJ"
        columns[14] = "679400"
        dump.write_text("\t".join(columns) + "\n", encoding="utf-8")

        places = gazetteer_from_geonames(dump)
        assert len(places) == 1
        assert places[0].name == "Душанбе"
        assert places[0].population == 679400
        assert places[0].country == "TJ"

    def test_min_population_filters_the_dump(self, tmp_path):
        dump = tmp_path / "cities.txt"
        rows = []
        for name, population in (("Big", "500000"), ("Small", "1200")):
            columns = [""] * 19
            columns[1] = name
            columns[4] = "10.0"
            columns[5] = "20.0"
            columns[14] = population
            rows.append("\t".join(columns))
        dump.write_text("\n".join(rows) + "\n", encoding="utf-8")

        assert len(gazetteer_from_geonames(dump, min_population=10_000)) == 1

    def test_default_path_sits_beside_the_index(self, tmp_path):
        assert default_gazetteer_path(tmp_path / "index.db").parent == tmp_path
