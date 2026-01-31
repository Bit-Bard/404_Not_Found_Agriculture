# Agentic Crop Advisory (Hackathon, Production-leaning)

A minimal, agentic crop advisory system that continuously updates guidance as:
- farmer inputs change (crop stage, symptoms, practices)
- weather changes (OpenWeather One Call 3.0)
- web context changes (Tavily search)

UI: Telegram Bot  
Orchestration: LangGraph (GraphState + interactive loop)  
LLM: OpenAI `gpt-4.1-mini`  
DB: MySQL (XAMPP) for state persistence

## Features
- Conversational intake: turns farmer text into structured context
- Stage-aware guidance: sowing → growth → harvest
- Tool calls:
  - Weather forecast & alerts via OpenWeather One Call 3.0
  - Web search via Tavily for local practices/common issues
- Persistence:
  - Stores per-user GraphState by Telegram `chat_id` in MySQL (recommended)
  - Optional fallback JSON store
- Guardrails:
  - Strict structured output validation (Pydantic)
  - Safety checks (no pesticide dosage; escalation for risky requests)
  - Optional human oversight triggers

## Directory
agentic_crop_advisor/
run.py
requirements.txt
.env.example
src/app/
config.py
models.py
tools.py
db.py
store.py
graph.py
telegram_bot.py


## Setup
1. Create venv and install deps:
```bash
cd agentic_crop_advisor
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
Create .env:

cp .env.example .env
# fill keys: OPENAI_API_KEY, TAVILY_API_KEY, OPENWEATHER_API_KEY, TELEGRAM_BOT_TOKEN
MySQL setup (XAMPP)

Start Apache + MySQL in XAMPP

Create database agentic_crop_advisor

Use .env values (host/port/user/pass)

The app will create the required table automatically on first run.

Run (Telegram polling):

python run.py
Notes
LangGraph “agents” are defined in src/app/graph.py.

MySQL is used only to persist session state; the graph logic stays clean.

Guardrails & Safety Notes
This system is decision support, not guaranteed agronomy outcomes.

Avoids pesticide dosage/mixing instructions

Avoids guaranteed yield claims

Escalates severe pest/disease or safety-critical requests to human review