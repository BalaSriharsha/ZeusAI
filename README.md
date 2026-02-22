# AI Phone Agent

A multi-agent AI system that makes real phone calls on your behalf. Give it a task -- book a hospital appointment, call customer support, check a bank balance -- and it dials the number, holds the conversation, and sends you an SMS summary when done.

The system supports two modes: **Real Call Mode** (dials actual phone numbers via Twilio) and **Simulated Mode** (uses a built-in hospital agent for testing without real calls).

---

## Architecture

![Architecture Diagram](assets/architecture-diagram.png)

```
+-----------------------------------------------------------------+
|                     BROWSER (Frontend)                           |
|  +-------------+  +----------------+  +---------------------+   |
|  | Voice/Text  |  | WebSocket      |  | Audio Playback      |   |
|  | Input       |  | Client         |  | Queue               |   |
|  +-------------+  +-------+--------+  +---------------------+   |
+---------------------------|--------------------------------------+
                            | WebSocket /ws/browser (JSON)
                            v
+-----------------------------------------------------------------+
|                  BACKEND SERVICE (FastAPI, Port 8000)            |
|                                                                  |
|  +--------------+   +---------------+   +------------------+     |
|  | Input Agent  |   | Call Monitor  |   | Action Agent     |     |
|  | (A1)         |-->| (A2)          |-->| (A3)             |     |
|  | - STT        |   | - Classify    |   | - Decide action  |     |
|  | - Extract    |   |   IVR prompts |   | - Generate speech|     |
|  |   intent     |   | - Track       |   | - DTMF digits    |     |
|  | - Resolve    |   |   history     |   | - End call       |     |
|  |   phone #    |   |               |   |                  |     |
|  +------+-------+   +-------+-------+   +--------+---------+    |
|         |                   |                     |              |
|  +------v-------------------v---------------------v----------+   |
|  |                    SERVICES LAYER                         |   |
|  |  +---------+ +---------+ +------------+ +-------------+  |   |
|  |  | Groq    | | Groq    | | Deepgram   | | Twilio      |  |   |
|  |  | Whisper | | LLaMA   | | TTS (Aura) | | REST API    |  |   |
|  |  | (STT)   | | 3.3 70B | |            | |             |  |   |
|  |  +---------+ +---------+ +------------+ +-------------+  |   |
|  +-----------------------------------------------------------+   |
+---------+-----------------------+-------------------+------------+
          |                       |                   |
          v                       v                   v
+------------------+  +-------------------+  +----------------+
| Hospital Agent   |  | Real Phone Target |  | User Phone     |
| Simulator        |  | (via Twilio Media |  | (SMS Summary)  |
| (Port 8001)      |  |  Stream + ngrok)  |  |                |
+------------------+  +-------------------+  +----------------+
  (Simulated Mode)       (Real Call Mode)
```

---

## How It Works

### 1. User Input

You type or speak your request in the browser:

```
"Book an appointment with Dermatologist Chandra at Apollo Hospital,
 Madinaguda, Hyderabad on 15th April 2026"
```

**Agent 1 (Input Agent)** transcribes your voice, extracts a structured intent (target entity, task, doctor, date, etc.), and looks up the target phone number from the registry.

### 2. Call Initiation

The system picks one of two modes:

| Condition | Mode | What Happens |
|---|---|---|
| Target phone found + Twilio configured | **Real Call** | Dials the actual number via Twilio. Bidirectional audio streams through a Media Stream WebSocket. |
| No phone found or no Twilio | **Simulated** | Connects to the built-in Hospital Agent (port 8001) via WebSocket for a simulated conversation. |

### 3. Conversation Loop

**Real Call Mode:**
1. The agent introduces itself to the other party
2. Listens for speech via Twilio Media Stream (mulaw audio)
3. Transcribes with Groq Whisper STT
4. **Agent 3 (Action Agent)** decides the response via Groq LLM
5. Generates speech via Deepgram TTS and sends it back through the stream
6. Repeats until the task is complete or the call ends

**Simulated Mode:**
1. Hospital Agent generates a response using its own LLM brain
2. **Agent 2 (Call Monitor)** classifies the IVR prompt type
3. **Agent 3 (Action Agent)** decides what to say or which DTMF key to press
4. Sends the response back to the Hospital Agent
5. Repeats until the conversation ends

### 4. Post-Call

After the call completes, the system sends an **SMS summary** to your phone number via Twilio with the task details, turn count, and last response.

---

## The Agents

### Agent 1 -- Input Agent

| Responsibility | Details |
|---|---|
| Voice capture | Browser microphone -> WebM audio |
| Transcription | Groq Whisper `whisper-large-v3-turbo` |
| Intent extraction | Groq LLaMA 3.3 70B -> structured JSON (target entity, task, doctor, date, etc.) |
| Phone resolution | Fuzzy-matches entity name against the hospital registry to find the real phone number |
| Session setup | Creates `CallState` and determines call mode (real vs. simulated) |

### Agent 2 -- Call Monitor Agent

| Responsibility | Details |
|---|---|
| IVR classification | Groq LLaMA classifies prompts: greeting, menu, confirmation, date input, etc. |
| Context tracking | Maintains full conversation history for the Action Agent |
| Turn segmentation | Groups audio segments into IVR turns based on silence gaps |

### Agent 3 -- Action Agent

| Responsibility | Details |
|---|---|
| LLM-driven decisions | Groq LLaMA generates action type + speech text dynamically |
| Action types | `SPEAK`, `DTMF`, `WAIT`, `END_CALL` |
| Real-time transcripts | `handle_raw_transcript()` for live Twilio calls (bypasses Agent 2) |
| DTMF handling | Formats dates, selects menu options, presses digits |
| Context-aware | Uses full conversation history + intent summary to stay on track |

### Hospital Agent (Simulator)

| Responsibility | Details |
|---|---|
| Simulated IVR | LLM-driven hospital receptionist that responds naturally |
| TTS generation | Deepgram Aura voice (`aura-luna-en`) for hospital-side audio |
| Conversation modes | Supports conversational and DTMF-based interactions |
| Separate service | Runs independently on port 8001 |

---

## Technology Stack

| Component | Technology | Purpose |
|---|---|---|
| **Backend** | Python 3.12+ / FastAPI | REST API + WebSocket server |
| **STT** | Groq Whisper (`whisper-large-v3-turbo`) | Speech-to-Text |
| **LLM** | Groq LLaMA 3.3 70B (`llama-3.3-70b-versatile`) | Intent extraction, IVR classification, response generation |
| **TTS** | Deepgram Aura | Text-to-Speech (MP3 for browser, mulaw for Twilio) |
| **Telephony** | Twilio (Calls, Media Streams, SMS) | Real phone calls + SMS summaries |
| **Tunneling** | ngrok | Exposes local server for Twilio webhooks |
| **State** | In-memory (Redis optional) | Session and call state management |
| **Frontend** | Vanilla JS + WebSocket | Browser UI, voice input, audio playback |

---

## Prerequisites

### API Keys Required

| Service | Sign Up | What You Need |
|---|---|---|
| **Groq** | https://console.groq.com | API Key (`gsk_...`) |
| **Deepgram** | https://console.deepgram.com | API Key (for TTS) |
| **Twilio** | https://console.twilio.com | Account SID, Auth Token, Phone Number |

---

## Deployment Guide

### Option A: Docker (Recommended for Production)

Runs the full stack (backend + hospital agent + redis + nginx) in containers.

#### Step 1: Clone and Configure

```bash
git clone <your-repo-url>
cd ai-phone-agent
cp .env.example .env
```

Edit `.env` and fill in all your keys:

```bash
GROQ_API_KEY=gsk_your_key_here
DEEPGRAM_API_KEY=your_deepgram_key_here
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_PHONE_NUMBER=+18451234567

# Set this to your server's public domain or IP
PUBLIC_BASE_URL=https://yourdomain.com

DEFAULT_USER_NAME=YourName
DEFAULT_USER_PHONE=+91XXXXXXXXXX

HOSPITAL_REGISTRY='{"apollo_hospital_madinaguda": "+91XXXXXXXXXX"}'
```

> `HOSPITAL_AGENT_URL` and `REDIS_URL` are automatically overridden in `docker-compose.yml`
> to use internal service names -- do not change them in `.env`.

#### Step 2: Add SSL Certificates

Place your SSL certificate files in `nginx/ssl/`:

```
nginx/ssl/fullchain.pem    # your certificate chain
nginx/ssl/privkey.pem      # your private key
```

**To get a free certificate with Let's Encrypt (certbot):**

```bash
# On your server (before starting Docker):
sudo apt install certbot
sudo certbot certonly --standalone -d yourdomain.com
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/ssl/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem nginx/ssl/
```

**No SSL yet? Use HTTP-only for initial setup:**

In `docker-compose.yml`, change the nginx volume line to use the HTTP-only config:

```yaml
- ./nginx/nginx.http-only.conf:/etc/nginx/nginx.conf:ro
```

Then set `PUBLIC_BASE_URL=http://your-server-ip`.

#### Step 3: Start the Stack

```bash
docker compose up --build -d
```

This starts four containers:
- `backend` -- main FastAPI app on port 8000 (internal)
- `hospital-agent` -- simulator on port 8001 (internal)
- `redis` -- session state (internal)
- `nginx` -- reverse proxy on ports 80 and 443 (public)

Open `https://yourdomain.com` in your browser.

#### Useful Docker Commands

```bash
# View logs for all services
docker compose logs -f

# View logs for a specific service
docker compose logs -f backend
docker compose logs -f hospital-agent

# Restart a single service after code changes
docker compose up --build -d backend

# Stop everything
docker compose down

# Stop and remove volumes (clears redis data)
docker compose down -v
```

---

### Option B: Local Development (with Docker)

Uses source-mounted volumes so code changes are reflected without rebuilding:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

This adds `--reload` to both services and skips nginx, so you access the app directly at http://localhost:8000.

---

### Option C: Run Without Docker (Local Development)

Requires Python 3.11+ and the dependencies installed.

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt

cp .env.example .env
# Fill in your API keys in .env
```

For real Twilio calls locally, you need ngrok to expose port 8000:

```bash
# Install: https://ngrok.com/download
ngrok http 8000
# Copy the https:// URL into .env as PUBLIC_BASE_URL
```

Start both services in separate terminals:

```bash
# Terminal 1
python -m uvicorn hospital_agent.main:app --port 8001 --reload

# Terminal 2
python -m uvicorn backend.main:app --port 8000 --reload
```

Open http://localhost:8000 in your browser.

---

## Usage

1. **Enter your request** -- Type or speak what you want the agent to do
2. **Review the extracted intent** -- The system shows what it understood (target, task, doctor, date, etc.)
3. **Optionally update your name/phone** -- Override the defaults in the UI
4. **Start the call** -- The button shows whether it will be a real call or simulated
5. **Watch the conversation** -- Both sides appear in the chat panel with playable audio
6. **Receive SMS** -- After the call, check your phone for the summary

---

## Project Structure

```
ai-phone-agent/
├── .env.example               # Environment variables template
├── .dockerignore              # Files excluded from Docker build
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Single image for both services
├── docker-compose.yml         # Production: backend + hospital-agent + redis + nginx
├── docker-compose.dev.yml     # Development overrides (hot reload, no nginx)
├── README.md                  # This file
│
├── nginx/
│   ├── nginx.conf             # Production nginx config (HTTPS + WebSocket)
│   ├── nginx.http-only.conf   # HTTP-only config (before SSL is set up)
│   └── ssl/                   # Place fullchain.pem + privkey.pem here
│
├── assets/
│   └── architecture-diagram.png
│
├── backend/
│   ├── main.py                # FastAPI app, WebSocket handlers, call orchestration
│   ├── config.py              # Pydantic settings management
│   │
│   ├── agents/
│   │   ├── input_agent.py     # Agent 1: User input -> intent -> phone resolution
│   │   ├── call_monitor.py    # Agent 2: IVR classification + conversation tracking
│   │   └── action_agent.py    # Agent 3: LLM-driven action decisions + speech generation
│   │
│   ├── services/
│   │   ├── groq_stt.py        # Groq Whisper STT service
│   │   ├── groq_llm.py        # Groq LLaMA LLM service
│   │   ├── tts_service.py     # Deepgram TTS (MP3 for browser, mulaw for Twilio)
│   │   ├── audio_utils.py     # Mulaw decoding, VAD, speech detection, WAV conversion
│   │   └── twilio_call.py     # Twilio telephony helpers
│   │
│   └── models/
│       └── schemas.py         # Pydantic models (UserIntent, CallState, ActionResult, etc.)
│
├── hospital_agent/
│   ├── main.py                # Hospital Agent FastAPI app + WebSocket handler
│   ├── config.py              # Hospital Agent settings
│   └── brain.py               # LLM-driven hospital receptionist logic
│
└── frontend/
    ├── index.html             # Browser UI
    ├── app.js                 # WebSocket client, audio handling, UI logic
    └── styles.css             # Styling
```

---

## Call Modes in Detail

### Real Call Mode

When the Input Agent resolves a phone number from the registry:

1. Backend creates a Twilio outbound call to the target number
2. TwiML instructs Twilio to play a brief greeting, then start a bidirectional Media Stream
3. Audio flows through a WebSocket: Twilio <-> ngrok <-> Backend
4. Inbound audio (mulaw, 8kHz) is buffered, voice-activity-detected, and transcribed
5. The Action Agent generates a response, which is converted to mulaw TTS and sent back
6. The browser sees both sides of the conversation in real time with playable audio
7. After the call, an SMS summary is sent to the user

> **Twilio Trial Accounts:** The other party will hear a Twilio trial message first and must press a key before the conversation starts. The system accounts for this with extended timeouts and retry logic.

### Simulated Mode

When no real phone number is available:

1. Backend connects to the Hospital Agent WebSocket on port 8001
2. The Hospital Agent uses its own LLM brain to generate realistic responses
3. Both agents converse via WebSocket, with TTS audio generated for each turn
4. The browser shows the full conversation with audio playback
5. Optionally, Twilio calls your phone so you can listen in

---

## Key Configuration Reference

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key for STT and LLM |
| `DEEPGRAM_API_KEY` | Yes | Deepgram API key for TTS |
| `TWILIO_ACCOUNT_SID` | For real calls | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | For real calls | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | For real calls | Your Twilio phone number (E.164 format) |
| `PUBLIC_BASE_URL` | For real calls | Publicly accessible URL (ngrok for local dev) |
| `HOSPITAL_REGISTRY` | For real calls | JSON mapping of entity names to phone numbers |
| `DEFAULT_USER_NAME` | No | Default user name (overridable in UI) |
| `DEFAULT_USER_PHONE` | No | Default phone for SMS summaries |
| `AGENT_TTS_VOICE` | No | Agent voice (default: `aura-orion-en`) |
| `HOSPITAL_TTS_VOICE` | No | Hospital simulator voice (default: `aura-luna-en`) |

---

## Known Limitations

1. **Twilio trial accounts** play a message before connecting and require the other party to press a key
2. **Groq Whisper** has a file size limit (~25MB per request)
3. **Energy-based VAD** may occasionally misdetect speech in noisy environments
4. **Single concurrent call** per session (no parallel call support)
5. **DTMF over real calls** is not yet implemented (only supported in simulated mode)
6. **ngrok URL changes** on restart (local dev) -- remember to update `PUBLIC_BASE_URL`; on a real server with a fixed domain this is not an issue

---

## Future Enhancements

- Upgrade to Twilio paid account for clean call flow (no trial message)
- Redis-backed state for multi-instance deployment
- Streaming STT (when Groq supports streaming transcription)
- Multi-language support (Whisper supports 100+ languages)
- Call recording and playback
- DTMF tone injection for real Twilio calls
- User authentication and persistent profiles
- Analytics dashboard for call metrics
- Support for multiple simultaneous calls

---

## License

MIT
