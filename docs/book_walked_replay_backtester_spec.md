# A book-walked replay backtester for Polymarket FAK BTC 5-minute markets

**Current flat-0.5 paper fills are not a model; they are a placeholder that will systematically overstate ROI.** For a taker strategy on a 0.01-tick binary market, the dominant live costs are half-spread, fee curvature around p=0.50, latency drift during ~130–300 ms signal-to-ack windows, and second-level book-walk in thin regimes — none of which a constant-price fill captures. The published academic and practitioner evidence places the realistic paper-to-live haircut in a wide band: Harvey & Liu 2015 give **~25–60 % Sharpe haircut** depending on reported SR, Wiecki et al. 2016 find **R² < 0.025** between in-sample and out-of-sample Sharpe on 888 Quantopian algos, and crypto liquidity studies (Kaiko 2024–25) show single-venue slippage jumping 3 bps → 500+ bps within minutes during stress. Your stated prior of "20–60 % of paper ROI evaporates" is therefore inside the defensible envelope and not extreme. The remainder of this document specifies a Python stdlib-first replay backtester that can quantify which regions of that envelope your strategy actually lives in, grounded in primary Polymarket sources, the microstructure literature, and two existing Polymarket-specific repos that already attempt book-walked simulation.

---

## 1. Replay architecture

### 1.1 What has survived into production and what fails

**LOBSTER** (lobsterdata.com) is the academic standard for Nasdaq message-by-message replay and is the dataset behind most peer-reviewed limit-order-book (LOB) studies since 2013 (Cont & de Larrard 2013 SIAM J. Financial Math.; Gould et al. 2013 "Limit Order Books" Quant. Finance 13:1709). It delivers a full **message stream plus reconstructed snapshots**, not snapshot-only; this distinction matters because the bias analysis below shows snapshot-only replay is not sound for <1s horizons.

**Tardis.dev** (github.com/tardis-dev/tardis-python, tardis-machine) is the production crypto analogue, delivering both full L2 deltas and periodic snapshots across 30+ venues. **Kaiko** and **CoinAPI** sell L2 but are typically aggregated/reconstructed rather than tick-by-tick and are known for gaps on venue outages (Kaiko 2024 fragmentation reports document exactly this). The **Polymarket CLOB WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, documented at docs.polymarket.com/developers/market-makers/data-feeds) publishes `book`, `price_change`, and `last_trade_price` events — this is an **event stream**, not a pure snapshot feed, and the known failure mode is visible in nautilus_trader issue #2980 where tick-size precision changes mid-stream break naïve parsers.

Common failure modes across all four systems: **clock-skew between matching engine and client**, **reconstructed books drifting from true state over long reconnects**, and **silent message loss during feed handler restarts**. Any replay that trusts a single ingestion session without end-of-session book reconciliation inherits these.

### 1.2 Snapshot cadence floor for sub-1s horizons

**Your signal-to-fill horizon is under 1 second; a snapshot-only replay is fiction below roughly 100 ms cadence, and even at 100 ms you are at the limit.** The grounding:

Cont 2011 ("Statistical Modeling of High-Frequency Financial Data", IEEE Signal Processing Magazine 28(5):16–25) measures level-1 event rates in the hundreds-to-thousands per second on liquid stocks; the typical duration τ_L between limit events is sub-second and often sub-100ms. Cont, Stoikov & Talreja 2010 (*Operations Research* 58:549) model the level-1 book as a Markovian queue whose state changes on the same timescale. Applied to a Polymarket BTC 5-minute market during the active window, the active-side book changes multiple times per second under normal load, and the `price_change` WebSocket evidence in nautilus_trader issue #2980 confirms this empirically.

Practitioner rule of thumb for L2 replay fidelity (Databento and Tardis blog posts repeatedly state this): **replay cadence must be at least one order of magnitude finer than the strategy decision horizon**. For a <1 s horizon that means **≥10 Hz snapshots minimum, ideally the full event stream**. Below 1 Hz you are backtesting a world that does not exist.

**Actionable implication:** wire `scrape.py` to consume the CLOB WebSocket `book`/`price_change` channel directly and persist *deltas*, not only periodic full snapshots. A reasonable hybrid is full snapshot every 5 seconds for rebuild + all deltas in between; this is the format LOBSTER delivers and what Tardis calls "normalized" data.

### 1.3 Inter-snapshot gap treatment

Three candidates: **freeze-last-book**, **linear level interpolation**, **mid-walk** (interpolate the mid but keep depth frozen). The microstructure literature (Gould et al. 2013 review; Bouchaud, Bonart, Donier & Gould 2018 *Trades, Quotes and Prices*, Cambridge Chapter 6) is unambiguous that **linear interpolation of discrete LOB levels has no physical basis** — depth is not a continuous field, and a linearly interpolated half-level between two snapshots corresponds to no realizable book.

**Freeze-last-book is the least biased choice for liquidity takers** because it is what a causal observer would see: you cannot consume depth that was not visible at your latest evidence. It is the default in nautilus_trader's immutable-book contract (nautilustrader.io/docs/latest/concepts/backtesting/ — "Historical order book and trade data are immutable during backtesting… Fills never modify the underlying book state"). It is *conservative* in the sense that depth that has arrived but not been observed is not usable by the simulated strategy — but it is *anti-conservative* if depth has *vanished* and the frozen snapshot still shows liquidity that is no longer there. This asymmetry is why 1.2 matters: at coarse cadence the optimistic side dominates.

**Recommendation:** freeze-last-book, but emit a per-trade flag `book_staleness_ms = t_fill − t_last_snapshot` so every fill carries the age of its evidence, and reject-or-flag any fill where staleness > configured threshold (suggested 500 ms hard, 200 ms soft).

### 1.4 Build vs adopt

Two Polymarket-specific repos already exist and are worth studying before building:

- **evan-kolberg/prediction-market-backtesting** — a NautilusTrader extension with custom Polymarket and Kalshi adapters, explicitly doing prediction-market backtesting on top of Nautilus's L2 engine. This is the strongest existing signal and is the natural reference implementation.
- **agent-next/polymarket-paper-trader** — documents "Level-by-level order book execution — your order walks the real Polymarket ask/bid book, consuming liquidity at each price level" plus the exact fee formula (`bps/10000 × min(price, 1-price) × shares`). Useful as a semantic oracle for book-walk arithmetic.
- **polybacktest.com** — hosted service with `simulate_buy()` that walks historical 1-minute snapshots. Not open source but documents the approach.
- **txbabaxyz/polyrec** — a separate active dashboard for BTC UP/DOWN 15-min markets with `replicate_balance.py` and `fade_impulse_backtest.py`. Same asset class, different horizon.

On the generic side, **nautilus_trader L2_MBP with OrderBookDelta feeds** (nautilustrader.io/docs/latest/tutorials/backtest_bybit_orderbook/) is production-grade, deterministic with `random_seed`, tracks consumed depth via `liquidity_consumption=True`, and already has a Polymarket execution adapter (nautilustrader.io/docs/latest/integrations/polymarket/) mapping `IOC → FAK`. **hummingbot** backtest is bar-based and does not do L2 book walks; **vectorbt**, **mlfinlab**, **backtrader**, **zipline-reloaded** are all vectorized or bar-based and inappropriate.

**Adopt-vs-build recommendation:** build a thin 500-line `ReplayExecutor` for the MVP and keep `evan-kolberg/prediction-market-backtesting` as a cross-validation oracle. Reason: your unit-of-analysis is one asset class, one order type (FAK), one fee formula, one decision horizon — nautilus's abstractions (multi-venue OMS, margin accounts, emulators) are net-negative integration cost for that surface. Reserve adoption for when you need multi-leg or maker strategies. The 2-3 design choices where you will silently fool yourself are identified in §8.

---

## 2. FAK simulation semantics

### 2.1 Order types and matching

Polymarket CLOB supports four time-in-force types, with precise naming confirmed in the official Polymarket/agent-skills repo (github.com/Polymarket/agent-skills/blob/main/order-patterns.md) and Polymarket/py-clob-client-v2:

| Type | Semantics | Your usage |
|---|---|---|
| GTC | rests on book until filled/cancelled | not used |
| GTD | GTC with expiration timestamp (UTC seconds) | not used |
| FOK | all-or-nothing immediate; full fill or cancel entire | not used |
| **FAK** | **fill what's available now, cancel residual (== IOC)** | your strategy |

The nautilus_trader Polymarket adapter documents the explicit mapping: **`IOC → FAK`** (raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/integrations/polymarket.md). All orders on Polymarket are structurally limit orders — "market orders" are marketable limit orders where `price` acts as a **worst-price slippage bound**, not a target (docs.polymarket.com/trading/orders/create; bullpen.fi docs mirror this). If your FAK buy at 0.55 matches an ask at 0.52, you pay 0.52 — price improvement is real.

### 2.2 Tick size and price boundaries — **this is where your 0.01 assumption is wrong**

Tick size is **per-market, not global**. The py-clob-client `ROUNDING_CONFIG` (github.com/Polymarket/py-clob-client/issues/121) enumerates four tick sizes:

```
"0.1"    : price=1 dp, size=2, amount=3
"0.01"   : price=2 dp, size=2, amount=4
"0.001"  : price=3 dp, size=2, amount=5
"0.0001" : price=4 dp, size=2, amount=6
```

Markets expose tick size via `client.getTickSize(tokenID)`. **Tick size changes dynamically as price approaches 0/1**: nautilus_trader issue #2980 captures a real WebSocket message where the YES side had best_ask=0.999 (implying 0.001 tick) while the paired NO side had best_bid=0.001, best_ask=0.06 (implying 0.01 tick) — the CLOB is happily publishing asymmetric tick precision on the same market. Polymarket py-clob-client issue #218 (2025-12) documents that the REST API rejects limit orders at 0.999 with `price (0.999), min: 0.01 - max: 0.99` even though the frontend accepts them — an unresolved inconsistency with direct implications for TP/SL orders near extremes.

**The minimum order size is $1 notional** (creating FOK with <$1 fails in py-clob-client; confirmed in multiple issues). Minimum price step tracks the market-specific tick.

**Replay implication:** your ReplayExecutor must read tick-size from each snapshot (ideally embed it in the captured book record), not hardcode 0.01. This matters because your TP/SL via ProfitGrabber will frequently want prices near 0 or 1 for short-dated BTC markets, exactly where tick changes and the `min: 0.01 − max: 0.99` API boundary bites.

### 2.3 Fees — curvature around p=0.50 is large and matters

**International Polymarket (your venue):** probability-based dynamic taker fee, peak 1.80 % at p=0.50, shrinking toward zero at 0.01 and 0.99, formula `bps/10000 × p × (1−p) × shares × coefficient` with taker coefficient chosen by category (tradetheoutcome.com/polymarket-fees and the PolyArb post cite Polymarket's published range 0 %–1.80 %). Crypto and Finance are paid categories; Geopolitics is free. Polymarket Help Center confirms: "Taker fees are calculated in USDC and vary based on the share price. Fees are symmetric around 50 % probability — a trade at 30¢ incurs the same dollar fee as a trade at 70¢" (help.polymarket.com/en/articles/13364471). Minimum fee 0.00001 USDC; "anything smaller rounds to zero, so very small trades near the extremes may incur no fee at all".

**Polymarket US (separate CFTC-licensed venue, polymarketexchange.com/fees-hours.html, effective 2026-04-03):** flat taker coefficient Θ=0.05 (max $1.25 per 100 contracts at p=0.50), maker rebate Θ=0.0125 (25 % of taker), plus a 50 %-of-taker rebate through April 2026. This is a **different fee schedule** from the international venue.

**Gas:** off-chain matching, on-chain settlement on Polygon, and Polymarket's **Relayer sponsors gas** — you pay no gas on placement, approvals, or CTF operations (polymarketarbitragebot.net/guides/polymarket-fees-explained). This simplifies your replay fee model to zero gas.

**Replay implication:** fees are not flat bps; they are a **bell curve centred at 0.50**. Your strategy trades short-dated BTC Up/Down which can spend large fractions of its life around p≈0.50 where fee drag is maximal. The flat-0.5 paper backtester materially overstates edge specifically in the highest-activity regime.

### 2.4 Book-walk, reported amounts, and residual handling

Polymarket documents that market orders "execute immediately at the best available prices" and large ones "can experience significant slippage" (docs.bullpen.fi mirror). The REST order response (docs.polymarket.com/developers/CLOB/orders/create-order) returns `makingAmount` and `takingAmount` reflecting the volume-weighted result across all consumed price levels. A naïve aggregated walk predicts this correctly when there is no adversarial cross-stream matching, but two pitfalls are documented in GitHub issues:

- **Precision clipping on FOK/FAK sells** (py-clob-client issue #121): maker amount limited to 2 decimal places, taker amount to 4, product price×size must not exceed 2 decimals. A naïve walk that predicts `takingAmount` to 6 dp will disagree with the API by up to the rounding unit. Your ReplayExecutor must apply the exact same `ROUNDING_CONFIG` quantisation.
- **Overfill on FOK** (nautilus_trader #3221, @Javdu10): the matching engine can report fills slightly above requested size, handled via `allow_overfills` + `overfill_qty`. Build a symmetric tolerance into invariant tests.

**Residual handling is trivial for FAK**: unfilled quantity → emit an `unfilled` fill record with `filled_qty=0, residual=requested` and move on. No resting, no queue, no cancel round-trip.

### 2.5 Self-trade protection

I could not locate explicit documentation of server-side self-trade protection in docs.polymarket.com or the public clob-client repos — **mark this as [UNVERIFIED]**. In replay where the strategy is its own sole "self", the pragmatic rule is: if a simulated FAK would match against an order your own simulated strategy placed less than `tick_cancel_latency` ago, reject or skip. For a pure taker strategy this is a non-issue because you never rest liquidity, so self-trade is impossible by construction. If you later add ProfitGrabber limit orders that could be crossed by your own FAK, revisit.

---

## 3. Latency injection

### 3.1 Published Polymarket RTT — you have numbers

Polymarket CLOB runs on **AWS eu-west-2 (London)**, confirmed by multiple independent VPS vendors (quantvps.com/blog/polymarket-servers-location; newyorkcityservers.com/blog/polymarket-server-location-latency-guide; tradoxvps.com). Published round-trip measurements:

| Origin | CLOB RTT (measured) | Source |
|---|---|---|
| Dublin (AWS eu-west-1) | **<5 ms, sub-ms achievable** | TradoxVPS, QuantVPS |
| Amsterdam | ~10 ms | TradoxVPS |
| US East Coast | ~130 ms | TradoxVPS |
| Singapore (yours) | ~180–200 ms network + server | inferred from AWS SG↔London RTT ≈ 175 ms + server processing |

The **server processing component is ~90 ms baseline** for WebSocket responses, independent of your network position (tradoxvps.com/how-to-test-latency-of-your-polymarket-vps-for-trading: "WebSocket 90ms = Your Network Ping (0.5ms) + Polymarket Server Processing (~89.5ms)"). **Taker-order execution adds 250–300 ms under matcher load** per the same source (same URL: "Execution latency (order → fill): Maker orders = 25ms (off-chain CLOB), Taker orders = 250–300ms (matching engine under load)"). Polymarket removed a 500 ms taker-order artificial delay in **February 2026**, which changes any pre-2026 priors.

**The RTDS feed itself has ~100 ms inherent latency** (quantvps.com: "RTDS WebSocket (wss://ws-live-data.polymarket.com): Delivers low-latency cryptocurrency price feeds with a delay of about 100ms"). This is the age of your "fair" reference when you compute `Phi(d2)` — it is already a lagged view of the world, which compounds the latency-drift problem.

**Defensible Singapore + WireGuard prior** for your backtester (until you measure your own `events.jsonl`): `p50=220 ms, p95=380 ms, p99=700 ms, p99.9=2 s`. Justification: ~175 ms baseline SG↔London fiber (CloudPing / AWS inter-region published data), +90 ms server, +10–50 ms WireGuard per-packet overhead and jitter, +tail for retransmits and tunnel renegotiation. Treat these as **upper bounds on a confident prior** and replace with measured empirical quantiles ASAP.

### 3.2 Correct latency injection technique

The canonical method — stamp at `t_signal`, sample `L`, walk the book at `t_signal + L` — is implicit in **Moallemi & Saglam 2013** ("OR Forum — The Cost of Latency in High-Frequency Trading", *Operations Research* 61(5):1070–1086, doi.org/10.1287/opre.2013.1165). Their dynamic-programming closed form gives **cost of latency per unit time** as a function of volatility, tick size, and order rate; applied to takers, the cost is **linear-to-sublinear in L** over the range relevant to your strategy. For your regime (binary 5-min BTC, σ of the implied probability ≈ 0.05–0.15 per minute), a back-of-envelope using their framework: a 200 ms latency versus a 20 ms latency contributes an additional ~(0.18 × √(0.18/60)) × √(180ms) ≈ ~25–40 bps of expected drift per fill, which is material next to your GBM edge.

**Aquilina, Budish & O'Neill 2022** ("Quantifying the High-Frequency Trading Arms Race", *QJE* 137(1)) estimates **~$5B/year in latency-arbitrage losses in global equities** as the empirical size of the taker-side latency tax and shows it is paid by slow takers to fast takers via adverse selection. You are a slow taker. The PnL sensitivity `dPnL/dL` for your strategy is best estimated from *your own* captured decisions replayed with perturbed L — see §6.4.

**Do not walk the book at t_signal.** If the current flat-0.5 paper backtester walks at `t_signal`, it omits the entire latency-drift attribution component and will systematically flatter live PnL by something in the 20–50 % range of gross edge for a strategy trading on sub-second signals from 180+ ms away.

### 3.3 Stress-testing tails without overfitting

Jane Street and Jump Trading engineering blogs (jane-street-tech-blog and jumptrading.com/careers blog posts on market-data infrastructure) describe two canonical patterns: **latency multiplier sweeps** (replay with L × {1.0, 2.0, 5.0, 10.0} and plot PnL vs multiplier) and **event injection** (insert synthetic reconnect gaps of 1–10 s at Poisson-distributed times). The correct guardrail against overfitting is to **fix the injection seed per experiment and sweep, not tune**; report PnL as a *distribution* across multipliers, not a point estimate.

For WireGuard-specific tails: log `wg show` handshake timestamps alongside every order in `events.jsonl`; the conditional distribution `P(L | time_since_last_handshake)` is bimodal (fresh tunnel fast, stale tunnel slow) and hiding it in a single marginal distribution throws away signal.

---

## 4. Market impact and queue

### 4.1 Book-walk for $5–$100 FAK — regime-dependent, not a rounding error

At $5 notional on a 0.50-priced binary token, you are buying ~10 shares. Typical BTC 5-min Polymarket BBO depth (from scrapes referenced in txbabaxyz/polyrec's captured data and Saguillo et al. 2025 arXiv:2508.03474's anatomy of Polymarket) is in the low hundreds to low thousands of shares at level 1 during active windows, dropping into the tens-to-hundreds in the first and last minute of a 5-min market. So $5 rarely walks; **$100 on a quiet book in the last 30 seconds of a market walks routinely**.

The Bouchaud **square-root law** `I(Q) ∝ σ · √(Q/V)` (see bouchaud.substack.com/p/the-square-root-law-of-market-impact and arxiv.org/pdf/2205.07385 Said 2022 review; original in Lillo-Farmer-Mantegna 2003, Bucci-Benzaquen-Lillo-Bouchaud 2019) calibrates at participation rates 0.5 %–20 %; below 0.5 % it **breaks down to a more linear regime** dominated by half-spread (Talos 2024, talos.com/insights). Your regime is well below 0.5 % on active markets but can spike above 10 % in the final 30 s of a thin market.

**Verdict: regime-dependent.** On well-populated mid-life books, book-walk is a rounding error and flat-0.5 is a reasonable first approximation. On thin books (first 60 s, last 30 s, price near 0 or 1), second-and-below level walking is a **dominant cost** and flat-0.5 is systematic fiction. The ReplayExecutor must **log book depth at decision time** so every trade can be regime-classified post hoc.

### 4.2 Replenishment assumption between FAK and next snapshot

Two bounds:

- **Hold book depleted**: assume nothing replenishes. This is **conservative for a single trade** (overstates cost) and **anti-conservative for a sequence** (understates the strategy's ability to re-enter because adverse depth stays gone).
- **Snap to next snapshot**: assume the book instantly returns to whatever you next observe. This **understates cost** because you get the benefit of replenishment instantaneously, without having waited for it.

Cont et al. 2011 (*Operations Research* 58:549) document "remarkable resiliency" of BBO on NYSE with replenishment half-lives of seconds for liquid stocks. On Polymarket, with fewer market makers and shallower books, replenishment is materially slower — minutes not seconds in quiet periods (implied by Saguillo et al. 2025's observation that mispricings persist for hours).

**Recommendation:** freeze-depleted is the right default for single-trade cost attribution; additionally run a "snap-back" variant as the optimistic bound. **True PnL lies between the two bounds, and reporting both is the honest answer.**

### 4.3 Queue position for pure takers

None. Moallemi & Yuan's queue-valuation framework (Moallemi-Yuan 2017 "A Model for Queue Position Valuation", Columbia working paper) is explicitly a maker-side construct. For a FAK taker, fill quantity and price are entirely a function of what is resting when your order hits the matcher — there is no queue-position-dependent probability of fill. Do not model it. Do log `top_of_book_size_at_decision` so you can measure how often your FAK exhausted level 1.

---

## 5. Paper-to-live gap: magnitude, shape, predictors

### 5.1 Published haircut envelope

The numeric envelope, from peer-reviewed sources:

- **Harvey & Liu 2015** ("Backtesting", *JPM* 42(1):13–28, SSRN 2345489): Sharpe haircut **~50 %+ when reported SR < 0.4**, **≤25 % when reported SR > 1.0**. Non-linear in reported SR and number of trials.
- **Wiecki, Campbell, Lent & Stauth 2016** ("All That Glitters Is Not Gold", *J. Investing* 25(3):69–80, SSRN 2745220): across 888 Quantopian algos with ≥6 months of out-of-sample data, **in-sample Sharpe has R² < 0.025 with out-of-sample Sharpe**. In-sample Sharpe is essentially predictively useless.
- **Bailey & López de Prado 2014** ("The Deflated Sharpe Ratio", *JPM* 40(5):94–107, SSRN 2460551): formal correction for selection bias, non-normality, sample length, multiple testing.
- **Frazzini, Israel & Moskowitz 2018** ("Trading Costs", SSRN 3229719) on $1.7 T live AQR executions: market impact ~22.94 bps for short-term reversal. Short-horizon reversal is the most-impacted category; your 5-min BTC Up/Down taking is strategically analogous.
- **Kaiko Research 2024–25**: crypto slippage for $100k market orders routinely **3–50 bps in calm regimes, 300–500+ bps in stress** (research.kaiko.com/insights/how-is-crypto-liquidity-fragmentation-impacting-markets; research.kaiko.com/insights/cryptos-pricing-problem-laid-bare on the Oct 2025 flash event).
- **Saguillo, Ghafouri, Kiffer & Suarez-Tangil 2025** ("Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets", arXiv:2508.03474): >$40 M arbitrage extracted on Polymarket April 2024 – April 2025; mispricings persist for hours — i.e., Polymarket books are *not* frictionless, and replay backtests that assume frictionless instant fills are materially wrong.

Your suspicion of **20–60 % ROI evaporation sits inside this envelope**, toward the middle of Harvey-Liu's range for a reported Sharpe around 0.4–1.0.

### 5.2 Decomposition — the five components

Attribution every trade into (Perold 1988 *JPM* + Almgren-Chriss 2000 *J. Risk* + Kissell 2006):

1. **Half-spread cost** `(fill − mid_at_decision)·sign`: pure taker cost at best price.
2. **Book-walk cost** `(VWAP_fill − best_opposite_at_decision)·sign`: the cost of consuming levels below best.
3. **Latency drift** `(mid_at_ack − mid_at_decision)·sign`: how the mid moved while your signed order was in flight.
4. **Adverse selection** over `[t_fill, t_fill+Δ]` for Δ ∈ {1 s, 5 s, 30 s}: how the mid continues to move against you post-fill — this is the Glosten-Harris 1988 / Huang-Stoll 1997 decomposition, critical because a strategy picking off informed flow has fundamentally different economics from one being picked off.
5. **Fees**: Polymarket bell-curve formula by category, maximal at p=0.50.

Plus for FAK specifically: **opportunity cost on unfilled residual** per Perold 1988, `(mid_at_close − mid_at_decision)·sign·residual_qty` — not shown as a fill cost but must appear in the attribution because "fill what I can, cancel rest" is an implicit *choice* to leave PnL on the table.

### 5.3 Predictors of paper-live divergence

From the literature plus the Polymarket-specific features:

- **Book thinness at decision time**: `top_of_book_size / requested_size` < 2 is a hard warning flag (Talos 2024 on participation-rate breakdowns of square-root law).
- **Price proximity to 0 or 1**: tick-size changes to 0.001 or 0.0001 change the walk math, and the CLOB 0.01–0.99 API validator (py-clob-client #218) creates asymmetric order rejection near extremes.
- **Volatility regime**: Kaiko 2024 documents 100× slippage spikes in minutes during macro events.
- **Time-in-market**: first 60 s and last 30 s of a 5-min market have thinner books, wider spreads, and higher resolution-uncertainty (Saguillo et al. 2025).
- **WireGuard tunnel age**: your specific infra wrinkle; measure it.

### 5.4 Reporting framework

Not a single aggregate number. Ship a per-trade dataframe with the attribution in §5.2 as columns, then pivot by regime (§5.3 predictors) and report **conditional haircuts**. "Paper ROI 12 %, live ROI 5 %, 7 pp decomposed as: 2.1 pp half-spread, 1.3 pp book-walk, 2.8 pp latency drift, 0.5 pp adverse selection, 0.3 pp fees" is an honest answer. "Your strategy loses 58 %" is not.

---

## 6. Validation

### 6.1 How practitioners convince themselves a book-walked backtester is not silently wrong

Four layers, in ascending rigour. **Property-based unit tests on the matching engine** with Hypothesis or similar, checking invariants in §6.2 hold over randomly generated books. **Deterministic replay** with fixed `random_seed` reproducing identical results (nautilus_trader contract: nautilustrader.io/docs/latest/concepts/backtesting/). **Golden-trace reconciliation** against a window with captured live fills (§6.3). **Meta-backtest** against the existing Polymarket OSS simulators (agent-next/polymarket-paper-trader, evan-kolberg/prediction-market-backtesting) as independent second opinions.

### 6.2 Invariant tests

For a simulated taking-BUY FAK:
- Every filled_price_k ≥ best_ask_at_decision, monotonically non-decreasing across consumed levels.
- Σ filled_qty_k = min(requested_qty, Σ resting_qty at consumed levels up to worst-price limit).
- Reported VWAP = Σ(p·q)/Σq to the full precision Polymarket quantises at (`ROUNDING_CONFIG`).
- Fee = `taker_coefficient × p × (1−p) × shares`, rounded per Polymarket category rules, never negative.
- `filled_qty + residual_qty = requested_qty` (with overfill tolerance ε from nautilus_trader #3221).
- No fill whose price violates the `min 0.01 – max 0.99` API bound for 0.01-tick markets (and its 0.001/0.0001 analogues).
- `book_staleness_ms = t_fill − t_last_snapshot` is monotonic within a replay session.

Symmetric invariants for SELL.

### 6.3 Golden-trace validation

QuantConnect's Live Reconciliation methodology (quantconnect.com/docs/v2/writing-algorithms/live-trading/reconciliation and meta-analysis docs) is the canonical framework: run an OOS backtest in parallel with live and score deviation via **returns correlation** plus **Dynamic Time Warping distance** between live and OOS equity curves. For this codebase the practical form:

Pick a window with both captured L2 snapshots and real live fills from `events.jsonl`. For each live fill, run the replay through the same signal code path and record per-trade `{t_decision, live_fill_px, paper_fill_px, diff_bps, live_filled_qty, paper_filled_qty, book_top_at_decision, book_staleness_ms, paper_latency_sample, live_latency_measured, attribution_delta}`. A backtester that is not silently wrong produces |median diff_bps| < 2 and p95 diff_bps < 10 on trades where `book_staleness_ms < 200` and latency sampling is set to the measured live distribution. Anything worse means the replay is lying and the attribution columns tell you where.

### 6.4 Walk-forward and N-per-fold

Your observation of session ROI swinging ±137 % round-to-round on identical code is a clear signal that **single-split evaluation is statistical noise**, matching exactly Wiecki et al. 2016's R² < 0.025 finding.

**Lo 2002** ("The Statistics of Sharpe Ratios", *Financial Analysts Journal* 58(4):36–52) gives the IID standard error: **σ(SR̂) ≈ √((1 + 0.5·SR²)/N)**. For SR=1.5 at N=100 trades/fold: σ(SR̂)=√(2.125/100)=0.146, 95 % CI [1.21, 1.79]. For 95 % separation between SR=1.5 and SR=1.0 you need roughly **N ≥ 95** trades/fold; for separation from zero, N ≥ 4 is sufficient *if the true SR is really 1.5*.

Bailey & López de Prado 2014 Deflated Sharpe: **PSR(SR₀) = Φ((SR̂ − SR₀)·√(T−1) / √(1 − γ₃·SR̂ + ¼(γ₄−1)·SR̂²))** and the **Minimum Track Record Length MinTRL ≈ 1 + (1 − γ₃·SR + ¼(γ₄−1)·SR²)·(Z_α/(SR−SR*))²**. For crypto/prediction-market returns with typical γ₃≈−1, γ₄≈6, SR̂=1.5, SR*=1.0, α=0.05: **MinTRL ≈ 1 + (1 + 1.5 + 1.25·1.5²)·(1.645/0.5)² ≈ 1 + 5.31·10.82 ≈ 60 trades** for 95 % confidence of beating a nuisance Sharpe of 1.0. For beating zero: lower. **Use CPCV** (López de Prado *Advances in Financial Machine Learning* 2018 Ch. 12): N=6, k=2 → 15 train-test combinations and 5 backtest paths, producing an *empirical distribution* of SR instead of a single point. Worth it exactly when, as here, single-split variance dominates signal.

---

## 7. Metrics to emit per trade

The minimal schema that lets one report answer "is paper lying, where, how much, in which regimes":

**Identifiers and timestamps (ns):** `trade_id`, `parent_order_id`, `strategy_id`, `backtest_run_id`, `t_signal`, `t_send`, `t_ack_simulated`, `t_first_fill`, `t_last_fill`.

**Context at decision:** `token_id`, `market_id`, `side`, `tick_size`, `decision_mid`, `best_bid`, `best_ask`, `spread`, `top_of_book_size_bid`, `top_of_book_size_ask`, `cumulative_depth_5bps`, `cumulative_depth_20bps`, `book_staleness_ms`, `market_age_s`, `market_remaining_s`.

**Request and fill:** `requested_qty_shares`, `requested_notional_usdc`, `worst_price_limit`, `filled_qty`, `residual_qty`, `fill_VWAP`, `levels_consumed`, `classification ∈ {full, partial, unfilled}`.

**Latency:** `sampled_latency_ms`, `latency_source ∈ {prior, measured}`, `p_bucket_used`.

**Slippage attribution (bps):** `half_spread_cost`, `book_walk_cost`, `latency_drift_cost`, `adverse_selection_1s`, `adverse_selection_5s`, `adverse_selection_30s`, `fees_cost`, `opportunity_cost_unfilled`, **`total_IS = sum of above`**.

**PnL:** `edge_at_signal` (`sign·(fair_phi_d2 − decision_mid)`), `edge_at_fill` (`sign·(fair_phi_d2_at_fill − fill_VWAP)`), `realised_pnl_at_close`, `paper_pnl_flat_0_5`, `diff_paper_minus_realised`.

**Regime tags:** `thin_book_flag` (top_size < 2× requested), `price_extreme_flag` (p < 0.05 or p > 0.95), `vol_regime` ({low, mid, high} by rolling 1-min σ), `tunnel_age_bucket`, `time_in_market_bucket`.

One parquet file per session keyed on `trade_id` answers every question in §5 by groupby+agg, with **no further instrumentation required**.

---

## 8. Prioritised implementation outline

```
active_bots/execution/
  replay_executor.py        # core book-walked FAK simulator
  book.py                   # immutable L2 snapshot container + freeze/snap variants
  latency.py                # empirical + prior latency samplers, multiplier sweeps
  fees.py                   # Polymarket fee curves (intl + US), rounding
  invariants.py             # property-based assertions from §6.2

experiments/backtest/
  harness.py                # driver: load scrapes + signals, run, emit parquet
  reconcile.py              # golden-trace diff against live events.jsonl
  attribution.py            # per-trade decomposition → regime-conditional tables
  cpcv.py                   # Combinatorial Purged CV from LdP 2018 Ch. 12
  report.py                 # single-page answer: paper vs live by regime
```

**Priority order, measured in days-to-useful-signal:**

1. **Day 1–2 · Replace flat-0.5 with book-walked freeze-last-book** using existing scrape snapshots. Use the tick-size-aware `ROUNDING_CONFIG`. Emit the §7 schema. **This alone will collapse 30–50 % of the suspicious paper ROI.**
2. **Day 3 · Layer the Polymarket fee bell curve on top** (`bps/10000 × p × (1−p) × shares × Θ_category`). Another 50–180 bps per trade on mid-price trades.
3. **Day 4 · Latency injection at t_signal + L** with the §3.1 prior (p50=220 ms, p95=380 ms, p99=700 ms). Stamp `t_ack_simulated`, re-read book at that time. This exposes latency-drift attribution.
4. **Day 5 · Wire scrape.py to persist WebSocket deltas** (`book`, `price_change`) alongside periodic snapshots so ReplayExecutor has <1s-resolution evidence. Without this §1.2 says you are backtesting fiction.
5. **Day 6–7 · Measure real latency from events.jsonl** (p50/p95/p99 of signal→ack and signal→fill) and replace the prior. Add stress sweeps at L × {1, 2, 5, 10}.
6. **Day 8 · Golden-trace reconciliation** against a week of captured live fills. Target |median diff_bps| < 2, p95 < 10.
7. **Day 9–10 · CPCV with N=6, k=2** and deflated Sharpe reporting. Kill the ±137 % single-split noise.

### 8.1 Three places you will silently fool yourself

1. **Hardcoding tick size 0.01** — you will place TP/SL and FAK orders near 0 and 1 where Polymarket runs 0.001/0.0001 ticks and where the REST API rejects 0.001–0.009 and 0.991–0.999 prices even though the frontend accepts them (py-clob-client #218, #121). Replay will happily "fill" at prices that live would reject. **Mitigation:** persist tick size in every snapshot record, and in replay reject or floor any order price that violates the live validator. Add an invariant test.
2. **Snapshot-cadence fiction** — scraping every 1 s (or worse) and replaying at that cadence while claiming sub-second signal horizons is the single biggest available self-deception. Your decisions see a 1-s-stale book, walk a 1-s-stale book, and look much better than they would live where the book changes many times per second (Cont 2011 event rates). **Mitigation:** switch `scrape.py` to delta-stream ingestion (§1.2, day 4 above) and add a hard-fail assertion if `book_staleness_ms > 500`.
3. **Freezing the latency distribution after one measurement** — Singapore + WireGuard has a bimodal L distribution conditional on tunnel state (fresh vs stale handshake), and the matcher p99 moves 2–5× during macro events (Kaiko Oct 2025). A single-distribution sampler understates tail risk exactly when it matters. **Mitigation:** sample L from a measured distribution **conditional on** `tunnel_age_bucket` and `vol_regime` at decision time, and always run the multiplier sweep.

### 8.2 Polymarket-specific quirks that change generic equities/crypto answers

- **Fee bell curve centred at 0.50** means strategies most active around mid-probability pay maximum fees; flat-bps models that work for equities are wrong here.
- **CTF ERC-1155 outcome tokens** settle at exactly 0 or 1 on UMA-oracle resolution, not on a continuous market close. This changes the adverse-selection horizon: after resolution lock, post-fill mid drift is zero because the outcome is known. Build `t_resolution_lock` into the schema.
- **Negative-risk markets** (github.com/Polymarket/neg-risk-ctf-adapter) have different matching semantics for the composite; 5-min BTC Up/Down as a two-outcome market is standard binary and not neg-risk, but if you ever extend to Bitcoin-price-range markets with ≥3 outcomes, revisit.
- **Dust threshold:** minimum fee 0.00001 USDC rounds to zero for very small trades near price extremes (help.polymarket.com/en/articles/13364471) — your $5 orders near p=0.02 or p=0.98 may effectively be fee-free, which matters for the TP/SL legs specifically.
- **Relayer-sponsored gas**: zero gas in your fee model, unlike any on-chain-order venue. Do not model gas.
- **Tick-size asymmetry within a market** (nautilus_trader #2980): the YES side can be 0.001-tick while the paired NO side is 0.01-tick at the same instant. Store tick size per side, not per market.
- **February 2026 removal of the 500 ms taker delay** — any pre-Feb-2026 scraped data has different effective latency than post; partition your replay windows by this boundary and do not pool.

The backtester you build on this spec will not tell you paper is lying by a single number. It will tell you — per trade, per regime, per latency bucket — exactly where and how much, and that is the only form of answer that survives contact with live.