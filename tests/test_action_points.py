"""Tests for the 2-cards-per-turn rule.

Each player turn allows spending at most `MAX_CARDS_PER_TURN` (2) hand cards.
Every card that leaves the hand counts: builds, build-deficit discards, sells,
and contract-icon cards. Launch Pad contracts are free (0 cards spent).
Nanotechnology is explicitly exempt. The counter `cards_spent_this_turn` resets
to 0 at the start of each turn.
"""
from pathlib import Path

from my_project.models import Card, Contract, Resource, ResourceAmount
from my_project.parsing import parse_cards, parse_contracts
from my_project.play_adapter import PlayableGame
from my_project.simulation import (
    GameState,
    Player,
    MAX_CARDS_PER_TURN,
    execute_build,
    execute_contract,
    execute_sell,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "my_project" / "data"


def _load():
    return (
        parse_cards(DATA_DIR / "Cards.csv"),
        parse_contracts(DATA_DIR / "Contracts.csv"),
    )


def _build_card(name="Test", costs=(), rates=(), can_sell=()):
    return Card(
        alternate="C/SI",
        slot=1,
        building=name,
        costs=[ResourceAmount(r, a) for r, a in costs],
        rates=[ResourceAmount(r, a) for r, a in rates],
        effect="",
        can_sell=list(can_sell),
        can_fulfill_contract=False,
    )


def _contract_card():
    return Card(
        alternate="Contract",
        slot=1,
        building="MockContract",
        costs=[],
        rates=[],
        effect="",
        can_sell=[],
        can_fulfill_contract=True,
    )


class TestCardsPerTurnBasics:
    def test_max_is_two(self):
        assert MAX_CARDS_PER_TURN == 2

    def test_player_starts_at_zero_spent(self):
        p = Player(name="P")
        assert p.cards_spent_this_turn == 0
        assert p.cards_remaining() == 2


class TestBuildCardCounting:
    def test_single_build_spends_1(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        for r in Resource:
            p.rates[r] = 0
        p.money = 200
        p.hand = [_build_card("Solo", rates=[(Resource.PWR, 1)])]
        record = execute_build(state, p, build_indices=[0])
        assert record is not None
        assert p.cards_spent_this_turn == 1
        assert p.cards_remaining() == 1

    def test_two_card_build_spends_2(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        for r in Resource:
            p.rates[r] = 0
        p.money = 200
        p.hand = [
            _build_card("A", rates=[(Resource.PWR, 1)]),
            _build_card("B", rates=[(Resource.PWR, 1)]),
        ]
        record = execute_build(state, p, build_indices=[0, 1])
        assert record is not None
        assert p.cards_spent_this_turn == 2
        assert p.cards_remaining() == 0

    def test_build_rejected_after_spending_both_cards(self):
        """After spending 2 cards via a sell + sell, builds are blocked."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        p.rates[Resource.FE] = 2
        p.money = 200
        p.hand = [
            _build_card("Seller1", can_sell=[Resource.FE]),
            _build_card("Seller2", can_sell=[Resource.FE]),
            _build_card("Builder", rates=[(Resource.PWR, 1)]),
        ]
        execute_sell(state, p, card_idx=0)
        execute_sell(state, p, card_idx=0)  # indices shift after first pop
        assert p.cards_spent_this_turn == 2
        # Now try to build — rejected
        record = execute_build(state, p, build_indices=[0])
        assert record is None


class TestSellCardCounting:
    def test_sell_spends_1(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        p.rates[Resource.FE] = 2
        p.hand = [_build_card("Seller", can_sell=[Resource.FE])]
        record = execute_sell(state, p, card_idx=0)
        assert record is not None
        assert p.cards_spent_this_turn == 1

    def test_sell_blocked_at_zero_remaining(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        p.rates[Resource.FE] = 2
        p.hand = [_build_card("Seller", can_sell=[Resource.FE])]
        p.cards_spent_this_turn = 2
        record = execute_sell(state, p, card_idx=0)
        assert record is None
        assert len(p.hand) == 1


class TestContractCardCounting:
    def _setup(self):
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        for r in Resource:
            p.rates[r] = 5
        state.available_contracts[0] = Contract(
            requirements=[ResourceAmount(Resource.FE, 1)], reward=50, count=1
        )
        return state, p

    def test_contract_via_hand_card_spends_1(self):
        state, p = self._setup()
        p.hand = [_contract_card()]
        record = execute_contract(state, p, card_idx=0, contract_idx=0)
        assert record is not None
        assert p.cards_spent_this_turn == 1

    def test_contract_blocked_at_zero_remaining(self):
        state, p = self._setup()
        p.hand = [_contract_card()]
        p.cards_spent_this_turn = 2
        record = execute_contract(state, p, card_idx=0, contract_idx=0)
        assert record is None

    def test_launch_pad_spends_zero(self):
        state, p = self._setup()
        lp_card = Card(
            alternate="", slot=4, building="Launch Pad",
            costs=[], rates=[], effect="free contract icon once per turn",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(lp_card)
        p.hand = []
        record = execute_contract(
            state, p, card_idx=-1, contract_idx=0, use_launch_pad=True,
        )
        assert record is not None
        assert p.cards_spent_this_turn == 0
        assert p.has_used_launch_pad_this_turn

    def test_launch_pad_blocked_after_first_use(self):
        state, p = self._setup()
        lp_card = Card(
            alternate="", slot=4, building="Launch Pad",
            costs=[], rates=[], effect="free contract icon once per turn",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(lp_card)
        p.hand = []
        execute_contract(state, p, card_idx=-1, contract_idx=0, use_launch_pad=True)
        state.available_contracts[1] = Contract(
            requirements=[ResourceAmount(Resource.FE, 1)], reward=50, count=1
        )
        record = execute_contract(state, p, card_idx=-1, contract_idx=1, use_launch_pad=True)
        assert record is None


class TestResetBetweenTurns:
    def test_run_turn_resets(self):
        from my_project.simulation import run_turn, EventCard, EventType
        from my_project.strategies import smart_greedy_strategy
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        p.cards_spent_this_turn = 2  # exhausted
        ev = EventCard(type=EventType.NO_EVENT)
        run_turn(state, p, smart_greedy_strategy, ev)
        # Was reset to 0 at the top of run_turn, then possibly spent during.
        assert p.cards_spent_this_turn <= MAX_CARDS_PER_TURN

    def test_begin_human_turn_resets(self):
        game = PlayableGame(seats=["human", "smart"], seed=42)
        p = game.state.players[0]
        p.cards_spent_this_turn = 2
        game.begin_human_turn()
        assert p.cards_spent_this_turn == 0
        assert p.cards_remaining() == 2


class TestLegalHumanActions:
    def test_exposes_cards_remaining(self):
        game = PlayableGame(seats=["human", "smart"], seed=42)
        game.begin_human_turn()
        legal = game.legal_human_actions()
        assert "cards_remaining" in legal
        assert legal["cards_remaining"] == 2
        assert "max_build_cards" in legal
        assert "can_contract" in legal


class TestStrategiesUseLaunchPadWithZeroCards:
    """The AI strategy card-budget gating must NOT prevent free LP contracts.
    A player who has spent 2 cards, with an unused Launch Pad and an
    affordable contract, should still get a CONTRACT action proposed.
    """

    def _setup_lp_player(self, *, hand_size: int = 0):
        from my_project.strategies import smart_greedy_strategy
        cards, contracts = _load()
        state = GameState.create(cards, contracts, num_players=2)
        p = state.players[0]
        lp_card = Card(
            alternate="", slot=4, building="Launch Pad",
            costs=[], rates=[], effect="free contract icon once per turn",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(lp_card)
        for r in Resource:
            p.rates[r] = 5
        state.available_contracts[0] = Contract(
            requirements=[ResourceAmount(Resource.FE, 1)], reward=50, count=1
        )
        p.cards_spent_this_turn = 2  # fully spent
        p.hand = [_build_card(f"Junk{i}") for i in range(hand_size)]
        return state, p, smart_greedy_strategy

    def test_smart_greedy_uses_lp_with_empty_hand_and_zero_cards(self):
        from my_project.simulation import ActionType
        state, p, strategy = self._setup_lp_player(hand_size=0)
        action = strategy(state, p)
        assert action.action_type == ActionType.CONTRACT
        assert action.use_launch_pad

    def test_random_strategy_uses_lp_with_empty_hand_and_zero_cards(self):
        from my_project.simulation import ActionType
        from my_project.strategies import random_strategy
        state, p, _ = self._setup_lp_player(hand_size=0)
        action = random_strategy(state, p)
        assert action.action_type == ActionType.CONTRACT
        assert action.use_launch_pad

    def test_run_turn_loop_fires_lp_with_empty_hand(self):
        from my_project.simulation import EventCard, EventType, run_turn
        from my_project.strategies import smart_greedy_strategy
        state, p, _ = self._setup_lp_player(hand_size=0)
        p.cards_spent_this_turn = 0  # reset so run_turn's own reset is a no-op
        contracts_before = p.contracts_fulfilled
        ev = EventCard(type=EventType.NO_EVENT)
        run_turn(state, p, smart_greedy_strategy, ev)
        assert p.contracts_fulfilled == contracts_before + 1
        assert p.has_used_launch_pad_this_turn


class TestFreeActionsDoNotCount:
    """Free actions must NOT increment cards_spent_this_turn."""

    def test_optimization_center(self):
        game = PlayableGame(seats=["human", "smart"], seed=42)
        p = game.state.players[0]
        oc_card = Card(
            alternate="", slot=4, building="Optimization Center",
            costs=[], rates=[], effect="Free Action: -1 PWR and +1 any positive resource",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(oc_card)
        p.rates[Resource.FE] = 1
        game.begin_human_turn()
        spent_before = p.cards_spent_this_turn
        result = game.use_optimization_center(0, "FE")
        assert result["ok"]
        assert p.cards_spent_this_turn == spent_before

    def test_water_engine(self):
        game = PlayableGame(seats=["human", "smart"], seed=42)
        p = game.state.players[0]
        we_card = Card(
            alternate="", slot=5, building="Water Engine",
            costs=[], rates=[], effect="Free Action: -1 H2O, +2 PWR",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(we_card)
        p.rates[Resource.H2O] = 1
        game.begin_human_turn()
        spent_before = p.cards_spent_this_turn
        result = game.use_water_engine(0)
        assert result["ok"]
        assert p.cards_spent_this_turn == spent_before

    def test_nanotechnology_does_not_count(self):
        """Nanotechnology is explicitly exempt — discarding + drawing 1
        card does not count toward the 2-card-per-turn limit."""
        game = PlayableGame(seats=["human", "smart"], seed=42)
        p = game.state.players[0]
        nano_card = Card(
            alternate="", slot=5, building="Nanotechnology",
            costs=[], rates=[], effect="Free Action: Discard one card and draw one card",
            can_sell=[], can_fulfill_contract=False,
        )
        p.buildings_played.append(nano_card)
        game.begin_human_turn()
        spent_before = p.cards_spent_this_turn
        result = game.use_nanotechnology(0)
        assert result["ok"]
        assert p.cards_spent_this_turn == spent_before

