# stock-tracker

Systematisch dip-koop-systeem voor large caps: signalen, backtests en een dashboard.
Geen order-execution — dit systeem plaatst geen echte orders, het genereert alleen
signalen en simuleert paper trades.

## Status

Module 1 en 2 zijn klaar. Andere modules volgen in aparte stappen.

- [x] Module 1 — Datalaag (tickers, prijzen, fundamentals, ingest)
- [x] Module 2 — Kwaliteitsfilter
- [ ] Module 3 — Signaal-engine
- [ ] Module 4 — Backtester
- [ ] Module 5 — Dashboard
- [ ] Module 6 — Dagelijkse run en alerts
- [ ] Module 7 — Paper trading

## Installatie

Vereisten: Python 3.12, [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --dev
cp .env.example .env
# vul FMP_API_KEY in .env in
```

## Configuratie

Alle drempels, percentages en universum-filters staan in `config.yaml`. Nooit
hardcoded in code — pas de strategie aan door dit bestand te wijzigen.

API-keys (Financial Modeling Prep, later Telegram) staan in `.env`, niet in
`config.yaml`. Kopieer `.env.example` naar `.env` en vul ze in.

## Database

SQLite op `data/tracker.db`, schema gedefinieerd via SQLAlchemy in
`src/stock_tracker/db/models.py`. De database wordt aangemaakt bij de eerste
`ingest`-run (`init_db()` maakt alle tabellen aan als ze nog niet bestaan).

## Dagelijkse run

```bash
# eerste keer: volledige historie (12 jaar) ophalen voor het hele universum
uv run python -m stock_tracker.ingest --full

# daarna: dagelijks incrementeel bijwerken
uv run python -m stock_tracker.ingest --daily
```

`--daily` haalt voor elke ticker alleen de dagen op na de laatste bekende
koersdatum in de database (geen dubbele of overlappende data).

```bash
# kwaliteitsfilter herberekenen (bv. elke maandag, zie config.yaml: quality.recompute_weekday)
uv run python -m stock_tracker.quality

# eligible-lijst met terugwerkende kracht opbouwen na `ingest.py --full`,
# zodat de backtest (Module 4) point-in-time eligibility heeft
uv run python -m stock_tracker.quality --backfill-start 2012-01-01 --backfill-end 2026-01-01
```

## Modules

### Module 1 — Datalaag

- **`src/stock_tracker/db/models.py`** — SQLAlchemy-modellen voor alle tabellen:
  `tickers`, `fx_rates`, `prices_daily`, `fundamentals`, `analyst_estimates`,
  `earnings_calendar`, `signals`, `trades_paper`, `journal`. De laatste drie
  worden pas gevuld vanaf Module 3/7, maar staan nu al in het schema.
- **`src/stock_tracker/db/session.py`** — engine/sessionfactory en `init_db()`.
- **`src/stock_tracker/providers/`** — `DataProvider`-interface (`base.py`) met
  twee implementaties: `yfinance_provider.py` voor OHLCV-koersen en
  `fmp_provider.py` voor fundamentals, analistenverwachtingen en
  earnings-datums (Financial Modeling Prep). Nieuwe bronnen toevoegen betekent
  alleen een nieuwe klasse die `DataProvider` implementeert — de rest van de
  code roept alleen de interface aan.
- **`src/stock_tracker/universe.py`** — bouwt de tickerlijst (S&P 500 +
  Euronext 100 + AEX + BEL 20) met exchange, valuta en sector, en zet die weg
  in de `tickers`-tabel.
- **`src/stock_tracker/fx.py`** — haalt dagelijkse wisselkoersen op en
  rekent koersen om naar EUR (`close_eur` in `prices_daily`).
- **`src/stock_tracker/ingest.py`** — CLI-entrypoint: `--full` haalt 12 jaar
  historie op, `--daily` werkt incrementeel bij op basis van de laatst
  bekende datum per ticker.

### Module 2 — Kwaliteitsfilter

- **`src/stock_tracker/quality.py`** — berekent per ticker een score op zes
  harde criteria (marktkap, positieve FCF laatste 4 kwartalen, omzetgroei TTM,
  nettoschuld/EBITDA, stijgende consensus-EPS t.o.v. 90 dagen geleden,
  gemiddeld dagvolume) en slaat het resultaat op in `quality_scores`. Een
  ticker is `eligible` alleen als alle zes criteria slagen.
- Alles is point-in-time: fundamentals worden gefilterd op `report_date <=
  as_of` (de dag waarop het cijfer daadwerkelijk gepubliceerd werd, niet de
  boekhoudperiode die het beslaat), analistenverwachtingen op `as_of_date <=
  as_of`, koersen op `date <= as_of`. Er wordt dus nooit informatie gebruikt
  die op dat moment nog niet bekend was — noodzakelijk voor een eerlijke
  backtest.
- `run_for_date(as_of)` berekent en bewaart scores voor het hele universum op
  één datum; `get_eligible_tickers(as_of)` geeft de eligible-lijst terug op
  basis van elke tickers meest recente snapshot op of vóór die datum (want de
  score wordt wekelijks herberekend, maar signalen worden dagelijks gebruikt).
- `backfill(start, end)` herberekent voor elke `quality.recompute_weekday`
  (default maandag) in een periode — nodig om na `ingest.py --full` de
  historie te vullen zodat de walk-forward backtest (Module 4) niet met
  look-ahead werkt.

### Volgende modules

Worden hier aangevuld zodra ze gebouwd zijn.

## Tests

```bash
uv run pytest
uv run ruff check .
```
