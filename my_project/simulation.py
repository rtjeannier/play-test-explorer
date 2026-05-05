"""Game state model and simulation engine for the board game.

Simulates N players taking turns: draw cards, take one action (build/sell/contract),
draw back to hand size, then resolve one event from the event deck.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from my_project.accounting import CostLedger
from my_project.models import (
    Card, CardZone, Contract, Currency, GameLog, Resource, ResourceAmount,
    ResourceRates,
)


# --- Game configuration (loaded from GameConfig.csv) ---
#
# These module-level constants are the single source of truth consumed by
# the engine. They are loaded from data/GameConfig.csv at import time so
# non-technical users can tune them by editing the CSV. The names are kept
# as module-level constants (ALL_CAPS) for backward compatibility with
# existing imports throughout the codebase.

def _load_game_config() -> dict[str, str]:
    from my_project.parsing import parse_game_config
    cfg_path = Path(__file__).parent / "data" / "GameConfig.csv"
    if cfg_path.exists():
        return parse_game_config(cfg_path)
    return {}

_CFG = _load_game_config()

# --- Market ---

PRICE_TRACK = [1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 8, 9, 10]

# Game balance constants (sourced from GameConfig.csv, with hardcoded fallbacks)
DEFAULT_MAX_TURNS = int(_CFG.get("default_max_turns", "8"))
DEFAULT_NUM_PLAYERS = int(_CFG.get("default_num_players", "3"))
DEFAULT_START_MONEY = int(_CFG.get("default_start_money", "20"))
DEFAULT_MARKET_POS = int(_CFG.get("default_market_position", "9"))
HAND_SIZE = int(_CFG.get("hand_size", "3"))
POOL_SIZE = int(_CFG.get("pool_size", "4"))
CONTRACTS_AVAILABLE_BASE = int(_CFG.get("contracts_available_base", "2"))
CONTRACT_REWARD = int(_CFG.get("contract_reward", "50"))
DEBT_INTEREST_DIVISOR = int(_CFG.get("debt_interest_divisor", "10"))
MAX_CARDS_PER_TURN = int(_CFG.get("max_cards_per_turn", "2"))
MAX_ACTIONS_PER_TURN = 10  # safety limit — not a design parameter

# Event deck composition (random within ranges)
POWER_BILL_RANGE = (3, 4)
DEBT_COLLECTION_RANGE = (2, 4)
FUTURES_TRADING_RANGE = (3, 4)





def _load_corporations(data_dir: Path | None = None) -> list[tuple[str, dict[str, int]]]:
    """Load corporations from Corporations.csv."""
    from my_project.parsing import parse_corporations
    if data_dir is None:
        data_dir = Path(__file__).parent / "data"
    return parse_corporations(data_dir / "Corporations.csv")


def apply_corporation(
    player: "Player",
    corp_name: str,
    corp_rates: dict[str, int],
    log: "GameLog | None" = None,
    player_idx: int = -1,
) -> None:
    """Assign a corporation's starting rates to a player.

    Used by both the random-assignment path in GameState.create and the
    draft path in PlayableGame.submit_draft_pick, so both routes write
    the same four pieces of state (corporation name, starting_rates
    snapshot, live rates, ledger rate) in lockstep.

    When `log` is supplied, wraps the writes in a "corp_assigned" log
    entry so the draft picks show up as mutations in the action log.
    """
    if log is not None:
        log.begin("corp_assigned", player.name, player_idx, f"{corp_name}")
    player.corporation = corp_name
    player.starting_rates = dict(corp_rates)
    for r_str, v in corp_rates.items():
        res = Resource(r_str)
        player.rates[res] = v
        # Sync to ledger so contract/build cost tracking is consistent.
        # Starting rates have zero cost basis (free from corporation).
        player.ledger.accounts[res].rate = v
    if log is not None:
        log.end()


@dataclass
class Market:
    """Tracks price position (index into PRICE_TRACK) for each resource."""
    positions: dict[Resource, int] = field(default_factory=dict)
    _log: GameLog | None = field(default=None, init=False, repr=False, compare=False)

    # Per-resource starting positions on PRICE_TRACK (tiered by resource complexity)
    DEFAULT_POSITIONS: ClassVar[dict[Resource, int]] = {
        Resource.PWR: 3,   # $2
        Resource.H2O: 3,   # $2
        Resource.FE: 3,    # $2
        Resource.C: 6,     # $3
        Resource.SI: 6,    # $3
        Resource.O2: 8,    # $4
        Resource.FOOD: 8,  # $4
        Resource.GLS: 10,  # $5
        Resource.ELX: 10,  # $5
    }

    @classmethod
    def create(cls, start_position: int | None = None) -> Market:
        """Create market with tiered starting prices per resource."""
        if start_position is not None:
            positions = {r: start_position for r in Resource}
        else:
            positions = dict(cls.DEFAULT_POSITIONS)
        return cls(positions=positions)

    def _record(self, resource: Resource, old_pos: int, new_pos: int) -> None:
        if self._log and old_pos != new_pos:
            self._log.record(
                f"market.{resource.value}", old_pos, new_pos,
            )

    def price(self, resource: Resource) -> int:
        pos = self.positions[resource]
        pos = max(0, min(pos, len(PRICE_TRACK) - 1))
        return PRICE_TRACK[pos]

    def buy(self, resource: Resource, amount: int) -> int:
        """Buy `amount` units at the current price. Returns total cost. Then price rises by amount."""
        cost = self.price(resource) * amount
        old = self.positions[resource]
        self.positions[resource] = min(old + amount, len(PRICE_TRACK) - 1)
        self._record(resource, old, self.positions[resource])
        return cost

    def sell(self, resource: Resource, amount: int) -> int:
        """Sell `amount` units at the current price. Returns total revenue. Then price drops by amount."""
        revenue = self.price(resource) * amount
        old = self.positions[resource]
        self.positions[resource] = max(old - amount, 0)
        self._record(resource, old, self.positions[resource])
        return revenue

    def adjust(self, resource: Resource, delta: int) -> None:
        """Shift price position by delta (positive = up, negative = down)."""
        old = self.positions[resource]
        self.positions[resource] = max(0, min(old + delta, len(PRICE_TRACK) - 1))
        self._record(resource, old, self.positions[resource])

    def estimate_buy_cost(self, resource: Resource, amount: int) -> int:
        """Estimate cost of buying without modifying market state.

        All units pay the current price (matches buy() semantics).
        """
        return self.price(resource) * amount

    def snapshot(self) -> dict[str, int]:
        return {r.value: self.price(r) for r in Resource}


# --- Player ---

@dataclass
class Player:
    name: str
    money: int = 20
    debt: int = 0
    # Contract credit: leftover reward from fulfilled contracts after paying
    # off any existing debt. Counts toward net worth (like cash) AND absorbs
    # future debt before it becomes real debt. NOT spendable as cash.
    credit: int = 0
    rates: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    hand: list[Card] = field(default_factory=list)
    # Cards the player has built. Each entry is the original Card so special-
    # building handlers can read its `effect` field at activation/trigger time.
    buildings_played: list[Card] = field(default_factory=list)
    contracts_fulfilled: int = 0
    hand_size: int = HAND_SIZE
    ledger: CostLedger = field(default_factory=CostLedger.create)
    corporation: str = ""
    starting_rates: dict[str, int] = field(default_factory=dict)
    # Per-player resource flows (units and cash)
    flow_bought_units: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    flow_sold_units: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    flow_buy_cost: dict[Resource, float] = field(default_factory=lambda: {r: 0.0 for r in Resource})
    flow_sell_revenue: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    flow_futures_units: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    flow_futures_cost: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    # Per-turn state: resets at the start of each of this player's turns.
    # Rule: only one BUILD action is allowed per turn (multi-card builds
    # are fine, but subsequent build actions are blocked). This prevents
    # rates from being reused as a free discount across separate actions.
    has_built_this_turn: bool = False
    # Special-building per-turn flags (each is a "use once per turn"
    # capability that refreshes between turns, NOT a one-shot consumable).
    # Space Elevator is NOT here: it's always-on, applied to every contract.
    has_used_launch_pad_this_turn: bool = False
    has_used_optimization_center_this_turn: bool = False
    # Active-patent per-turn flags (same shape).
    has_used_water_engine_this_turn: bool = False
    has_used_nanotechnology_this_turn: bool = False
    has_used_teleportation_this_turn: bool = False
    # Tracks how many hand cards have been spent this turn (builds, sells,
    # contracts, and build-deficit discards all count). Resets to 0 at the
    # start of each player turn. Capped at MAX_CARDS_PER_TURN.
    # Nanotechnology is explicitly exempt.
    cards_spent_this_turn: int = 0
    # Per-patent stateful storage. Currently used by Energy Vault — key
    # "energy_vault" → remaining PWR units in the vault. Other patents may
    # add their own keys later.
    patent_state: dict[str, int] = field(default_factory=dict)

    def net_worth(self) -> int:
        return self.money - self.debt + self.credit

    def cards_remaining(self) -> int:
        """How many more hand cards the player can spend this turn."""
        return MAX_CARDS_PER_TURN - self.cards_spent_this_turn

    def rate(self, resource: Resource) -> int:
        return self.rates.get(resource, 0)

    def apply_rates(self, card: Card) -> None:
        for ra in card.rates:
            self.rates[ra.resource] = self.rates.get(ra.resource, 0) + ra.amount

    def building_names(self) -> list[str]:
        """Names of buildings the player has constructed, in build order."""
        return [c.building for c in self.buildings_played]

    def snapshot(self) -> dict:
        return {
            "money": self.money,
            "debt": self.debt,
            "credit": self.credit,
            "net_worth": self.net_worth(),
            "rates": {r.value: v for r, v in self.rates.items()},
            "buildings_played": self.building_names(),
            "contracts_fulfilled": self.contracts_fulfilled,
        }

    # --- Observable state support ---

    _CURRENCY_FIELDS: ClassVar[frozenset[str]] = frozenset({"money", "debt", "credit"})
    _ZONE_FIELDS: ClassVar[frozenset[str]] = frozenset({"hand", "buildings_played"})

    _currencies: dict[str, Currency] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    def _init_observables(self, log: GameLog, player_idx: int) -> None:
        """Upgrade money/debt/credit to Currency, rates to ResourceRates,
        hand/buildings_played to CardZone."""
        prefix = f"player.{player_idx}"
        currencies = {
            "money": Currency(self.money, log, f"{prefix}.money"),
            "debt": Currency(self.debt, log, f"{prefix}.debt"),
            "credit": Currency(self.credit, log, f"{prefix}.credit"),
        }
        object.__setattr__(self, "_currencies", currencies)
        object.__setattr__(
            self, "rates",
            ResourceRates(self.rates, log, f"{prefix}.rates"),
        )
        object.__setattr__(
            self, "hand",
            CardZone(self.hand, log, f"{prefix}.hand"),
        )
        object.__setattr__(
            self, "buildings_played",
            CardZone(self.buildings_played, log, f"{prefix}.buildings"),
        )

    def __getattribute__(self, name: str) -> Any:
        if name in Player._CURRENCY_FIELDS:
            currencies = object.__getattribute__(self, "_currencies")
            if currencies and name in currencies:
                return currencies[name]._value
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in Player._CURRENCY_FIELDS:
            currencies = object.__getattribute__(self, "__dict__").get("_currencies")
            if currencies and name in currencies:
                currencies[name].set(value)
                return
        if name in Player._ZONE_FIELDS:
            existing = object.__getattribute__(self, "__dict__").get(name)
            if isinstance(existing, CardZone):
                existing.replace(value if isinstance(value, list) else list(value))
                return
        object.__setattr__(self, name, value)


# --- Deck ---

@dataclass
class Deck:
    cards: list[Card] = field(default_factory=list)
    discard: list[Card] = field(default_factory=list)

    @classmethod
    def from_cards(cls, cards: list[Card]) -> Deck:
        deck = cls(cards=list(cards))
        random.shuffle(deck.cards)
        return deck

    def draw(self, n: int = 1) -> list[Card]:
        drawn = []
        for _ in range(n):
            if not self.cards:
                if not self.discard:
                    break
                self.cards = self.discard
                self.discard = []
                random.shuffle(self.cards)
            if self.cards:
                drawn.append(self.cards.pop())
        return drawn

    def remaining(self) -> int:
        return len(self.cards) + len(self.discard)


# --- Events ---

class EventType(StrEnum):
    NO_EVENT = "no_event"
    PWR_ADJUST = "pwr_adjust"
    POWER_BILL = "power_bill"
    DEBT_COLLECTION = "debt_collection"
    FUTURES_TRADING = "futures_trading"
    # Direct news with payload-based market deltas (for ad-hoc JSON config).
    NEWS = "news"
    # Draws and resolves a card from the news deck (data-driven via Events.csv).
    NEWS_BULLETIN = "news_bulletin"
    # Refreshes the building pool by drawing a fresh card.
    DRAW_BUILDING_CARD = "draw_building_card"
    PATENT_AUCTION = "patent_auction"
    # End-of-round: fires PB + Futures Settlement like END_GAME, but the
    # game continues into the next round with a fresh event deck shuffle.
    END_ROUND = "end_round"
    END_GAME = "end_game"


@dataclass
class EventCard:
    """A single card in the event deck.

    For simple events (power bill, futures settlement, etc.) only `type` is
    needed. The optional `payload` dict carries per-event parameters for
    data-driven events like NEWS. `label` is a human-readable display string
    for the turn log; if empty, falls back to `type.value`.

    `redraws=True` means: after firing this card's effect, the engine
    immediately draws and fires the NEXT event card too as part of the same
    player-turn. The deck must be sized to account for these extra
    consumptions (build_event_deck handles this).
    """
    type: EventType
    payload: dict | None = None
    label: str = ""
    redraws: bool = False
    pwr_adjust: bool = False

    def display_label(self) -> str:
        return self.label or self.type.value


def _ec(t: EventType, redraws: bool = False, pwr_adjust: bool = False) -> EventCard:
    """Shorthand for creating a simple (no-payload) EventCard."""
    return EventCard(type=t, redraws=redraws, pwr_adjust=pwr_adjust)


# --- News deck ---


@dataclass
class NewsEffect:
    """A single effect on a news card.

    `kind` selects the handler:
      - "rate_all": apply rate deltas to every player. payload = {"FOOD": -1, ...}
      - "market_random": roll the d20 distribution N times for each listed
            resource. payload = {"resources": ["H2O", ...], "rolls": 1}
      - "trigger": re-fire one of the standard event types in-place.
            payload = {"event": "power_bill"|"debt_collection"|"futures_trading"}
    """
    kind: str
    payload: dict


@dataclass
class NewsCard:
    name: str
    effects: list[NewsEffect] = field(default_factory=list)


# Same d20 distribution used by GameState.create's randomize_market roll.
_D20_DELTAS = [3, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, -2, -2, -2, -3, -3, -4, -4, 0]


def build_default_news_deck(data_dir: Path | None = None) -> list[NewsCard]:
    """Build a fresh news deck from News.csv.

    Each row in News.csv becomes one NewsCard with typed effects. The CSV
    is the single source of truth for news content — no hardcoded effects.
    """
    from my_project.parsing import parse_news
    if data_dir is None:
        data_dir = Path(__file__).parent / "data"
    raw = parse_news(data_dir / "News.csv")
    cards: list[NewsCard] = []
    for entry in raw:
        effects = [NewsEffect(kind=e["kind"], payload=e["payload"]) for e in entry["effects"]]
        cards.append(NewsCard(name=entry["name"], effects=effects))
    return cards


@dataclass
class EventDeckConfig:
    """Configurable composition for the event deck.

    Each count field accepts either a fixed int or a (min, max) tuple for
    random variation. Defaults are sourced from default_event_counts() which
    encodes the Events.csv composition (player-count conditionals included).

    `news_pool` is the JSON-friendly direct-NEWS pool (legacy NEWS event type
    with explicit market_deltas in payload). The data-driven NEWS_BULLETIN
    flow uses the news deck on GameState, populated from NEWS_EFFECTS.
    """
    power_bill_count: int | tuple[int, int] | None = None
    debt_collection_count: int | tuple[int, int] | None = None
    futures_trading_count: int | tuple[int, int] | None = None
    news_bulletin_count: int | tuple[int, int] | None = None
    patent_auction_count: int | tuple[int, int] | None = None
    draw_building_count: int | tuple[int, int] | None = None
    draw_building_redraw_count: int | tuple[int, int] | None = None
    # Legacy direct-NEWS payload pool (used by the JSON Advanced section).
    news_pool: list[EventCard] = field(default_factory=list)
    news_count: int | tuple[int, int] = 0


def default_event_counts(num_players: int, data_dir: Path | None = None) -> dict[str, int]:
    """Return the default per-event-type counts for a given player count.

    Reads Events.csv — the single source of truth for event deck composition.
    Each CSV row has a Condition column that filters by player count and a
    Redraw column that flags draw_building_card redraws.
    """
    from my_project.parsing import parse_event_counts
    if data_dir is None:
        data_dir = Path(__file__).parent / "data"
    return parse_event_counts(data_dir / "Events.csv", num_players)


def _resolve_count(spec: int | tuple[int, int]) -> int:
    """Resolve a count spec to a concrete int."""
    if isinstance(spec, tuple):
        return random.randint(spec[0], spec[1])
    return spec


def _resolve_or_default(spec, default):
    """Use the user-supplied count if given, else fall back to the default int."""
    if spec is None:
        return default
    return _resolve_count(spec)


def _build_event_pool(
    num_players: int,
    config: EventDeckConfig,
) -> list[EventCard]:
    """Build the event pool from Events.csv (without the round-end card).

    The round-end card (END_ROUND or END_GAME) is added by
    build_event_deck / reshuffle_for_next_round as the last card
    in the shuffled deck. It IS part of the deck — a player draws
    it as their event on the last turn of the round.
    """
    from my_project.parsing import parse_event_rows
    data_dir = Path(__file__).parent / "data"
    rows = parse_event_rows(data_dir / "Events.csv", num_players)

    _type_map = {e.value: e for e in EventType}

    events: list[EventCard] = []
    for spec in rows:
        event_str = spec["event"]
        et = _type_map.get(event_str)
        if et is None:
            continue
        count = spec["count"]
        if event_str == "draw_building_card":
            override_key = "draw_building_redraw_count" if spec["redraw"] else "draw_building_count"
        else:
            override_key = f"{event_str}_count"
        override = getattr(config, override_key, None)
        if override is not None:
            count = _resolve_count(override)
        # Store round2 conversion info on the event card
        r2_event = spec.get("round2_event", "")
        r2_redraw = spec.get("round2_redraw", "")
        for _ in range(count):
            ec = _ec(et, redraws=spec["redraw"], pwr_adjust=spec["pwr_adjust"])
            ec._round2_event = r2_event  # type: ignore[attr-defined]
            ec._round2_redraw = r2_redraw  # type: ignore[attr-defined]
            events.append(ec)

    # Legacy direct-NEWS pool (JSON Advanced section).
    news_n = _resolve_count(config.news_count)
    if news_n > 0 and config.news_pool:
        events.extend(random.choices(config.news_pool, k=news_n))

    # Each event keeps its own redraw flag from CSV. No redistribution.
    random.shuffle(events)
    return events


def build_event_deck(
    num_players: int,
    config: EventDeckConfig | None = None,
    num_rounds: int = 2,
    num_turns: int = 0,  # deprecated, ignored — kept for backward compat
) -> list[EventCard]:
    """Build a shuffled event deck for a multi-round game.

    IMPORTANT — TURN COUNTING:
    Every card in the deck is a player turn, INCLUDING END_ROUND and
    END_GAME. A player takes their actions, then draws and fires the
    event card as THEIR event for that turn. END_ROUND/END_GAME are
    not special "cleanup" cards — they are the last player's event.

    Cards with redraws=True chain: they fire, then ALSO draw and fire
    the next card. This consumes 2 deck slots for 1 player turn.
    So: player_turns = total_cards - redraw_cards.

    The deck is the same composition played twice (2 rounds). In
    round 2, patent auctions convert to draw_building_card (all
    patents have a Round2_Event conversion). The total card count
    per round should be the same.

    END_ROUND / END_GAME is placed as the last card of each round's
    shuffled deck. It is part of the deck — not an extra card.
    """
    cfg = config or EventDeckConfig()
    pool = _build_event_pool(num_players, cfg)

    # DECK SIZE (3P example):
    #   CSV produces 19 base cards (3 news + 2 debt + 1 power + 2 futures
    #   + 4 patent + 4 draw + 4 draw-redraw - 1 excluded by condition).
    #   Append END_ROUND = 20 total. This is correct. DO NOT CHANGE.
    round_events = list(pool)
    random.shuffle(round_events)
    terminal = EventType.END_GAME if num_rounds <= 1 else EventType.END_ROUND
    round_events.append(_ec(terminal))  # 19 + 1 = 20. Correct.
    return round_events


def reshuffle_for_next_round(
    state: GameState,
    num_players: int,
    current_round: int,
    num_rounds: int,
) -> None:
    """Reshuffle the event deck for the next round.

    Called when END_ROUND fires. Converts patent auctions to their
    round-2 replacements (all patents have a Round2_Event conversion),
    reshuffles, and places the round-end card (END_GAME or END_ROUND)
    at the bottom of the deck. The round-end card is part of the deck —
    a player draws it as their event on the last turn.
    """
    _type_map = {e.value: e for e in EventType}
    from my_project.parsing import _condition_matches

    # The base pool (without the round-end card) was stored at game creation.
    pool = list(state._event_pool)

    # Convert events for round 2+
    converted: list[EventCard] = []
    for e in pool:
        r2 = getattr(e, "_round2_event", "")
        if r2:
            r2_type = _type_map.get(r2)
            if r2_type:
                r2_cond = getattr(e, "_round2_redraw", "")
                is_redraw = bool(r2_cond) and _condition_matches(r2_cond, num_players)
                converted.append(_ec(r2_type, redraws=is_redraw, pwr_adjust=e.pwr_adjust))
            continue
        elif e.type == EventType.PATENT_AUCTION:
            continue  # patent with no round2 conversion → drop (shouldn't happen)
        converted.append(e)

    # Same deck size as round 1: 19 converted cards + 1 terminal = 20.
    random.shuffle(converted)
    is_last = current_round + 1 >= num_rounds
    terminal = EventType.END_GAME if is_last else EventType.END_ROUND
    converted.append(_ec(terminal))  # 19 + 1 = 20. Correct.

    # Replace the event deck and reset the index
    state.event_deck = converted
    state.event_idx = 0
    # Bump state.current_round so AI valuation (remaining_events_full_game)
    # can tell how many more rounds remain beyond the one being entered.
    state.current_round = current_round + 1


# --- Actions ---

class ActionType(StrEnum):
    BUILD = "build"
    SELL = "sell"
    CONTRACT = "contract"
    PASS = "pass"


@dataclass
class Action:
    action_type: ActionType
    build_cards: list[int] = field(default_factory=list)  # indices of cards to build
    sell_card: int = -1  # index of card to sell with
    contract_card: int = -1  # index of card to use for contract
    contract_idx: int = -1  # index into available_contracts
    # Special-building flags carried through to execute_*:
    use_launch_pad: bool = False   # use Launch Pad as the contract icon source
    elevator_target: str = ""      # which contract requirement to discount (resource value)
    # Per-sell Hacker Array choice (used by execute_sell when set)
    hacker_target: str = ""        # resource value (e.g. "GLS"); empty = no bonus
    hacker_direction: int = 0      # +1 / -1 / 0 for no bonus
    detail: str = ""


# --- Game State ---

@dataclass
class ActionRecord:
    """Structured record of a single action within a turn."""
    action_type: str
    detail: str
    buildings: list[str] = field(default_factory=list)
    build_costs_paid: dict[str, int] = field(default_factory=dict)  # resource -> amount bought
    build_money_spent: int = 0
    rates_gained: dict[str, int] = field(default_factory=dict)
    sell_resource: str = ""
    sell_amount: int = 0
    sell_revenue: int = 0
    contract_label: str = ""
    contract_rates_spent: dict[str, int] = field(default_factory=dict)
    contract_reward: int = 0
    contract_true_cost: float = 0.0  # net cost (after sell revenue)
    contract_gross_cost: float = 0.0  # gross cost (total invested, before revenue)


@dataclass
class TurnRecord:
    turn: int
    player: str
    action: str
    detail: str
    event: str
    money_before: int
    money_after: int
    debt: int = 0
    contracts_fulfilled: int = 0
    market_snapshot: dict[str, int] = field(default_factory=dict)
    rates_snapshot: dict[str, int] = field(default_factory=dict)
    actions: list[ActionRecord] = field(default_factory=list)
    free_actions: list[str] = field(default_factory=list)
    event_player_snapshots: list[dict] = field(default_factory=list)


@dataclass
class GameState:
    players: list[Player]
    market: Market
    deck: Deck
    contracts: list[Contract]
    available_contracts: list[Contract]
    pool: list[Card]
    event_deck: list[EventCard]
    # News deck consumed by NEWS_BULLETIN events. Drawn without replacement;
    # reshuffled when exhausted via _shuffle_news_deck().
    news_deck: list[NewsCard] = field(default_factory=list)
    news_idx: int = 0
    # Patent pile consumed by PATENT_AUCTION events. Drawn from the top
    # without replacement. When empty, auctions become no-ops.
    patent_pile: list[Card] = field(default_factory=list)
    patent_idx: int = 0
    # Per-player bid overrides for the next patent auction. Used by the
    # play adapter to thread human-supplied bids in. Key = seat index,
    # value = bid in $5 increments. Defaults to None for "use AI heuristic".
    pending_bids: dict[int, int] = field(default_factory=dict)
    # Human-supplied debt paydown amounts for the next debt collection.
    # Key = seat index, value = cash amount to pay toward debt.
    pending_debt_paydowns: dict[int, int] = field(default_factory=dict)
    # Mid-event interruption state. When set, the event resolution loop has
    # paused waiting for human input. The play adapter exposes this to the
    # UI and resumes after the resolve_prompt call.
    # Shape:
    #   {"kind": "patent_auction", "patent_card_dict": {...}}
    pending_prompt: dict | None = None
    # When pending_prompt is set, this captures the in-flight event so the
    # play adapter can resume it after the prompt is answered.
    _suspended_event: EventCard | None = field(default=None, repr=False)
    # When the suspended event was part of a redraw chain, True means there
    # might be more events to fire after the resume.
    _suspended_chain_active: bool = field(default=False, repr=False)
    turn: int = 0
    event_idx: int = 0
    log: GameLog = field(default_factory=GameLog)
    history: list[TurnRecord] = field(default_factory=list)
    # Event-driven economy tracking (summed across all players)
    pwr_total_earned: int = 0  # cash earned from positive PWR at power bills
    pwr_total_debt: int = 0  # debt incurred from negative PWR at power bills
    futures_total_debt: int = 0  # debt incurred from negative non-PWR rates at settlements
    # Per-resource event accounting:
    # bills_units_earned[r] = total positive rate units "sold" via power bills
    # bills_units_owed[r] = total negative rate units "bought" via power bills (PWR only)
    # futures_units_bought[r] = total negative rate units bought at futures settlements
    bills_units_earned: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    bills_units_owed: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    futures_units_bought: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    futures_debt_per_resource: dict[Resource, int] = field(default_factory=lambda: {r: 0 for r in Resource})
    # Per-event detail rows captured during the most recent event chain.
    # Cleared at the start of execute_event_with_redraws and populated by
    # _record_event_line as each do_* event runs. The play adapter snapshots
    # this list and surfaces it to the UI for the expandable event log row.
    # Lines are dicts shaped like:
    #   {"kind": "header", "text": "Power Bill — PWR @ $4"}
    #   {"kind": "note",   "text": "Energy Vault: +$40"}
    #   {"kind": "player", "player_idx": 0, "name": "Alice",
    #    "text": "+$12 (sold 3 PWR)",
    #    "money_after": 112, "debt_after": 0, "credit_after": 0,
    #    "net_worth_after": 112}
    last_event_lines: list[dict] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS
    num_rounds: int = 1
    # 1-indexed current deck-round. Starts at 1 and increments at each
    # reshuffle_for_next_round call. Read by remaining_events_full_game()
    # so AI valuation can account for events in un-started future rounds.
    current_round: int = 1

    def remaining_events(self) -> dict[EventType, int]:
        """Count remaining events from current position in event deck.

        IMPORTANT: only walks the CURRENT deck — future rounds that haven't
        been reshuffled in yet are invisible to this count. AI valuation
        code should call remaining_events_full_game() instead so the horizon
        includes the un-started rounds."""
        counts: dict[EventType, int] = {e: 0 for e in EventType}
        for ec in self.event_deck[self.event_idx:]:
            counts[ec.type] += 1
        return counts

    def remaining_events_full_game(self) -> dict[EventType, int]:
        """Events remaining across the WHOLE game, including unplayed future
        rounds. Needed so AI valuation (rate ongoing value, expected sell
        events, contract proximity, etc.) doesn't treat end-of-round as
        end-of-game.

        The future rounds' composition is deterministic: the stored base
        pool (_event_pool) goes through the same round-2 conversion applied
        at reshuffle — patents become draw_building_card with a redraw flag
        that depends on num_players — plus a terminal (END_ROUND for
        non-last rounds, END_GAME for the last)."""
        counts = dict(self.remaining_events())
        future_rounds = max(0, self.num_rounds - self.current_round)
        if future_rounds == 0:
            return counts

        # Per-round composition (converted pool + terminal). Runs once per
        # call; cheap (~20 entries in the pool).
        from my_project.parsing import _condition_matches
        type_map = {e.value: e for e in EventType}
        per_round: dict[EventType, int] = {e: 0 for e in EventType}
        pool = getattr(self, "_event_pool", []) or []
        num_players = max(len(self.players), 1)
        for e in pool:
            r2_name = getattr(e, "_round2_event", "")
            if r2_name:
                r2_type = type_map.get(r2_name)
                if r2_type:
                    # Count ALL entries (redraws too) — matches
                    # remaining_events() behavior on the current deck,
                    # which also counts redraw slots.
                    per_round[r2_type] = per_round.get(r2_type, 0) + 1
            elif e.type == EventType.PATENT_AUCTION:
                continue  # dropped at reshuffle when no r2 conversion
            else:
                per_round[e.type] = per_round.get(e.type, 0) + 1

        # Terminal card: last un-started round is END_GAME, middle rounds
        # END_ROUND. Both count as settlements for valuation, but stay
        # precise so consumers that care about the distinction get it.
        for r_offset in range(future_rounds):
            is_last = (self.current_round + 1 + r_offset) == self.num_rounds
            term = EventType.END_GAME if is_last else EventType.END_ROUND
            for t, n in per_round.items():
                counts[t] = counts.get(t, 0) + n
            counts[term] = counts.get(term, 0) + 1
        return counts

    def _init_observables(self) -> None:
        """Upgrade pool to CardZone, wire market log, init player observables."""
        object.__setattr__(
            self, "pool", CardZone(self.pool, self.log, "pool"),
        )
        self.market._log = self.log
        for idx, p in enumerate(self.players):
            p._init_observables(self.log, idx)
        self._log_setup()

    def _log_setup(self) -> None:
        """Record the initial game state as a 'setup' action entry."""
        self.log.begin("setup", "", -1, "Game setup")
        # Market positions (from default tier, before any randomization)
        for r in Resource:
            pos = self.market.positions[r]
            self.log.record(f"market.{r.value}", 0, pos)
        # Pool
        self.log.record("pool", [], [CardZone._card_desc(c) for c in self.pool])
        # Players
        for idx, p in enumerate(self.players):
            prefix = f"player.{idx}"
            self.log.record(f"{prefix}.name", "", p.name)
            if p.corporation:
                self.log.record(f"{prefix}.corporation", "", p.corporation)
            self.log.record(f"{prefix}.money", 0, p.money)
            self.log.record(f"{prefix}.debt", 0, p.debt)
            self.log.record(f"{prefix}.credit", 0, p.credit)
            for r in Resource:
                v = p.rates.get(r, 0)
                if v != 0:
                    self.log.record(f"{prefix}.rates.{r.value}", 0, v)
            if p.hand:
                self.log.record(f"{prefix}.hand", [], [CardZone._card_desc(c) for c in p.hand])
        self.log.end()

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "pool":
            existing = object.__getattribute__(self, "__dict__").get("pool")
            if isinstance(existing, CardZone):
                existing.replace(value if isinstance(value, list) else list(value))
                return
        object.__setattr__(self, name, value)

    @classmethod
    def create(
        cls,
        all_cards: list[Card],
        all_contracts: list[Contract],
        num_players: int = 1,
        start_money: int = DEFAULT_START_MONEY,
        start_market_pos: int | None = None,
        randomize_market: bool = False,
        max_turns: int = DEFAULT_MAX_TURNS,
        corporation_rates: list[dict[Resource, int]] | None = None,
        event_deck_config: EventDeckConfig | None = None,
        event_deck: list[EventCard] | None = None,
        news_deck: list[NewsCard] | None = None,
        patent_pile: list[Card] | None = None,
        num_rounds: int = 1,
        skip_corporations: bool = False,
    ) -> GameState:
        market = Market.create(start_market_pos)

        # All cards from Cards.csv are playable. Cards with an `effect`
        # column have their mechanical effects wired up via patent/build
        # hooks keyed on the building name. Unknown effects are harmless —
        # the card plays as a vanilla building with its rates/costs.
        deck = Deck.from_cards(list(all_cards))

        # Draw contracts
        contracts = list(all_contracts)
        random.shuffle(contracts)
        num_available = CONTRACTS_AVAILABLE_BASE + num_players
        available = contracts[:num_available]
        remaining_contracts = contracts[num_available:]

        # Draw pool
        pool = deck.draw(POOL_SIZE)

        # Build event deck (use explicit deck if provided, else build from config)
        if event_deck is None:
            event_deck = build_event_deck(
                num_players, event_deck_config, num_rounds=num_rounds,
            )
        # Store the base event pool for reshuffling at round boundaries
        _event_pool = _build_event_pool(num_players, event_deck_config or EventDeckConfig())

        # Build news deck (defaults to one card per NEWS_EFFECTS entry,
        # shuffled). Callers can supply a custom list for tests / playtesting.
        if news_deck is None:
            news_deck = build_default_news_deck()
            random.shuffle(news_deck)

        # Patent pile (shuffled at game start). Empty list = no patents
        # available; auctions become no-ops in that case.
        if patent_pile is None:
            patent_pile = []
        else:
            patent_pile = list(patent_pile)
            random.shuffle(patent_pile)

        # Create players. Assign unique corporations randomly (capped at # of corps).
        # When skip_corporations is True (draft mode), leave corp/rates empty;
        # PlayableGame will apply them via apply_corporation() as picks come in.
        corp_pool: list[tuple[str, dict[str, int]]] = []
        if not skip_corporations:
            corp_pool = list(_load_corporations())
            random.shuffle(corp_pool)
        players = []
        for i in range(num_players):
            p = Player(name=f"Player_{i+1}", money=start_money)

            # Assign corporation if explicit rates not provided
            if corporation_rates and i < len(corporation_rates):
                for r, v in corporation_rates[i].items():
                    p.rates[r] = v
            elif i < len(corp_pool):
                corp_name, corp_rates = corp_pool[i]
                apply_corporation(p, corp_name, corp_rates)

            hand = deck.draw(p.hand_size)
            p.hand = hand
            players.append(p)

        state = cls(
            players=players,
            market=market,
            deck=deck,
            contracts=remaining_contracts,
            available_contracts=available,
            pool=pool,
            event_deck=event_deck,
            news_deck=news_deck,
            patent_pile=patent_pile,
            max_turns=max_turns,
            num_rounds=num_rounds,
        )
        state._event_pool = _event_pool
        state._init_observables()

        # Randomize market AFTER observables are wired so the d20 rolls
        # are captured as individual market mutations in the log.
        if randomize_market:
            state.log.begin("market_roll", "", -1, "Market randomization")
            for r in Resource:
                roll = random.choice([3, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, -2, -2, -2, -3, -3, -4, -4, 0])
                state.market.adjust(r, roll)
            state.log.end()

        return state


# --- Build cost calculation ---

def compute_build_deficit(
    cards: list[Card],
    player: Player,
    market: Market,
) -> tuple[dict[Resource, int], int] | None:
    """Compute the market deficit and estimated cost for building multiple cards.

    Returns (deficit_per_resource, estimated_total_cost) or None if unaffordable.
    Deficit = total cost per resource - player rate.
    """
    combined: dict[Resource, int] = defaultdict(int)
    for card in cards:
        for ra in card.costs:
            combined[ra.resource] += ra.amount

    deficit: dict[Resource, int] = {}
    for resource, total_cost in combined.items():
        d = max(0, total_cost - max(0, player.rate(resource)))
        if d > 0:
            deficit[resource] = d

    total_cost = 0
    for resource, amount in deficit.items():
        total_cost += market.estimate_buy_cost(resource, amount)

    if total_cost > player.money:
        return None

    return deficit, total_cost


# --- Rate valuation ---

def compute_rate_time_value(resource: Resource, state: GameState) -> float:
    """Compute the time-dependent value of +1 rate of a resource.

    END_GAME and END_ROUND events fire both a power bill and a futures
    settlement, so they count toward both PWR and non-PWR collection
    totals.  FUTURES_TRADING events only push prices (no debt).
    """
    remaining = state.remaining_events_full_game()
    price = state.market.price(resource)
    end_game = remaining.get(EventType.END_GAME, 0)
    end_round = remaining.get(EventType.END_ROUND, 0)

    if resource == Resource.PWR:
        collections = remaining.get(EventType.POWER_BILL, 0) + end_round + end_game
        return price * collections
    else:
        # Debt only at END_ROUND and END_GAME (not FUTURES_TRADING)
        collections = end_round + end_game
        return price * collections


# --- Simulation Engine ---

def execute_build(
    state: GameState,
    player: Player,
    build_indices: list[int],
    patent_office_pick: int | None = None,
) -> ActionRecord | None:
    """Play one or more building cards.

    Returns ActionRecord on success, None if:
      - the player has already built this turn (unless Matter Replication),
      - the build cards would exceed MAX_CARDS_PER_TURN.
    """
    if player.has_built_this_turn and not _player_owns_patent(player, "Matter Replication"):
        return None

    n_build_cards = len(build_indices)
    if n_build_cards == 0:
        return None
    if n_build_cards > player.cards_remaining():
        return None

    build_cards = [player.hand[i] for i in build_indices]

    # One-of-each special-building constraint: a player can only ever own
    # ONE copy of any slot-4 special. The `effect` field is the slot-4
    # marker (it's empty for ordinary buildings). This also rejects
    # multi-card builds that include duplicates of the same special.
    seen_specials_this_build: set[str] = set()
    for card in build_cards:
        if not card.effect:
            continue
        if _count_buildings(player, card.building) > 0:
            return None
        if card.building in seen_specials_this_build:
            return None
        seen_specials_this_build.add(card.building)

    # Patent build-time hooks. If the player owns any patent in
    # PATENT_BUILD_HOOKS, deep-copy the build cards before mutating them
    # so we don't corrupt the prototype shared across the deck/pool/hand.
    # The mutated copies are what get applied AND what land in
    # buildings_played, so future patent triggers see the modified rates.
    patent_names = _player_patent_names(player)
    has_patent_hooks = any(name in PATENT_BUILD_HOOKS for name in patent_names)
    if has_patent_hooks:
        import copy as _copy
        build_cards = [_copy.deepcopy(c) for c in build_cards]
        for card in build_cards:
            for name in patent_names:
                hook = PATENT_BUILD_HOOKS.get(name)
                if hook:
                    hook(state, player, card)

    result = compute_build_deficit(build_cards, player, state.market)
    if result is None:
        return None

    deficit, _ = result

    # Log action
    card_names = [c.building for c in build_cards]
    pidx = state.players.index(player)
    state.log.begin("build", player.name, pidx, f"Build {', '.join(card_names)}")

    # Actually buy from market
    total_cost = 0
    costs_paid: dict[str, int] = {}
    cost_detail = []
    for resource, amount in deficit.items():
        spent = state.market.buy(resource, amount)
        total_cost += spent
        costs_paid[resource.value] = amount
        cost_detail.append(f"{amount} {resource.value}=${spent}")
        # Per-player flow tracking
        player.flow_bought_units[resource] += amount
        player.flow_buy_cost[resource] += spent

    player.money -= total_cost

    # Aggregate rates across all cards
    all_costs: list[ResourceAmount] = []
    positive_rates: list[ResourceAmount] = []
    negative_rates: list[ResourceAmount] = []
    rates_gained: dict[str, int] = {}

    for card in build_cards:
        all_costs.extend(card.costs)
        for ra in card.rates:
            rates_gained[ra.resource.value] = rates_gained.get(ra.resource.value, 0) + ra.amount
            if ra.amount > 0:
                positive_rates.append(ra)
            elif ra.amount < 0:
                negative_rates.append(ResourceAmount(ra.resource, abs(ra.amount)))

    # Record in cost ledger (before applying rates to player)
    market_prices = {r: state.market.price(r) for r in Resource}
    player.ledger.record_build(
        costs=all_costs,
        positive_rates=positive_rates,
        negative_rates=negative_rates,
        market_spend=total_cost,
        market_prices=market_prices,
        player_rates=dict(player.rates),
    )

    # Apply rates to player
    for card in build_cards:
        player.apply_rates(card)
        player.buildings_played.append(card)
        # Patent Office build-time trigger: draw 2 patents, keep one,
        # return the other. AI auto-picks; human pick_idx comes from the
        # play adapter.
        if card.building == "Patent Office":
            _patent_office_trigger(state, player, pick_idx=patent_office_pick)

    # Remove cards from hand (highest indices first to avoid shifting) → discard pile
    all_indices = sorted(set(build_indices), reverse=True)
    for idx in all_indices:
        state.deck.discard.append(player.hand.pop(idx))

    # Enforce one build per turn
    player.has_built_this_turn = True
    player.cards_spent_this_turn += n_build_cards

    names = ", ".join(c.building for c in build_cards)
    detail = f"Built {names}"
    if cost_detail:
        detail += f" (bought {', '.join(cost_detail)})"

    state.log.annotate("buildings", [c.building for c in build_cards])
    state.log.annotate("costs_paid", costs_paid)
    state.log.annotate("money_spent", total_cost)
    state.log.annotate("rates_gained", rates_gained)
    state.log.end()
    return ActionRecord(
        action_type="build",
        detail=detail,
        buildings=[c.building for c in build_cards],
        build_costs_paid=costs_paid,
        build_money_spent=total_cost,
        rates_gained=rates_gained,
    )


def execute_sell(
    state: GameState,
    player: Player,
    card_idx: int,
    sell_resource: str | None = None,
    hacker_target: str | None = None,
    hacker_direction: int = 0,
) -> ActionRecord | None:
    """Sell resources using a card's alternate sell types.

    `sell_resource`: when provided (e.g. "FE"), sells that specific resource
    instead of auto-picking the highest-revenue one. The resource must be
    in the card's can_sell list and the player must have a positive rate.
    When None (AI default), auto-picks the best.

    `hacker_target` + `hacker_direction` are used by the Hacker Array picker:
    if the player owns a Hacker Array, they can specify a non-sold resource
    and a direction (+1 or -1) to bump the market by ±3. If the params
    aren't supplied (or the player doesn't own an HA), no bonus fires.

    Returns None if the player has no remaining card budget (sell spends 1
    hand card).
    """
    if player.cards_remaining() < 1:
        return None
    card = player.hand[card_idx]
    pidx = state.players.index(player)
    best_resource = None
    best_revenue = 0

    if sell_resource is not None:
        # Human chose a specific resource
        try:
            res = Resource(sell_resource)
        except ValueError:
            return None
        if res not in card.can_sell:
            return None
        rate = max(0, player.rate(res))
        if rate > 0:
            best_resource = res
            best_revenue = state.market.price(res) * rate
    else:
        # AI auto-pick: highest revenue
        for sell_res in card.can_sell:
            rate = max(0, player.rate(sell_res))
            if rate > 0:
                revenue = state.market.price(sell_res) * rate
                if revenue > best_revenue:
                    best_revenue = revenue
                    best_resource = sell_res

    if best_resource is None:
        state.log.begin("sell", player.name, pidx, f"Sell {card.building} (no resources)")
        state.deck.discard.append(player.hand.pop(card_idx))
        player.cards_spent_this_turn += 1
        state.log.annotate("sell_resource", "")
        state.log.annotate("sell_amount", 0)
        state.log.annotate("sell_revenue", 0)
        state.log.end()
        return ActionRecord(action_type="sell", detail="Sold (no matching resources)")

    rate = max(0, player.rate(best_resource))
    state.log.begin("sell", player.name, pidx, f"Sell {rate} {best_resource.value} via {card.building}")
    revenue = state.market.sell(best_resource, rate)
    player.money += revenue
    player.ledger.record_sell(best_resource, revenue)
    # Per-player flow tracking
    player.flow_sold_units[best_resource] += rate
    player.flow_sell_revenue[best_resource] += revenue
    state.deck.discard.append(player.hand.pop(card_idx))

    # Hacker Array bonus: only fires if the player owns one AND supplied a
    # target+direction (e.g. via the picker UI). A "no choice" sell skips
    # the bonus entirely — that matches the rule "the player chooses".
    detail_extra = ""
    ha_count = _count_buildings(player, "Hacker Array")
    if ha_count > 0 and hacker_target and hacker_direction != 0:
        try:
            target = Resource(hacker_target)
            if target != best_resource:
                delta = 3 if hacker_direction > 0 else -3
                state.market.adjust(target, delta)
                sign = "+" if delta >= 0 else ""
                detail_extra = f" [HA: {sign}{delta} {target.value}]"
        except ValueError:
            pass

    player.cards_spent_this_turn += 1
    state.log.annotate("sell_resource", best_resource.value)
    state.log.annotate("sell_amount", rate)
    state.log.annotate("sell_revenue", revenue)
    state.log.end()
    return ActionRecord(
        action_type="sell",
        detail=f"Sold {rate} {best_resource.value} for ${revenue}{detail_extra}",
        sell_resource=best_resource.value,
        sell_amount=rate,
        sell_revenue=revenue,
    )


def execute_contract(
    state: GameState,
    player: Player,
    card_idx: int,
    contract_idx: int,
    *,
    use_launch_pad: bool = False,
    elevator_target: str | None = None,
) -> ActionRecord | None:
    """Fulfill a contract.

    Two ways to fulfill a contract:

    1. **Hand card** (default): pass `card_idx` pointing at a hand card
       with `can_fulfill_contract=True`. Costs 1 AP.

    2. **Launch Pad** (free): pass `use_launch_pad=True`. Skips the
       hand-card requirement entirely. **Free** (0 AP) but still gated by
       `has_used_launch_pad_this_turn` (only one Launch Pad contract per
       turn). `card_idx` is ignored.

    Space Elevator: if the player owns one, the contract always gets -1
    to ONE resource (floor at 0). The player picks which resource via
    `elevator_target` (e.g. "FE"); if not provided, defaults to the
    largest requirement (best default for AI). There is no per-turn
    limit — every contract fulfilled while SE is owned gets the discount.

    Returns ActionRecord on success, None if any precondition fails
    (including insufficient action points).
    """
    if contract_idx < 0 or contract_idx >= len(state.available_contracts):
        return None
    contract = state.available_contracts[contract_idx]

    # Determine which path is being taken and how many hand cards will be
    # consumed. Launch Pad spends 0 cards (it IS the free icon).
    if use_launch_pad:
        cards_cost = 0
    else:
        cards_cost = 1  # the contract-icon hand card
    if cards_cost > player.cards_remaining():
        return None

    # Validate Launch Pad path
    if use_launch_pad:
        if player.has_used_launch_pad_this_turn:
            return None
        if _count_buildings(player, "Launch Pad") == 0:
            return None
    else:
        if card_idx < 0 or card_idx >= len(player.hand):
            return None
        if not player.hand[card_idx].can_fulfill_contract:
            return None

    # Space Elevator always applies if the player owns one.
    has_elevator = _count_buildings(player, "Space Elevator") > 0
    effective_reqs = effective_contract_requirements(
        player, contract, apply_elevator=has_elevator, elevator_target=elevator_target
    )

    # Check if player can afford the (discounted) rate costs.
    # A requirement of 0 (e.g. discounted by SE) is always affordable
    # regardless of the player's rate in that resource.
    for req in effective_reqs:
        if req.amount > 0 and player.rate(req.resource) < req.amount:
            return None

    # Log action
    pidx = state.players.index(player)
    req_label = ", ".join(f"{r.amount} {r.resource.value}" for r in contract.requirements)
    state.log.begin("contract", player.name, pidx, f"Contract ({req_label}) for ${contract.reward}")

    # Compute costs from ledger before spending rates (uses discounted reqs)
    contract_true_cost = player.ledger.contract_cost(effective_reqs)
    contract_gross_cost = player.ledger.contract_gross_cost(effective_reqs)

    # Spend rates permanently (the effective amount, not the original)
    rates_spent: dict[str, int] = {}
    for req in effective_reqs:
        if req.amount > 0:
            player.rates[req.resource] -= req.amount
            rates_spent[req.resource.value] = req.amount

    # Record in ledger
    player.ledger.record_contract(effective_reqs)

    # Contract reward: pay off existing debt first, leftover becomes credit.
    # Credit counts toward net worth and absorbs FUTURE debt before it
    # becomes real debt (see _apply_debt). It is NOT spendable as cash.
    debt_payoff = min(player.debt, contract.reward)
    player.debt -= debt_payoff
    leftover = contract.reward - debt_payoff
    if leftover > 0:
        player.credit += leftover
    player.contracts_fulfilled += 1

    # Burn the contract-icon card (Launch Pad doesn't consume a card).
    if use_launch_pad:
        pass  # no card consumed — Launch Pad is the free icon
    else:
        state.deck.discard.append(player.hand.pop(card_idx))

    # Set per-turn flags
    if use_launch_pad:
        player.has_used_launch_pad_this_turn = True
    # Count hand cards spent toward per-turn cap
    player.cards_spent_this_turn += cards_cost

    # Display label uses the ORIGINAL requirements so the log is consistent
    # (the discount is reflected in rates_spent for analytics).
    req_str = ", ".join(f"{r.amount} {r.resource.value}" for r in contract.requirements)
    label = req_str
    if has_elevator and contract.requirements:
        # Pick which resource was discounted for the log. Matches the
        # default-target logic in effective_contract_requirements.
        if elevator_target:
            target = elevator_target
        else:
            largest = max(contract.requirements, key=lambda r: r.amount)
            target = largest.resource.value
        label += f" [SE -1 {target}]"
    if use_launch_pad:
        label += " [LP]"

    # Replace contract
    state.available_contracts.pop(contract_idx)
    if state.contracts:
        state.available_contracts.append(state.contracts.pop())

    state.log.annotate("contract_label", label)
    state.log.annotate("rates_spent", rates_spent)
    state.log.annotate("reward", contract.reward)
    state.log.annotate("true_cost", round(contract_true_cost, 2))
    state.log.annotate("gross_cost", round(contract_gross_cost, 2))
    state.log.end()
    return ActionRecord(
        action_type="contract",
        detail=f"Fulfilled contract ({label}) for ${contract.reward}",
        contract_label=label,
        contract_rates_spent=rates_spent,
        contract_reward=contract.reward,
        contract_true_cost=round(contract_true_cost, 2),
        contract_gross_cost=round(contract_gross_cost, 2),
    )


# --- Special-building helpers ---

def _count_buildings(player: Player, name: str) -> int:
    """Number of copies of `name` in the player's buildings_played."""
    return sum(1 for c in player.buildings_played if c.building == name)


# --- Building tags (used by patent build hooks) ---
#
# A card is "tagged" with each resource it produces a positive rate of.
# Tags are NOT exclusive — a card with `+1 PWR, +1 FE` is BOTH a power
# building AND an iron building. Patent hooks like Superconductors and
# Slant Drilling read these tags to decide whether to fire on a build.

RESOURCE_TAGS: dict[Resource, str] = {
    Resource.PWR: "power",
    Resource.H2O: "water",
    Resource.FE: "iron",
    Resource.C: "carbon",
    Resource.SI: "silicon",
    Resource.O2: "oxygen",
    Resource.FOOD: "food",
    Resource.GLS: "glass",
    Resource.ELX: "electronics",
}


def building_tags(card: Card) -> set[str]:
    """Set of resource tags this card produces (positive rates only).

    A card with `+1 PWR, +1 FE` returns {"power", "iron"}. Empty if the
    card has no positive rates (e.g. a building that only consumes).
    """
    return {RESOURCE_TAGS[ra.resource] for ra in card.rates if ra.amount > 0}


# --- Patent helpers ---
#
# Patents are slot=5 Card instances. They live in player.buildings_played
# alongside normal buildings, but their `slot == 5` distinguishes them.

def _player_patent_names(player: Player) -> list[str]:
    """Names of all patents (slot-5 cards) the player owns."""
    return [c.building for c in player.buildings_played if c.slot == 5]


def _player_owns_patent(player: Player, patent_name: str) -> bool:
    """True iff the player owns a patent with this exact name."""
    return any(
        c.slot == 5 and c.building == patent_name for c in player.buildings_played
    )


def _bump_rate(card: Card, resource: Resource, amount: int) -> None:
    """Add `amount` to the card's rate for `resource`. ResourceAmount is
    frozen, so we replace the matching entry with a new instance (or
    append a new entry if no rate for that resource exists yet).

    Used by build-hook patents that add to a card's rates (Superconductors,
    Cold Fusion, Slant Drilling).
    """
    for i, ra in enumerate(card.rates):
        if ra.resource == resource:
            card.rates[i] = ResourceAmount(resource=resource, amount=ra.amount + amount)
            return
    card.rates.append(ResourceAmount(resource=resource, amount=amount))


# --- Patent build-time hooks ---
#
# These run inside execute_build BEFORE the card's rates and costs are
# applied. They mutate a deep-copied card so the prototype shared across
# the deck/pool/hand is unaffected. The hook only fires for the player who
# owns the patent.

def _hook_superconductors(state: GameState, player: Player, card: Card) -> None:
    """New Power Buildings: +1 PWR. Bumps PWR rate on any power-tagged card."""
    if "power" not in building_tags(card):
        return
    _bump_rate(card, Resource.PWR, +1)


def _hook_cold_fusion(state: GameState, player: Player, card: Card) -> None:
    """+1 PWR from new Water Buildings. Bumps PWR on any water-tagged card."""
    if "water" not in building_tags(card):
        return
    _bump_rate(card, Resource.PWR, +1)


def _hook_slant_drilling(state: GameState, player: Player, card: Card) -> None:
    """+1 from new FE/SI Buildings.

    A card tagged `iron` gets +1 FE; a card tagged `silicon` gets +1 SI.
    Both can apply if the card has both tags.
    """
    tags = building_tags(card)
    if "iron" in tags:
        _bump_rate(card, Resource.FE, +1)
    if "silicon" in tags:
        _bump_rate(card, Resource.SI, +1)


def _hook_perpetual_motion(state: GameState, player: Player, card: Card) -> None:
    """New H2O/FE/C/SI buildings consume no PWR.

    Strips negative PWR rates from cards tagged water/iron/carbon/silicon.
    Positive PWR rates and the card's COSTS are unaffected — the patent
    text says "consume" which we interpret as the per-turn negative rate.
    """
    tags = building_tags(card)
    if not (tags & {"water", "iron", "carbon", "silicon"}):
        return
    card.rates = [
        ra for ra in card.rates
        if not (ra.resource == Resource.PWR and ra.amount < 0)
    ]


def _hook_carbon_scrubbing(state: GameState, player: Player, card: Card) -> None:
    """New buildings do not produce negative rates of C or O2.

    Strips negative C and O2 rates from the card. Build-time costs are
    UNTOUCHED (the patent only affects the per-turn drain, not what you
    pay to put the building down). Positive C/O2 rates are also untouched.
    """
    card.rates = [
        ra for ra in card.rates
        if not (ra.resource in (Resource.C, Resource.O2) and ra.amount < 0)
    ]



# Energy Vault no longer has a build hook — it only affects Power Bills.
# The old _hook_energy_vault (which absorbed negative PWR from new builds)
# has been removed. The vault now shields the player from paying power
# bill debt when they have a negative rate, up to 10 uses.


# Patent name → build hook (signature: (state, player, card) -> None).
# Only patents that mutate a card at build time go here. Passive event hooks
# (Financial Instruments, Virtual Reality) and active patents (Water Engine,
# Nanotechnology, Teleportation) live elsewhere.
PATENT_BUILD_HOOKS = {
    "Superconductors": _hook_superconductors,
    "Cold Fusion": _hook_cold_fusion,
    "Slant Drilling": _hook_slant_drilling,
    "Perpetual Motion": _hook_perpetual_motion,
    "Carbon Scrubbing": _hook_carbon_scrubbing,
}


def _apply_patent_acquisition(state: GameState, player: Player, patent: Card) -> None:
    """Run any one-shot/initialization effects when a player WINS a patent.

    Called from settle_silent_auction after the patent is appended to
    buildings_played but before normal apply_rates would (which is also
    called by settle for any inherent rate effects, though most CSV
    patents have empty rates).

    Currently handles:
      - Energy Vault: initialize the 10-PWR vault
      - Thinking Machines: draw 1 card and bump hand_size by 1
    """
    if patent.building == "Energy Vault":
        player.patent_state["energy_vault"] = 10
    elif patent.building == "Thinking Machines":
        # Draw 1 card immediately
        player.hand.extend(state.deck.draw(1))
        # Permanently bump hand_size so future draws keep one extra
        player.hand_size += 1


# Pleasure Dome bonus tiers: indexed by GLOBAL number of PDs in play - 1.
# Source: Cards.csv "Power Bill: $20/$15/$10 if 1/2/3 PD in play".
# Each owner who has at least one PD receives the same per-owner amount
# from the tier — so:
#   1 PD globally  → that owner gets $20
#   2 PDs globally → each owner gets $15
#   3+ PDs globally → each owner gets $10
# With one-of-each enforcement, "PDs globally" == number of distinct
# owners (each owner has 0 or 1 PD).
PLEASURE_DOME_TIERS = [int(x) for x in _CFG.get("pleasure_dome_tiers", "20,15,10").split(",")]


def _global_dome_count(state: GameState) -> int:
    """Total number of Pleasure Domes across all players."""
    return sum(_count_buildings(p, "Pleasure Dome") for p in state.players)


def _pleasure_dome_bonus(state: GameState, player: Player) -> int:
    """Per-owner power-bill bonus from Pleasure Dome.

    The tier is keyed on the GLOBAL number of PDs in play, not on this
    player's count. Each owner who has at least one PD gets the same
    per-owner amount from that tier.

    Virtual Reality patent doubles this bonus for its owner (only if they
    also own a Pleasure Dome — no PD = no doubling because there's nothing
    to double).
    """
    if _count_buildings(player, "Pleasure Dome") == 0:
        return 0
    total = _global_dome_count(state)
    bonus = PLEASURE_DOME_TIERS[min(total - 1, len(PLEASURE_DOME_TIERS) - 1)]
    if _player_owns_patent(player, "Virtual Reality"):
        bonus *= 2
    return bonus


def _patent_office_trigger(
    state: GameState, player: Player, pick_idx: int | None = None,
) -> list[Card]:
    """Build-time trigger: draw 2 patents, keep one, return the other.

    When `pick_idx` is None (AI), auto-picks the patent with the higher
    total positive rate sum (or the higher AI_Value from the CSV).
    When `pick_idx` is 0 or 1 (human), keeps the indicated patent.

    The kept patent is appended to player.buildings_played and its rates
    are applied. The returned patent goes back on TOP of the pile.

    Returns the list of drawn patents (for UI display). Empty list if
    no patents are available.
    """
    available = len(state.patent_pile) - state.patent_idx
    if available <= 0:
        return []
    drawn: list[Card] = []
    for _ in range(min(2, available)):
        drawn.append(state.patent_pile[state.patent_idx])
        state.patent_idx += 1

    if len(drawn) == 1:
        kept = drawn[0]
    else:
        # Determine which to keep
        if pick_idx is not None and 0 <= pick_idx < len(drawn):
            kept_i = pick_idx
        else:
            # AI auto-pick: higher AI_Value, then positive rate sum as tiebreaker
            values = _get_patent_base_values()
            def _patent_score(c: Card) -> float:
                base = values.get(c.building, 0)
                rate_sum = sum(ra.amount for ra in c.rates if ra.amount > 0)
                return base + rate_sum
            kept_i = 0 if _patent_score(drawn[0]) >= _patent_score(drawn[1]) else 1
        returned_i = 1 - kept_i
        kept = drawn[kept_i]
        returned = drawn[returned_i]
        # Put the returned patent back on TOP of the pile
        state.patent_idx -= 1
        state.patent_pile[state.patent_idx] = returned

    player.buildings_played.append(kept)
    player.apply_rates(kept)
    _apply_patent_acquisition(state, player, kept)
    return drawn


def effective_contract_requirements(
    player: Player,
    contract: Contract,
    apply_elevator: bool = False,
    elevator_target: str | None = None,
) -> list[ResourceAmount]:
    """Return contract requirements, optionally with Space Elevator -1.

    Space Elevator gives -1 to ONE resource on the contract. The discount
    applies whenever:
      - the player owns a Space Elevator
      - `apply_elevator` is True (callers that want plain reqs pass False)

    Space Elevator is always-on (no per-turn limit). `elevator_target` is
    the resource value of one of the contract's requirements (e.g. "FE");
    if None, defaults to the LARGEST requirement (best auto-pick). Floor at 0.
    """
    if not apply_elevator or _count_buildings(player, "Space Elevator") == 0:
        return list(contract.requirements)
    if not contract.requirements:
        return []
    # Pick which req to discount. If elevator_target is specified (human),
    # use that. Otherwise discount the largest requirement (saves the
    # most rate).
    target_idx = 0
    if elevator_target:
        for i, req in enumerate(contract.requirements):
            if req.resource.value == elevator_target:
                target_idx = i
                break
    else:
        # Default: discount the largest requirement
        best_amount = -1
        for i, req in enumerate(contract.requirements):
            if req.amount > best_amount:
                best_amount = req.amount
                target_idx = i
    out: list[ResourceAmount] = []
    for i, req in enumerate(contract.requirements):
        if i == target_idx:
            out.append(ResourceAmount(resource=req.resource, amount=max(0, req.amount - 1)))
        else:
            out.append(ResourceAmount(resource=req.resource, amount=req.amount))
    return out


def can_use_launch_pad(player: Player) -> bool:
    """True iff the player owns a Launch Pad and hasn't used it this turn."""
    return (
        _count_buildings(player, "Launch Pad") > 0
        and not player.has_used_launch_pad_this_turn
    )


# --- Events ---
# Event handlers moved to my_project/events.py; re-imported below.

# --- Pool Swapping ---

def swap_pool_card(state: GameState, player: Player, hand_idx: int, pool_idx: int) -> None:
    """Swap a card from the player's hand with a card from the pool."""
    pidx = state.players.index(player)
    h_name = player.hand[hand_idx].building
    p_name = state.pool[pool_idx].building
    state.log.begin("swap", player.name, pidx, f"Swap {h_name} ↔ {p_name}")
    player.hand[hand_idx], state.pool[pool_idx] = state.pool[pool_idx], player.hand[hand_idx]
    state.log.end()


# --- Turn & Game ---

def _execute_action(state: GameState, player: Player, action: Action) -> ActionRecord | None:
    """Execute a single action. Returns ActionRecord or None on failure."""
    if action.action_type == ActionType.BUILD and action.build_cards:
        return execute_build(state, player, action.build_cards)

    elif action.action_type == ActionType.SELL and action.sell_card >= 0:
        return execute_sell(
            state,
            player,
            action.sell_card,
            hacker_target=action.hacker_target or None,
            hacker_direction=action.hacker_direction,
        )

    elif action.action_type == ActionType.CONTRACT:
        # Launch Pad path doesn't need a contract_card; the normal path does.
        if not action.use_launch_pad and action.contract_card < 0:
            return None
        return execute_contract(
            state,
            player,
            action.contract_card,
            action.contract_idx,
            use_launch_pad=action.use_launch_pad,
            elevator_target=action.elevator_target or None,
        )

    return None


def _expected_price(resource: Resource, state: GameState) -> float:
    """Estimate the average price of a resource over remaining events.

    Projects market drift from all players' net rates (positive rates
    push price down from selling, negative rates push up from buying).
    Returns the average between current and projected price.
    """
    if not state.players:
        return float(state.market.price(resource))

    remaining = state.remaining_events_full_game()

    # For PWR, use PWR_ADJUST events; for others, use sell/settlement events
    if resource == Resource.PWR:
        num_adjusts = remaining.get(EventType.PWR_ADJUST, 0)
        avg_rate = sum(p.rate(resource) for p in state.players) / len(state.players)
        expected_shift = -avg_rate * num_adjusts
    else:
        # Non-PWR: selling pushes price down, futures buying pushes up.
        # Net effect over remaining player turns.
        total_events = sum(remaining.values())
        player_turns = total_events / len(state.players)
        avg_rate = sum(p.rate(resource) for p in state.players) / len(state.players)
        # Positive avg rate = net selling → price drops
        expected_shift = -avg_rate * player_turns

    current_pos = state.market.positions[resource]
    projected_pos = max(0, min(
        current_pos + expected_shift, len(PRICE_TRACK) - 1,
    ))
    avg_pos = int(round((current_pos + projected_pos) / 2))
    avg_pos = max(0, min(avg_pos, len(PRICE_TRACK) - 1))
    return float(PRICE_TRACK[avg_pos])


def _expected_sell_events(
    resource: Resource, state: GameState, player: Player,
) -> float:
    """Expected number of sell events for this resource over remaining game.

    Computed from:
    - Remaining player turns (from event deck)
    - Fraction of remaining deck cards that can sell THIS resource
    - Visible cards per turn (hand + pool, pool swaps are free)
    - AP budget cap

    Does NOT include Teleportation free sells (valued separately as
    a patent effect to avoid double-counting).
    """
    remaining = state.remaining_events_full_game()
    total_events = sum(remaining.values())
    player_turns = total_events / max(len(state.players), 1)

    # Sell fraction from actual remaining deck cards for THIS resource
    deck_remaining = state.deck.cards
    if not deck_remaining:
        return 0.0
    sell_fraction = sum(
        1 for c in deck_remaining
        if c.can_sell and resource in c.can_sell
    ) / len(deck_remaining)
    if sell_fraction <= 0:
        return 0.0

    # Visible cards per turn = actual hand + actual pool
    visible_cards = len(player.hand) + len(state.pool)
    if _player_owns_patent(player, "Thinking Machines"):
        visible_cards += 1

    # P(at least 1 matching sell card in visible cards)
    sell_prob = 1.0 - (1.0 - sell_fraction) ** visible_cards

    # Cap by AP budget
    sells_per_turn = min(sell_prob, float(MAX_CARDS_PER_TURN))

    return player_turns * sells_per_turn


def _sell_rank_discount(
    resource: Resource, state: GameState, player: Player,
) -> float:
    """Discount factor based on how this resource ranks among the
    player's sellable rates. Best gets 1.0, 2nd gets 0.5, 3rd 0.33, etc.

    The player has limited AP per turn, so they prioritize selling
    their most valuable resources first. Lower-ranked resources get
    fewer sell turns.
    """
    sellable = sorted(
        [r for r in Resource if r != Resource.PWR and player.rate(r) > 0],
        key=lambda r: player.rate(r) * state.market.price(r),
        reverse=True,
    )
    for rank, r in enumerate(sellable):
        if r == resource:
            return 1.0 / (rank + 1)
    return 0.0


def _rate_ongoing_value(
    resource: Resource, state: GameState, player: Player | None = None,
) -> float:
    """Value of +1 rate of a resource for the rest of the game.

    PWR: earns automatically at power bills → avg_price × bills,
        plus OC fuel premium if player owns Optimization Center.
    Non-PWR with player: max of sell income vs futures cost, plus
        Water Engine premium (H2O only), OC new-target bonus (rate
        <= 0 only), and contract proximity bonus.
    Non-PWR without player: futures cost only.
    """
    remaining = state.remaining_events_full_game()

    if resource == Resource.PWR:
        bills = (
            remaining.get(EventType.POWER_BILL, 0)
            + remaining.get(EventType.END_ROUND, 0)
            + remaining.get(EventType.END_GAME, 0)
        )
        avg_price = _expected_price(resource, state)
        pwr_raw = avg_price * bills

        # OC synergy: PWR is fuel for OC (-1 PWR → +1 best rate).
        # Additive premium: how much more the best conversion output
        # is worth compared to the raw PWR value itself.
        if player and _count_buildings(player, "Optimization Center") > 0:
            best_other = max(
                (_rate_ongoing_value(r, state)  # no player → avoids recursion
                 for r in Resource if r != Resource.PWR),
                default=0,
            )
            pwr_raw += max(0, best_other - pwr_raw)

        return pwr_raw

    # Non-PWR: max of sell income vs futures cost
    sell_value = 0.0
    if player and player.rate(resource) > 0:
        sell_events = _expected_sell_events(resource, state, player)
        discount = _sell_rank_discount(resource, state, player)
        avg_price = _expected_price(resource, state)
        sell_value = avg_price * sell_events * discount

    end_events = (
        remaining.get(EventType.END_ROUND, 0)
        + remaining.get(EventType.END_GAME, 0)
    )
    # Debt only at END_ROUND and END_GAME (FUTURES_TRADING only pushes prices)
    settlements = end_events
    futures_value = state.market.price(resource) * settlements

    base = max(sell_value, futures_value)

    # Patent synergies and contract bonus (only when player is known)
    if player:
        # Water Engine: +1 H2O can become +2 PWR (free action).
        # Additive premium: how much more 2×PWR is worth vs raw H2O.
        if resource == Resource.H2O and _player_owns_patent(player, "Water Engine"):
            pwr_value = _rate_ongoing_value(Resource.PWR, state, player) * 2
            base += max(0, pwr_value - base)

        # OC new-target synergy: if this resource has rate <= 0 and player
        # has OC, gaining +1 here unlocks it as an OC conversion target.
        # Premium = net conversion value (resource value minus PWR cost).
        if (
            _count_buildings(player, "Optimization Center") > 0
            and player.rate(resource) <= 0
        ):
            pwr_cost = _rate_ongoing_value(Resource.PWR, state, player)
            base += max(0, base - pwr_cost)

        # Contract proximity bonus
        from my_project.strategies import _contract_proximity_bonus
        base += _contract_proximity_bonus(resource, state, player)

    return base


def _execute_free_actions(state: GameState, player: Player) -> list[str]:
    """Auto-fire free actions for AI players at the start of their turn.

    Free actions don't cost cards and don't count toward the per-turn budget.
    They're fired BEFORE the action loop so the AI benefits from them (e.g.
    Water Engine's +2 PWR changes power-bill economics, Optimization Center's
    +1 rate makes builds/contracts cheaper).

    Returns a list of human-readable descriptions of what fired (for the
    turn log). Empty list if nothing fired.
    """
    fired: list[str] = []
    pidx = state.players.index(player)

    # --- Rate conversion free actions ---
    # Each conversion permanently changes rates. Only fire when the
    # ongoing value of what you GAIN exceeds what you LOSE.
    # Evaluated like building a zero-cost card with those rates.
    pwr_cost = _rate_ongoing_value(Resource.PWR, state, player)

    # Optimization Center: -1 PWR, +1 any positive non-PWR rate.
    # OC target selection uses _rate_ongoing_value which includes
    # sell income, patent synergies, and contract proximity.
    if (
        _count_buildings(player, "Optimization Center") > 0
        and not player.has_used_optimization_center_this_turn
    ):
        candidates = [
            r for r in Resource
            if r != Resource.PWR and player.rate(r) > 0
        ]
        if candidates:
            best = max(candidates, key=lambda r: _rate_ongoing_value(r, state, player))
            gain = _rate_ongoing_value(best, state, player)
            loss = _rate_ongoing_value(Resource.PWR, state, player)
            if gain > loss:
                state.log.begin("free:oc", player.name, pidx, f"OC: -1 PWR, +1 {best.value}")
                player.rates[Resource.PWR] = player.rate(Resource.PWR) - 1
                player.rates[best] = player.rate(best) + 1
                player.has_used_optimization_center_this_turn = True
                state.log.end()
                fired.append(f"Optimization Center: -1 PWR, +1 {best.value} (return ${gain:.0f} > cost ${loss:.0f})")

    # Water Engine: -1 H2O, +2 PWR. Evaluate like a free building with
    # -1 H2O rate and +2 PWR rate: fire when the expected return of
    # +2 PWR over remaining events exceeds the loss of -1 H2O.
    if (
        _player_owns_patent(player, "Water Engine")
        and not player.has_used_water_engine_this_turn
        and player.rate(Resource.H2O) >= 1
    ):
        gain = 2 * _rate_ongoing_value(Resource.PWR, state, player)
        loss = _rate_ongoing_value(Resource.H2O, state, player)
        if gain > loss:
            state.log.begin("free:water_engine", player.name, pidx, "Water Engine: -1 H2O, +2 PWR")
            player.rates[Resource.H2O] = player.rate(Resource.H2O) - 1
            player.rates[Resource.PWR] = player.rate(Resource.PWR) + 2
            player.has_used_water_engine_this_turn = True
            state.log.end()
            fired.append(f"Water Engine: -1 H2O, +2 PWR (return ${gain:.0f} > cost ${loss:.0f})")

    # Teleportation: free sell (rate × price cash), -1 PWR permanent.
    if (
        _player_owns_patent(player, "Teleportation")
        and not player.has_used_teleportation_this_turn
    ):
        candidates = [
            r for r in Resource
            if r != Resource.PWR and player.rate(r) > 0
        ]
        if candidates:
            best = max(candidates, key=lambda r: state.market.price(r) * player.rate(r))
            rate = player.rate(best)
            revenue = rate * state.market.price(best)
            if revenue > pwr_cost:
                state.log.begin("free:teleportation", player.name, pidx, f"Teleportation: sell {rate} {best.value}")
                revenue = state.market.sell(best, rate)
                player.money += revenue
                player.rates[Resource.PWR] = player.rate(Resource.PWR) - 1
                player.has_used_teleportation_this_turn = True
                state.log.end()
                fired.append(f"Teleportation: sold {rate} {best.value} for ${revenue}, -1 PWR (cost ${pwr_cost:.0f})")

    # Nanotechnology: draw a card from the deck, replace the pool card at
    # the slot position indicated by the drawn card, if that offers value.
    if (
        _player_owns_patent(player, "Nanotechnology")
        and not player.has_used_nanotechnology_this_turn
        and state.pool
        and state.deck.cards
    ):
        from my_project.strategies import _card_value

        drawn = state.deck.draw(1)
        if drawn:
            drawn_card = drawn[0]
            pool_idx = drawn_card.slot - 1
            if 0 <= pool_idx < len(state.pool):
                old_card = state.pool[pool_idx]
                old_val = _card_value(old_card, player, state)
                new_val = _card_value(drawn_card, player, state)
                if new_val > old_val:
                    state.log.begin(
                        "free:nanotech",
                        player.name,
                        pidx,
                        f"Nanotech: draw {drawn_card.building} (slot {drawn_card.slot}), replace {old_card.building}",
                    )
                    state.pool[pool_idx] = drawn_card
                    state.deck.discard.append(old_card)
                    player.has_used_nanotechnology_this_turn = True
                    state.log.end()
                    fired.append(f"Nanotechnology: replaced {old_card.building} with {drawn_card.building}")
                else:
                    # Card is not valuable enough, put it back on deck
                    state.deck.cards.append(drawn_card)
            else:
                # Invalid slot, put card back
                state.deck.cards.append(drawn_card)


    return fired


def reset_per_turn_flags(player: Player) -> None:
    """Reset all per-turn flags to their start-of-turn defaults.

    Called at the start of every player turn — shared by run_turn (MC),
    begin_human_turn, and step_ai_turn (play adapter).
    """
    player.has_built_this_turn = False
    player.has_used_launch_pad_this_turn = False
    player.has_used_optimization_center_this_turn = False
    player.has_used_water_engine_this_turn = False
    player.has_used_nanotechnology_this_turn = False
    player.has_used_teleportation_this_turn = False
    player.cards_spent_this_turn = 0


def run_turn(state: GameState, player: Player, strategy, event: EventCard) -> None:
    """Run one turn: pool swaps, then actions until hand empty or pass, draw, event."""
    state.turn += 1
    money_before = player.money
    action_records: list[ActionRecord] = []

    reset_per_turn_flags(player)

    # Free actions phase (before pool swaps and the action loop).
    # Auto-fires Optimization Center, Water Engine, Teleportation,
    # Nanotechnology using simple heuristics.
    fired_free = _execute_free_actions(state, player)

    # Pool swapping phase (free, before actions)
    swap_fn = getattr(strategy, 'pool_swap', None)
    if swap_fn:
        swap_fn(state, player)

    # Action phase: keep taking actions until the strategy passes. The
    # strategy is responsible for returning PASS when no legal action is
    # available (whether due to AP=0, empty hand, or no affordable plays).
    # MAX_ACTIONS_PER_TURN is a safety cap against infinite loops.
    actions_taken = 0
    while actions_taken < MAX_ACTIONS_PER_TURN:
        action = strategy(state, player)
        if action.action_type == ActionType.PASS:
            break

        record = _execute_action(state, player, action)
        if record is not None:
            action_records.append(record)
        actions_taken += 1

    # Draw back to hand size
    needed = player.hand_size - len(player.hand)
    if needed > 0:
        player.hand.extend(state.deck.draw(needed))

    # Execute the event. Each event is one player-turn — no redraw chaining
    # in the main game loop. The deck is pre-built with the right number of
    # events so each player gets exactly N turns.
    event_detail = execute_event(state, event, player)

    # Snapshot all players after the event for the log
    event_snapshots = [
        {"name": p.name, "money": p.money, "debt": p.debt,
         "credit": p.credit, "net_worth": p.net_worth()}
        for p in state.players
    ]

    detail_strs = [r.detail for r in action_records]
    state.history.append(TurnRecord(
        turn=state.turn,
        player=player.name,
        action=f"{len(action_records)} actions",
        detail="; ".join(detail_strs) if detail_strs else "Pass",
        event=event_detail,
        money_before=money_before,
        money_after=player.money,
        debt=player.debt,
        contracts_fulfilled=player.contracts_fulfilled,
        market_snapshot=state.market.snapshot(),
        rates_snapshot={r.value: v for r, v in player.rates.items()},
        actions=action_records,
        free_actions=fired_free,
        event_player_snapshots=event_snapshots,
    ))


def run_game(
    all_cards: list[Card],
    all_contracts: list[Contract],
    strategy=None,
    num_players: int = 1,
    start_money: int = DEFAULT_START_MONEY,
    start_market_pos: int = DEFAULT_MARKET_POS,
    randomize_market: bool = False,
    max_turns: int = DEFAULT_MAX_TURNS,
    corporation_rates: list[dict[Resource, int]] | None = None,
    strategies: list | None = None,
    num_rounds: int = 1,
) -> GameState:
    """Run a complete game and return the final state.

    Args:
        strategy: Single strategy function applied to all players.
        strategies: Per-player strategy list (overrides `strategy`).
                    Length must match num_players.
        num_rounds: How many times to play through the event deck.
                    Each round reshuffles the same composition; the final
                    round ends with END_GAME.
    """
    # Load patents from CSV if available (same as the play adapter does).
    from my_project.parsing import parse_patents
    patents_path = Path(__file__).parent / "data" / "Patents.csv"
    patent_pile = parse_patents(patents_path) if patents_path.exists() else []

    state = GameState.create(
        all_cards=all_cards,
        all_contracts=all_contracts,
        num_players=num_players,
        start_money=start_money,
        start_market_pos=start_market_pos,
        randomize_market=randomize_market,
        max_turns=max_turns,
        corporation_rates=corporation_rates,
        num_rounds=num_rounds,
        patent_pile=patent_pile,
    )
    # Snapshot initial market before any events (for analytics Turn 0)
    state._initial_market = state.market.snapshot()

    # Build per-player strategy list
    if strategies is not None:
        player_strategies = strategies
    elif strategy is not None:
        player_strategies = [strategy] * num_players
    else:
        raise ValueError("Must provide either `strategy` or `strategies`")

    # Play until the event deck is exhausted. Each card in the deck is a
    # player turn (including END_ROUND/END_GAME). Redraws chain: they fire
    # then also draw+fire the next card (2 cards consumed, 1 player turn).
    current_round = 0
    safety = 500
    player_turn = 0
    while state.event_idx < len(state.event_deck) and player_turn < safety:
        player_idx = player_turn % num_players
        player = state.players[player_idx]

        # Draw and fire the event for this turn
        primary_event = state.event_deck[state.event_idx]
        state.event_idx += 1
        run_turn(state, player, player_strategies[player_idx], primary_event)

        # Chain redraw events: execute event only (no actions).
        # Merge chained event details into the parent turn's history record.
        # Chains may absorb END_ROUND/END_GAME; the reshuffle check below
        # tracks whether END_ROUND fired in the primary or the chain.
        event = primary_event
        end_round_consumed = primary_event.type == EventType.END_ROUND
        while event.redraws and state.event_idx < len(state.event_deck):
            event = state.event_deck[state.event_idx]
            state.event_idx += 1
            chain_detail = execute_event(state, event, player)
            if event.type == EventType.END_ROUND:
                end_round_consumed = True
            if state.history:
                last = state.history[-1]
                last.event = f"{last.event} | {chain_detail}"
                last.money_after = player.money
                last.debt = player.debt
                last.contracts_fulfilled = player.contracts_fulfilled
                last.market_snapshot = state.market.snapshot()
                last.rates_snapshot = {r.value: player.rate(r) for r in Resource}

        # END_ROUND fired (primary or chained) → reshuffle for next round
        if end_round_consumed:
            current_round += 1
            reshuffle_for_next_round(state, num_players, current_round, num_rounds)

        player_turn += 1

    return state

# --- Event module re-exports (keep test and play_adapter imports working) ---
# The event block lives in my_project.events now. Tests and play_adapter
# historically imported these names from my_project.simulation; re-export
# them here so no call site needs to change. Import is at the BOTTOM so the
# classes/constants events.py needs (GameState, Player, EventCard,
# DEBT_INTEREST_DIVISOR, _D20_DELTAS, _pleasure_dome_bonus, etc.) are all
# defined by the time events.py runs its own top-level imports.
from my_project.events import (  # noqa: E402
    _apply_debt,
    _default_ai_bid,
    _event_needs_prompt,
    _get_patent_base_values,
    _has_human_player,
    do_debt_collection,
    do_draw_building_card,
    do_futures_settlement,
    do_futures_trading,
    do_news,
    do_news_bulletin,
    do_patent_auction,
    do_power_bill,
    do_pwr_adjust,
    execute_event,
    execute_event_with_redraws,
    resume_pending_event,
    settle_silent_auction,
)
