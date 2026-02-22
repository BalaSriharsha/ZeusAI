# AI Phone Agent

A multi-agent AI system that makes real phone calls on your behalf. Give it a task -- book a hospital appointment, call customer support, check a bank balance -- and it dials the number, holds the conversation, and sends you an SMS summary when done.

Supports two modes: **Real Call Mode** (dials actual numbers via Twilio with live bidirectional audio) and **Simulated Mode** (uses a built-in hospital agent for testing without real calls).

---

## Architecture

![Architecture Diagram](architecture.png)

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
|  +--------------+   +---------------+   +------------------+    |
|  | Input Agent  |   | Call Monitor  |   | Action Agent     |    |
|  | (A1)         |-->| (A2)          |-->| (A3)             |    |
|  | - STT        |   | - Classify    |   | - Decide action  |    |
|  | - Extract    |   |   IVR prompts |   | - Generate speech|    |
|  |   intent     |   | - Track       |   | - DTMF digits    |    |
|  | - Resolve    |   |   history     |   | - End call       |    |
|  |   phone #    |   |               |   |                  |    |
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
| (Port 8001)      |  |  Stream)          |  |                |
+------------------+  +-------------------+  +----------------+
  (Simulated Mode)       (Real Call Mode)
```

---

## How It Works

### 1. User Input

Type or speak your request in the browser:

```
"Book an appointment with Dermatologist Chandra at Apollo Hospital,
 Madinaguda, Hyderabad on 15th April 2026"
```

**Agent 1 (Input Agent)** transcribes your voice (Groq Whisper), extracts a structured intent via LLM (target entity, task, doctor, date, etc.), and fuzzy-matches the entity name against the phone registry to resolve the number to call.

### 2. Call Initiation

| Condition | Mode | What Happens |
|---|---|---|
| Target phone found + Twilio configured | **Real Call** | Dials the actual number via Twilio. Bidirectional audio streams through a Media Stream WebSocket. |
| No phone found or Twilio not configured | **Simulated** | Connects to the built-in Hospital Agent via WebSocket for a simulated conversation. |

### 3. Conversation Loop

**Real Call Mode:**
1. The agent speaks first -- introduces itself and states the purpose of the call
2. Listens for speech from the other party via Twilio Media Stream (8kHz mulaw audio)
3. Voice Activity Detection (VAD) detects when the other party finishes speaking
4. Transcribes with Groq Whisper STT
5. **Agent 3 (Action Agent)** decides the response via Groq LLM
6. Generates speech via Deepgram TTS, sends mulaw audio back through the stream
7. Repeats until the task is complete; retries with nudge messages if the other party is silent

**Simulated Mode:**
1. Hospital Agent generates a response using its own LLM brain
2. **Agent 2 (Call Monitor)** classifies the IVR prompt type (greeting, menu, confirmation, etc.)
3. **Agent 3 (Action Agent)** decides what to say or which DTMF key to press
4. Response is sent back to the Hospital Agent; loop continues until the conversation ends

### 4. Post-Call

An **SMS summary** is sent to your phone via Twilio with the task details, turn count, and last response from the other party.

---

## The Agents

### Agent 1 -- Input Agent

| Responsibility | Details |
|---|---|
| Voice capture | Browser microphone -> WebM audio |
| Transcription | Groq Whisper `whisper-large-v3-turbo` |
| Intent extraction | Groq LLaMA 3.3 70B -> structured JSON (entity, task, doctor, date, etc.) |
| Phone resolution | Multi-tier fuzzy matching of entity name against the phone registry |
| Session setup | Creates `CallState` and determines call mode (real vs. simulated) |

### Agent 2 -- Call Monitor Agent

| Responsibility | Details |
|---|---|
| IVR classification | Groq LLaMA classifies prompts: greeting, menu, confirmation, date input, farewell, etc. |
| Context tracking | Maintains full conversation history for the Action Agent |
| Turn segmentation | Groups audio segments into turns based on silence gaps |

### Agent 3 -- Action Agent

| Responsibility | Details |
|---|---|
| LLM-driven decisions | Groq LLaMA generates action type + speech text dynamically -- no hardcoded scripts |
| Action types | `SPEAK`, `DTMF`, `WAIT`, `END_CALL` |
| Real-time transcripts | `handle_raw_transcript()` for live Twilio calls (bypasses Agent 2) |
| DTMF handling | Formats dates, selects menu options, formats key presses |
| Context-aware | Uses full conversation history + intent summary to stay on task |

### Hospital Agent (Simulator)

| Responsibility | Details |
|---|---|
| Simulated IVR | LLM-driven hospital receptionist that responds naturally |
| TTS generation | Deepgram Aura voice (`aura-luna-en`) for hospital-side audio |
| Conversation modes | Supports conversational and DTMF-based interactions |
| Separate service | Runs independently on port 8001, communicates via WebSocket |

---

## Technology Stack

| Component | Technology | Purpose |
|---|---|---|
| **Backend** | Python 3.12+ / FastAPI | REST API + WebSocket server |
| **STT** | Groq Whisper (`whisper-large-v3-turbo`) | Speech-to-Text |
| **LLM** | Groq LLaMA 3.3 70B (`llama-3.3-70b-versatile`) | Intent extraction, IVR classification, response generation |
| **TTS** | Deepgram Aura | Text-to-Speech (MP3 for browser, mulaw/8kHz for Twilio) |
| **Telephony** | Twilio (Calls, Media Streams, SMS) | Real phone calls + post-call SMS summaries |
| **Audio** | Voice Activity Detection, mulaw codec | Real-time speech detection from Twilio streams |
| **Proxy** | Cloudflare Tunnel / nginx | Expose server publicly; handles HTTPS + WebSocket |
| **State** | In-memory + Redis (optional) | Session and call state management |
| **Frontend** | Vanilla JS + WebSocket | Browser UI, voice input, sequential audio playback |
| **Container** | Docker + Docker Compose | Multi-service orchestration |

---

## API Keys Required

| Service | Sign Up | What You Need |
|---|---|---|
| **Groq** | https://console.groq.com | API Key (`gsk_...`) |
| **Deepgram** | https://console.deepgram.com | API Key |
| **Twilio** | https://console.twilio.com | Account SID, Auth Token, Phone Number |

---

## Deployment Guide

### Option A: Cloudflare Tunnel (Recommended)

This is the easiest way to go live. Cloudflare Tunnel runs as a Docker container, creates a secure outbound connection to Cloudflare's edge, and routes your domain to the app -- no open ports, no SSL certs to manage.

#### Step 1: Get a VPS

You need a Linux server with Docker. Recommended options:

| Provider | Plan | Monthly Cost |
|---|---|---|
| **Hetzner Cloud** | CX22 (2 vCPU, 4 GB RAM) | ~$4 |
| **DigitalOcean** | Basic 2 GB Droplet | ~$12 |
| **AWS Lightsail** | 2 GB instance | ~$10 |

Install Docker on the VPS:

```bash
ssh root@YOUR_SERVER_IP
curl -fsSL https://get.docker.com | sh
```

#### Step 2: Create a Cloudflare Tunnel

1. Go to **https://one.dash.cloudflare.com** and log in
2. In the sidebar: **Networks** -> **Tunnels**
3. Click **Create a tunnel** -> select **Cloudflared** -> click Next
4. Name the tunnel: `ai-phone-agent` -> click Save
5. **Copy the tunnel token** (the long string after `--token` in the install command)
6. Click Next to reach the **Public Hostname** screen and add:

   | Field | Value |
   |---|---|
   | Subdomain | `app` |
   | Domain | `ayanetic.com` |
   | Service Type | `HTTP` |
   | URL | `backend:8000` |

7. Save the tunnel -- your app will be at `https://app.ayanetic.com`

**Enable WebSockets** (required for browser + Twilio audio streams):
- Cloudflare dashboard -> `ayanetic.com` -> **Network** tab -> turn on **WebSockets**

#### Step 3: Clone and Configure

```bash
git clone <your-repo-url>
cd ai-phone-agent
cp .env.example .env
```

Edit `.env`:

```bash
GROQ_API_KEY=gsk_your_key_here
DEEPGRAM_API_KEY=your_deepgram_key_here

TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_PHONE_NUMBER=+18451234567

# Must match the public hostname configured in the tunnel
PUBLIC_BASE_URL=https://app.ayanetic.com

# Tunnel token from Step 2
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoixxxxxxxx...

DEFAULT_USER_NAME=YourName
DEFAULT_USER_PHONE=+91XXXXXXXXXX

HOSPITAL_REGISTRY='{"apollo_hospital_madinaguda": "+91XXXXXXXXXX"}'
```

> `HOSPITAL_AGENT_URL` and `REDIS_URL` are automatically set to internal Docker service names by the compose files -- do not override them.

#### Step 4: Start the Stack

```bash
docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml up --build -d
```

This starts four containers: `backend`, `hospital-agent`, `redis`, and `cloudflared` (no nginx needed). Open `https://app.ayanetic.com`.

---

### Option B: VPS with nginx (Manual SSL)

Use this if you prefer managing your own SSL certificates (e.g., with Let's Encrypt).

#### Step 1: Get SSL Certificates

```bash
sudo apt install certbot
sudo certbot certonly --standalone -d yourdomain.com
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/ssl/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem nginx/ssl/
```

No SSL yet? Use `nginx/nginx.http-only.conf` during initial setup:
In `docker-compose.yml`, change the nginx volume line to:
```yaml
- ./nginx/nginx.http-only.conf:/etc/nginx/nginx.conf:ro
```

#### Step 2: Configure and Deploy

```bash
cp .env.example .env
# Fill in API keys + set PUBLIC_BASE_URL=https://yourdomain.com
docker compose up --build -d
```

This starts: `backend`, `hospital-agent`, `redis`, and `nginx` on ports 80/443.

---

### Option C: Local Development (Docker)

Hot-reload mode with source-mounted volumes -- no nginx, access directly on port 8000:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

For Twilio calls locally, use ngrok to expose port 8000:

```bash
ngrok http 8000
# Copy the https:// URL into .env as PUBLIC_BASE_URL
```

---

### Option D: Local Development (No Docker)

Requires Python 3.11+.

```bash
python -m venv venv
source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
cp .env.example .env
```

Start both services in separate terminals:

```bash
# Terminal 1
python -m uvicorn hospital_agent.main:app --port 8001 --reload

# Terminal 2
python -m uvicorn backend.main:app --port 8000 --reload
```

Open http://localhost:8000.

---

## Useful Docker Commands

```bash
# View logs for all services
docker compose logs -f

# View logs for a specific service
docker compose logs -f backend
docker compose logs -f hospital-agent
docker compose logs -f cloudflared

# Rebuild and restart after a code change
git pull
docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml up --build -d

# Restart a single service without rebuilding
docker compose restart backend

# Stop everything
docker compose down

# Stop and wipe redis data
docker compose down -v
```

---

## Usage

1. **Enter your request** -- Type or speak what you want the agent to do
2. **Review the intent** -- The system shows what it extracted (entity, task, doctor, date, etc.)
3. **Update name/phone if needed** -- Override the defaults directly in the UI
4. **Check the call button** -- It shows whether a real call or simulated call will happen, and the target
5. **Start the call** -- Both sides of the conversation appear in the chat panel with playable audio
6. **Receive SMS** -- After the call ends, check your phone for the summary

---

## Project Structure

```
ai-phone-agent/
├── .env.example                  # Environment variables template
├── .dockerignore                 # Files excluded from Docker build
├── .gitignore
├── LICENSE
├── requirements.txt              # Python dependencies
├── Dockerfile                    # Single image used by both services
├── docker-compose.yml            # Production: backend + hospital-agent + redis + nginx
├── docker-compose.dev.yml        # Development: hot reload, no nginx
├── docker-compose.cloudflare.yml # Cloudflare Tunnel: replaces nginx with cloudflared
├── README.md
│
├── nginx/
│   ├── nginx.conf                # Production nginx (HTTPS + WebSocket proxying)
│   ├── nginx.http-only.conf      # HTTP-only nginx (use before SSL is set up)
│   └── ssl/                      # Place fullchain.pem + privkey.pem here
│
├── assets/
│   └── architecture-diagram.png
│
├── backend/
│   ├── main.py                   # FastAPI app, WebSocket handlers, call orchestration
│   ├── config.py                 # Pydantic settings (reads from .env)
│   │
│   ├── agents/
│   │   ├── input_agent.py        # Agent 1: voice/text -> intent -> phone resolution
│   │   ├── call_monitor.py       # Agent 2: IVR classification + conversation history
│   │   └── action_agent.py       # Agent 3: LLM-driven action decisions + speech
│   │
│   ├── services/
│   │   ├── groq_stt.py           # Groq Whisper STT
│   │   ├── groq_llm.py           # Groq LLaMA (intent, classification, responses)
│   │   ├── tts_service.py        # Deepgram TTS (MP3 for browser, mulaw for Twilio)
│   │   ├── audio_utils.py        # Mulaw codec, VAD, speech detection, WAV conversion
│   │   └── twilio_call.py        # Twilio helpers
│   │
│   └── models/
│       └── schemas.py            # Pydantic models (UserIntent, CallState, etc.)
│
├── hospital_agent/
│   ├── main.py                   # Hospital Agent FastAPI app + WebSocket handler
│   ├── config.py                 # Hospital Agent settings
│   └── brain.py                  # LLM-driven hospital receptionist brain
│
└── frontend/
    ├── index.html                # Browser UI
    ├── app.js                    # WebSocket client, audio handling, UI logic
    └── styles.css                # Styling
```

---

## Call Modes in Detail

### Real Call Mode

1. Backend creates a Twilio outbound call to the target number via REST API
2. TwiML plays a brief greeting ("Hello, please hold..."), then starts a bidirectional Media Stream
3. The Media Stream WebSocket connects to the backend via the public URL
4. The agent speaks first -- introduces itself and states the purpose of the call
5. Inbound audio (8kHz mulaw) is buffered, voice-activity-detected, and segmented
6. Each speech segment is decoded to WAV and transcribed by Groq Whisper
7. Agent 3 generates a response via Groq LLM; TTS audio is sent back through the stream
8. If silence is detected, the agent sends a nudge (up to 3 retries before ending)
9. Browser sees both sides in real time with playable audio; SMS summary sent at the end

> **Twilio Trial Accounts:** The other party hears a trial message and must press a key before the call connects. The system uses a 90-second stream timeout to accommodate this.
>
> **Unverified Numbers:** Trial accounts can only call verified numbers. Upgrade to a paid account or verify the number in the Twilio console.

### Simulated Mode

1. Backend connects to the Hospital Agent WebSocket (`ws://hospital-agent:8001/ws/call`)
2. Sends `call_start` with the user's intent
3. Hospital Agent brain generates a natural, contextual response and TTS audio
4. Agent 2 classifies the response type; Agent 3 decides the reply
5. Loop continues with `caller_speech` / `hospital_speech` messages until `call_end`
6. Browser shows full conversation with audio playback

---

## Configuration Reference

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key for STT and LLM |
| `DEEPGRAM_API_KEY` | Yes | Deepgram API key for TTS |
| `TWILIO_ACCOUNT_SID` | For real calls | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | For real calls | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | For real calls | Your Twilio number (E.164 format, e.g. `+18451234567`) |
| `PUBLIC_BASE_URL` | For real calls | Public HTTPS URL Twilio can reach (ngrok, Cloudflare, or your domain) |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare deploy | Tunnel token from Cloudflare Zero Trust |
| `HOSPITAL_REGISTRY` | For real calls | JSON map of entity names to phone numbers |
| `DEFAULT_USER_NAME` | No | Default name shown in UI (overridable per session) |
| `DEFAULT_USER_PHONE` | No | Default phone for SMS summaries (must be verified on trial) |
| `AGENT_TTS_VOICE` | No | Agent TTS voice (default: `aura-orion-en`) |
| `HOSPITAL_TTS_VOICE` | No | Hospital simulator voice (default: `aura-luna-en`) |

### Hospital Registry Format

```bash
HOSPITAL_REGISTRY='{"apollo_hospital_madinaguda": "+919491025667", "sbi_bank_hyderabad": "+914012345678"}'
```

Key format: `entity_name_branch` (lowercase, underscores). The lookup uses multi-tier fuzzy matching, so variations like "Apollo Hospital Madinaguda" or "apollo hospital madinagura" (misspelling) will still resolve correctly.

---

## Known Limitations

1. **Twilio trial accounts** play a message before connecting and require the other party to press a key; upgrade to paid for a clean call flow
2. **Unverified numbers** cannot be called on a Twilio trial account; verify in the Twilio console or upgrade
3. **Groq Whisper** has a ~25 MB file size limit per request
4. **Energy-based VAD** may occasionally misdetect speech in noisy environments; the threshold is tunable in `audio_utils.py`
5. **DTMF over real calls** is not yet implemented (only supported in simulated mode)
6. **Single concurrent call** per session; no parallel call support
7. **ngrok URL changes** on restart (local dev only) -- update `PUBLIC_BASE_URL` each time; a fixed domain avoids this

---

## Future Enhancements

- DTMF tone injection for real Twilio calls
- Streaming STT for lower latency (when Groq supports it)
- Redis-backed persistent state for multi-instance deployments
- User authentication and persistent profiles
- Multi-language support (Whisper supports 100+ languages)
- Call recording and playback
- Analytics dashboard for call metrics
- Support for multiple simultaneous calls

---

## License

MIT -- see [LICENSE](LICENSE)
