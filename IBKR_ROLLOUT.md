# IBKR / ForecastEx Rollout Plan

> **Status**: Planning — Phase FX-A not started (drafted 2026-07-09).
> **Relationship to scope.md**: independent parallel track (phases numbered
> FX-A…FX-D to avoid collision with the main Phase 0–8 roadmap).  Runs
> alongside the sports paper-collection run; both feed one combined
> economics gate.
> **No live capital on this track before FX-D's own gate + the compliance
> checklist below.**

---

## Why this track exists

The sports arb (Kalshi↔Polymarket) carries a ~3.3¢/contract fee floor at
p=0.5.  The ForecastEx pairs are structurally different:

- **ForecastEx has no commission** — cost is embedded in a ~$0.01
  round-trip spread (verify exact mechanics in FX-A).
- **Polymarket's non-sports markets charge zero fee** → a Poly↔FEX econ
  pair has a near-zero fee floor.
- **ForecastEx pays an incentive coupon** (~3-month-rate APY, accrued on
  committed capital).  Econ contracts resolve on scheduled prints
  (days–weeks out), so capital that sits dead on Kalshi/Poly *earns* on
  FEX.  Because resolution dates are known, the coupon credit is exactly
  computable and belongs in the net-edge math.
- Macro markets dislocate around scheduled releases (FOMC, CPI) with
  unambiguous resolution sources.

**ForecastEx lists no sports.** This is a new market category (econ /
climate / government), not an extension of the sports thesis.

## The May 2026 IBKR development (changes the landscape)

On 2026-05-14 IBKR launched a unified Prediction Markets platform
aggregating **Kalshi, CME Group event contracts, and ForecastEx** in one
account, including a **smart order router** that sends each order to the
venue with the best net price including fees.

Implications, in order of importance:

1. **The SOR compresses intra-bundle spreads.**  Every IBKR customer's
   flow now drifts toward the cheapest of Kalshi/CME/FEX.  The naive
   cross-venue edge *inside the bundle* is being productized away —
   expect Kalshi↔FEX dislocations to decay.
2. **Polymarket is NOT in the bundle.**  Poly↔X spreads are untouched by
   broker aggregation.  Combined with the fee structure, **Poly↔FEX is
   the primary target pair**, for two independent reasons.
3. **Kalshi via IBKR exists but is not our path.**  Reported cost is
   ~$0.02–0.07/contract through IBKR (third-party figure — verify against
   [IBKR's pricing page](https://www.interactivebrokers.com/en/pricing/commissions-events.php)),
   and third-party reviews report end-to-end API automation only for
   ForecastEx.  Direct Kalshi API keeps native fills, `client_order_id`
   reconciliation, full book depth, and lower cost.
4. **CME event contracts** (~$0.01/contract) become a possible *fourth*
   venue later; the pairwise venue architecture below extends to it.
5. IBKR's UI groups "similar contracts" across venues — inspect those
   groupings in FX-A as a free source of candidate pair mappings (their
   grouping implies a resolution-equivalence judgment; we still verify
   strike boundaries independently before trusting any pair).

Sources: [IBKR press release](https://www.interactivebrokers.com/en/general/about/mediaRelations/5-14-26.php),
[Finance Magnates](https://www.financemagnates.com/forex/interactive-brokers-bundles-kalshi-cme-forecastex-in-unified-event-trading-push/),
[Market Math ForecastEx review](https://marketmath.io/platforms/forecastex),
[IBKR Prediction Markets](https://www.interactivebrokers.com/predictionmarkets/en/home.php).

## Locked decisions (2026-07-09)

| Decision | Choice | Rationale |
|---|---|---|
| IB access path | **IB Gateway + ib_async** | Battle-tested for automation; Client Portal sessions are fragile headless. Pin the ib_async version (community-maintained fork). |
| First market category | **Fed / rates decisions** | Unambiguous resolution (FOMC target range), liquid around meetings, no rounding traps. Episodic (8 meetings/yr) is acceptable for a pilot. |
| Architecture | **Pairwise venue config** | Each event names its two venues; the tested two-leg engine survives; extends to CME later without another redesign. |
| Sequencing | **Parallel track** | Sports collection proceeds unchanged; shared observation infra; one combined economics gate. |
| Venue roles | **Direct Kalshi API stays for data + execution; IBKR account is the FEX (and later CME) gateway** | Latency, native order semantics, cost. Revisit only if FX-B shows rebalancing friction dominates. |
| Pair priority | **1) Poly↔FEX  2) Kalshi↔FEX  3) CME pairs (deferred)** | SOR compression hits intra-bundle pairs; Poly pairs are outside it and have the lowest fee floor. |

---

## Phase FX-A — Feasibility spike (~2 days, no code)

- [ ] **Compliance pre-clearance** (do first, it has the longest lead
      time): IBKR is unambiguously a personal brokerage account —
      personal-account-dealing rules at an SEC-registered adviser almost
      certainly apply.  Also confirm platform eligibility/ToS.
- [ ] IBKR account: forecast-trading permission, API entitlement,
      **paper-trading account** for development; confirm paper accounts
      stream forecast-contract market data.
- [ ] **API verification on paper account** (ib_async): contract
      discovery/qualification for ForecastEx contracts, top-of-book
      streaming, tick size, supported TIFs (IOC? FOK? — this determines
      leg ordering in FX-C), order placement + cancel round-trip.
- [ ] Confirm the fee model ($0 commission; spread mechanics) and the
      **incentive coupon**: exact rate, what balance accrues, payment
      cadence.
- [ ] Confirm whether Kalshi-routed orders are API-accessible via IBKR
      at all, and their true cost (informational — not our path).
- [ ] **Product census**: ForecastEx Fed/rates contracts vs Kalshi Fed
      series vs Polymarket FOMC markets.  For each candidate pair,
      document strike convention (inclusive/exclusive boundary, range vs
      upper-bound wording), resolution source, and settlement time.
      Inspect IBKR's cross-venue contract groupings for candidates.
- [ ] Manual spread/depth sampling on live Fed contracts, a few times a
      day for several days (all three venues).
- [ ] Note ForecastEx trading hours vs Kalshi hours vs Poly (24/7) for
      the hours-gating design.

**Deliverable**: `forecastex_spike.md` — pair census with verified strike
conventions, measured spreads/depth, API findings, coupon mechanics.
**Gate**: visible cross-venue dislocations ≥ a few cents at sampled
moments, and end-to-end API automation confirmed on paper.  If the spike
shows nothing, stop here — total cost was two days.

## Phase FX-B — Observation only (~3 days build; run through next FOMC)

Standalone observer process (`fex_observer.py`) — deliberately **not**
integrated into the running sports bot:

- Imports the existing `KalshiWS`, `ArbEngine`, `ObservationLogger`.
- Adds an ib_async top-of-book feed normalized into `MarketSnapshot`
  (float→Decimal at the boundary).
- Writes the standard observation format **plus a `venue_pair` column**
  (`kalshi-poly` / `kalshi-fex` / `poly-fex`).
- **Known shim** (acceptable for observation, replaced in FX-C):
  `ArbEngine` hardcodes Kalshi+Poly fee functions, so the FEX leg rides
  in the "poly" slot with `POLY_FEE_RATE=0` (correct — FEX is fee-free).
- **Hours gating from day one**: every observation row carries
  `both_venues_open`; a quote moving while the other venue is closed is
  not an opportunity and must not be counted as one.
- **Strike-boundary sign-off**: no pair enters the observer config
  without its documented boundary/rounding equivalence from FX-A.
  (Econ version of the YES-alignment trap: "above 3.0%" vs "3.0% or
  above" resolve opposite ways on an exact print.)

Ops: dockerized IB Gateway (IBC-based images) joins compose next to the
observer.  Design for: the gateway's nightly reset window, headless
2FA/re-auth, and treating FEX feed downtime as *venue closed* — never as
tradeable staleness.

**Gate**: ≥2 weeks of observations spanning one FOMC meeting (next is
expected late July 2026 — confirm the official calendar).  Analysis must
show net-positive dislocations at fillable depth, after fees and
including coupon credit, on `both_venues_open` rows only.

## Phase FX-C — Pairwise execution integration (~1–2 weeks, gated on FX-B)

- [ ] `events.json` v2: entries name their two venues + venue-specific
      IDs; existing sports entries default to kalshi+polymarket
      unchanged.
- [ ] Small `VenueAdapter` interface (snapshot stream, `place_order(side,
      price, qty, tif)`, cancel, positions, balance, `fees(price, qty)`,
      `is_open(now)`) wrapping the three client stacks.  Two-leg
      `UnwindManager` engine unchanged.
- [ ] **Leg-order policy table** keyed by venue pair (principle
      unchanged: all-or-nothing / less-reliable venue first; FX-A's TIF
      findings decide the FEX position).
- [ ] **Carry-aware economics**: coupon credit = days-to-resolution ×
      rate × committed capital, entered as a negative fee on the FEX leg.
- [ ] Hours gating in the execution path (not just observations).
- [ ] Extend: parity guard (`ALLOWED_LIVE_REFS`), failure-injection tests
      with a mock IB client, startup reconciliation via ib_async
      `positions()`, balance monitor for the IBKR account, per-venue kill
      switch.  Risk caps remain **global across venues** (the reason this
      is one bot, not a second instance).

## Phase FX-D — Live pilot

One verified Fed pair family, minimum size, through a full FOMC cycle
(September 2026 realistically if July is the observation cycle).  Same
graduation discipline as the sports side: clean observation record →
tiny live size → fills-vs-paper review → size decision.

---

## Risk register

| Risk | Mitigation |
|---|---|
| **Strike-boundary mismatch** (worst risk — resolves opposite ways on an exact print) | Per-pair written sign-off in FX-A; price-coherence monitoring; start with FOMC target-range contracts where wording is cleanest |
| **SOR spread compression** decays the intra-bundle edge before we measure it | Prioritize Poly↔FEX (outside the bundle); start observation promptly; treat FX-B data as perishable |
| IB Gateway operational fragility (nightly reset, 2FA, session drops) | IBC-managed dockerized gateway; feed downtime = venue closed; observer is stateless and restart-safe |
| Thin FEX books / wide spreads | FX-A manual sampling before any build; depth-aware sizing already exists in the engine |
| ib_async maintenance risk (community fork) | Pin exact version; adapter isolates it |
| Hours mismatch phantom arbs | `both_venues_open` gating in observation AND execution |
| Capital fragmentation across three venues | IBKR wallet consolidates FEX (and later CME); balance monitor extended; Poly remains separately funded |
| Fee/coupon assumptions wrong (third-party sourced) | FX-A verifies against IBKR's own pricing page and account statements before any economics conclusion |

## Combined economics gate

FX-B observations land in the same format as the sports run.  The
go/no-go analysis (main roadmap Phase 7) evaluates both tracks together:
sports may fail its ~3.3¢ fee floor while Poly↔FEX clears a near-zero
one — or the SOR may have already flattened everything.  Either answer
is cheap to obtain and decides where Phase 8 (discovery/automation)
effort goes.
