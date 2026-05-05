// ===== UI Rendering + Feed Formatting + Game Controls =====
//
// Wrapped in an IIFE; public API on MP.ui.
// Depends on: window.MP; top-level constants from multiplayer.js
// (RESOURCE_ORDER, RESOURCE_COLORS, PLAYER_COLORS) resolved at call time.
// Calls into MP.anim.* / MP.core.* / MP.network.* for cross-module work;
// those are already namespaced in the function bodies below.

(function () {
  const MP = window.MP = window.MP || {};

  // ===== Selection helpers =====
  
  function clearSelection() {
    MP.selectedCards.clear();
    MP.selectedContract = -1;
    MP.pendingPoolSwap = -1;
  }
  
  // ===== Rendering =====
  
  function isMyTurn(s) {
    s = s || MP.currentState;
    if (!s || s.is_over) return false;
    // During the corporation draft the "active seat" is the drafter, not a
    // regular-turn seat — action buttons / hand interactions must stay
    // disabled for everyone until the draft completes.
    if (s.draft_state && !s.draft_state.is_complete) return false;
    return s.current_player_index === MP.mySeat;
  }
  
  function renderGame() {
    if (!MP.currentState) return;
    const s = MP.currentState;
    const myTurn = isMyTurn(s);
  
    // Status bar
    const roundStr = s.num_rounds > 1 ? `${s.round} (Rd ${s.deck_round || 1}/${s.num_rounds})` : `${s.round || "-"}`;
    document.getElementById("mp-round").textContent = roundStr;
    document.getElementById("mp-turn").textContent = `${(s.turn_index || 0) + 1} / ${s.total_turns || "?"}`;
    const activeP = s.players[s.current_player_index];
    const activeLabel = activeP ? `${activeP.name}${s.current_player_index === MP.mySeat ? " (You)" : ""}` : "-";
    document.getElementById("mp-active").textContent = activeLabel;
    document.getElementById("mp-cards-left").textContent = s.cards_in_deck ?? "-";
  
    // Cards remaining / AP display
    const apEl = document.getElementById("mp-ap-display");
    if (apEl) {
      if (myTurn && MP.currentLegal) {
        const cr = MP.currentLegal.cards_remaining ?? 2;
        const builtMsg = s.human_already_built ? " (built)" : "";
        apEl.textContent = `Cards: ${cr}/2${builtMsg}`;
        apEl.className = "status-value " + (cr >= 2 ? "ap-full" : cr >= 1 ? "ap-half" : "ap-empty");
      } else {
        apEl.textContent = "-";
        apEl.className = "status-value";
      }
    }
  
    const banner = document.getElementById("turn-banner");
    banner.style.display = myTurn ? "block" : "none";
  
    renderMarket(s);
    renderContracts(s);
    renderPool(s);
    renderHand(s);
    renderActions(s);
    renderPlayerPanel(s);
    renderOpponents(s);
    renderDraftPanel(s);
    MP.anim.renderDeckViewer();

    // V2: DOM relocations
    if (document.body.classList.contains("theme-v2")) {
      // Event deck → own grid child in board-zone, before contracts section
      const deckArea = document.getElementById("event-deck-area");
      const boardZone = document.querySelector(".board-zone");
      const contractsSection = document.querySelector(".board-zone > section:nth-child(3)");
      if (deckArea && boardZone && contractsSection && deckArea.parentElement !== boardZone) {
        boardZone.insertBefore(deckArea, contractsSection);
      }
    } else {
      // Classic: restore event deck to status bar
      const deckArea = document.getElementById("event-deck-area");
      const statusBar = document.getElementById("status-bar");
      if (deckArea && statusBar && deckArea.parentElement !== statusBar) {
        statusBar.appendChild(deckArea);
      }
    }
  }
  
  // ===== Draggable popups =====

  // Make `host` draggable by its `handle` element. The host becomes
  // position:fixed at its first-paint coordinates so the drag can shift
  // it in pixel space. `onDrag` (optional) is called with {left, top}
  // as drag progresses so callers can persist the position across
  // re-renders. Window listeners are scoped to the live drag so they
  // don't accumulate on repeated installs.
  function makeDraggable(host, handle, onDrag) {
    if (!host || !handle) return;
    handle.addEventListener("mousedown", e => {
      const rect = host.getBoundingClientRect();
      const startX = e.clientX;
      const startY = e.clientY;
      const startLeft = rect.left;
      const startTop = rect.top;
      // Switch from whatever positioning was inherited (flex-centered,
      // etc.) to pixel coords we control for the rest of the drag.
      host.style.position = "fixed";
      host.style.left = startLeft + "px";
      host.style.top = startTop + "px";
      host.style.transform = "none";
      host.classList.add("dragging");
      e.preventDefault();
      const onMove = (ev) => {
        const newLeft = Math.max(0, startLeft + (ev.clientX - startX));
        const newTop = Math.max(0, startTop + (ev.clientY - startY));
        host.style.left = newLeft + "px";
        host.style.top = newTop + "px";
        if (onDrag) onDrag({left: newLeft, top: newTop});
      };
      const onUp = () => {
        host.classList.remove("dragging");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  // ===== Corporation draft =====

  // Persist drag offset across re-renders — renderDraftPanel rewrites
  // innerHTML on every state refresh, so the element + its position
  // would otherwise reset every tick.
  let _draftDragOffset = null;  // {left: px, top: px} or null

  function _installDraftDrag(host) {
    const title = host.querySelector(".draft-title");
    makeDraggable(host, title, pos => { _draftDragOffset = pos; });
  }

  // Install drag on a .modal-overlay by its inner .modal-box h2 title.
  // Called on every modal-open so freshly-rewritten content gets the
  // handler attached. Resets position on each open so the modal re-
  // centers in the flex layout instead of stacking at a stale drag offset.
  function _installModalDrag(overlayId) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    const box = overlay.querySelector(".modal-box");
    if (!box) return;
    const title = box.querySelector("h2");
    // Clear any stale pixel-coord positioning so flex-centering kicks in
    // again on the new open.
    box.style.position = "";
    box.style.left = "";
    box.style.top = "";
    box.style.transform = "";
    makeDraggable(box, title);
  }

  function renderDraftPanel(s) {
    const host = document.getElementById("mp-draft-panel");
    const d = s.draft_state;
    if (!host) return;
    if (!d || d.is_complete) {
      host.style.display = "none";
      host.innerHTML = "";
      _draftDragOffset = null;
      return;
    }

    const picker = d.current_picker;
    const picks = d.picks || {};
    const myPick = picker === MP.mySeat;
    const pickerName = s.players?.[picker]?.name || `Seat ${picker}`;

    // Pick history — order by the reverse-turn pick_order so players can
    // see the sequence as it unfolds.
    const history = (d.pick_order || [])
      .filter(seat => picks[seat])
      .map(seat => {
        const name = s.players?.[seat]?.name || `Seat ${seat}`;
        return `<div class="draft-pick-row"><strong>${name}</strong> — ${picks[seat]}</div>`;
      }).join("");

    // Available corporations — show name + starting rates. Button enabled
    // only when it's this client's turn to pick.
    const formatRates = (rates) => {
      const entries = Object.entries(rates || {}).filter(([, v]) => v !== 0);
      if (!entries.length) return '<span style="color:var(--text-muted)">no starting rates</span>';
      return entries.map(([r, v]) => {
        const cls = v > 0 ? "rate-pos" : "rate-neg";
        const color = RESOURCE_COLORS[r] || "var(--text-muted)";
        return `<span class="${cls}" style="color:${color}">${v > 0 ? '+' : ''}${v} ${r}</span>`;
      }).join(" ");
    };
    const pool = (d.available || []).map(corp => {
      const btnAttrs = myPick ? '' : 'disabled';
      return `<div class="draft-corp">
        <div class="draft-corp-head">
          <span class="draft-corp-name">${corp.name}</span>
          <button class="action-btn draft-pick-btn" data-corp="${corp.name}" ${btnAttrs}>Pick</button>
        </div>
        <div class="draft-corp-rates">${formatRates(corp.rates)}</div>
      </div>`;
    }).join("");

    const headline = myPick
      ? `<strong>Your pick</strong> — choose a corporation`
      : `Waiting for <strong>${pickerName}</strong> to pick…`;

    host.innerHTML = `
      <div class="draft-card">
        <h3 class="draft-title" title="Drag to reposition">Corporation Draft</h3>
        <div class="draft-headline">${headline}</div>
        <div class="draft-pool">${pool}</div>
        ${history ? `<div class="draft-history"><div class="draft-history-title">Picks so far</div>${history}</div>` : ''}
      </div>
    `;
    host.style.display = "block";
    // Preserve the drag-set position across re-renders; otherwise each
    // refresh would snap the card back to its CSS default.
    if (_draftDragOffset) {
      host.style.left = _draftDragOffset.left + "px";
      host.style.top = _draftDragOffset.top + "px";
    }
    _installDraftDrag(host);

    if (myPick) {
      host.querySelectorAll(".draft-pick-btn").forEach(btn => {
        btn.addEventListener("click", () => {
          const corp = btn.dataset.corp;
          if (MP.role === "host") {
            MP.core.handleHostDraftPick(corp);
          } else if (MP.hostConn) {
            MP.hostConn.send(JSON.stringify({type: "draft_pick", corp}));
          }
        });
      });
    }
  }

  const PRICE_TRACK = [1,1,1,2,2,2,3,3,4,4,5,5,6,7,8,9,10];
  let expandedMarketResource = null;
  
  function stepsAtSamePrice(pos, direction) {
    // Count remaining positions in direction with the SAME price (not including current)
    const curPrice = PRICE_TRACK[pos];
    let steps = 0;
    let p = pos + direction;
    while (p >= 0 && p < PRICE_TRACK.length && PRICE_TRACK[p] === curPrice) {
      steps++;
      p += direction;
    }
    return steps;
  }
  
  function renderMarket(s) {
    const grid = document.getElementById("mp-market-grid");
    const positions = s.market_positions || {};
  
    grid.innerHTML = RESOURCE_ORDER.map(r => {
      const price = s.market[r] || 0;
      const pos = positions[r] ?? 9;
      const stepsDown = stepsAtSamePrice(pos, -1);
      const stepsUp = stepsAtSamePrice(pos, 1);
      const dotsLeft = stepsDown > 0 ? Array(stepsDown).fill('<span class="step-dot"></span>').join("") : "";
      const dotsRight = stepsUp > 0 ? Array(stepsUp).fill('<span class="step-dot"></span>').join("") : "";
      const isExpanded = expandedMarketResource === r;
      return `
        <div class="market-cell ${isExpanded ? 'expanded' : ''}" data-res="${r}">
          <div class="res-name" style="color:${RESOURCE_COLORS[r]}">${r}</div>
          <div class="res-price-wrap">
            <span class="dots-down" title="${stepsDown} step${stepsDown !== 1 ? 's' : ''} down">${dotsLeft}</span>
            <span class="res-price">$${price}</span>
            <span class="dots-up" title="${stepsUp} step${stepsUp !== 1 ? 's' : ''} up">${dotsRight}</span>
          </div>
        </div>
      `;
    }).join("");
  
    // Wire click to expand ruler + toggle chart line
    grid.querySelectorAll(".market-cell").forEach(el => {
      el.addEventListener("click", () => {
        const r = el.dataset.res;
        const wasSelected = expandedMarketResource === r;
        expandedMarketResource = wasSelected ? null : r;
        renderMarketRuler(s);
        // Toggle chart visibility: isolate clicked resource or show all
        if (MP.marketChart) {
          RESOURCE_ORDER.forEach((res, i) => {
            if (wasSelected) {
              MP.marketChart.setDatasetVisibility(i, true);
            } else {
              MP.marketChart.setDatasetVisibility(i, res === r);
            }
          });
          MP.marketChart.update();
        }
        // Dim/undim market cells
        grid.querySelectorAll(".market-cell").forEach(cell => {
          if (wasSelected) {
            cell.style.opacity = "1";
          } else {
            cell.style.opacity = cell.dataset.res === r ? "1" : "0.4";
          }
        });
      });
    });
  
    renderMarketRuler(s);
    renderMarketChart(s);
  }
  
  function renderMarketRuler(s) {
    const ruler = document.getElementById("mp-market-ruler");
    if (!ruler) return;
    ruler.style.display = "block";
  
    const positions = s.market_positions || {};
    const selectedRes = expandedMarketResource;
    const selectedPos = selectedRes ? (positions[selectedRes] ?? 9) : -1;
    const selectedColor = selectedRes ? RESOURCE_COLORS[selectedRes] : "#888";
  
    // Build position → list of resources at that position
    const resourcesAtPos = {};
    for (const r of RESOURCE_ORDER) {
      const pos = positions[r] ?? 9;
      if (!resourcesAtPos[pos]) resourcesAtPos[pos] = [];
      resourcesAtPos[pos].push(r);
    }
  
    const isV2 = document.body.classList.contains("theme-v2");

    if (isV2) {
      // Group positions by price into parent sections
      const groups = [];
      let cur = null;
      PRICE_TRACK.forEach((p, i) => {
        if (!cur || cur.price !== p) {
          cur = {price: p, positions: []};
          groups.push(cur);
        }
        cur.positions.push(i);
      });

      ruler.innerHTML = `
        <div class="ruler-track">
          ${groups.map(g => {
            const subSlots = g.positions.map(i => {
              const isCurrent = i === selectedPos;
              const resHere = resourcesAtPos[i] || [];
              const dots = resHere.map(r => {
                const isSelected = r === selectedRes;
                const longCls = r.length > 3 ? ' ruler-long' : '';
                return `<span class="ruler-res-dot${longCls} ${isSelected ? 'selected' : ''}" style="background:${RESOURCE_COLORS[r]};color:${MP.pipTextColor(r)}" title="${r}">${r}</span>`;
              }).join("");
              return `<div class="ruler-sub ${isCurrent ? 'current' : ''}">${dots}</div>`;
            }).join("");
            const hasSelected = g.positions.includes(selectedPos);
            // flex:N so the group takes N/17 of the ruler height — each of
            // its N subsections ends up 1/17 of the total ruler.
            return `<div class="ruler-group ${hasSelected ? 'has-selected' : ''}" style="flex:${g.positions.length} 1 0">
              <div class="ruler-group-label">$${g.price}</div>
              <div class="ruler-group-slots">${subSlots}</div>
            </div>`;
          }).join("")}
        </div>
      `;
    } else {
      ruler.innerHTML = `
        <div class="ruler-track">
          ${PRICE_TRACK.map((p, i) => {
            const isCurrent = i === selectedPos;
            const resHere = resourcesAtPos[i] || [];
            const dots = resHere.map(r => {
              const isSelected = r === selectedRes;
              return `<span class="ruler-res-dot ${isSelected ? 'selected' : ''}" style="background:${RESOURCE_COLORS[r]}" title="${r}"></span>`;
            }).join("");
            return `<span class="ruler-pip ${isCurrent ? 'current' : ''}" style="${isCurrent ? 'border-color:' + selectedColor : ''}">
              <span class="ruler-dots-row">${dots}</span>
              <span class="ruler-price">$${p}</span>
            </span>`;
          }).join("")}
        </div>
      `;
    }
  }
  
  function renderMarketChart(s) {
    let history = s.market_history || [];
    // Prepend current market as turn 0 if history is empty or missing the start
    if (history.length === 0 && s.market) {
      history = [{turn: 0, market: s.market}];
    }
    if (history.length < 1) return;
    const canvas = document.getElementById("mp-market-chart");
    if (!canvas) return;
    const labels = history.map(h => h.turn);
    const datasets = RESOURCE_ORDER.map(r => ({
      label: r,
      data: history.map(h => h.market?.[r] || 0),
      borderColor: RESOURCE_COLORS[r],
      backgroundColor: "transparent",
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
    }));
    if (MP.marketChart) {
      MP.marketChart.data.labels = labels;
      MP.marketChart.data.datasets = datasets;
      MP.marketChart.update("none");
    } else {
      // Chart.js renders to canvas — it can't resolve var(--*) references,
      // so we resolve palette colors eagerly via MP.cssVar.
      const axisText = MP.cssVar("--text-muted");
      const gridLine = MP.cssVar("--bg-subtle");
      MP.marketChart = new Chart(canvas, {
        type: "line",
        data: {labels, datasets},
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {legend: {display: false}},
          scales: {
            x: {display: true, ticks: {color: axisText, font: {size: 9}}, grid: {color: gridLine}},
            y: {display: true, ticks: {color: axisText, font: {size: 9}, callback: v => "$" + v}, grid: {color: gridLine}},
          },
        },
      });
    }
  }
  
  function renderContracts(s) {
    const grid = document.getElementById("mp-contracts-grid");
    const me = s.players[MP.mySeat];
    const myTurn = isMyTurn(s);
    grid.innerHTML = (s.available_contracts || []).map((c, ci) => {
      const reqs = (c.requirements || []).map(r => {
        const have = me?.rates?.[r.resource] || 0;
        const met = have >= r.amount;
        return `<span class="contract-req ${met ? 'met' : 'unmet'}">${r.amount} ${r.resource}</span>`;
      }).join(", ");
      const sel = ci === MP.selectedContract ? "selected" : "";
      const inactive = !myTurn ? "inactive" : "";
      const isV2 = document.body.classList.contains("theme-v2");
      if (isV2) {
        const reqLines = (c.requirements || []).map(r => {
          const have = me?.rates?.[r.resource] || 0;
          const met = have >= r.amount;
          return `<span class="contract-req-line ${met ? 'met' : 'unmet'}">${r.amount}${resPip(r.resource, true)}</span>`;
        }).join(" ");
        return `<div class="contract-card ${sel} ${inactive}" data-ci="${ci}">
          <div class="contract-reqs-v2">${reqLines || '&nbsp;'}</div>
          <div class="contract-reward">$${c.reward}</div>
        </div>`;
      }
      return `<div class="contract-card ${sel} ${inactive}" data-ci="${ci}">
        <div class="contract-reward">$${c.reward}</div>
        <div>${reqs}</div>
      </div>`;
    }).join("");
    if (myTurn) {
      grid.querySelectorAll(".contract-card").forEach(el => {
        el.addEventListener("click", () => {
          const ci = parseInt(el.dataset.ci);
          MP.selectedContract = MP.selectedContract === ci ? -1 : ci;
          renderContracts(s);
          // Re-render actions so the Space Elevator resource picker
          // updates to reflect the newly-selected contract's requirements.
          renderActions(s);
        });
      });
    }
  }
  
  function renderPool(s) {
    const grid = document.getElementById("mp-pool-grid");
    const myTurn = isMyTurn(s);
    const canSwap = myTurn && s.can_pool_swap && MP.selectedCards.size === 1;
    const poolInactive = canSwap ? "" : "inactive";
    grid.innerHTML = (s.pool || []).map((c, i) => {
      const swappable = canSwap ? "swap-target" : "";
      return `<div class="pool-card ${swappable} ${poolInactive}" data-pi="${i}">${renderCard(c)}</div>`;
    }).join("");
    if (canSwap) {
      grid.querySelectorAll(".pool-card").forEach(el => {
        el.addEventListener("click", () => {
          const pi = parseInt(el.dataset.pi);
          const hi = Array.from(MP.selectedCards)[0];
          executePoolSwap(hi, pi);
          // Keep the swapped-in card selected at the same hand index
          MP.selectedCards.clear();
          MP.selectedCards.add(hi);
          MP.selectedContract = -1;
          MP.pendingPoolSwap = -1;
        });
      });
    }
  }
  
  function renderHand(s) {
    const grid = document.getElementById("mp-hand-grid");
    const me = s.players[MP.mySeat];
    const hand = me?.hand || [];
    const myTurn = isMyTurn(s);
  
    if (hand.length === 0) {
      grid.innerHTML = `<div style="color:var(--text-muted);padding:12px">Hand is empty</div>`;
      document.getElementById("mp-build-estimate").textContent = "";
      return;
    }
  
    // Affordability hints from legal actions
    const affordableSet = new Set(MP.currentLegal?.affordable_single_builds || []);
    const cr = MP.currentLegal?.cards_remaining ?? 2;
  
    const inactive = !myTurn ? "inactive" : "";
    const isV2 = document.body.classList.contains("theme-v2");
    const cardCount = hand.length;
    grid.innerHTML = hand.map((c, i) => {
      const sel = MP.selectedCards.has(i) ? "selected" : "";
      const fanStyle = isV2 ? `--card-index:${i}; --card-count:${cardCount};` : "";
      const intentCls = isV2 && MP.selectedCards.has(i) && MP._v2CardIntent
        ? ` intent-${MP._v2CardIntent}` : "";
      return `<div class="hand-card ${sel}${intentCls} ${inactive}" data-hi="${i}" style="${fanStyle}">${renderCard(c)}</div>`;
    }).join("");
  
    // V2: auto-select best sell resource BEFORE computing estimates
    if (isV2 && MP._v2CardIntent === "sell" && MP.selectedCards.size === 1) {
      const selIdx = Array.from(MP.selectedCards)[0];
      const selCard = hand[selIdx];
      if (selCard?.can_sell?.length > 0) {
        if (!MP._v2SellResource || !selCard.can_sell.includes(MP._v2SellResource)) {
          let bestRes = selCard.can_sell[0];
          let bestRev = 0;
          const me = s.players[MP.mySeat];
          for (const r of selCard.can_sell) {
            const rate = me?.rates?.[r] || 0;
            const rev = rate > 0 ? rate * (s.market?.[r] || 0) : 0;
            if (rev > bestRev) { bestRev = rev; bestRes = r; }
          }
          MP._v2SellResource = bestRes;
        }
      }
    }

    // Build + sell estimates
    const estimateEl = document.getElementById("mp-build-estimate");
    if (MP.selectedCards.size > 0 && myTurn) {
      if (isV2) {
        const intent = MP._v2CardIntent;
        if (intent === "build") {
          const est = estimateBuildCostLocal(s, [...MP.selectedCards]);
          if (est.ok) {
            const defParts = Object.entries(est.deficit || {}).map(([r, a]) => `${a} ${r}`).join(", ");
            estimateEl.innerHTML = `Build: <strong>$${est.cost}</strong>${defParts ? ` (buy ${defParts})` : ' (free)'}`;
          } else {
            estimateEl.innerHTML = `<span style="color:var(--accent-red)">${est.reason || 'Cannot build'}</span>`;
          }
        } else if (intent === "sell" && MP.selectedCards.size === 1) {
          const selIdx = Array.from(MP.selectedCards)[0];
          const selCard = hand[selIdx];
          const me = s.players[MP.mySeat];
          if (selCard?.can_sell?.length > 0) {
            const r = MP._v2SellResource || selCard.can_sell[0];
            const rate = me?.rates?.[r] || 0;
            const price = s.market?.[r] || 0;
            const rev = rate > 0 ? rate * price : 0;
            estimateEl.innerHTML = `Sell: <strong>$${rev}</strong> (${rate} ${r} @ $${price})`;
          } else if (selCard?.can_fulfill_contract) {
            if (MP.selectedContract >= 0) {
              const c = s.available_contracts?.[MP.selectedContract];
              estimateEl.innerHTML = `Contract: <strong>$${c?.reward || 50}</strong>`;
            } else {
              estimateEl.innerHTML = `<span style="color:var(--text-muted)">Select a contract</span>`;
            }
          } else {
            estimateEl.textContent = "";
          }
        } else {
          estimateEl.textContent = "";
        }
      } else {
        const est = estimateBuildCostLocal(s, [...MP.selectedCards]);
        if (est.ok) {
          const defParts = Object.entries(est.deficit || {}).map(([r, a]) => `${a} ${r}`).join(", ");
          estimateEl.innerHTML = `Build cost: <strong>$${est.cost}</strong>${defParts ? ` (buy ${defParts})` : ' (free)'}`;
        } else {
          estimateEl.innerHTML = `<span style="color:var(--accent-red)">${est.reason || 'Cannot build'}</span>`;
        }
      }
    } else {
      estimateEl.textContent = "";
    }
  
    if (myTurn) {
      grid.querySelectorAll(".hand-card:not(.disabled)").forEach(el => {
        el.addEventListener("click", (e) => {
          const hi = parseInt(el.dataset.hi);

          // V2: clicking a sell pip on ANY card = select that card + sell intent + that resource
          if (isV2 && e.target.classList.contains("card-sell-btn")) {
            MP.selectedCards.clear();
            MP.selectedCards.add(hi);
            MP._v2CardIntent = "sell";
            MP._v2SellResource = e.target.dataset.sellRes;
            renderHand(s);
            renderPool(s);
            renderActions(s);
            return;
          }
          // Classic: don't toggle selection if they clicked a sell button
          if (!isV2 && e.target.classList.contains("card-sell-btn")) return;

          if (isV2) {
            // Split card: detect which half was clicked
            const buildHalf = e.target.closest(".card-build-half");
            const sellHalf = e.target.closest(".card-sell-half");
            if (MP.selectedCards.has(hi)) {
              // Already selected — toggle off if clicking same half, switch intent otherwise
              if ((buildHalf && MP._v2CardIntent === "build") || (sellHalf && MP._v2CardIntent === "sell")) {
                MP.selectedCards.delete(hi);
                if (MP.selectedCards.size === 0) {
                  MP._v2CardIntent = null;
                  MP._v2SellResource = null;
                }
              } else if (sellHalf) {
                // Switching to sell — sell is single-card only
                MP.selectedCards.clear();
                MP.selectedCards.add(hi);
                MP._v2CardIntent = "sell";
              } else {
                MP._v2CardIntent = "build";
              }
            } else if (sellHalf) {
              // Sell: single card only, clear others
              MP.selectedCards.clear();
              MP.selectedCards.add(hi);
              MP._v2CardIntent = "sell";
              MP._v2SellResource = null;
            } else {
              // Build: allow multi-select up to cards_remaining.
              // If we're already at capacity, replace the selection with
              // the new card (so the player isn't stuck on an old selection
              // after building).
              if (MP._v2CardIntent === "sell") {
                MP.selectedCards.clear();
                MP._v2SellResource = null;
              } else if (MP.selectedCards.size >= Math.max(cr, 1)) {
                MP.selectedCards.clear();
              }
              MP._v2CardIntent = "build";
              MP.selectedCards.add(hi);
            }
          } else {
            if (MP.selectedCards.has(hi)) MP.selectedCards.delete(hi);
            else if (MP.selectedCards.size < Math.max(cr, 1)) MP.selectedCards.add(hi);
          }

          renderHand(s);
          renderPool(s);
          renderActions(s);
        });
      });

      // V2: activate sell buttons ONLY when intent is "sell".
      if (isV2 && MP._v2CardIntent === "sell" && MP.selectedCards.size === 1) {
        const selIdx = Array.from(MP.selectedCards)[0];
        const selCard = hand[selIdx];
        const me = s.players[MP.mySeat];
        const canSell = selCard?.can_sell?.length > 0 && cr >= 1;
        if (canSell) {
          // (Auto-select already ran above, before estimate computation)
          const cardEl = grid.querySelectorAll(".hand-card")[selIdx];
          if (cardEl) {
            cardEl.querySelectorAll(".card-sell-btn").forEach(btn => {
              btn.classList.remove("dimmed");
              btn.classList.add("active");
              if (MP._v2SellResource === btn.dataset.sellRes) {
                btn.classList.add("chosen");
              }
              btn.addEventListener("click", (e) => {
                e.stopPropagation();
                MP._v2SellResource = btn.dataset.sellRes;
                MP._v2CardIntent = "sell";  // clicking a pip = sell intent
                renderHand(s);
                renderActions(s);
              });
            });

            // (Sell info is shown in the estimate area above via the
            // combined build+sell estimate logic.)
          }
        } else {
          MP._v2SellResource = null;
        }
      } else if (isV2) {
        MP._v2SellResource = null;
      }
    }
  }
  
  function executePoolSwap(handIdx, poolIdx) {
    // Reset sell state so the swapped-in card comes back clean
    MP._v2SellResource = null;
    MP._v2CardIntent = null;

    // Capture card names before swap
    const handCard = MP.currentState?.players?.[MP.mySeat]?.hand?.[handIdx];
    const poolCard = MP.currentState?.pool?.[poolIdx];
    const handCardName = handCard?.building || "?";
    const poolCardName = poolCard?.building || "?";

    // Capture positions before swap
    const handEl = document.querySelectorAll("#mp-hand-grid .hand-card")[handIdx];
    const poolEl = document.querySelectorAll("#mp-pool-grid .pool-card")[poolIdx];
    const handRect = handEl?.getBoundingClientRect();
    const poolRect = poolEl?.getBoundingClientRect();
    const handHtml = handEl?.innerHTML || "";
    const poolHtml = poolEl?.innerHTML || "";

    // Hide source cards so they're only visible as flying clones during transit
    if (handEl) handEl.style.visibility = "hidden";
    if (poolEl) poolEl.style.visibility = "hidden";

    // Start animation from the old positions
    if (handRect && poolRect) {
      MP.anim.animateCard(handRect, poolRect, handHtml);
      MP.anim.animateCard(poolRect, handRect, poolHtml);
    }

    // Delay the state refresh so the new cards don't appear at the
    // destination before the animation arrives (~400ms flight time).
    const ANIM_DELAY = 420;
    if (MP.role === "host") {
      MP.game.human_pool_swap(parseInt(handIdx), parseInt(poolIdx));
      setTimeout(() => {
        MP.core.hostRefreshState();
        // Add swap to event feed
        const playerName = MP.currentState?.players?.[MP.mySeat]?.name || "Player";
        MP.core.addFeedEntry({
          kind: "free-action",
          text: `${playerName}: Swapped ${handCardName} ↔ ${poolCardName}`
        });
      }, ANIM_DELAY);
    } else {
      setTimeout(() => {
        MP.hostConn.send(JSON.stringify({type: "pool_swap", hand_idx: parseInt(handIdx), pool_idx: parseInt(poolIdx)}));
        // Add swap to event feed
        const playerName = MP.currentState?.players?.[MP.mySeat]?.name || "Player";
        MP.core.addFeedEntry({
          kind: "free-action",
          text: `${playerName}: Swapped ${handCardName} ↔ ${poolCardName}`
        });
      }, ANIM_DELAY);
    }
  }
  
  // Mirrors compute_build_deficit() in simulation.py. Runs on both host and
  // client so build cost estimates render identically regardless of MP.role.
  function estimateBuildCostLocal(state, indices) {
    const me = state?.players?.[MP.mySeat];
    if (!me) return {ok: false, reason: "No state"};
    if (state.human_already_built && !MP.currentLegal?.matter_replication) {
      return {ok: false, reason: "Already built this turn"};
    }
    if (!indices.length) return {ok: false, reason: "No cards selected"};
    const cards = indices.map(i => me.hand?.[i]).filter(Boolean);
    if (cards.length !== indices.length) return {ok: false, reason: "Invalid selection"};
  
    // Sum cost resources across selected cards
    const combined = {};
    for (const c of cards) {
      for (const ra of (c.costs || [])) {
        combined[ra.resource] = (combined[ra.resource] || 0) + ra.amount;
      }
    }
    // Deduct player rate (floor at 0 — negative rates don't help)
    const deficit = {};
    for (const [res, total] of Object.entries(combined)) {
      const rate = Math.max(0, me.rates?.[res] || 0);
      const d = Math.max(0, total - rate);
      if (d > 0) deficit[res] = d;
    }
    let cost = 0;
    for (const [res, amount] of Object.entries(deficit)) {
      const price = state.market?.[res] || 0;
      cost += price * amount;
    }
    if (cost > (me.money || 0)) {
      return {ok: false, reason: "Cannot afford", cost};
    }
    return {ok: true, cost, deficit};
  }
  
  // Render a resource as a colored pip icon (just the name, no number)
  // isRate=true → square shape for rates, circle for costs/resources
  function resPip(resource, isRate) {
    const bg = RESOURCE_COLORS[resource] || 'var(--text-muted)';
    const color = MP.pipTextColor(resource);
    const cls = isRate ? "res-pip res-pip-rate" : "res-pip";
    const longCls = resource.length > 3 ? " res-pip-long" : "";
    return `<span class="${cls}${longCls}" style="background:${bg};color:${color}">${resource}</span>`;
  }

  function renderCard(c) {
    // Returns full card HTML: costs → name → rates → effect → sell/contract
    const isV2 = document.body.classList.contains("theme-v2");
    const costs = (c.costs || []).map(r =>
      isV2
        ? (r.amount > 4 ? `<span class="cost-num">${r.amount}</span>${resPip(r.resource)}` : resPip(r.resource).repeat(r.amount))
        : `${r.amount} ${r.resource}`
    ).join(isV2 ? " " : ", ");
    const rates = (c.rates || []).map(r =>
      isV2
        ? `<span class="rate-line"><span class="rate-num">${r.amount > 0 ? '+' : ''}${r.amount}</span>${resPip(r.resource, true)}</span>`
        : `<span class="${r.amount > 0 ? 'rate-pos' : 'rate-neg'}">${r.amount > 0 ? '+' : ''}${r.amount} ${r.resource}</span>`
    ).join(isV2 ? "<br>" : " ");
    const sell = (c.can_sell || []).join("/");
    const canContract = c.can_fulfill_contract;
    let html = "";
    if (isV2) {
      const me = MP.currentState?.players?.[MP.mySeat];
      const market = MP.currentState?.market || {};

      // BUILD HALF: costs → name → rates → effect
      html += `<div class="card-build-half" data-intent="build">`;
      html += `<div class="card-costs">${costs || '&nbsp;'}</div>`;
      html += `<div class="card-name">${c.building}</div>`;
      html += `<div class="card-rates">${rates || '&nbsp;'}</div>`;
      if (c.effect) html += `<div class="card-effect">${c.effect}</div>`;
      html += `</div>`;

      // SELL HALF: resource pips with revenue
      html += `<div class="card-sell-half" data-intent="sell">`;
      const sellResources = (c.can_sell || []);
      if (sellResources.length > 0) {
        const btns = sellResources.map(r =>
          `<span class="card-sell-btn dimmed" data-sell-res="${r}" style="background:${RESOURCE_COLORS[r]};color:${MP.pipTextColor(r)}">${r}</span>`
        ).join("");
        html += `<div class="card-sell-row">${btns}</div>`;
      } else if (canContract) {
        html += `<div class="card-sell-row"><span class="card-contract-icon">\u{1F4CB} Contract</span></div>`;
      } else {
        html += `<div class="card-sell-row"><span class="card-no-sell">&nbsp;</span></div>`;
      }
      html += `</div>`;
    } else {
      if (costs) html += `<div class="card-costs">${costs}</div>`;
      html += `<div class="card-name">${c.building}</div>`;
      if (rates) html += `<div class="card-rates">${rates}</div>`;
      if (c.effect) html += `<div class="card-effect">${c.effect}</div>`;
      const bottomParts = [];
      if (sell) bottomParts.push(`<span class="card-sell">Sell: ${sell}</span>`);
      if (canContract) bottomParts.push(`<span class="card-contract">\u{1F4CB}</span>`);
      if (bottomParts.length) html += `<div class="card-bottom">${bottomParts.join(" ")}</div>`;
    }
    return html;
  }
  
  function renderActions(s) {
    const myTurn = isMyTurn(s);
    const cr = MP.currentLegal?.cards_remaining ?? 0;
    const alreadyBuilt = s.human_already_built;
    const buildBtn = document.getElementById("mp-build-btn");
    const sellBtn = document.getElementById("mp-sell-btn");
    const contractBtn = document.getElementById("mp-contract-btn");
    const passBtn = document.getElementById("mp-pass-btn");

    const canBuild = myTurn && MP.selectedCards.size > 0 && (!alreadyBuilt || MP.currentLegal?.matter_replication);
    const isV2 = document.body.classList.contains("theme-v2");

    // V2: adaptive button — text + enabled state follows card intent
    if (isV2) {
      const intent = MP._v2CardIntent;
      const selCardIdx = MP.selectedCards.size === 1 ? Array.from(MP.selectedCards)[0] : -1;
      const selCard = selCardIdx >= 0 ? (s.players[MP.mySeat]?.hand || [])[selCardIdx] : null;
      const canSellV2 = myTurn && intent === "sell" && selCard?.can_sell?.length > 0 && cr >= 1;
      const isContractCard = selCard?.can_fulfill_contract && !(selCard?.can_sell?.length > 0);
      // Launch Pad path: free contract that bypasses cards_remaining. Available
      // when LP is owned, unused this turn, a contract is selected, and no
      // hand card is in the picture (no card means use LP).
      const lpAvail = MP.currentLegal?.launch_pad_status?.available;
      const canLPContract = myTurn && lpAvail && MP.selectedContract >= 0 && !MP.selectedCards.size;

      if (intent === "sell" && isContractCard && MP.selectedContract >= 0) {
        buildBtn.textContent = "Contract";
        buildBtn.disabled = false;
      } else if (intent === "sell" && isContractCard) {
        buildBtn.textContent = "Contract";
        buildBtn.disabled = true;  // need to select a contract first
      } else if (intent === "sell" && canSellV2) {
        buildBtn.textContent = "Sell";
        buildBtn.disabled = false;
      } else if (intent === "build" && canBuild) {
        buildBtn.textContent = "Build";
        buildBtn.disabled = false;
      } else if (canLPContract) {
        buildBtn.textContent = "Contract (LP)";
        buildBtn.disabled = false;
      } else if (!intent && !MP.selectedCards.size) {
        buildBtn.textContent = "Select a card";
        buildBtn.disabled = true;
      } else {
        buildBtn.textContent = intent === "sell" ? "Sell" : "Build";
        buildBtn.disabled = true;
      }
      passBtn.disabled = !myTurn;
    } else {
      buildBtn.disabled = !canBuild;
    }

    if (alreadyBuilt && !MP.currentLegal?.matter_replication) {
      buildBtn.title = "Already built this turn";
    } else {
      buildBtn.title = "";
    }
  
    // Sell: need exactly 1 card selected and it must be sellable
    const sellCardIdx = MP.selectedCards.size === 1 ? Array.from(MP.selectedCards)[0] : -1;
    const sellCard = sellCardIdx >= 0 ? (s.players[MP.mySeat]?.hand || [])[sellCardIdx] : null;
    const canSell = myTurn && sellCard?.can_sell?.length > 0 && cr >= 1;
    sellBtn.disabled = !canSell;
  
    // Sell resource picker + Hacker Array controls
    const sellPicker = document.getElementById("mp-sell-picker");
    if (sellPicker) {
      if (canSell) {
        const me = s.players[MP.mySeat];
        let resBtnsHtml = "";
        let bestRes = sellCard.can_sell[0];
  
        if (sellCard.can_sell.length > 1) {
          let bestRev = 0;
          resBtnsHtml = `<div class="sell-res-label">Sell resource:</div><div class="sell-res-btns">` +
            sellCard.can_sell.map(r => {
              const rate = me.rates?.[r] || 0;
              const rev = rate > 0 ? rate * (s.market[r] || 0) : 0;
              if (rev > bestRev) { bestRev = rev; bestRes = r; }
              const color = RESOURCE_COLORS[r] || "var(--text-muted)";
              return `<button class="sell-res-btn" data-res="${r}">
                <span class="sell-res-name" style="color:${color}">${r}</span>
                <span class="sell-res-detail">rate ${rate} · $${rev}</span>
              </button>`;
            }).join("") + `</div>`;
        }
  
        // Hacker Array config lives in the Special Actions panel now; sell
        // action reads MP._v2HackerTarget / MP._v2HackerDir at execute time.

        if (resBtnsHtml) {
          sellPicker.innerHTML = `${resBtnsHtml}<input type="hidden" id="mp-sell-resource" value="${bestRes}">`;
          sellPicker.style.display = "block";
          sellPicker.querySelectorAll(".sell-res-btn").forEach(btn => {
            if (btn.dataset.res === bestRes) btn.classList.add("selected");
            btn.addEventListener("click", () => {
              sellPicker.querySelectorAll(".sell-res-btn").forEach(b => b.classList.remove("selected"));
              btn.classList.add("selected");
              document.getElementById("mp-sell-resource").value = btn.dataset.res;
            });
          });
        } else {
          sellPicker.style.display = "none";
          sellPicker.innerHTML = "";
        }
      } else {
        sellPicker.style.display = "none";
        sellPicker.innerHTML = "";
      }
    }
  
    // Contract needs: contract selected AND (hand card with contract icon + AP, OR Launch Pad available)
    const lpAvail = MP.currentLegal?.launch_pad_status?.available;
    const hasContractCard = MP.selectedCards.size === 1 && cr >= 1 &&
      (s.players[MP.mySeat]?.hand || [])[Array.from(MP.selectedCards)[0]]?.can_fulfill_contract;
    contractBtn.disabled = !myTurn || MP.selectedContract < 0 || (!hasContractCard && !lpAvail);
    passBtn.disabled = !myTurn;
  
    renderPatentActions(s);
    renderSpecialToggles(s);
  }
  
  function renderPatentActions(s) {
    const host = document.getElementById("mp-patent-actions");
    if (!host) return;
    const myTurn = isMyTurn(s);
    const pa = MP.currentLegal?.patent_actions || {};

    // Always show all 3 patent actions; grayed out when not owned/unavailable.
    const row = (owned, canUse, title, descr, body) => {
      const cls = !owned ? 'patent-action-row locked' : (canUse ? 'patent-action-row' : 'patent-action-row inactive');
      return `<div class="${cls}"><strong>${title}</strong> &mdash; ${descr}${body || ''}</div>`;
    };

    const we = pa.water_engine || {};
    const weCanUse = myTurn && we.owned && we.available;
    const weBody = `<button id="pa-we-btn" class="action-btn" ${weCanUse ? "" : "disabled"}>Use WE</button>`;

    const nano = pa.nanotechnology || {};
    const nanoCanUse = myTurn && nano.owned && nano.available;
    const nanoBody = `<button id="pa-nano-btn" class="action-btn" ${nanoCanUse ? "" : "disabled"}>Draw Card</button>`;

    const tele = pa.teleportation || {};
    const teleOpts = (tele.valid_resources || []).map(r => `<option value="${r}">${r}</option>`).join("");
    const teleCanUse = myTurn && tele.owned && tele.available && teleOpts;
    const teleBody = `
      <select id="pa-tele-resource" class="toggle-select" ${teleCanUse ? "" : "disabled"}>
        ${teleOpts || '<option value="">none</option>'}
      </select>
      <button id="pa-tele-btn" class="action-btn" ${teleCanUse ? "" : "disabled"}>Sell</button>`;

    const parts = [
      row(we.owned, weCanUse, "Water Engine", "-1 H2O, +2 PWR.", weBody),
      row(nano.owned, nanoCanUse, "Nanotechnology", "draw a card, replace pool slot.", nanoBody),
      row(tele.owned, teleCanUse, "Teleportation", "sell any resource, -1 PWR.", teleBody),
    ];

    host.innerHTML = `<h4 class="special-heading">Patent Actions</h4>${parts.join("")}`;

    // Wire buttons (OC lives in the Special Actions panel now)
    document.getElementById("pa-we-btn")?.addEventListener("click", () => {
      sendPatentAction("water_engine", {});
    });
    document.getElementById("pa-nano-btn")?.addEventListener("click", () => {
      console.log("Nanotechnology button clicked");
      sendPatentAction("nanotech", {});
    });
    document.getElementById("pa-tele-btn")?.addEventListener("click", () => {
      const r = document.getElementById("pa-tele-resource")?.value;
      if (!r) return;
      // Teleportation is a sell — carry the Hacker Array pick through so
      // HA owners get the ±3 market bump, matching a regular card sell.
      const params = {resource: r};
      if (MP._v2HackerTarget) {
        params.hacker_target = MP._v2HackerTarget;
        params.hacker_direction = MP._v2HackerDir ?? 1;
      }
      sendPatentAction("teleport", params);
    });
  }
  
  function sendPatentAction(action, params) {
    if (MP.role === "host") {
      let result;
      switch (action) {
        case "water_engine":
          result = MP.game.use_water_engine(MP.mySeat).toJs({dict_converter: Object.fromEntries});
          break;
        case "nanotech":
          result = MP.game.use_nanotechnology(MP.mySeat).toJs({dict_converter: Object.fromEntries});
          break;
        case "oc":
          result = MP.game.use_optimization_center(MP.mySeat, params.resource).toJs({dict_converter: Object.fromEntries});
          break;
        case "teleport":
          result = MP.game.use_teleportation(
            MP.mySeat,
            params.resource,
            params.hacker_target || null,
            params.hacker_direction || 0,
          ).toJs({dict_converter: Object.fromEntries});
          break;
      }
      if (result?.ok) {
        const pName = MP.currentState?.players[MP.mySeat]?.name || "You";
        MP.core.addFeedEntry({kind: "free-action", text: `You: ${result.detail}`});
        MP.network.broadcastFeed({kind: "free-action", text: `${pName}: ${result.detail}`});
        MP.core.hostRefreshState();
      } else if (result) {
        alert(result.reason || "Patent action failed");
        MP.core.hostRefreshState();
      } else {
        MP.core.hostRefreshState();
      }
    } else {
      MP.hostConn.send(JSON.stringify({type: "patent_action", action, ...params}));
    }
  }
  
  function renderSpecialToggles(s) {
    const host = document.getElementById("mp-special-toggles");
    if (!host) return;
    const myTurn = isMyTurn(s);
    const legal = MP.currentLegal || {};

    // Optimization Center — direct action; owner uses it for -1 PWR, +1 rate.
    const oc = legal.optimization_center_status || {};
    const ocOpts = (oc.valid_resources || []).map(r => `<option value="${r}">${r}</option>`).join("");
    const ocCanUse = myTurn && oc.owned && oc.available && ocOpts;
    const ocCls = !oc.owned ? 'patent-action-row locked' : (ocCanUse ? 'patent-action-row' : 'patent-action-row inactive');
    const ocHtml = `
      <div class="${ocCls}"><strong>Optimization Center</strong> &mdash; -1 PWR, +1 positive rate.
        <select id="pa-oc-resource" class="toggle-select" ${ocCanUse ? "" : "disabled"}>
          ${ocOpts || '<option value="">none</option>'}
        </select>
        <button id="pa-oc-btn" class="action-btn" ${ocCanUse ? "" : "disabled"}>Use OC</button>
      </div>`;

    // Hacker Array — per-sell modifier; target + direction are stashed on MP
    // and applied when the sell action runs.
    const ha = legal.hacker_array_status || {};
    const haResOpts = RESOURCE_ORDER.filter(r => r !== "PWR").map(r => {
      const sel = (MP._v2HackerTarget === r) ? 'selected' : '';
      return `<option value="${r}" ${sel}>${r}</option>`;
    }).join("");
    const haDir = MP._v2HackerDir ?? 1;
    const haCls = !ha.owned ? 'patent-action-row locked' : (myTurn ? 'patent-action-row' : 'patent-action-row inactive');
    const haHtml = `
      <div class="${haCls}"><strong>Hacker Array</strong> &mdash; on sell, adjust another resource.
        <select id="ha-target" class="toggle-select" ${ha.owned && myTurn ? "" : "disabled"}>
          <option value="">none</option>${haResOpts}
        </select>
        <select id="ha-dir" class="toggle-select" ${ha.owned && myTurn ? "" : "disabled"}>
          <option value="1" ${haDir === 1 ? 'selected' : ''}>+3</option>
          <option value="-1" ${haDir === -1 ? 'selected' : ''}>-3</option>
        </select>
      </div>`;

    // Space Elevator — always shown; grayed if not owned or not my turn.
    const se = legal.space_elevator_status || {};
    let seBody = "";
    if (se.owned) {
      const contract = MP.selectedContract >= 0
        ? s.available_contracts?.[MP.selectedContract]
        : null;
      const reqs = contract?.requirements || [];
      if (reqs.length > 1) {
        const opts = reqs.map(r =>
          `<option value="${r.resource}">-1 ${r.resource} (was ${r.amount})</option>`
        ).join("");
        seBody = ` &mdash; reduce <select id="se-target" class="toggle-select" ${myTurn ? "" : "disabled"}>${opts}</select> by 1`;
      } else if (reqs.length === 1) {
        seBody = ` &mdash; auto -1 ${reqs[0].resource}`;
      } else {
        seBody = " &mdash; always-on";
      }
    } else {
      seBody = " &mdash; -1 to a contract req";
    }
    const seCls = !se.owned ? 'patent-action-row locked' : (myTurn ? 'patent-action-row' : 'patent-action-row inactive');
    const seHtml = `<div class="${seCls}"><strong>Space Elevator</strong>${seBody}</div>`;

    // Launch Pad — always shown; grayed if not owned or not available.
    const lp = legal.launch_pad_status || {};
    const lpCanUse = myTurn && lp.owned && lp.available;
    const lpCls = !lp.owned ? 'patent-action-row locked' : (lpCanUse ? 'patent-action-row' : 'patent-action-row inactive');
    const lpHtml = `
      <label class="${lpCls}">
        <input type="checkbox" id="toggle-lp" ${lpCanUse ? "" : "disabled"}>
        <strong>Launch Pad</strong> &mdash; free contract, no card cost
      </label>`;

    host.innerHTML = `<h4 class="special-heading">Special Actions</h4>${ocHtml}${haHtml}${seHtml}${lpHtml}`;

    // Wire OC button (it used to live in the Patent Actions panel)
    document.getElementById("pa-oc-btn")?.addEventListener("click", () => {
      const r = document.getElementById("pa-oc-resource")?.value;
      if (!r) return;
      sendPatentAction("oc", {resource: r});
    });
    // Keep Hacker Array selection stashed so sell action can read it later.
    document.getElementById("ha-target")?.addEventListener("change", e => {
      MP._v2HackerTarget = e.target.value || null;
    });
    document.getElementById("ha-dir")?.addEventListener("change", e => {
      MP._v2HackerDir = parseInt(e.target.value);
    });
  }
  
  function _buildRateNumberLine(rates) {
    const entries = RESOURCE_ORDER.map(r => ({
      res: r,
      val: rates[r] || 0,
      color: RESOURCE_COLORS[r],
    }));
  
    // Two rows sharing 11 columns: -5 -4 -3 -2 -1 | 0 | +1 +2 +3 +4 +5
    const onesSlots = Array.from({length: 11}, () => []);
    const fivesSlots = Array.from({length: 11}, () => []);

    for (const e of entries) {
      const abs = Math.abs(e.val);
      const neg = e.val < 0;
      const ones = abs <= 5 ? abs : abs % 5;
      // The fives row only kicks in starting at |val|=6. At exactly ±5 the
      // value lives entirely in the ones row (pip at ±5); a fives pip at
      // ±5 here would visually read as ±10.
      const fives = abs <= 5 ? 0 : Math.floor(abs / 5);

      let onesSlot = 5;
      if (ones > 0) onesSlot = neg ? 5 - ones : 5 + ones;
      onesSlots[onesSlot].push(e);

      if (fives > 0) {
        let fivesSlot = neg ? 5 - fives : 5 + fives;
        fivesSlot = Math.max(0, Math.min(10, fivesSlot));
        fivesSlots[fivesSlot].push(e);
      }
    }
  
    // Cell labels: 1s track shows -5..-1, 0, +1..+5; 5s track shows -25..-5, 0, +5..+25
    const onesLabels = ["\u22125","\u22124","\u22123","\u22122","\u22121","0","+1","+2","+3","+4","+5"];
    const fivesLabels = ["\u221225","\u221220","\u221215","\u221210","\u22125","0","+5","+10","+15","+20","+25"];
  
    function renderRow(slots, labels, dotClass, slotClass) {
      return slots.map((dots, i) => {
        const isZero = i === 5;
        const isNeg = i < 5;
        const hue = isZero ? "" : isNeg ? "rnl-cell-neg" : "rnl-cell-pos";
        const dotHtml = dots.map(e => {
          const longCls = e.res.length > 3 ? ' rnl-long' : '';
          return `<span class="${dotClass}${longCls}" style="background:${e.color};color:${MP.pipTextColor(e.res)}" title="${e.res}: ${e.val > 0 ? '+' : ''}${e.val}">${dotClass === 'rnl-dot' ? e.res : ''}</span>`;
        }).join("");
        return `<div class="rnl-slot-wrap ${isZero ? 'rnl-zero' : ''}">
          <div class="rnl-slot ${hue} ${slotClass || ''}">
            <span class="rnl-bg-num">${labels[i]}</span>
            ${dotHtml}
          </div>
        </div>`;
      }).join("");
    }
  
    return `<div class="rate-number-line">
      <div class="rnl-track">${renderRow(onesSlots, onesLabels, "rnl-dot", "")}</div>
      <div class="rnl-track">${renderRow(fivesSlots, fivesLabels, "rnl-dot-sm", "rnl-slot-sm")}</div>
    </div>`;
  }
  
  function renderPlayerPanel(s) {
    const me = s.players[MP.mySeat];
    if (!me) return;
    const panel = document.getElementById("mp-your-stats");
  
    // Set zone title with NW
    const title = document.getElementById("player-zone-title");
    if (title) {
      const nwColor = me.net_worth >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
      title.innerHTML = `<span>${me.name}${me.corporation ? ` \u2014 ${me.corporation}` : ''}</span><span class="player-zone-nw" style="color:${nwColor}">NW $${me.net_worth}</span>`;
    }
  
    const ratesGrid = RESOURCE_ORDER.map(r => {
      const v = me.rates?.[r] || 0;
      const cls = v > 0 ? "rate-pos" : v < 0 ? "rate-neg" : "rate-zero";
      return `<div class="rate-chip ${cls}"><span class="rate-res" style="color:${RESOURCE_COLORS[r]}">${r}</span><span class="rate-val">${v > 0 ? "+" : ""}${v}</span></div>`;
    }).join("");
  
    // Build rate number line — ones track (0-9 each side) + tens track if needed
    const rateNumberLine = _buildRateNumberLine(me.rates || {});
  
    const builtCards = me.built_cards || [];
    const buildings = builtCards.filter(c => !c.effect && c.slot !== 5).map(c => c.building);
    const specials = builtCards.filter(c => c.effect && c.slot !== 5);
    const patents = builtCards.filter(c => c.slot === 5);
    const buildingList = buildings.length ? buildings.join(", ") : "none";
    const specialList = specials.map(c => `<span class="opp-special">${c.building}</span>`).join(" ");
    const patentList = patents.map(c => `<span class="opp-patent">${c.building}</span>`).join(" ");
  
    const isV2 = document.body.classList.contains("theme-v2");
    const moneyHtml = isV2
      ? `<div class="player-stats-v2">
          <div class="player-nw-badge" style="color:${me.net_worth >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">NW $${me.net_worth}</div>
          <div class="player-money-line">
            <span>Cash: <strong>$${me.money}</strong></span>
            <span>Contracts: <strong>${me.contracts_fulfilled || 0}</strong></span>
            ${me.debt > 0 ? `<span style="color:var(--accent-red)">Debt: <strong>$${me.debt}</strong></span>` : ''}
            ${me.credit > 0 ? `<span style="color:var(--accent-yellow)">Credit: <strong>$${me.credit}</strong></span>` : ''}
          </div>
        </div>`
      : `<div class="player-stats-money">
          Cash $${me.money}${me.debt > 0 ? ` | <span style="color:var(--accent-red)">Debt $${me.debt}</span>` : ''}${me.credit > 0 ? ` | <span style="color:var(--accent-yellow)">Credit $${me.credit}</span>` : ''} | ${me.contracts_fulfilled || 0} contracts
        </div>`;

    panel.innerHTML = `
      <div class="player-stats">
        ${moneyHtml}
        <div class="player-rates-grid">${ratesGrid}</div>
        ${rateNumberLine}
        <div class="player-buildings-box">
          <div class="player-stats-buildings">${buildingList}</div>
          ${specialList ? `<div class="player-stats-specials">${specialList}</div>` : ''}
          ${patentList ? `<div class="player-stats-specials">${patentList}</div>` : ''}
        </div>
      </div>
    `;
  }
  
  function renderOpponents(s) {
    const strip = document.getElementById("mp-opponents");
    const isV2 = document.body.classList.contains("theme-v2");

    // In v2: show ALL players (including you) as a turn-order list.
    // In classic: show only opponents.
    const playerList = isV2
      ? s.players.map((p, i) => ({p, idx: i}))
      : s.players.filter((_, i) => i !== MP.mySeat).map(p => ({p, idx: s.players.indexOf(p)}));

    strip.innerHTML = playerList.map(({p, idx}) => {
      const isActive = idx === s.current_player_index;
      const isYou = idx === MP.mySeat;
      const color = PLAYER_COLORS[idx % PLAYER_COLORS.length];

      const ratesGrid = RESOURCE_ORDER
        .filter(r => !isV2 || (p.rates?.[r] || 0) !== 0)
        .map(r => {
          const v = p.rates?.[r] || 0;
          const cls = v > 0 ? "rate-pos" : v < 0 ? "rate-neg" : "rate-zero";
          return `<div class="rate-chip sm ${cls}"><span class="rate-res" style="color:${RESOURCE_COLORS[r]}">${r}</span><span class="rate-val">${v > 0 ? "+" : ""}${v}</span></div>`;
        }).join("");

      const builtCards = p.built_cards || [];
      const buildings = builtCards.filter(c => !c.effect && c.slot !== 5).map(c => c.building);
      const specials = builtCards.filter(c => c.effect && c.slot !== 5);
      const patents = builtCards.filter(c => c.slot === 5);
      const buildingList = buildings.length ? buildings.join(", ") : "none";
      const specialList = specials.map(c => `<span class="opp-special">${c.building}</span>`).join(" ");
      const patentList = patents.map(c => `<span class="opp-patent">${c.building}</span>`).join(" ");

      const youTag = isYou ? ' <span style="opacity:0.6">(You)</span>' : '';
      const aiTag = !p.is_human && !isYou ? ' (AI)' : '';
      const turnIndicator = isActive ? '<span class="turn-dot" title="Active turn">●</span> ' : '';
      const youClass = isYou ? ' is-you' : '';
      // V2: player color as subtle background tint instead of left border
      const bgStyle = isV2
        ? `background: color-mix(in srgb, ${color} 8%, var(--bg-elevated));`
        : `border-left:3px solid ${color};`;

      return `
        <div class="opponent-card ${isActive ? 'active-turn' : ''}${youClass}" style="${bgStyle}">
          <div class="opponent-header">
            <span class="opponent-name" style="color:${color}">${turnIndicator}${p.name}${aiTag}${youTag}</span>
            <span class="opponent-nw" style="color:${p.net_worth >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">NW $${p.net_worth}</span>
          </div>
          ${isV2 ? `
            <div class="opponent-stats-v2">
              <div>Cash: <strong>$${p.money}</strong></div>
              ${p.debt > 0 ? `<div style="color:var(--accent-red)">Debt: <strong>$${p.debt}</strong></div>` : ''}
              <div>Contracts: <strong>${p.contracts_fulfilled || 0}</strong></div>
            </div>
          ` : `
            <div class="opponent-money">
              $${p.money}${p.debt > 0 ? ` | <span style="color:var(--accent-red)">Debt $${p.debt}</span>` : ''} | ${p.contracts_fulfilled || 0} contracts
            </div>
          `}
          <div class="opponent-rates-grid">${ratesGrid}</div>
          <div class="opponent-buildings">${buildingList}</div>
          ${specialList ? `<div class="opponent-specials">${specialList}</div>` : ''}
          ${patentList ? `<div class="opponent-specials">${patentList}</div>` : ''}
        </div>
      `;
    }).join("");
  }
  
  
  function buildEventCardLabel(ed) {
    // Simple label for the discard card face — just the event type, no effects
    if (!ed) return "";
    switch (ed.event_type || "") {
      case "draw_building_card": return "Draw Building Card";
      case "news_bulletin": {
        const name = ed.news_name || "?";
        const effects = ed.news_effects || "";
        return effects ? `News: ${name} (${effects})` : `News: ${name}`;
      }
      case "patent_auction": return "Patent Auction";
      case "power_bill": return "Power Bill";
      case "debt_collection": return "Debt Collection";
      case "futures_trading": return "Futures Trading";
      case "futures_settlement": return "Futures Settlement";
      case "end_round": return "END OF ROUND";
      case "end_game": return "END GAME";
      default: return (ed.event_type || "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    }
  }
  
  function buildEventTitle(ed) {
    if (!ed) return "";
    const type = ed.event_type || "";
    let title = "";
  
    switch (type) {
      case "draw_building_card":
        title = `Draw: ${ed.card_drawn || "?"}`;
        break;
      case "news_bulletin":
        title = `News: ${ed.news_name || "?"}`;
        break;
      case "patent_auction":
        title = `Patent Auction: ${ed.auction_result || ""}`;
        break;
      case "power_bill": title = "Power Bill"; break;
      case "debt_collection": title = "Debt Collection"; break;
      case "futures_trading": {
        const changes = (ed.market_changes || []).map(c =>
          `${c.resource} $${c.price_before}\u2192$${c.price_after}`
        ).join(", ");
        title = `Futures Trading${changes ? ": " + changes : ""}`;
        break;
      }
      case "futures_settlement": title = "Futures Settlement"; break;
      case "end_round": title = "END OF ROUND"; break;
      case "end_game": title = "END GAME"; break;
      default: title = type.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    }
  
    // PWR adjust prepended before title (rendered before lightning icon)
    if (ed.pwr_adjust) {
      const rate = ed.pwr_adjust.rate;
      const cls = rate >= 0 ? "pwr-up" : "pwr-down";
      const sign = rate >= 0 ? "+" : "";
      title = `<span class="${cls}">${sign}${rate} PWR</span> ${title}`;
    }
  
    return title;
  }
  
  function fmtMoney(text) {
    if (!text) return "";
    return text.replace(/\$(\d+)/g, '<span class="feed-money">$$$1</span>');
  }
  
  function formatRates(rates) {
    // {FE: 1, PWR: -1} → "+1 FE -1 PWR"
    if (!rates || typeof rates !== "object") return "";
    return Object.entries(rates)
      .filter(([, v]) => v !== 0)
      .map(([r, v]) => `<span class="${v > 0 ? 'rate-pos' : 'rate-neg'}">${v > 0 ? '+' : ''}${v} ${r}</span>`)
      .join(" ");
  }
  
  function formatActionSummary(a) {
    // Build a concise one-line summary with rates
    if (a.type === "build") {
      const names = (a.buildings || []).join(", ");
      const cost = a.build_money_spent > 0 ? ` ($${a.build_money_spent})` : "";
      const rates = formatRates(a.rates_gained);
      return `Built ${names}${cost}${rates ? " " + rates : ""}`;
    }
    if (a.type === "sell") {
      return `Sold ${a.sell_amount || ""} ${a.sell_resource || ""} for $${a.sell_revenue || 0}`;
    }
    if (a.type === "contract") {
      return `Contract ${a.contract_label || ""} for $${a.contract_reward || 0}`;
    }
    if (a.type === "free_action") {
      return a.detail || "Free action";
    }
    return a.detail || a.type || "?";
  }
  
  // formatEventTitle removed — all events now use structured data via buildEventTitle()
  
  function renderFeedEntry(e) {
    if (e.kind === "turn-start") {
      // Game kickoff or round marker
      const detailLines = (e.details || "").split("\n").map(l => `<div>${fmtMoney(l)}</div>`).join("");
      return `
        <details class="feed-group kickoff">
          <summary class="feed-group-title">${e.text}</summary>
          <div class="feed-group-body">${detailLines}</div>
        </details>
      `;
    }
  
    if (e.kind === "turn") {
      const colonIdx = (e.text || "").indexOf(":");
      const playerName = colonIdx > 0 ? e.text.substring(0, colonIdx) : "";
      const playerIdx = MP.currentState?.players?.findIndex(p => p.name === playerName) ?? -1;
      const color = playerIdx >= 0 ? PLAYER_COLORS[playerIdx % PLAYER_COLORS.length] : "var(--text-muted)";
      const title = playerName || "Turn";
      const summary = colonIdx > 0 ? e.text.substring(colonIdx + 1).trim() : e.text;
  
      const actionList = e.actions || [];
      const before = e.playerBefore;
      const after = e.playerAfter;
  
      // Build structured detail: action table + before/after comparison
      let detailHtml = "";
      if (actionList.length > 0) {
        detailHtml += `<table class="feed-action-table">`;
        for (const a of actionList) {
          const rates = formatRates(a.rates_gained);
          const cost = a.build_money_spent > 0 ? `<span class="feed-money">-$${a.build_money_spent}</span>` : "";
          const rev = a.sell_revenue > 0 ? `<span class="feed-money">+$${a.sell_revenue}</span>` : "";
          const reward = a.contract_reward > 0 ? `<span class="feed-money">+$${a.contract_reward}</span>` : "";
          const label = a.type === "build" ? (a.buildings || []).join(", ")
            : a.type === "sell" ? `Sell ${a.sell_amount || ""} ${a.sell_resource || ""}`
            : a.type === "contract" ? `Contract ${a.contract_label || ""}`
            : a.detail || a.type;
          detailHtml += `<tr><td class="feed-at-action">${label}</td><td class="feed-at-cash">${cost}${rev}${reward}</td><td class="feed-at-rates">${rates}</td></tr>`;
        }
        detailHtml += `</table>`;
      }
      // Before/after snapshot
      if (before && after) {
        const nwDelta = after.net_worth - before.net_worth;
        const nwClass = nwDelta >= 0 ? "positive" : "negative";
        detailHtml += `<div class="feed-before-after">`;
        detailHtml += `<span>$${before.money}→$${after.money}</span>`;
        if (after.debt > 0 || before.debt > 0) detailHtml += `<span class="negative">Debt $${before.debt}→$${after.debt}</span>`;
        detailHtml += `<span class="${nwClass}">NW $${before.net_worth}→$${after.net_worth} (${nwDelta >= 0 ? "+" : ""}${nwDelta})</span>`;
        detailHtml += `</div>`;
      }
  
      if (detailHtml) {
        return `
          <details class="feed-group turn" style="border-left-color:${color}">
            <summary class="feed-group-title"><span class="feed-player-name" style="color:${color}">${title}</span> ${fmtMoney(summary)} <span class="feed-time">${e.time}</span></summary>
            <div class="feed-group-body">${detailHtml}</div>
          </details>
        `;
      }
      return `
        <div class="feed-group turn" style="border-left-color:${color}">
          <div class="feed-group-title"><span class="feed-player-name" style="color:${color}">${title}</span> ${fmtMoney(summary)} <span class="feed-time">${e.time}</span></div>
        </div>
      `;
    }
  
    if (e.kind === "event") {
      const ed = e.eventData || {};
      const eventTitle = buildEventTitle(ed);
  
      // Build rich expandable detail from structured data
      let detailHtml = "";
  
      // Draw building card: show card details + what was replaced.
      // Slot info is included so the debug log makes it clear which pool
      // location the drawn card went to.
      if (ed.event_type === "draw_building_card") {
        const slotLabel = ed.card_drawn_slot != null ? ` (slot ${ed.card_drawn_slot})` : "";
        detailHtml += `<div class="feed-line-note">Drew <strong>${ed.card_drawn || "?"}</strong>${slotLabel} into pool</div>`;
        if (ed.pool_idx != null) {
          const fallback = ed.replaced_via_fallback ? ' <em>(no slot match — FIFO)</em>' : '';
          detailHtml += `<div class="feed-line-note">→ pool[${ed.pool_idx + 1}]${fallback}</div>`;
        }
        if (ed.card_replaced) {
          const rSlot = ed.card_replaced_slot != null ? ` (slot ${ed.card_replaced_slot})` : "";
          detailHtml += `<div class="feed-line-note">Replaced: ${ed.card_replaced}${rSlot}</div>`;
        }
        if (ed.card_rates?.length) {
          const rates = ed.card_rates.map(r => `<span class="${r.amount > 0 ? 'rate-pos' : 'rate-neg'}">${r.amount > 0 ? '+' : ''}${r.amount} ${r.resource}</span>`).join(" ");
          detailHtml += `<div class="feed-line-note">Rates: ${rates}</div>`;
        }
        if (ed.card_costs?.length) {
          detailHtml += `<div class="feed-line-note">Costs: ${ed.card_costs.map(c => c.amount + " " + c.resource).join(", ")}</div>`;
        }
        if (ed.card_effect) detailHtml += `<div class="feed-line-note" style="color:var(--accent-violet)">${ed.card_effect}</div>`;
      }
  
      // Futures trading: show who caused the market moves
      if (ed.event_type === "futures_trading") {
        if (ed.market_changes?.length) {
          detailHtml += `<div class="feed-line-header">Market Changes</div>`;
          for (const c of ed.market_changes) {
            const color = RESOURCE_COLORS[c.resource] || "var(--text-muted)";
            detailHtml += `<div class="feed-line-note"><span style="color:${color}">${c.resource}</span>: +${c.units} units → $${c.price_before} → $${c.price_after}</div>`;
          }
        }
        if (ed.player_contributions?.length) {
          detailHtml += `<div class="feed-line-header">Caused By</div>`;
          for (const p of ed.player_contributions) {
            const rates = Object.entries(p.rates).map(([r, v]) => `<span class="rate-neg">${v} ${r}</span>`).join(" ");
            detailHtml += `<div class="feed-line-player"><span class="feed-line-name">${p.name}</span> ${rates}</div>`;
          }
        }
      }
  
      // News: show effects
      if (ed.event_type === "news_bulletin" && ed.news_effects) {
        detailHtml += `<div class="feed-line-note">${ed.news_effects}</div>`;
      }
  
      // PWR adjust detail
      if (ed.pwr_adjust) {
        const pa = ed.pwr_adjust;
        detailHtml += `<div class="feed-line-note">PWR price: $${pa.price_before} → $${pa.price_after}</div>`;
      }
  
      // Event lines from the backend (power bill per-player details, etc.)
      if (e.event_lines?.length > 0) {
        detailHtml += e.event_lines.map(line => {
          if (line.kind === "header") return `<div class="feed-line-header">${line.text}</div>`;
          if (line.kind === "note") return `<div class="feed-line-note">${fmtMoney(line.text)}</div>`;
          if (line.kind === "player") {
            const nw = line.net_worth_after !== undefined ? `<span class="feed-nw-inline">NW $${line.net_worth_after}</span>` : '';
            return `<div class="feed-line-player"><span class="feed-line-name">${line.name || ''}</span> ${fmtMoney(line.text)} ${nw}</div>`;
          }
          return `<div class="feed-line-note">${fmtMoney(line.text || '')}</div>`;
        }).join("");
      }
  
      // Player snapshots
      if (e.player_snapshots?.length > 0) {
        detailHtml += `<div class="feed-impact">${e.player_snapshots.map(p => {
          const cls = p.net_worth >= 0 ? "positive" : "negative";
          return `<span class="feed-nw-chip ${cls}">${p.name} $${p.net_worth}</span>`;
        }).join("")}</div>`;
      }
  
      const redrawIcon = ed.redraws ? '<span class="redraw-icon" title="Draws another event">&#8635;</span>' : '';
      const isRedraw = ed._is_redraw ? '<span class="redraw-marker" title="Redrawn event">&#8627;</span> ' : '';
  
      // Always expandable for events
      return `
        <details class="feed-group event">
          <summary class="feed-group-title">${isRedraw}${ed.pwr_adjust ? '<span class="feed-event-icon">&#9889;</span> ' : ''}${eventTitle} ${redrawIcon} <span class="feed-time">${e.time}</span></summary>
          <div class="feed-group-body">${detailHtml || '<div class="feed-line-note">No additional details</div>'}</div>
        </details>
      `;
    }
  
    if (e.kind === "action" || e.kind === "free-action") {
      return `
        <div class="feed-group ${e.kind}">
          <div class="feed-group-title">${fmtMoney(e.text)} <span class="feed-time">${e.time}</span></div>
        </div>
      `;
    }
  
    if (e.kind === "round-marker") {
      return `<div class="feed-round-marker">${e.text}</div>`;
    }
  
    // Fallback
    return `<div class="feed-group"><div class="feed-group-title">${fmtMoney(e.text || '')} <span class="feed-time">${e.time || ''}</span></div></div>`;
  }
  
  function renderFeed() {
    const container = document.getElementById("feed-entries");
    if (!container) return;
    container.innerHTML = MP.feedEntries.slice(-80).reverse().map(renderFeedEntry).join("");
    container.scrollTop = 0;
  }
  
  // ===== Patent Office Picker =====
  
  function showPatentOfficePicker(patents, onPick) {
    const titleEl = document.getElementById("prompt-title");
    const bodyEl = document.getElementById("prompt-body");
    titleEl.textContent = "Patent Office — Choose a Patent";
    bodyEl.innerHTML = `
      <p style="color:var(--text-muted);margin-bottom:12px">You drew 2 patents. Pick one to keep — the other goes back.</p>
      <div style="display:flex;gap:12px;justify-content:center">
        ${patents.map((p, i) => `
          <button class="patent-pick-btn action-btn" data-pick="${i}" style="flex:1;padding:12px;text-align:center">
            <div style="font-weight:600;font-size:1rem;margin-bottom:4px">${p.name}</div>
            <div style="font-size:0.8rem;color:var(--accent-violet)">${p.effect || ''}</div>
          </button>
        `).join("")}
      </div>
    `;
    document.getElementById("prompt-modal").style.display = "flex";
    _installModalDrag("prompt-modal");
    bodyEl.querySelectorAll(".patent-pick-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.getElementById("prompt-modal").style.display = "none";
        onPick(parseInt(btn.dataset.pick));
      });
    });
  }

  // ===== Prompt Modal =====

  function showPrompt(prompt) {
    const titleEl = document.getElementById("prompt-title");
    const bodyEl = document.getElementById("prompt-body");
  
    if (prompt.kind === "patent_auction") {
      titleEl.textContent = "Patent Auction";
      const patent = prompt.patent || {};
      const ratesStr = (patent.rates || []).map(r => `${r.amount > 0 ? "+" : ""}${r.amount} ${r.resource}`).join(", ");
      bodyEl.innerHTML = `
        <p><strong>${patent.name}</strong> &mdash; ${patent.effect || ratesStr || "no effect"}</p>
        <div class="prompt-row">
          <label>Your bid ($5 increments):</label>
          <input type="number" class="prompt-bid-input" data-seat-idx="${MP.mySeat}" min="0" max="500" step="5" value="0">
        </div>
        <p style="color:var(--text-muted);font-size:0.8rem">Bid 0 to pass. Highest bidder wins; pays runner-up + $5 as debt.</p>
      `;
    } else if (prompt.kind === "debt_paydown") {
      titleEl.textContent = "Debt Collection - Pay Down Debt";
      const me = (prompt.players || []).find(p => p.seat === MP.mySeat);
      if (me) {
        const maxPay = Math.floor(Math.min(me.debt, me.money) / 10) * 10;
        bodyEl.innerHTML = `
          <p>You have $${me.debt} debt and $${me.money} cash.</p>
          <div class="prompt-row">
            <label>Pay down ($10 increments):</label>
            <input type="number" class="prompt-paydown-input" data-seat-idx="${MP.mySeat}" min="0" max="${maxPay}" step="10" value="${maxPay}">
          </div>
        `;
      } else {
        bodyEl.innerHTML = `<p>Waiting for other players...</p>`;
        // Auto-submit empty answer since we have no debt
        if (MP.role === "host") {
          MP.core.setPromptAnswer(MP.mySeat, {});
          MP.core.tryResolvePrompt();
          return;
        } else {
          MP.hostConn.send(JSON.stringify({type: "prompt_answer", answers: {}}));
          return;
        }
      }
    } else if (prompt.kind === "patent_office_pick") {
      // Remote client building Patent Office: host peeked the two
      // patents and sent them here. Reuse the existing picker UI;
      // the callback sends the pick index back to the host.
      showPatentOfficePicker(prompt.patents || [], (pickIdx) => {
        if (MP.role === "host") {
          // Shouldn't happen (host renders the picker inline in
          // wireGameButtons), but handle defensively.
          clearSelection();
        } else {
          MP.hostConn.send(JSON.stringify({type: "patent_office_pick", pick_idx: pickIdx}));
        }
      });
      return;  // picker manages the modal itself
    } else {
      titleEl.textContent = "Prompt";
      bodyEl.innerHTML = `<p>${prompt.kind}</p>`;
    }

    document.getElementById("prompt-modal").style.display = "flex";
    _installModalDrag("prompt-modal");
  }
  
  // ===== Endgame =====
  
  let endgameNwChart = null;
  
  let endgameShown = false;
  function showEndgame() {
    if (!MP.currentState || endgameShown) return;
    endgameShown = true;
    const overlay = document.getElementById("endgame-overlay");
    const rankings = document.getElementById("endgame-rankings");
    const sorted = [...MP.currentState.players].sort((a, b) => b.net_worth - a.net_worth);
    rankings.innerHTML = sorted.map((p, i) => {
      const color = PLAYER_COLORS[MP.currentState.players.indexOf(p) % PLAYER_COLORS.length];
      const isYou = MP.currentState.players.indexOf(p) === MP.mySeat;
      return `
        <div style="display:flex;justify-content:space-between;padding:8px 12px;margin:4px 0;background:var(--bg-canvas);border-radius:6px;border-left:3px solid ${color}${isYou ? ';border:1px solid var(--accent-blue)' : ''}">
          <span>${i + 1}. ${p.name}${isYou ? ' (You)' : ''}${p.is_human ? '' : ' (AI)'}</span>
          <span style="font-weight:bold;color:${p.net_worth >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">NW: $${p.net_worth} | $${p.money} cash | ${p.contracts_fulfilled || 0} contracts</span>
        </div>
      `;
    }).join("");
    renderEndgameChart();
    overlay.style.display = "flex";
    _installModalDrag("endgame-overlay");
  }
  
  function renderEndgameChart() {
    const history = MP.currentState?.player_history || [];
    if (history.length < 2) return;
    const canvas = document.getElementById("endgame-nw-chart");
    if (!canvas) return;
    const labels = history.map(h => h.turn);
    const datasets = (MP.currentState.players || []).map((p, idx) => ({
      label: p.name,
      data: history.map(h => h.players?.[idx]?.net_worth ?? 0),
      borderColor: PLAYER_COLORS[idx % PLAYER_COLORS.length],
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
    }));
    if (endgameNwChart) endgameNwChart.destroy();
    // Chart.js renders to canvas — it can't resolve var(--*) references,
    // so we resolve palette colors eagerly via MP.cssVar.
    const labelText = MP.cssVar("--text-primary");
    const axisText = MP.cssVar("--text-muted");
    const gridLine = MP.cssVar("--bg-subtle");
    endgameNwChart = new Chart(canvas, {
      type: "line",
      data: {labels, datasets},
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {labels: {boxWidth: 12, font: {size: 11}, color: labelText}},
          title: {display: true, text: "Net Worth Over Time", color: axisText},
        },
        scales: {
          x: {ticks: {color: axisText}, grid: {color: gridLine}},
          y: {ticks: {color: axisText, callback: v => "$" + v}, grid: {color: gridLine}},
        },
      },
    });
  }
  
  // ===== Action Wiring =====
  
  function wireGameButtons() {
    // Deck viewer toggle (click the deck card back)
    document.getElementById("event-deck-card")?.addEventListener("click", MP.anim.toggleDeckViewer);
  
    // Market chart toggle
    document.getElementById("market-toggle")?.addEventListener("click", () => {
      const wrap = document.getElementById("market-chart-wrap");
      if (wrap) wrap.style.display = wrap.style.display === "none" ? "block" : "none";
    });
  
    document.getElementById("mp-build-btn").addEventListener("click", () => {
      const isV2 = document.body.classList.contains("theme-v2");

      // V2 Launch Pad path: contract selected, no card selected, LP available.
      // Free contract that doesn't spend an action — works even when the
      // player has already used both AP for the turn.
      if (isV2 && MP.selectedCards.size === 0 && MP.selectedContract >= 0
          && MP.currentLegal?.launch_pad_status?.available) {
        const action = {type: "contract", contract_idx: MP.selectedContract, use_launch_pad: true};
        const seTargetEl = document.getElementById("se-target");
        if (seTargetEl?.value) action.elevator_target = seTargetEl.value;
        const contractForAnim = MP.currentState?.available_contracts?.[MP.selectedContract];
        clearSelection();
        const ok = sendAction(action);
        if (ok && contractForAnim) {
          // Animate reward popup near the player panel.
          const youEl = document.querySelector("#mp-opponents .opponent-card.is-you");
          const rect = youEl?.getBoundingClientRect();
          if (rect) MP.anim.animateReward(rect, `+$${contractForAnim.reward}`);
        }
        return;
      }

      if (MP.selectedCards.size === 0) return;

      // V2: dispatch sell or contract based on intent
      if (isV2 && MP._v2CardIntent === "sell") {
        if (MP.selectedCards.size !== 1) return;
        const cardIdx = Number([...MP.selectedCards][0]);
        const hand = MP.currentState?.players[MP.mySeat]?.hand || [];
        const card = hand[cardIdx];
        const el = document.querySelectorAll("#mp-hand-grid .hand-card")[cardIdx];
        const cardRect = el?.getBoundingClientRect();
        const cardHtml = el?.innerHTML || "";

        // Contract action (card has contract icon, not sell)
        if (card?.can_fulfill_contract && !(card?.can_sell?.length > 0) && MP.selectedContract >= 0) {
          const action = {type: "contract", contract_idx: MP.selectedContract, card_idx: cardIdx};
          const seTargetEl = document.getElementById("se-target");
          if (seTargetEl?.value) action.elevator_target = seTargetEl.value;
          const contractForAnim = MP.currentState?.available_contracts?.[MP.selectedContract];
          MP._v2CardIntent = null;
          clearSelection();
          const ok = sendAction(action);
          if (ok && cardRect) {
            MP.anim.animateFadeOut(cardRect, cardHtml);
            if (contractForAnim) MP.anim.animateReward(cardRect, `+$${contractForAnim.reward}`);
          }
          return;
        }

        // Sell action
        const action = {type: "sell", card_idx: cardIdx};
        if (MP._v2SellResource) {
          action.sell_resource = MP._v2SellResource;
          MP._v2SellResource = null;
        }
        if (MP._v2HackerTarget) {
          action.hacker_target = MP._v2HackerTarget;
          action.hacker_direction = MP._v2HackerDir ?? 1;
        }
        MP._v2CardIntent = null;
        clearSelection();
        const ok = sendAction(action);
        if (ok && cardRect) MP.anim.animateFadeOut(cardRect, cardHtml);
        return;
      }
      // Capture card rects before action
      const cardRects = [...MP.selectedCards].map(i => {
        const el = document.querySelectorAll("#mp-hand-grid .hand-card")[i];
        return el ? {rect: el.getBoundingClientRect(), html: el.innerHTML} : null;
      }).filter(Boolean);
      // Target the player's own opponent-card in the turn-order strip so
      // the build animation SHRINKS the card toward the player panel
      // (rather than stretching it to fill the wider stats row).
      const youEl = document.querySelector("#mp-opponents .opponent-card.is-you")
        || document.getElementById("mp-your-stats");
      const destRect = youEl?.getBoundingClientRect();
  
      const buildCards = [...MP.selectedCards].map(Number);
  
      // Check if Patent Office is in the build — needs patent pick
      const hand = MP.currentState?.players[MP.mySeat]?.hand || [];
      const hasPatentOffice = buildCards.some(i => hand[i]?.building === "Patent Office");
  
      if (hasPatentOffice && MP.role === "host") {
        const patents = MP.game.peek_patent_office_patents().toJs({dict_converter: Object.fromEntries});
        if (patents.length >= 2) {
          showPatentOfficePicker(patents, (pickIdx) => {
            clearSelection();
            const ok = sendAction({type: "build", build_cards: buildCards, patent_office_pick: pickIdx});
            if (ok && destRect) {
              cardRects.forEach((c, i) => {
                setTimeout(() => MP.anim.animateCard(c.rect, destRect, c.html), i * 80);
              });
            }
          });
          return;
        }
      }
  
      clearSelection();
      const ok = sendAction({type: "build", build_cards: buildCards});
  
      if (ok && destRect) {
        cardRects.forEach((c, i) => {
          setTimeout(() => MP.anim.animateCard(c.rect, destRect, c.html), i * 80);
        });
      }
    });
    document.getElementById("mp-sell-btn").addEventListener("click", () => {
      if (MP.selectedCards.size !== 1) return;
      const cardIdx = Number([...MP.selectedCards][0]);
      // Capture card rect
      const el = document.querySelectorAll("#mp-hand-grid .hand-card")[cardIdx];
      const cardRect = el?.getBoundingClientRect();
      const cardHtml = el?.innerHTML || "";
  
      const action = {type: "sell", card_idx: cardIdx};
      // V2: use the on-card resource selection; classic: use the picker
      const isV2 = document.body.classList.contains("theme-v2");
      if (isV2 && MP._v2SellResource) {
        action.sell_resource = MP._v2SellResource;
        MP._v2SellResource = null;
      } else {
        const resSel = document.getElementById("mp-sell-resource");
        if (resSel?.value) action.sell_resource = resSel.value;
      }
      if (MP._v2HackerTarget) {
        action.hacker_target = MP._v2HackerTarget;
        action.hacker_direction = MP._v2HackerDir ?? 1;
      }
      clearSelection();
      const ok = sendAction(action);

      if (ok && cardRect) MP.anim.animateFadeOut(cardRect, cardHtml);
    });
    document.getElementById("mp-contract-btn").addEventListener("click", () => {
      if (MP.selectedContract < 0) return;
      const cardIdx = MP.selectedCards.size === 1 ? Number([...MP.selectedCards][0]) : -1;
      // Capture card rect
      const el = cardIdx >= 0 ? document.querySelectorAll("#mp-hand-grid .hand-card")[cardIdx] : null;
      const cardRect = el?.getBoundingClientRect();
      const cardHtml = el?.innerHTML || "";
  
      const useLP = document.getElementById("toggle-lp")?.checked || false;
      const action = {type: "contract", contract_idx: MP.selectedContract};
      if (useLP) {
        action.use_launch_pad = true;
      } else if (cardIdx >= 0) {
        action.card_idx = cardIdx;
      }
      // Space Elevator is always-on — the engine applies the discount
      // automatically when the player owns one. We only send the resource
      // pick so the player controls which requirement gets the -1.
      const seTargetEl = document.getElementById("se-target");
      if (seTargetEl?.value) action.elevator_target = seTargetEl.value;
      const contractForAnim = MP.currentState?.available_contracts?.[MP.selectedContract];
      clearSelection();
      const ok = sendAction(action);
  
      if (ok && cardRect) {
        MP.anim.animateFadeOut(cardRect, cardHtml);
        if (contractForAnim) MP.anim.animateReward(cardRect, `+$${contractForAnim.reward}`);
      }
    });
    document.getElementById("mp-pass-btn").addEventListener("click", () => {
      clearSelection();
      // Log the pass/end turn
      const playerName = MP.currentState?.players[MP.mySeat]?.name || "You";
      MP.core.addFeedEntry({kind: "turn", text: `${playerName}: End Turn`});
      MP.network.broadcastFeed({kind: "turn", text: `${playerName}: End Turn`});
  
      if (MP.role === "host") {
        const result = MP.game.end_human_turn().toJs({dict_converter: Object.fromEntries});
        humanTurnActions = [];
  
        // If awaiting prompt (patent auction, debt paydown), don't add event
        // entry yet — the prompt resolution will add the complete event.
        if (result.awaiting_prompt) {
          MP.core.hostRefreshState();
          MP.core.handleHostPrompt(MP.currentState.pending_prompt);
          return;
        }
  
        // Event resolved normally — add feed entry
        const snapAfter = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
        const playerSnapsAfter = snapAfter.players.map(p => ({name: p.name, money: p.money, debt: p.debt, net_worth: p.net_worth}));
        const evData = result.structured || snapAfter.last_event_data || {};
        MP.core.addEventFeedEntries(
          result.detail || "Turn ended",
          (result.lines || snapAfter.last_event_lines || []).map(l => Object.assign({}, l)),
          playerSnapsAfter,
          evData,
        );
        // Render chained (redraw) events as separate feed entries
        const chained = result.chained_events || [];
        for (const ce of chained) {
          const ceData = ce.structured || {};
          ceData._is_redraw = true;
          const ceLines = ce.lines || [];
          MP.core.addEventFeedEntries(ce.detail, ceLines, playerSnapsAfter, ceData);
        }
        MP.core.hostRefreshState();
        MP.core.hostAdvanceGame();
      } else {
        MP.hostConn.send(JSON.stringify({type: "end_turn"}));
      }
    });
  }
  
  // Accumulate human actions during their turn for a grouped feed entry
  let humanTurnActions = [];
  
  function sendAction(action) {
    if (MP.role === "host") {
      const beforeSnap = MP.currentState?.players[MP.mySeat];
      const playerBefore = beforeSnap ? {money: beforeSnap.money, debt: beforeSnap.debt, net_worth: beforeSnap.net_worth, rates: Object.assign({}, beforeSnap.rates)} : null;
  
      const result = MP.game.apply_human_action(MP.pyodide.toPy(action)).toJs({dict_converter: Object.fromEntries});
      if (result.ok) {
        humanTurnActions.push(result);
        MP.core.hostRefreshState();
        if (result.rates_gained) {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              MP.anim.animateRateChanges(result.rates_gained, ".player-panel-rates");
            });
          });
        }
        const afterSnap = MP.currentState?.players[MP.mySeat];
        const playerAfter = afterSnap ? {money: afterSnap.money, debt: afterSnap.debt, net_worth: afterSnap.net_worth, rates: Object.assign({}, afterSnap.rates)} : null;
        const playerName = MP.currentState?.players[MP.mySeat]?.name || "You";
        const title = formatActionSummary(result);
        MP.core.addFeedEntry({
          kind: "turn",
          text: `${playerName}: ${title}`,
          actions: [result],
          playerBefore: playerBefore,
          playerAfter: playerAfter,
        });
        MP.network.broadcastFeed({
          kind: "turn",
          text: `${playerName}: ${title}`,
          actions: [result],
          playerBefore: playerBefore,
          playerAfter: playerAfter,
        });
        return true;
      } else {
        alert(result.reason || "Action failed");
        MP.core.hostRefreshState();
        return false;
      }
    } else {
      MP.hostConn.send(JSON.stringify({type: "action", action}));
      return true; // assume success for clients (host validates)
    }
  }
  

  MP.ui = {
    renderGame,
    isMyTurn,
    clearSelection,
    showPrompt,
    showEndgame,
    wireGameButtons,
    buildEventCardLabel,
    renderFeed,
  };
})();
