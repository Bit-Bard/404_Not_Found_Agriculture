# 🌎  DhartiQ - Agentic Crop Advisor


### *A production-grade **agentic crop advisory system** that supports farmers across crop stages using:*

<p align="center">
  <img src="DartiQ_Logo.jpeg" alt="DhartiQ - Agentic Crop Advisor" width="150">
  <br>
  <i>An agentic, AI-powered crop advisory system delivering stage-wise guidance to farmers in real time.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/github/repo-size/Bit-Bard/404_Not_Found_Agriculture?style=flat-square&color=orange" alt="Repo Size">
  <img src="https://img.shields.io/github/stars/Bit-Bard/404_Not_Found_Agriculture?style=flat-square" alt="Stars">
  <img src="https://badges.frapsoft.com/os/v2/open-source.svg?v=103" alt="Open Source">
</p>

# Agentic Crop Advisor (Telegram + LangGraph + MySQL)

A production-grade (hackathon-ready) **agentic crop advisory system** that supports farmers across crop stages using:

- **LangGraph** for agent workflow + state management
- **OpenAI (gpt-4.1-mini)** for advisory + image-based diagnosis
- **OpenWeather** for weather context (with automatic fallback)
- **Tavily** for web search (best practices, schemes, market info, and input purchase links)
- **MySQL (XAMPP)** for persistent profiles + sessions + image records
- **Telegram Bot UI** with rich inline buttons + multi-language support (English / Hindi / Marathi)

---

## Problem Statement 

Farmers often receive **generic or delayed guidance**, even though decisions depend on **crop stage, weather, local conditions, and symptoms**.

This system provides **continuous and contextual guidance** across:

- Pre-sowing → Sowing → Growth → Harvest
- Weather-aware updates
- Farmer inputs via text, GPS location, and photos
- Actionable advice with safety guardrails (no pesticide dosage/mixing ratios)

---
## Demo

<table align="center" width="40%">
  <!-- TOP ROW: BIG GIF -->
  <tr>
    <td align="center" colspan="2">
      <img src="1d.gif" alt="Main Demo" width="70%">
      <br>
      <i>Features: Live Location,Crop Suggestions,Govt Scheme</i>
    </td>
  </tr>

  <!-- SECOND ROW: TWO GIFS SIDE BY SIDE -->
  <tr>
    <td align="center" width="50%">
      <img src="2d.gif" alt="Feature One" width="100%">
      <br>
      <i>Features: Govt Scheme, Buy Inputs</i>
    </td>
    <td align="center" width="50%">
      <img src="3d.gif" alt="Feature Two" width="100%">
      <br>
      <i>Features: Languages </i>
    </td>
  </tr>
</table>

#  Phone Tutorial

[![DhartiQ Mobile Tutorial](https://img.youtube.com/vi/fsKwvcneShw/maxresdefault.jpg)](https://www.youtube.com/watch?v=fsKwvcneShw)


## Key Features

### Advisory Core
- Continuous advisory loop based on **crop + stage + location + symptoms**
- **Stage selection buttons** (Sowing / Germination / Vegetative / Flowering / Fruiting / Maturity / Harvest)
- Location capture options:
  - Manual: city/village or `lat,lon`
  - GPS: Telegram “Share Location”
- Photo diagnosis flow:
  - Upload crop photo → detect likely issue → suggested safe actions
- On-demand modules (shown only when clicked):
  - Govt schemes
  - Market prices
  - Buy inputs (fresh links via Tavily)
  - Crop suggestions (location + climate + web hints)

### UX / UI
- Premium Telegram inline UI (fast, friendly, button-driven)
- Language switching: **English / हिंदी / मराठी**
- User state persisted in **MySQL**, stable across restarts

### Hackathon Reliability
- Best experience on **polling mode** (single laptop)
- Optional digest scheduling (configurable interval)

---

## Directory Structure


```
agentic_crop_advisor/
├─ .gitignore
├─ .env.example
├─ README.md
├─ requirements.txt
├─ run.py
└─ src/
   └─ app/
      ├─ __init__.py
      ├─ config.py
      ├─ models.py
      ├─ tools.py
      ├─ db.py              # NEW: database helpers / schema
      ├─ store.py
      ├─ graph.py           # LangGraph agents + orchestration
      └─ telegram_bot.py
```      
Where the Agents Live
LangGraph orchestration + node logic:

src/app/graph.py

Tech Stack
1) Python 3.10+ (3.12 supported)
2) Telegram Bot API
3) LangGraph
4) OpenAI Responses API (Chatgpt gpt-4.1-mini)
5) OpenWeather (One Call 3.0 with fallback)
6) Tavily (search)
7) MySQL (XAMPP)

# Local Setup 
```
1) Clone + Create Virtual Environment
git clone https://github.com/Bit-Bard/404_Not_Found_Agriculture.git
cd agentic_crop_advisor
```

```
python -m venv .venv
# Windows
.venv\Scripts\activate
# Mac/Linux
source .venv/bin/activate
2) Install Dependencies
pip install -r requirements.txt
3) Create .env
Copy .env.example → .env
# Windows
copy .env.example .env
# Mac/Linux
cp .env.example .env
Fill values inside .env.
```

Environment Variables
These keys should match .env.example.

```
1) Telegram
TELEGRAM_BOT_TOKEN=...
2) OpenAI
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_BASE_URL= (optional)
3) Tavily
TAVILY_API_KEY=...
TAVILY_MAX_RESULTS=5
4) OpenWeather
OPENWEATHER_API_KEY=...
OPENWEATHER_UNITS=metric
```

Note: One Call 3.0 may return 401 if your plan doesn’t support it.
This project is designed to fall back to One Call 2.5 and /weather.

```
MySQL (XAMPP)
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=
MYSQL_DB=agentic_crop_advisor
Digest Scheduling (Optional)
DIGEST_INTERVAL_SECONDS=86400 (24h)
For testing: set to 60
DIGEST_FIRST_DELAY_SECONDS=10
```

MySQL Setup (XAMPP)
1) Start MySQL
Open XAMPP Control Panel → Start MySQL
2) Create Database
CREATE DATABASE agentic_crop_advisor;
3) Create Tables (Schema Reference)


```
-- Farmers (profile)
CREATE TABLE IF NOT EXISTS farmers (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  chat_id VARCHAR(64) NOT NULL UNIQUE,
  farmer_name VARCHAR(120) NULL,
  crop VARCHAR(64) NULL,
  stage VARCHAR(32) NULL,
  land_size DECIMAL(10,2) NULL,
  land_unit VARCHAR(16) NULL,
  location_text VARCHAR(255) NULL,
  lat DECIMAL(9,6) NULL,
  lon DECIMAL(9,6) NULL,
  language VARCHAR(8) NOT NULL DEFAULT 'en',
  created_at_utc DATETIME NOT NULL,
  updated_at_utc DATETIME NOT NULL
);

-- Sessions (graph state persistence)
CREATE TABLE IF NOT EXISTS sessions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  chat_id VARCHAR(64) NOT NULL UNIQUE,
  state_json LONGTEXT NOT NULL,
  updated_at_utc DATETIME NOT NULL
);

-- Image uploads (optional history)
CREATE TABLE IF NOT EXISTS images (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  chat_id VARCHAR(64) NOT NULL,
  file_path VARCHAR(512) NOT NULL,
  telegram_file_id VARCHAR(256) NULL,
  caption TEXT NULL,
  created_at_utc DATETIME NOT NULL,
  INDEX idx_images_chat_id (chat_id)
);
```

If your db.py auto-creates tables, this schema is still useful as documentation.

Run the Bot (Polling)
Polling is the most reliable approach for hackathons.

python run.py
When successful, you should see logs like:

Telegram polling started
Incoming messages handled
Weather/Tavily/OpenAI calls executing

# Telegram Commands
```
/start — welcome + ask location + show main controls
/help — usage guide
/profile — profile template
/location — show “share location” button
/reset — reset user session state
```

# Buttons & Behavior
Language
English / हिंदी / मराठी
(Updates prompts + button labels)
Stage Controls
Sowing / Germination / Vegetative / Flowering / Fruiting / Maturity / Harvest
(Updates stage and regenerates advice)

# Actions
1) Set Profile
2) Update Location
3) Report Symptoms
4) Crop Suggestions
5) Buy Inputs
6) Govt Schemes
7) Market Prices
8) Important behavior
9) Govt Schemes show only when clicked
10) Market Prices show only when clicked
11) Buy Inputs fetches fresh links only when clicked

Farmer Usage Flow (Typical)
Start: /start
Share GPS location OR send:
Pune, Maharashtra
18.52, 73.85

# Share profile text:
My name is Ramesh
Crop: rice
Stage: germination
Land: 2 acres
Report issue: send symptoms or upload photo
Use buttons any time for schemes/market/buy-links

# Safety Guardrails
1) No pesticide dosage/mixing ratios
2) Advice stays concise and practical
3) Encourages expert review when uncertainty is high (especially unclear images)

## 👨‍💻 Authors

<p align="center">
  <b>Dhruv Devaliya & Yash Raj</b><br>
</p>

<p align="center">
  <!-- Dhruv -->
  <a href="https://github.com/Bit-Bard">
    <img src="https://img.shields.io/badge/GitHub-Bit--Bard-black?style=for-the-badge&logo=github">
  </a>
  <a href="http://www.linkedin.com/in/dhruv-devaliya">
    <img src="https://img.shields.io/badge/LinkedIn-Dhruv%20Devaliya-blue?style=for-the-badge&logo=linkedin">
  </a>
  <a href="https://www.instagram.com/ohh.dhruvv_/">
    <img src="https://img.shields.io/badge/Instagram-@ohh.dhruvv_-E4405F?style=for-the-badge&logo=instagram&logoColor=white">
  </a>
</p>

<p align="center">
  <!-- Yash -->
  <a href="https://github.com/KING-OF-FLAME">
    <img src="https://img.shields.io/badge/GitHub-KING--OF--FLAME-black?style=for-the-badge&logo=github">
  </a>
  <a href="https://www.linkedin.com/in/yash-developer/">
    <img src="https://img.shields.io/badge/LinkedIn-Yash%20Raj-blue?style=for-the-badge&logo=linkedin">
  </a>
  <a href="https://instagram.com/yash.developer">
    <img src="https://img.shields.io/badge/Instagram-@yash.developer-E4405F?style=for-the-badge&logo=instagram&logoColor=white">
  </a>
</p>


