# BetAnalyzer 🎯

Analisi quote scommesse con AI per **Serie A**, **Premier League** e **La Liga**.

## Setup rapido

```bash
# 1. Crea ambiente virtuale
python3 -m venv venv
source venv/bin/activate

# 2. Installa dipendenze
pip install -r requirements.txt

# 3. Configura le chiavi API
cp .env.example .env
# Apri .env e inserisci le tue chiavi

# 4. Avvia
python app.py
```

Poi apri **http://localhost:5000**

## Chiavi API necessarie

| Chiave | Dove ottenerla | Costo |
|--------|---------------|-------|
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | Pay-per-use |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com) | Gratuita (500 req/mese) |

## Come funziona

1. **Quote**: recuperate da The Odds API (include SNAI, Bet365, Betfair, Unibet, ecc.)
2. **Analisi AI**: ogni partita viene analizzata via OpenRouter con un prompt da esperto scommesse
3. **Cache**: le quote vengono salvate per 30 minuti (configurabile in `.env`)
4. **Raccomandazioni**: l'AI valuta value bet, consensus dei bookmaker e mercati alternativi

## Struttura progetto

```
betanalyzer/
├── app.py              # Flask server
├── scraper/odds_api.py # Client The Odds API
├── ai/analyzer.py      # Modulo AI (OpenRouter)
├── models/match.py     # Dataclass
├── templates/          # HTML
├── static/style.css    # CSS dark theme
├── cache/              # Cache JSON (auto-generata)
└── .env                # Le tue chiavi (da creare)
```

## Modello AI

Di default usa `openai/gpt-4o-mini` via OpenRouter (economico e veloce).
Puoi cambiarlo nel `.env`:
```
AI_MODEL=anthropic/claude-3-haiku  # alternativa veloce ed economica
AI_MODEL=openai/gpt-4o             # più preciso, più costoso
```
