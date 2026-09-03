# stock-tracker

Systematisch dip-koop-systeem voor large caps: signalen, backtests en een dashboard.
Geen order-execution — dit systeem plaatst geen echte orders, het genereert alleen
signalen en simuleert paper trades.

## Status

Alle 7 modules zijn klaar.

- [x] Module 1 — Datalaag (tickers, prijzen, fundamentals, ingest)
- [x] Module 2 — Kwaliteitsfilter
- [x] Module 3 — Signaal-engine
- [x] Module 4 — Backtester
- [x] Module 5 — Dashboard
- [x] Module 6 — Dagelijkse run en alerts
- [x] Module 7 — Paper trading

## Installatie

Vereisten: Python 3.12, [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --dev
cp .env.example .env
```

## Configuratie

Alle drempels, percentages en universum-filters staan in `config.yaml`. Nooit
hardcoded in code — pas de strategie aan door dit bestand te wijzigen.

`ingest.fundamentals_provider` in `config.yaml` kiest de bron voor
fundamentals/analistenschattingen/winstkalender:
- `"yfinance"` (standaard) — gratis, geen API-key nodig, dekt zowel VS- als
  Europese tickers. Gebruikt dezelfde bron als de koersdata. Kanttekening:
  `report_date` en `market_cap` zijn benaderingen (zie de docstring in
  `src/stock_tracker/providers/yfinance_fundamentals_provider.py`), en
  analistenschattingen kunnen een jaartje verschuiven bij bedrijven met een
  afwijkend boekjaar.
- `"fmp"` — nauwkeuriger en met een echte filing-datum, maar de gratis FMP-tier
  geeft `403 Forbidden` op de fundamentals-endpoints; hiervoor is een betaald
  FMP-abonnement nodig. Vereist `FMP_API_KEY` in `.env` (zie `.env.example`).

API-keys (Financial Modeling Prep, Telegram) staan in `.env`, niet in
`config.yaml`. Kopieer `.env.example` naar `.env` en vul ze in indien nodig.

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

# signalen genereren voor vandaag (entries + exits/review/fundamentele stops)
uv run python -m stock_tracker.signals

# walk-forward backtest draaien (in-sample 2012-2019, out-of-sample 2020-heden
# uit config.yaml) en het resultaat opslaan
uv run python -m stock_tracker.backtest

# eerdere runs met elkaar vergelijken
uv run python -m stock_tracker.backtest --list-runs

# paper trading: openstaande signalen omzetten in orders tegen de slotkoers
# van vandaag, en de 30-trade-milestone tonen zodra die gehaald is
uv run python -m stock_tracker.paper

# maandrapport (realised/unrealised, vs. backtest-verwachting, vs. benchmark)
uv run python -m stock_tracker.paper --monthly-report

# volledige dagelijkse pipeline: ingest -> quality (maandag) -> signals ->
# paper -> Telegram-alert (alleen als er iets te melden is)
uv run python -m stock_tracker.run_daily

# dashboard starten
uv run streamlit run app.py
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

### Module 3 — Signaal-engine

- **`src/stock_tracker/signals.py`** — genereert per dag entry- en
  exit-signalen en slaat ze op in `signals`.
- **Entry** (alleen voor tickers op de eligible-lijst van Module 2):
  `compute_indicators()` berekent SMA50, RSI(14) (eenvoudige, ongesmoothde
  variant) en de 52-weken-high, allemaal point-in-time (alleen koersen
  `<= as_of`). `entry_tranche()` kiest de diepste tranche waarvan de
  prijsdrempel gehaald wordt (tranche 3 > 2 > 1, want een daling van 25%
  impliceert ook een daling van 15% en 8%); tranche 1 vereist bovendien
  RSI < 35. Een aankomende earnings-datum binnen `earnings_guard_days`
  handelsdagen onderdrukt het entry-signaal (`within_earnings_guard()`,
  telt handelsdagen met `numpy.busday_count`).
- **Exit** (voor open posities in `trades_paper`, per tranche):
  winstdoel (`+10%` t.o.v. instapprijs) of slot boven SMA50 → `EXIT`;
  langer dan `time_stop_weeks` open zónder dat een van beide is gebeurd →
  `REVIEW`; ticker valt uit de eligible-lijst of de eerstvolgende-jaar
  EPS-consensus is > `eps_estimate_drop_pct` gedaald t.o.v. 90 dagen
  geleden (herbruikt `quality.latest_estimate`) → `EXIT_FUNDAMENTAL`
  (ticker-breed, niet per tranche — sluit alle open tranches van die
  ticker).
- `run_for_date(as_of)` genereert en bewaart beide soorten signalen voor
  één datum (upsert op ticker+datum+type+tranche, dus idempotent).

### Module 4 — Backtester

- **`src/stock_tracker/backtest.py`** — dag-voor-dag event-driven simulatie
  in pandas/pure Python, **niet** `vectorbt`: de regels van deze strategie
  (per-tranche sizing, per-ticker/per-sector exposure-caps, een cash-vloer,
  jaarlijkse belastingafrekening, point-in-time eligibility) zijn inherent
  sequentieel/stateful, geen vectoriseerbare signaalarray — vectorbt zou hier
  meer tegenwerken dan helpen, bovenop een zware numba-compile-dependency die
  deze sandbox niet nodig heeft. `pandas` wordt wel gebruikt waar het wél
  past: de equity curve omzetten naar CAGR/drawdown/Sharpe.
- **Geld in EUR, techniek in `close_eur`**: in tegenstelling tot Module 3
  (dat native `close` gebruikt, wat een trader op de eigen beurs ziet)
  rekent de backtest SMA50/RSI(14)/tranche-drempels op basis van `close_eur`
  (`indicators_eur()`, `entry_tranche_eur()`), zodat positiegrootte, kosten
  en belasting consistent in één munt lopen — inclusief het effect van
  wisselkoersbewegingen tussen instap en exit.
- **Portfolio-boekhouding** (`Portfolio`-klasse): elke tranche-instap wordt
  exact 2% van de actuele equity; een tweede instap in dezelfde tranche
  van dezelfde ticker wordt geweigerd zolang die tranche al open staat
  (anders zou een signaal dat meerdere dagen achter elkaar geldig blijft
  elke dag opnieuw kopen). Voor elke instap/exit: 0,1% slippage op de
  vulprijs, 0,35% Belgische TOB + configureerbare brokerfee op het
  transactiebedrag. Per kalenderjaar wordt het netto gerealiseerde
  resultaat bijgehouden; bij een jaarovergang wordt 10% meerwaardebelasting
  ingehouden op een positief jaarresultaat (nooit een teruggave bij
  verlies).
- **Exit-prioriteit per open tranche**: winstdoel/slot-boven-SMA50 eerst
  (`EXIT`), anders fundamentele stop (niet meer eligible, of EPS-verwachting
  > `eps_estimate_drop_pct` gedaald — ticker-breed, sluit alle tranches),
  anders een `REVIEW`-telling bij `time_stop_weeks` zonder herstel — dat
  laatste is puur informatief en sluit de positie niet automatisch (net als
  in Module 3: "flag", geen "EXIT").
- **Benchmark**: `^GSPC` (S&P 500-index) is via `universe.fetch_benchmark()`
  aan het universum toegevoegd zodat hij gewoon meeloopt in `ingest.py`
  (alleen koersen, geen fundamentals — verschijnt daardoor nooit op de
  eligible-lijst). `simulate_benchmark()` simuleert buy & hold met hetzelfde
  startkapitaal.
- **Metrics** (`compute_metrics()`, per periode en voor strategie/benchmark
  apart): CAGR, max drawdown, langste periode onder water (aaneengesloten
  dagen onder de lopende piek), Sharpe (configureerbare risicovrije voet),
  win rate, gemiddelde holding period, aantal trades, eindkapitaal.
- **Overfitting-waarschuwing** (`check_overfitting()`): waarschuwt expliciet
  als de out-of-sample CAGR relatief veel lager is dan in-sample
  (`overfitting_check.cagr_degradation_pct`), of als de out-of-sample Sharpe
  instort terwijl in-sample duidelijk positief was
  (`in_sample_sharpe_threshold` / `sharpe_floor`) — beide drempels in
  `config.yaml`, niet hardcoded.
- **Opslag** (`store_run()`/`list_runs()`): elke run komt met datum,
  config-hash (sha256 van de volledige config) en de vier metric-sets
  (in/out-of-sample × strategie/benchmark) in `backtest_runs` /
  `backtest_metrics`, zodat runs met verschillende configs te vergelijken
  zijn.
- `run()` lost `in_sample_start/end` en `out_of_sample_start/end` op uit
  `config.yaml` (of CLI-overrides); `out_of_sample_end: null` betekent "de
  laatst beschikbare koersdatum".

### Module 5 — Dashboard

- **`app.py`** (repo-root, per spec-conventie) — Streamlit-app met 5 tabs.
  Data-laadfuncties (`load_watchlist()`, `load_signals_for_date()`,
  `load_paper_portfolio()`, `load_recent_trades()`,
  `save_journal_entry()`/`load_journal_entries()`/`update_journal_outcome()`)
  bevatten bewust geen `st.*`-aanroepen, zodat ze los van Streamlit getest
  kunnen worden; alle UI zit in `main()`.
  - **Watchlist**: eligible tickers met afstand tot SMA50/52w-high, RSI,
    eerstvolgende earnings-datum en actief tranche-signaal, voor een zelf
    te kiezen datum.
  - **Signalen vandaag**: alle `signals`-rijen voor een datum (default: de
    meest recente met signalen), gesorteerd op type/tranche.
  - **Portfolio (paper)**: open `trades_paper`-posities met actuele koers,
    ongerealiseerd rendement, dagen open en de laatste `REVIEW`/
    `EXIT_FUNDAMENTAL`-flag voor die ticker.
  - **Backtest**: run-kiezer (uit `backtest_runs`), CAGR/max-drawdown/
    Sharpe/trades per periode, en een equity- en drawdown-grafiek van
    strategie vs. benchmark. Dit vereiste een schema-aanvulling: `store_run()`
    slaat nu ook de volledige equity curve op (`BacktestMetric.equity_curve_json`,
    uitgelezen via `backtest.load_equity_curve()`) — de eerdere versie
    bewaarde alleen de samenvattingscijfers, onvoldoende om een grafiek te
    tekenen.
  - **Journal**: formulier om een `trade_id` te kiezen en reden/verwachting
    vast te leggen; de signaalwaarden (instapprijs, SMA50, RSI14, afstand
    tot 52w-high) worden automatisch uit het bijbehorende `ENTRY`-signaal
    overgenomen in plaats van opnieuw te laten intypen. Bestaande entries
    zijn een tweede stap voor de uitkomst.
  - Smoke-getest in een echte headless Chromium-browser (Playwright) met
    geseede data — alle 5 tabs renderen zonder fouten. Dat testen vond een
    echte bug: een vrijwel perfect vlakke equity curve (near-zero variance)
    liet de Sharpe-ratio in `compute_metrics()` exploderen naar een absurd
    getal door een drijvendekomma-restje in de standaarddeviatie; nu
    afgekapt op een epsilon-drempel (regressietest toegevoegd).

### Module 6 — Dagelijkse run en alerts

- **`src/stock_tracker/run_daily.py`** — orkestreert
  `ingest.run_daily` → `quality.run_for_date` (alleen op
  `quality.recompute_weekday`) → `signals.generate_entry_signals` +
  `generate_exit_signals` → `paper.fill_pending_signals` → alert.
- **`src/stock_tracker/alerts.py`** — bouwt één berichttekst met nieuwe
  entries, exits (`EXIT`/`EXIT_FUNDAMENTAL`) en `REVIEW`-flags apart
  gegroepeerd; geeft `None` terug (dus geen bericht) als er niets te melden
  is. Verstuurt via de Telegram Bot API (`TELEGRAM_BOT_TOKEN`/
  `TELEGRAM_CHAT_ID` uit `.env`); zonder configuratie wordt het bericht
  alleen gelogd, niet verzonden (nuttig lokaal zonder bot).
- Bij het overschrijden van `paper_trading.review_after_closed_trades`
  (30 trades) in één run wordt er ook een milestone-bericht gestuurd
  (win rate/gemiddelde winst vs. de laatste backtest-run) — gedetecteerd
  door het aantal gesloten trades vóór en ná de paper-fill te vergelijken,
  dus precies één keer, niet elke dag opnieuw.
- **GitHub Actions — dagelijkse run** (`.github/workflows/daily.yml`): draait
  op `0 6 * * 1-5` UTC (≈ 07:00 CET / 08:00 CEST — cron kent geen DST, dus dit
  drift met een uur in de zomer). `data/tracker.db` overleeft runs doordat de
  workflow hem na elke run terugcommit naar de branch (`git add -f` ondanks
  de `.gitignore`-regel) — zo heeft Streamlit Community Cloud, dat automatisch
  herdeployt bij elke push, altijd de nieuwste data; daarnaast wordt de
  database ook als artifact geüpload (90 dagen bewaard). Secrets:
  `FMP_API_KEY` (optioneel, alleen nodig bij `fundamentals_provider: "fmp"`),
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- **GitHub Actions — volledige ingest** (`.github/workflows/full-ingest.yml`):
  `ingest.run_daily` (aangeroepen door de dagelijkse run hierboven) haalt
  bewust **geen** kwartaalcijfers op — die veranderen maar 4x per jaar, dus
  dat is het werk van `ingest.py --full`. Deze aparte workflow draait dat
  maandelijks (`0 6 1 * *` UTC) en is ook handmatig te starten via
  "Run workflow" — nodig na het opzetten van het project, of wanneer je wilt
  dat de fundamentals-tabel meteen gevuld wordt in plaats van te wachten op
  de eerstvolgende maandelijkse run.
- **Lokaal via cron**: `scripts/run_daily_cron.sh` (voegt geen restore/save
  toe — het bestand staat al lokaal op schijf). Voorbeeld crontab-regel
  staat in het script zelf.

### Module 7 — Paper trading

- **`src/stock_tracker/paper.py`** — `trades_paper` is de enige bewaarde
  staat; er is geen apart cash-grootboek. Elke run herbouwt de portefeuille
  (`reconstruct_portfolio()`) door `trades_paper` chronologisch te herspelen
  door dezelfde kosten-/sizingregels als de backtester
  (`backtest.Portfolio`/`CostConfig`/`SizingConfig`), zodat paper-resultaten
  rechtstreeks vergelijkbaar blijven met de out-of-sample backtestcijfers.
- **`fill_pending_signals(as_of)`**: vult elk nog niet verwerkt signaal met
  een datum vóór `as_of` tegen `as_of`'s slotkoers — normaal gesproken exact
  "de slotkoers van de volgende dag" t.o.v. het signaal, want de dagelijkse
  run verwerkt gisterens signalen vandaag. Een tranche die al open staat
  wordt niet opnieuw gekocht; `EXIT_FUNDAMENTAL` (tranche `None`) sluit alle
  open tranches van die ticker. Signalen zonder beschikbare koers (feestdag,
  nog niet geïngest) blijven onverwerkt voor een latere run.
- **Maandrapport** (`monthly_report()`): realised (gesloten trades die
  maand) en unrealised (laatst bekende koers) resultaat, plus het
  door de laatste backtest-run geïmpliceerde rendement over dezelfde
  periode (CAGR omgerekend naar de periodelengte) en het werkelijke
  benchmark-rendement over die periode.
- **30-trade-milestone** (`compare_to_backtest()`, drempel
  `paper_trading.review_after_closed_trades`): vergelijkt de gerealiseerde
  win rate en gemiddelde winst-per-trade met de laatste backtest-run,
  binnen een configureerbare bandbreedte (`win_rate_tolerance_pct`,
  `avg_profit_tolerance_pct`).

### Volgende modules

Alle modules uit de spec zijn gebouwd.

## Tests

```bash
uv run pytest
uv run ruff check .
```
