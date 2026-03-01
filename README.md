# Quiz Buttons 2026

A wireless quiz buzzer system built with **ESP32-S3** hardware buttons and a **Raspberry Pi** web-based game controller.
Players press physical buttons to buzz in; the server tracks press order, manages scores, rounds, jokers, and runs a live web dashboard for the quizmaster.

---

## Table of Contents

- [System Overview](#system-overview)
- [Hardware](#hardware)
  - [Components](#components)
  - [Wiring](#wiring)
- [Software Architecture](#software-architecture)
- [Raspberry Pi Setup](#raspberry-pi-setup)
  - [Dependencies](#dependencies)
  - [Running as a Service](#running-as-a-service)
- [ESP32 Firmware](#esp32-firmware)
  - [PlatformIO Build](#platformio-build)
  - [Configuration](#configuration)
  - [OTA Firmware Updates](#ota-firmware-updates)
- [Network Setup](#network-setup)
- [Game Flow](#game-flow)
- [LED Color Reference](#led-color-reference)
- [Buzzer Patterns](#buzzer-patterns)
- [MQTT Topics](#mqtt-topics)
- [Web Interface](#web-interface)
  - [Results Page](#results-page-http1922)
  - [Score Overview](#score-overview-http1922score)
  - [Setup Page](#setup-page-http1922setup)
- [File Structure](#file-structure)
- [Versioning](#versioning)

---

## System Overview

```
┌─────────────────────────────────────────────────────┐
│              Wi-Fi Network (192.168.0.x)            │
│                                                     │
│  ┌─────────────┐   MQTT    ┌─────────────────────┐  │
│  │  ESP32-S3   │◄─────────►│  Raspberry Pi 4B    │  │
│  │  Button #1  │           │  - Mosquitto MQTT   │  │
│  └─────────────┘           │  - Flask Web Server │  │
│  ┌─────────────┐   MQTT    │  - Python Controller│  │
│  │  ESP32-S3   │◄─────────►│                     │  │
│  │  Button #2  │           │  Port 5000 (HTTP)   │  │
│  └─────────────┘           └──────────┬──────────┘  │
│        ...                            │             │
└───────────────────────────────────────┼─────────────┘
                                        │
                              ┌─────────▼──────────┐
                              │  Quizmaster Browser │
                              │  (any device on LAN)│
                              └────────────────────┘
```

Each ESP32-S3 button connects to the Raspberry Pi via **MQTT** over Wi-Fi.
The Pi runs a **Flask** web server that the quizmaster uses to manage the game.

---

## Hardware

### Components

| Component | Part | Notes |
|---|---|---|
| Microcontroller | Seeed XIAO ESP32-S3 | One per player |
| Button | Momentary push button | Connects D0 (GPIO1) to GND |
| Buzzer | Active buzzer or piezo | Connects D1 (GPIO2) to GND |
| Status LED | Single LED | D2 (GPIO3), MQTT connection indicator |
| RGB LED | Common cathode RGB LED | D8/D9/D10 (GPIO7/8/9) |
| Battery | 3.7V 10000mAh 1260110 Li-ion Polymer Battery | Rechargeable LiPo, one per button |
| Charger / Boost | LX-LCBST TP4056 Module | Li-ion charger + DC-DC step-up boost (3.7V → 5V for ESP32) |
| Server | Raspberry Pi 4B | Runs MQTT broker + Flask |
| Buzzer (Pi) | Active buzzer | GPIO17, confirms quizmaster actions |

### Wiring

#### ESP32-S3 (Seeed XIAO ESP32-S3)

| Pin | GPIO | Function | Resistor |
|---|---|---|---|
| D0 | GPIO 1 | Button (INPUT_PULLUP → GND) | — |
| D1 | GPIO 2 | Active buzzer (→ GND) | 47 Ω |
| D2 | GPIO 3 | Status LED (solid = MQTT connected, flash = disconnected) | 47 Ω |
| D8 | GPIO 7 | RED LED (PWM) | 100 Ω |
| D9 | GPIO 8 | GREEN LED (PWM) | 47 Ω |
| D10 | GPIO 9 | BLUE LED (PWM) | 47 Ω |

#### Raspberry Pi 4B

| GPIO | Function |
|---|---|
| GPIO 17 | Active buzzer (quizmaster feedback) |

### 3D Printed Enclosure

The button housing is designed in **FreeCAD** and printed in 5 parts.
Source and print files are in the [`3D Printer Files/`](3D%20Printer%20Files/) folder.

| File | Description |
|---|---|
| `QuizButton2026.FCStd` | FreeCAD source file (full assembly) |
| `QuizButton2026-PartQB2026MainV3.3mf` | Main body |
| `QuizButton2026-PartQB2026Top.3mf` | Top cover |
| `QuizButton2026-PartQB2026Bottom.3mf` | Bottom plate |
| `QuizButton2026-PartQB2026AccuBracket.3mf` | Battery bracket |
| `QuizButton2026-PartQB2026AccuSupport.3mf` | Battery support |

### ESP32 Power Supply

Each button unit is powered by a LiPo battery with an integrated charger and boost converter:

| Part | Description |
|---|---|
| 3.7V 10000mAh 1260110 Li-ion Polymer Battery | Rechargeable LiPo battery |
| LX-LCBST TP4056 Module | Li-ion charger + DC-DC step-up boost converter (charges via USB, boosts 3.7V → 5V for ESP32) |

#### Battery Life Estimate

| Scenario | Draw | Estimated Runtime |
|---|---|---|
| Peak (WiFi TX bursts) | 125 mA | ~68 hours |
| Average (WiFi idle) | ~70 mA | ~120 hours (~5 days) |

Assumptions: boost converter efficiency 85–90% → usable capacity ≈ 8,500 mAh; LiPo discharged to 3.0V cutoff.
For a typical quiz evening of a few hours, the 10,000 mAh battery will last **dozens of sessions** between charges.

---

## Software Architecture

```
raspberry_pi/
├── server.py                  # Flask + MQTT controller (main application)
├── config.json                # Timeout, rounds, questions per round
├── player_names.json          # Button ID → player/team name mapping
├── scores.json                # Per-round scores per player
├── correct_answers.json       # Correct answer count per round per player
├── jokers.json                # Joker usage per player
├── current_round.json         # Persisted current round number
├── round_descriptions.json    # Round descriptions (e.g. "Music Round")
├── static/
│   └── style.css
└── templates/
    ├── results.html           # Main quizmaster dashboard (auto-refreshes)
    ├── score.html             # Score overview with chart
    ├── setup.html             # Game configuration + OTA
    └── restart.html           # Shown during server restart

src/
└── main.cpp                   # ESP32-S3 Arduino firmware (PlatformIO)

platformio.ini                 # PlatformIO build configuration
```

---

## Raspberry Pi Setup

### Dependencies

```bash
sudo apt update && sudo apt install -y mosquitto mosquitto-clients python3-pip python3-venv

# Create virtual environment
python3 -m venv /home/game/quiz/env
source /home/game/quiz/env/bin/activate

pip install flask paho-mqtt gpiozero matplotlib
```

### Deploy Files

```bash
# Copy project files to Pi
scp -P 2222 -r raspberry_pi/* game@<pi-ip>:/home/game/quiz/
```

### Running as a Service

Create `/etc/systemd/system/quiz.service`:

```ini
[Unit]
Description=Quiz Server
After=network.target mosquitto.service

[Service]
User=game
WorkingDirectory=/home/game/quiz
ExecStart=/home/game/quiz/env/bin/python3 /home/game/quiz/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable quiz
sudo systemctl start quiz
sudo systemctl status quiz
```

### Mosquitto MQTT Broker

The MQTT broker runs locally on the Pi on port `1883`.
The ESP32 buttons connect using credentials:

```
Username: quizuser
Password: quizpass
```

Configure `/etc/mosquitto/mosquitto.conf` to allow these credentials.

---

## ESP32 Firmware

### PlatformIO Build

```bash
# Install PlatformIO (VS Code extension or CLI)
pio run                        # Build
pio run --target upload        # Flash via USB (COM4)
```

Board: `seeed_xiao_esp32s3`
Framework: Arduino
Library: `knolleary/PubSubClient@^2.8`

### Configuration

Edit the constants at the top of [src/main.cpp](src/main.cpp):

```cpp
const char* version    = "v0.8 (01-03-2026)";
const char* ssid       = "your-wifi-ssid";
const char* password   = "your-wifi-password";
const char* mqttServer = "192.168.0.10";   // Raspberry Pi IP
const int   mqttPort   = 1883;
const char* mqttUser   = "quizuser";
const char* mqttPass   = "quizpass";
```

### OTA Firmware Updates

The ESP32 supports **HTTP pull-based OTA** — the device downloads firmware from the Pi's Flask server when triggered via MQTT.

**Process:**
1. Build the firmware: `pio run`
2. Copy `.pio/build/seeed_xiao_esp32s3/firmware.bin` to the project
3. Upload via the Setup page → **"Uploaden naar Pi"**
4. Click **"OTA Update — Alle Knoppen"** (all buttons) or per-button OTA
5. Buttons turn **purple** (downloading) → **green** (done) → reboot

The firmware binary is served at `http://<pi-ip>:5000/firmware.bin`.

---

## Network Setup

| Device | IP Address | Notes |
|---|---|---|
| Raspberry Pi 4B | `192.168.0.10` | MQTT broker + Flask server |
| ESP32 Buttons | `192.168.0.x` | DHCP, same subnet as Pi |
| Quizmaster PC | `10.10.8.x` | Different subnet, accesses Pi via port 5000 |

> The ESP32 buttons are on a dedicated `192.168.0.0/24` network. OTA uses HTTP pull from the Pi so the buttons can reach the server even when the quizmaster's PC cannot directly reach the buttons.

---

## Game Flow

```
Server start → Round 1, Question 1
     │
     ▼
[Joker phase] ← Only at round start, before first question
     │           Each player may use their joker once (doubles points for that round)
     ▼
"Start Vraag" (Blue button) → All buttons ENABLED (green LED)
     │
     ▼
Players press buttons → Press order recorded
     │                   Rank colors assigned (Blue=1st, Cyan=2nd, Yellow=3rd, White=rest)
     ▼
Countdown timer starts (configurable, default 30s)
     │
     ▼
1st player answers:
  ├── Correct → Award 1/2/3 points → "Volgende Vraag" / "Volgende Ronde"
  └── Wrong   → "Fout (Blokeren)" → 2nd player answers → ...
                                          └── All wrong → "Reset Ronde (geen punten)"
                                                          (wrong-answer buttons stay red,
                                                           unanswered buttons re-enabled)
     │
     ▼
After all questions in round → "Volgende Ronde" (cyan button)
     │
     ▼
After all rounds → "Quiz compleet!"
```

---

## LED Color Reference

### RGB LED (game state)

| Color | Meaning |
|---|---|
| Green | Button enabled / waiting for question |
| Red | Button disabled / locked |
| Blue | Rank 1 (buzzed in first) |
| Purple | Rank 2 (buzzed in second) |
| Yellow | Rank 3 (buzzed in third) |
| White | Rank 4+ (buzzed in but not top 3) |
| Purple | OTA firmware downloading |

**Startup sequence:**
Green (init) → Red (WiFi connected) → White (NTP synced) → Blue (MQTT ready) + 1s beep

### Status LED (D2 / GPIO3)

| State | Meaning |
|---|---|
| Solid ON | MQTT connected |
| Flashing (500ms) | MQTT disconnected, retrying every 5 s |

---

## Buzzer Patterns

| Pattern | Beeps | Trigger |
|---|---|---|
| PAT_ANSWER | 1 beep (200ms) | Your buzz acknowledged |
| PAT_RESET | 2 beeps (150ms each) | Round reset / button unlocked |
| PAT_DISABLE | 3 beeps (130ms each) | Button locked / disabled |

---

## MQTT Topics

| Topic | Direction | Payload | Description |
|---|---|---|---|
| `quiz/all` | Pi → Buttons | `enable`, `disable`, `lock`, `unlock`, `reset`, `reregister`, `ota` | Broadcast command to all buttons |
| `quiz/<id>` | Pi → Button | `enable`, `disable`, `buzz`, `rank:1`…`rank:4`, `ota` | Command to specific button |
| `quiz/register` | Button → Pi | `<button-id>` | Button announces itself on connect |
| `quiz/version` | Button → Pi | `<id>,<version>,<ip>` | Firmware version + IP report |
| `quiz/press` | Button → Pi | `<id>,<timestamp-ms>` | Button press event |
| `quiz/heartbeat` | Button → Pi | `<button-id>` | Keep-alive every 5 seconds |
| `quiz/offline` | Button → Pi (LWT) | `<button-id>` | Last-will message on disconnect |

Button IDs are the ESP32 MAC address formatted as a 16-character uppercase hex string.

---

## Web Interface

### Results Page (`http://<pi-ip>:5000/`)

The main quizmaster dashboard. Auto-refreshes every 2 seconds.

- Shows all registered buttons sorted by name (then by press order during a question)
- Displays press rank, speed (ms relative to first press), status, score, correct answers, joker
- Buttons: Start Question, Next Question, Next Round, Award Points (1/2/3), Block Wrong Answer, Reset Round, Reset Scores, Find All Buttons
- Countdown timer during answer phase

### Score Overview (`http://<pi-ip>:5000/score`)

- Sortable score table with per-round breakdown (points + correct answers)
- Cumulative score graph per round (matplotlib PNG)
- Joker usage shown per player

### Setup Page (`http://<pi-ip>:5000/setup`)

| Section | Setting |
|---|---|
| 1 | Answer timeout (15 / 20 / 25 / 30 / 40 / 60 seconds) |
| 2 | Total number of rounds |
| 3 | Number of questions per round |
| 4 | Round descriptions (e.g. "Music Round", "General Knowledge") |
| 5 | Player / team names and display colors per button |
| 6 | OTA firmware update (upload .bin + trigger update per button or all) |

Also: Restart server, Shutdown Pi, software version display.

---

## File Structure

```
Quiz Buttons 2026/
├── 3D Printer Files/
│   ├── QuizButton2026.FCStd      # FreeCAD source (full assembly)
│   ├── QuizButton2026-PartQB2026MainV3.3mf
│   ├── QuizButton2026-PartQB2026Top.3mf
│   ├── QuizButton2026-PartQB2026Bottom.3mf
│   ├── QuizButton2026-PartQB2026AccuBracket.3mf
│   └── QuizButton2026-PartQB2026AccuSupport.3mf
├── src/
│   └── main.cpp                  # ESP32-S3 Arduino firmware
├── raspberry_pi/
│   ├── server.py                 # Flask + MQTT quiz controller
│   ├── config.json               # Game settings
│   ├── player_names.json         # Button → name mapping
│   ├── player_colors.json        # Button → display color mapping
│   ├── scores.json               # Score data
│   ├── correct_answers.json      # Correct answer counts
│   ├── jokers.json               # Joker usage
│   ├── current_round.json        # Current round state
│   ├── round_descriptions.json   # Round labels
│   ├── static/
│   │   └── style.css
│   └── templates/
│       ├── results.html
│       ├── score.html
│       ├── setup.html
│       └── restart.html
├── platformio.ini                # PlatformIO config (seeed_xiao_esp32s3)
├── ...AI Info/
│   ├── Game rules.txt            # Game rules and flow description
│   └── OTA Upload Info.txt       # OTA setup notes
└── README.md
```

---

## Versioning

| Component | Version | File |
|---|---|---|
| ESP32 Firmware | v0.8 (01-03-2026) | `src/main.cpp` |
| Quiz Server | v1.0.5 (01-03-2026) | `raspberry_pi/server.py` |

Server version is displayed in the bottom-right corner of the Setup page.
Per-button firmware version is reported via MQTT on connect and shown in the OTA table on the Setup page.
