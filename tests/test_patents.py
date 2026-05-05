"""Tests for the patents from Patents.csv (Phase 2 patent system).

Each patent's mechanical effect is wired up via hooks in simulation.py
keyed on the patent's name. These tests construct patents directly and
exercise the relevant hook end-to-end.
"""

from pathlib import Path

from my_project.models import Card, Resource, ResourceAmount
from my_project.parsing import parse_cards, parse_contracts, parse_patents
from my_project.simulation import (
    GameState,
    PATENT_BUILD_HOOKS,
    Player,
    _apply_patent_acquisition,
    _bump_rate,
    _player_owns_patent,
    building_tags,
    do_debt_collection,
    do_power_bill,
    execute_build,
    settle_silent_auction,
)


DATA = Path(__file__).resolve().parent.parent / "my_project" / "data"


def _load():
    return parse_cards(DATA / "Cards.csv"), parse_contracts(DATA / "Contracts.csv")


def _patent(name: str) -> Card:
    """Construct a slot-5 patent card by name. Empty rates/effect — the
    mechanical effect is fired by name in the build/event hooks."""
    return Card(
        alternate="Patent",
        slot=5,
        building=name,
        costs=[],
        rates=[],
        effect="",
        can_sell=[],
        can_fulfill_contract=False,
    )


def _build_card(
    name: str,
    *,
    costs: list[tuple[Resource, int]] | None = None,
    rates: list[tuple[Resource, int]] | None = None,
) -> Card:
    """Construct a buildable Card with explicit costs and rates."""
    return Card(
        alternate="C/SI",
        slot=1,
        building=name,
        costs=[ResourceAmount(resource=r, amount=a) for r, a in (costs or [])],
        rates=[ResourceAmount(resource=r, amount=a) for r, a in (rates or [])],
        effect="",
        can_sell=[],
        can_fulfill_contract=False,
    )


# --- Building tags helper ---


class TestBuildingTags:
    def test_empty_card_has_no_tags(self):
        card = _build_card("Empty")
        assert building_tags(card) == set()

    def test_single_positive_rate_has_one_tag(self):
        card = _build_card("Solar", rates=[(Resource.PWR, 2)])
        assert building_tags(card) == {"power"}

    def test_negative_rates_do_not_tag(self):
        card = _build_card("Drain", rates=[(Resource.PWR, -1)])
        assert building_tags(card) == set()

    def test_multiple_positive_rates_yield_multiple_tags(self):
        card = _build_card("Hybrid", rates=[(Resource.PWR, 1), (Resource.FE, 1)])
        assert building_tags(card) == {"power", "iron"}

    def test_mixed_positive_and_negative(self):
        card = _build_card(
            "Mixed",
            rates=[(Resource.PWR, 1), (Resource.H2O, -1), (Resource.FE, 2)],
        )
        # Only positive rates count toward tags
        assert building_tags(card) == {"power", "iron"}


# --- Patent ownership helpers ---


class TestPatentHelpers:
    def test_player_owns_patent_true(self):
        player = Player(name="P")
        player.buildings_played.append(_patent("Superconductors"))
        assert _player_owns_patent(player, "Superconductors")

    def test_player_owns_patent_false(self):
        player = Player(name="P")
        assert not _player_owns_patent(player, "Superconductors")

    def test_player_owns_patent_distinguishes_from_buildings(self):
        """A slot-1 building with the same name doesn't count as patent ownership."""
        player = Player(name="P")
        fake = _build_card("Superconductors")  # slot=1, not a patent
        player.buildings_played.append(fake)
        assert not _player_owns_patent(player, "Superconductors")


# --- Test fixture: a player with patents in a real game state ---


def _setup_player_with_patent(patent_name: str) -> tuple[GameState, Player]:
    """Create a 3-player game and give player 0 the named patent."""
    cards, contracts = _load()
    state = GameState.create(cards, contracts, num_players=3)
    p = state.players[0]
    p.buildings_played.append(_patent(patent_name))
    # Give the player plenty of money so build affordability isn't a concern
    p.money = 100
    return state, p


def _execute_single_build(state: GameState, player: Player, card: Card) -> Card:
    """Put `card` in player's hand at index 0, build it, and return the
    Card object that landed in buildings_played (so tests can inspect the
    rates that were actually applied)."""
    player.hand = [card]
    record = execute_build(state, player, build_indices=[0])
    assert record is not None, "build failed"
    # The newly-appended card is the last one in buildings_played that isn't
    # the patent we set up earlier.
    return player.buildings_played[-1]


# --- Superconductors ---


class TestSuperconductors:
    def test_bumps_pwr_on_power_building(self):
        state, p = _setup_player_with_patent("Superconductors")
        card = _build_card("Solar", rates=[(Resource.PWR, 2)])
        pwr_before = p.rate(Resource.PWR)
        built = _execute_single_build(state, p, card)
        # Superconductors adds +1 PWR → built card now reads as +3 PWR
        assert any(ra.resource == Resource.PWR and ra.amount == 3 for ra in built.rates)
        # Player's actual rate should reflect +3 (not +2)
        assert p.rate(Resource.PWR) == pwr_before + 3

    def test_does_not_fire_on_non_power_building(self):
        state, p = _setup_player_with_patent("Superconductors")
        card = _build_card("Iron Mine", rates=[(Resource.FE, 2)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        # No PWR rate change
        assert p.rate(Resource.PWR) == pwr_before

    def test_fires_on_dual_tagged_card(self):
        """A card tagged power AND iron still gets the +1 PWR from Superconductors."""
        state, p = _setup_player_with_patent("Superconductors")
        card = _build_card("Hybrid", rates=[(Resource.PWR, 1), (Resource.FE, 1)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before + 2  # was +1, now +2

    def test_does_not_corrupt_card_prototype(self):
        """The shared Card prototype in the deck must NOT be mutated."""
        state, p = _setup_player_with_patent("Superconductors")
        card = _build_card("Solar", rates=[(Resource.PWR, 2)])
        # Keep a reference and a snapshot of the original rates
        original_rates_snapshot = [(ra.resource, ra.amount) for ra in card.rates]
        _execute_single_build(state, p, card)
        # The original prototype's rates should be unchanged
        actual = [(ra.resource, ra.amount) for ra in card.rates]
        assert actual == original_rates_snapshot


# --- Cold Fusion ---


class TestColdFusion:
    def test_bumps_pwr_on_water_building(self):
        state, p = _setup_player_with_patent("Cold Fusion")
        card = _build_card("Aquifer", rates=[(Resource.H2O, 1)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        # Card had no PWR; gets +1 PWR added
        assert p.rate(Resource.PWR) == pwr_before + 1

    def test_does_not_fire_on_non_water(self):
        state, p = _setup_player_with_patent("Cold Fusion")
        card = _build_card("Iron Mine", rates=[(Resource.FE, 2)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before

    def test_stacks_with_existing_pwr(self):
        """A card with both H2O and existing PWR gets +1 to its PWR rate."""
        state, p = _setup_player_with_patent("Cold Fusion")
        card = _build_card("WaterPower", rates=[(Resource.H2O, 1), (Resource.PWR, 1)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before + 2  # 1 from card, +1 from Cold Fusion


# --- Slant Drilling ---


class TestSlantDrilling:
    def test_bumps_fe_on_iron_building(self):
        state, p = _setup_player_with_patent("Slant Drilling")
        card = _build_card("Iron Mine", rates=[(Resource.FE, 1)])
        fe_before = p.rate(Resource.FE)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.FE) == fe_before + 2  # +1 from card, +1 from Slant Drilling

    def test_bumps_si_on_silicon_building(self):
        state, p = _setup_player_with_patent("Slant Drilling")
        card = _build_card("Quartz Mine", rates=[(Resource.SI, 1)])
        si_before = p.rate(Resource.SI)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.SI) == si_before + 2

    def test_does_not_fire_on_other_buildings(self):
        state, p = _setup_player_with_patent("Slant Drilling")
        card = _build_card("Solar", rates=[(Resource.PWR, 2)])
        fe_before = p.rate(Resource.FE)
        si_before = p.rate(Resource.SI)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.FE) == fe_before
        assert p.rate(Resource.SI) == si_before

    def test_dual_tag_gets_both_bumps(self):
        """A card producing both FE and SI gets +1 to each."""
        state, p = _setup_player_with_patent("Slant Drilling")
        card = _build_card("Combo", rates=[(Resource.FE, 1), (Resource.SI, 1)])
        fe_before = p.rate(Resource.FE)
        si_before = p.rate(Resource.SI)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.FE) == fe_before + 2
        assert p.rate(Resource.SI) == si_before + 2


# --- Perpetual Motion ---


class TestPerpetualMotion:
    def test_strips_negative_pwr_on_water_building(self):
        state, p = _setup_player_with_patent("Perpetual Motion")
        card = _build_card(
            "PowerHungryWater",
            rates=[(Resource.H2O, 2), (Resource.PWR, -1)],
        )
        pwr_before = p.rate(Resource.PWR)
        h2o_before = p.rate(Resource.H2O)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before  # -1 PWR was stripped
        assert p.rate(Resource.H2O) == h2o_before + 2  # H2O still applied

    def test_strips_negative_pwr_on_iron_building(self):
        state, p = _setup_player_with_patent("Perpetual Motion")
        card = _build_card("PowerHungryIron", rates=[(Resource.FE, 1), (Resource.PWR, -2)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before

    def test_does_not_strip_positive_pwr(self):
        state, p = _setup_player_with_patent("Perpetual Motion")
        card = _build_card("WaterAndPower", rates=[(Resource.H2O, 1), (Resource.PWR, 1)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before + 1

    def test_does_not_fire_on_non_qualifying_building(self):
        """A card that's only Power (no water/iron/carbon/silicon tag) keeps its -PWR."""
        state, p = _setup_player_with_patent("Perpetual Motion")
        card = _build_card("PWROnly", rates=[(Resource.PWR, -1)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.PWR) == pwr_before - 1


# --- Carbon Scrubbing ---


class TestCarbonScrubbing:
    def test_strips_negative_c_rate(self):
        """The patent zeroes out negative C rates on new buildings."""
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        card = _build_card("CarbonDrain", rates=[(Resource.PWR, 1), (Resource.C, -1)])
        c_before = p.rate(Resource.C)
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        # The -1 C rate is removed
        assert p.rate(Resource.C) == c_before
        # PWR rate gained +1
        assert p.rate(Resource.PWR) == pwr_before + 1

    def test_strips_negative_o2_rate(self):
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        card = _build_card("OxygenDrain", rates=[(Resource.O2, -2)])
        o2_before = p.rate(Resource.O2)
        _execute_single_build(state, p, card)
        # The -2 O2 rate is removed
        assert p.rate(Resource.O2) == o2_before

    def test_does_not_strip_c_from_costs(self):
        """Costs are NOT affected — the patent only touches per-turn rates.
        A card with 1 C cost still needs 1 C at build time."""
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        # Zero out all rates so we can be sure the C cost has nothing
        # to cover it (the corp setup randomizes starting rates).
        for r in Resource:
            p.rates[r] = 0
        card = _build_card(
            "Scrubbed",
            costs=[(Resource.C, 1), (Resource.FE, 1)],
            rates=[(Resource.PWR, 1)],
        )
        p.hand = [card]
        p.rates[Resource.FE] = 1  # FE cost covered
        money_before = p.money
        record = execute_build(state, p, build_indices=[0])
        # Build succeeded and the player paid market cost for the C
        assert record is not None
        assert p.money < money_before

    def test_does_not_strip_o2_from_costs(self):
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        for r in Resource:
            p.rates[r] = 0
        card = _build_card("OxygenHog", costs=[(Resource.O2, 2)], rates=[(Resource.PWR, 1)])
        p.hand = [card]
        money_before = p.money
        record = execute_build(state, p, build_indices=[0])
        # Build succeeded and the player paid market cost for the O2
        assert record is not None
        assert p.money < money_before

    def test_does_not_strip_other_negative_rates(self):
        """Only C and O2 negative rates are stripped — H2O etc. stay."""
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        card = _build_card("WaterDrain", rates=[(Resource.H2O, -1)])
        h2o_before = p.rate(Resource.H2O)
        _execute_single_build(state, p, card)
        # The -1 H2O rate is preserved
        assert p.rate(Resource.H2O) == h2o_before - 1

    def test_does_not_strip_positive_c_or_o2(self):
        """The patent only strips negative C/O2 rates. Positive rates are kept."""
        state, p = _setup_player_with_patent("Carbon Scrubbing")
        card = _build_card("Producer", rates=[(Resource.C, 2), (Resource.O2, 1)])
        c_before = p.rate(Resource.C)
        o2_before = p.rate(Resource.O2)
        _execute_single_build(state, p, card)
        assert p.rate(Resource.C) == c_before + 2
        assert p.rate(Resource.O2) == o2_before + 1


# --- PATENT_BUILD_HOOKS registry sanity check ---


class TestPatentBuildHooksRegistry:
    def test_all_5_easy_patents_registered(self):
        for name in [
            "Superconductors",
            "Cold Fusion",
            "Slant Drilling",
            "Perpetual Motion",
            "Carbon Scrubbing",
        ]:
            assert name in PATENT_BUILD_HOOKS, f"Missing hook for {name}"

    def test_unknown_patent_name_does_not_break_build(self):
        """A patent in buildings_played whose name has no hook is silently
        ignored — the build still succeeds."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("NonexistentPatent"))
        p.money = 100
        card = _build_card("Solar", rates=[(Resource.PWR, 1)])
        _execute_single_build(state, p, card)
        # No errors, no surprises


# --- Patents.csv parsing sanity check ---


class TestPatentsCsv:
    def test_parses_all_patents(self):
        patents = parse_patents(DATA / "Patents.csv")
        names = {p.building for p in patents}
        expected = {
            "Superconductors", "Energy Vault", "Financial Instruments",
            "Water Engine", "Nanotechnology", "Cold Fusion", "Virtual Reality",
            "Perpetual Motion", "Carbon Scrubbing", "Slant Drilling",
            "Thinking Machines", "Teleportation", "Matter Replication",
        }
        assert names == expected

    def test_patents_have_no_inherent_rates(self):
        """Patents from CSV have empty rates — the mechanical effect is in hooks."""
        patents = parse_patents(DATA / "Patents.csv")
        for p in patents:
            assert p.rates == [], f"{p.building} has unexpected rates"
            assert p.slot == 5

    def test_patents_carry_effect_text(self):
        """The Power column from the CSV lands in the effect field."""
        patents = parse_patents(DATA / "Patents.csv")
        sc = next(p for p in patents if p.building == "Superconductors")
        assert "PWR" in sc.effect or "Power" in sc.effect


# --- Energy Vault ---


class TestEnergyVault:
    """Energy Vault: shields from paying negative power bills, up to 10 uses.

    When a player has a negative PWR rate AND vault > 0, the vault absorbs
    the bill — the player neither earns nor pays for that power bill. The
    vault decrements by 1 each time. No effect on positive/zero PWR rates.
    No build-time effect (the old build hook was removed).
    """

    def test_acquisition_initializes_vault_to_10(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        patent = _patent("Energy Vault")
        settle_silent_auction(state, patent, bids={0: 5}, active_player=state.players[0])
        assert p.patent_state.get("energy_vault") == 10

    def test_shields_negative_power_bill(self):
        """Player with -3 PWR and vault=10: no debt, vault→9."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Energy Vault"))
        p.patent_state["energy_vault"] = 10
        p.rates[Resource.PWR] = -3
        debt_before = p.debt
        money_before = p.money
        do_power_bill(state)
        assert p.debt == debt_before  # shielded — no debt
        assert p.money == money_before  # no earnings either
        assert p.patent_state["energy_vault"] == 9

    def test_vault_decrements_each_bill(self):
        """Three power bills with negative rate: vault goes 10→9→8→7."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Energy Vault"))
        p.patent_state["energy_vault"] = 10
        p.rates[Resource.PWR] = -2
        for expected_vault in [9, 8, 7]:
            do_power_bill(state)
            assert p.patent_state["energy_vault"] == expected_vault

    def test_empty_vault_does_not_shield(self):
        """Vault=0, negative rate: normal debt applies."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Energy Vault"))
        p.patent_state["energy_vault"] = 0
        p.rates[Resource.PWR] = -2
        debt_before = p.debt
        do_power_bill(state)
        pwr_price = state.market.price(Resource.PWR)
        # Should have paid normally
        assert p.debt > debt_before

    def test_vault_does_not_affect_positive_rate(self):
        """Positive PWR rate: vault is untouched, normal earnings."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Energy Vault"))
        p.patent_state["energy_vault"] = 10
        p.rates[Resource.PWR] = 3
        money_before = p.money
        do_power_bill(state)
        pwr_price = state.market.price(Resource.PWR)
        assert p.money == money_before + 3 * pwr_price
        assert p.patent_state["energy_vault"] == 10  # unchanged

    def test_vault_does_not_affect_zero_rate(self):
        """Zero PWR rate: vault is untouched, no earnings or debt."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Energy Vault"))
        p.patent_state["energy_vault"] = 10
        p.rates[Resource.PWR] = 0
        money_before = p.money
        debt_before = p.debt
        do_power_bill(state)
        assert p.money == money_before
        assert p.debt == debt_before
        assert p.patent_state["energy_vault"] == 10

    def test_no_build_hook_anymore(self):
        """Building with negative PWR no longer drains the vault."""
        state, p = _setup_player_with_patent("Energy Vault")
        p.patent_state["energy_vault"] = 10
        card = _build_card("PowerHog", rates=[(Resource.PWR, -3)])
        pwr_before = p.rate(Resource.PWR)
        _execute_single_build(state, p, card)
        # Rate IS affected — vault doesn't absorb builds anymore
        assert p.rate(Resource.PWR) == pwr_before - 3
        # Vault unchanged by builds
        assert p.patent_state["energy_vault"] == 10


# --- Financial Instruments ---


class TestFinancialInstruments:
    def test_owner_gets_paid_for_others_debt_interest(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        # Player 0 owns FI
        state.players[0].buildings_played.append(_patent("Financial Instruments"))
        # Players 1 and 2 have debt; player 0 has none
        # Zero cash so debt paydown phase doesn't interfere with interest test
        state.players[0].debt = 0
        state.players[1].debt = 100; state.players[1].money = 0  # interest = 10
        state.players[2].debt = 50; state.players[2].money = 0   # interest = 5
        money_before = state.players[0].money
        do_debt_collection(state)
        # Owner gains 10 + 5 = 15 cash from others' debt interest
        assert state.players[0].money == money_before + 15

    def test_owner_does_not_count_own_debt_interest(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        state.players[0].buildings_played.append(_patent("Financial Instruments"))
        state.players[0].debt = 100  # owner's own debt → 10 interest, no bonus
        state.players[0].money = 0   # no cash to pay down
        state.players[1].debt = 0
        state.players[2].debt = 0
        money_before = state.players[0].money
        do_debt_collection(state)
        assert state.players[0].money == money_before  # no bonus from own debt

    def test_no_owner_no_bonus(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        state.players[0].debt = 50; state.players[0].money = 0
        state.players[1].debt = 50; state.players[1].money = 0
        money_before = [p.money for p in state.players]
        do_debt_collection(state)
        # No FI ownership → no bonuses
        assert [p.money for p in state.players] == money_before

    def test_credit_absorbs_interest_before_fi_payout(self):
        """When other players have credit, the credit absorbs the interest
        before it becomes real debt. Only the REAL debt added (post-credit)
        counts toward the FI owner's bonus."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        state.players[0].buildings_played.append(_patent("Financial Instruments"))
        state.players[0].debt = 0
        # Player 1: $100 debt → interest 10. But P1 has $5 credit which
        # absorbs $5 of that interest. Real debt added = 5.
        state.players[1].debt = 100
        state.players[1].money = 0  # no cash to pay down
        state.players[1].credit = 5
        money_before = state.players[0].money
        do_debt_collection(state)
        # FI owner gains 5 (the real-debt portion only)
        assert state.players[0].money == money_before + 5
        # P1's credit was consumed
        assert state.players[1].credit == 0


# --- Virtual Reality ---


class TestVirtualReality:
    def test_doubles_pleasure_dome_bonus(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        # Player 0 owns Pleasure Dome AND Virtual Reality (one PD globally → $20 base)
        # Build a fake Pleasure Dome card directly into buildings_played
        pd_card = Card(
            alternate="GLS/ELX",
            slot=4,
            building="Pleasure Dome",
            costs=[],
            rates=[],
            effect="passive",
            can_sell=[],
            can_fulfill_contract=False,
        )
        p.buildings_played.append(pd_card)
        p.buildings_played.append(_patent("Virtual Reality"))
        # Zero PWR rate so the only money change is from the dome bonus
        p.rates[Resource.PWR] = 0
        money_before = p.money
        do_power_bill(state)
        # 1 PD globally → $20 base; doubled by VR → $40
        assert p.money == money_before + 40

    def test_no_pd_no_vr_bonus(self):
        """VR alone (no Pleasure Dome) gives no bonus — there's nothing to double."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        p.buildings_played.append(_patent("Virtual Reality"))
        p.rates[Resource.PWR] = 0
        money_before = p.money
        do_power_bill(state)
        assert p.money == money_before

    def test_pd_only_no_vr_normal_bonus(self):
        """Just Pleasure Dome, no VR → normal $20 bonus."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        pd_card = Card(
            alternate="GLS/ELX",
            slot=4,
            building="Pleasure Dome",
            costs=[],
            rates=[],
            effect="passive",
            can_sell=[],
            can_fulfill_contract=False,
        )
        p.buildings_played.append(pd_card)
        p.rates[Resource.PWR] = 0
        money_before = p.money
        do_power_bill(state)
        assert p.money == money_before + 20


# --- Thinking Machines ---


class TestThinkingMachines:
    def test_acquisition_draws_one_card(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        hand_before = len(p.hand)
        patent = _patent("Thinking Machines")
        settle_silent_auction(state, patent, bids={0: 5}, active_player=state.players[0])
        # Drew 1 card → hand grew by 1
        assert len(p.hand) == hand_before + 1

    def test_acquisition_bumps_hand_size(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        hand_size_before = p.hand_size
        patent = _patent("Thinking Machines")
        settle_silent_auction(state, patent, bids={0: 5}, active_player=state.players[0])
        assert p.hand_size == hand_size_before + 1

    def test_apply_patent_acquisition_directly(self):
        """Calling _apply_patent_acquisition directly works (doesn't depend on auction)."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=3)
        p = state.players[0]
        hand_before = len(p.hand)
        hand_size_before = p.hand_size
        _apply_patent_acquisition(state, p, _patent("Thinking Machines"))
        assert len(p.hand) == hand_before + 1
        assert p.hand_size == hand_size_before + 1


# --- Active patents (Water Engine, Nanotechnology, Teleportation) ---


from my_project.play_adapter import PlayableGame


def _make_game_with_human() -> PlayableGame:
    """A game with one human seat (idx 0) and disable_prompts=True."""
    return PlayableGame(seed=42, max_turns=8, disable_prompts=True)


class TestWaterEngine:
    def test_use_consumes_h2o_yields_pwr(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 1
        p.rates[Resource.PWR] = 0
        result = game.use_water_engine(0)
        assert result["ok"]
        assert p.rate(Resource.H2O) == 0
        assert p.rate(Resource.PWR) == 2
        assert p.has_used_water_engine_this_turn

    def test_use_blocked_when_no_h2o(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 0
        result = game.use_water_engine(0)
        assert not result["ok"]

    def test_use_blocked_when_already_used(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 5
        game.use_water_engine(0)  # first use
        result = game.use_water_engine(0)  # second use same turn
        assert not result["ok"]

    def test_use_blocked_when_not_owned(self):
        game = _make_game_with_human()
        result = game.use_water_engine(0)
        assert not result["ok"]


class TestNanotechnology:
    def test_replaces_pool_card_at_drawn_card_slot(self):
        from my_project.models import Card as _Card
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Nanotechnology"))

        # Create a controlled pool with known slots
        def _mk(slot: int, name: str) -> _Card:
            return _Card(
                alternate="H2O/FE", slot=slot, building=name,
                costs=[], rates=[], effect="",
                can_sell=[], can_fulfill_contract=False,
            )
        game.state.pool.replace([_mk(1, "S1"), _mk(2, "S2"), _mk(3, "S3"), _mk(4, "S4")])

        # Stack the deck with a known card
        slot3_card = _mk(3, "NEW_S3")
        game.state.deck.cards.append(slot3_card)

        pool_before = len(game.state.pool)
        old_s3 = game.state.pool[2]
        result = game.use_nanotechnology(0)
        assert result["ok"]
        # Pool size unchanged
        assert len(game.state.pool) == pool_before
        # Slot 3 card was replaced (pool[2] since slot 1 → index 0, slot 3 → index 2)
        assert game.state.pool[2].building == "NEW_S3"
        # Old slot 3 card is in discard
        assert old_s3 in game.state.deck.discard
        assert p.has_used_nanotechnology_this_turn

    def test_use_blocked_when_already_used(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Nanotechnology"))
        game.use_nanotechnology(0)
        result = game.use_nanotechnology(0)
        assert not result["ok"]

    def test_empty_pool_blocked(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Nanotechnology"))
        game.state.pool = []
        result = game.use_nanotechnology(0)
        assert not result["ok"]
        assert "empty" in result["reason"].lower()

    def test_empty_deck_blocked(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Nanotechnology"))
        game.state.deck.cards = []
        result = game.use_nanotechnology(0)
        assert not result["ok"]
        assert "empty" in result["reason"].lower()



class TestTeleportation:
    def test_sells_resource_for_market_price(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        p.rates[Resource.FE] = 2
        money_before = p.money
        fe_rate_before = p.rate(Resource.FE)
        fe_price_before = game.state.market.price(Resource.FE)
        fe_pos_before = game.state.market.positions[Resource.FE]
        pwr_rate_before = p.rate(Resource.PWR)
        result = game.use_teleportation(0, "FE")
        assert result["ok"]
        # Player got cash equal to rate × market price at time of sell
        assert p.money == money_before + fe_rate_before * fe_price_before
        # FE rate UNCHANGED (the patent says "does not effect rates" for the sold resource)
        assert p.rate(Resource.FE) == fe_rate_before
        # PWR rate decreased by 1 (the patent's cost)
        assert p.rate(Resource.PWR) == pwr_rate_before - 1
        assert p.has_used_teleportation_this_turn
        # Market should drop by the sold amount (same as a normal sell)
        fe_pos_after = game.state.market.positions[Resource.FE]
        assert fe_pos_after < fe_pos_before

    def test_market_drops_on_sell(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        p.rates[Resource.FE] = 3
        fe_pos_before = game.state.market.positions[Resource.FE]
        result = game.use_teleportation(0, "FE")
        assert result["ok"]
        # Market position should decrease by the rate (3 units sold)
        assert game.state.market.positions[Resource.FE] == max(0, fe_pos_before - 3)

    def test_blocked_when_no_positive_rate(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        # Player has no FE rate
        p.rates[Resource.FE] = 0
        result = game.use_teleportation(0, "FE")
        assert not result["ok"]

    def test_blocked_when_already_used(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        p.rates[Resource.FE] = 5
        game.use_teleportation(0, "FE")
        result = game.use_teleportation(0, "FE")
        assert not result["ok"]

    def test_blocked_when_not_owned(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.rates[Resource.FE] = 5
        result = game.use_teleportation(0, "FE")
        assert not result["ok"]

    def test_invalid_resource_rejected(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        result = game.use_teleportation(0, "BOGUS")
        assert not result["ok"]

    def test_hacker_array_bonus_fires_on_teleport_sell(self):
        """Teleportation is a free sell, and the Hacker Array rule says
        "when selling: also shift a different resource's market price by
        ±3". A HA owner who teleports should get the ±3 bump just like
        they would on a regular card sell."""
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        # Seed a Hacker Array into the player's built cards so the
        # _count_buildings check passes.
        ha_card = Card(
            alternate="",
            slot=4,
            building="Hacker Array",
            costs=[],
            rates=[],
            effect="",
            can_sell=[Resource.FE, Resource.H2O],
            can_fulfill_contract=False,
        )
        p.buildings_played.append(ha_card)
        p.rates[Resource.FE] = 2
        gls_pos_before = game.state.market.positions[Resource.GLS]
        result = game.use_teleportation(
            0, "FE", hacker_target="GLS", hacker_direction=1,
        )
        assert result["ok"]
        # Hacker Array bumped GLS up by 3 positions.
        assert (
            game.state.market.positions[Resource.GLS] == gls_pos_before + 3
        ), f"HA bonus missing: GLS pos {gls_pos_before} -> {game.state.market.positions[Resource.GLS]}"
        assert "[HA:" in result["detail"]

    def test_hacker_array_bonus_ignored_without_ha(self):
        """Non-owners can't conjure the HA bump by passing the args."""
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        p.rates[Resource.FE] = 2
        gls_pos_before = game.state.market.positions[Resource.GLS]
        result = game.use_teleportation(
            0, "FE", hacker_target="GLS", hacker_direction=1,
        )
        assert result["ok"]
        assert game.state.market.positions[Resource.GLS] == gls_pos_before
        assert "[HA:" not in result["detail"]


class TestPatentActionsLegalState:
    def test_patent_actions_all_unowned(self):
        game = _make_game_with_human()
        game.begin_human_turn()
        legal = game.legal_human_actions()
        assert "patent_actions" in legal
        for name in ["water_engine", "nanotechnology", "teleportation"]:
            assert legal["patent_actions"][name]["owned"] is False
            assert legal["patent_actions"][name]["available"] is False

    def test_patent_actions_owned_and_available(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 2
        game.begin_human_turn()
        legal = game.legal_human_actions()
        assert legal["patent_actions"]["water_engine"]["owned"]
        assert legal["patent_actions"]["water_engine"]["available"]

    def test_water_engine_unavailable_after_use(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 2
        game.begin_human_turn()
        game.use_water_engine(0)
        legal = game.legal_human_actions()
        assert legal["patent_actions"]["water_engine"]["owned"]
        assert not legal["patent_actions"]["water_engine"]["available"]

    def test_teleportation_valid_resources_filters_pwr_and_zero(self):
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Teleportation"))
        # Reset all rates to 0, then give the player FE only
        for r in Resource:
            p.rates[r] = 0
        p.rates[Resource.FE] = 3
        p.rates[Resource.PWR] = 5  # PWR should NOT show up as a valid sell target
        game.begin_human_turn()
        legal = game.legal_human_actions()
        valid = legal["patent_actions"]["teleportation"]["valid_resources"]
        assert "FE" in valid
        assert "PWR" not in valid
        # Other zero-rate resources excluded
        assert "GLS" not in valid


class TestActivePatentFlagsResetEachTurn:
    def test_water_engine_flag_resets(self):
        """After ending a turn and starting a new one, the patent's per-turn
        flag is cleared so it can be used again."""
        game = _make_game_with_human()
        p = game.state.players[0]
        p.buildings_played.append(_patent("Water Engine"))
        p.rates[Resource.H2O] = 5
        game.begin_human_turn()
        game.use_water_engine(0)
        assert p.has_used_water_engine_this_turn
        # End the human turn (with no event prompts since disable_prompts=True)
        game.apply_human_action({"type": "pass"})
        game.end_human_turn()
        # Step through AI turns until human's turn comes back
        while not game.is_human_turn() and not game.is_over():
            game.step_ai_turn()
        if not game.is_over():
            game.begin_human_turn()
            assert not p.has_used_water_engine_this_turn
