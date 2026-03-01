// RGB LED states:
// setup():
//   GREEN   – Initialising
//   RED     – WiFi connected
//   WHITE   – NTP time synced
//   BLUE    – MQTT connected and ready  + long buzz
// loop():
//   GREEN   – Button enabled / unlocked
//   RED     – Button disabled / locked
//   BLUE    – Rank 1 (winner)
//   PURPLE  – Rank 2  (180,0,180 – distinct from OTA violet 128,0,255)
//   YELLOW  – Rank 3
//   WHITE   – Rank 4+
//   PURPLE  – OTA firmware downloading
//   GREEN   – OTA done → reboot
//   RED     – OTA error
//
// Status LED (D2):
//   ON      – MQTT connected
//   FLASH   – MQTT disconnected (retrying every 5 s)

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <Update.h>
#include <esp_mac.h>  // For ESP.getEfuseMac()
#include <time.h>     // For NTP sync

#define BUTTON_PIN     1  // D0 GPIO 1 Momentary switch, connect to GND (internal pull-up)
#define BUZZER_PIN     2  // D1 GPIO 2 Active buzzer or piezo, connect to GND
#define STATUS_LED_PIN 3  // D2 GPIO 3 Connection status LED (solid=connected, flash=disconnected)

#define RED_PIN   7   // D8  GPIO 7 PWM pin for RED LED
#define GREEN_PIN 8   // D9  GPIO 8 PWM pin for GREEN LED
#define BLUE_PIN  9   // D10 GPIO 9 PWM pin for BLUE LED

const char* version    = "v0.13 (01-03-2026)";
const char* ssid       = "gamecontroller2.4";
const char* password   = "gamecontroller";
const char* mqttServer = "192.168.0.10";       // MQTT Controller IP address RPI 4B
const int   mqttPort   = 1883;                 // Default MQTT port
const char* mqttUser   = "quizuser";           // MQTT username
const char* mqttPass   = "quizpass";           // MQTT password
const unsigned long hbInterval  = 5000;        // Heartbeat interval in ms
const int           keepAlive   = 3;           // MQTT keep-alive in seconds
const int           deBounceVal = 400;         // Button debounce in ms

// NTP settings
const char* ntpServer1        = "nl.pool.ntp.org";
const char* ntpServer2        = "time.nist.gov";
long        gmtOffset_sec     = 3600;          // UTC+1; adjust for timezone
int         daylightOffset_sec = 0;

// MQTT client and topics
WiFiClient   espClient;
PubSubClient client(espClient);

String myId;
String allTopic;
String myTopic;

bool          enabled         = false;  // Button starts disabled
bool          rankActive      = false;  // Suppress green/red override while rank color is shown
unsigned long lastHeartbeat   = 0;
unsigned long lastReconnectMs = 0;
unsigned long lastPress       = 0;
unsigned long blinkMs         = 0;
bool          blinkState      = false;


// ── LED (cached – skip PWM write when color is unchanged) ────────────────────

bool    ledReady = false;
uint8_t ledR, ledG, ledB;
float   brightnessR = 1.0f;   // Per-channel brightness scale (0.0 – 1.0)
float   brightnessG = 1.0f;
float   brightnessB = 1.0f;

void setColor(uint8_t r, uint8_t g, uint8_t b) {
  if (ledReady && r == ledR && g == ledG && b == ledB) return;
  ledR = r; ledG = g; ledB = b; ledReady = true;
  ledcWrite(0, (uint8_t)(r * brightnessR));  // channel 0 = RED_PIN
  ledcWrite(1, (uint8_t)(g * brightnessG));  // channel 1 = GREEN_PIN
  ledcWrite(2, (uint8_t)(b * brightnessB));  // channel 2 = BLUE_PIN
}


// ── BUZZER (non-blocking state machine) ──────────────────────────────────────
//
// Each pattern: alternating ON/OFF durations (ms), terminated by 0.
// Even indices (0,2,4…) = buzzer ON; odd indices = buzzer OFF.
//
//   PAT_ANSWER  {200, 0}                → 1 beep
//   PAT_RESET   {150, 150, 150, 0}      → 2 beeps
//   PAT_DISABLE {130,130,130,130,130,0} → 3 beeps

static const uint16_t PAT_ANSWER[]  = {200, 0};
static const uint16_t PAT_RESET[]   = {150, 150, 150, 0};
static const uint16_t PAT_DISABLE[] = {130, 130, 130, 130, 130, 0};

const uint16_t* buzPat  = nullptr;
int             buzStep = 0;
unsigned long   buzMs   = 0;

// ── Start Buzzer On ───────────────────────────
void startBuzz(const uint16_t* pat) {
  buzPat  = pat;
  buzStep = 0;
  buzMs   = millis();
  digitalWrite(BUZZER_PIN, HIGH);
}


// ── Update Buzzer (Non Blocking) ───────────────────────────
void updateBuzzer() {
  if (!buzPat) return;
  if (millis() - buzMs < (unsigned long)buzPat[buzStep]) return;

  buzStep++;
  buzMs = millis();

  if (buzPat[buzStep] == 0) {
    digitalWrite(BUZZER_PIN, LOW);
    buzPat = nullptr;
    return;
  }
  // Even step = ON, odd step = OFF
  digitalWrite(BUZZER_PIN, (buzStep % 2 == 0) ? HIGH : LOW);
}


// ── HTTP OTA (blocking – called from MQTT callback) ───────────────────────────
void performOTA() {
  client.disconnect();           // Clean MQTT disconnect before flashing
  buzPat = nullptr;              // Stop any active buzzer pattern
  digitalWrite(BUZZER_PIN, LOW);
  setColor(128, 0, 255);         // Purple: downloading

  String url = String("http://") + mqttServer + ":5000/firmware.bin";
  Serial.println("OTA: " + url);

  HTTPClient http;
  http.begin(url);
  int code = http.GET();

  if (code == 200) {
    int len = http.getSize();
    if (len <= 0) {
      Serial.println("OTA: no content-length");
      setColor(255, 0, 0);
      http.end();
      return;
    }
    WiFiClient* stream = http.getStreamPtr();
    if (Update.begin(len)) {
      if (Update.writeStream(*stream) == (size_t)len && Update.end(true)) {
        setColor(0, 255, 0);     // Green: done, rebooting
        http.end();
        delay(500);
        ESP.restart();
      } else {
        Update.printError(Serial);
        setColor(255, 0, 0);     // Red: flash error
      }
    } else {
      Update.printError(Serial);
      setColor(255, 0, 0);       // Red: not enough space
    }
  } else {
    Serial.println("OTA HTTP error: " + String(code));
    setColor(255, 0, 0);         // Red: server error
  }
  http.end();
}


// ── MQTT CALLBACK ─────────────────────────────────────────────────────────────
void callback(char* topic, byte* payload, unsigned int length) {
  // Pre-allocate to avoid String reallocation during append
  String msg;
  msg.reserve(length);
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  // strcmp avoids heap allocation from constructing String(topic) on each call
  if (strcmp(topic, allTopic.c_str()) == 0) {
    if (msg == "reset") {
      digitalWrite(BUZZER_PIN, HIGH); delay(200); digitalWrite(BUZZER_PIN, LOW);
      delay(100);
      ESP.restart();                  // Full reboot – same sequence as power-on
    } else if (msg == "reregister") {
      client.publish("quiz/register", myId.c_str());
      client.publish("quiz/version",  (myId + "," + String(version) + "," + WiFi.localIP().toString()).c_str());
    } else if (msg == "lock") {
      enabled = false;
      if (!rankActive) setColor(255, 0, 0);  // Red: locked (skip if rank color active)
      startBuzz(PAT_DISABLE);                // 3 beeps
    } else if (msg == "unlock") {
      rankActive = false;
      enabled = true;
      setColor(0, 255, 0);            // Green: unlocked
      startBuzz(PAT_RESET);           // 2 beeps
    } else if (msg == "ota") {
      performOTA();
    } else if (msg.startsWith("brightness:")) {
      String vals = msg.substring(11);
      int c1 = vals.indexOf(',');
      int c2 = vals.indexOf(',', c1 + 1);
      brightnessR = vals.substring(0, c1).toInt()       / 255.0f;
      brightnessG = vals.substring(c1 + 1, c2).toInt()  / 255.0f;
      brightnessB = vals.substring(c2 + 1).toInt()      / 255.0f;
      ledReady = false;  // Invalidate cache so setColor re-writes with new brightness
      setColor(ledR, ledG, ledB);
    }

  } else if (strcmp(topic, myTopic.c_str()) == 0) {
    if (msg == "buzz") {
      startBuzz(PAT_ANSWER);          // 1 beep
    } else if (msg == "disable") {
      rankActive = false;            // Always override rank color on explicit disable
      enabled = false;
      setColor(255, 0, 0);           // Red: disabled
      startBuzz(PAT_DISABLE);        // 3 beeps
    } else if (msg == "enable") {
      rankActive = false;
      enabled = true;
      setColor(0, 255, 0);            // Green: enabled
      startBuzz(PAT_RESET);           // 2 beeps
    } else if (msg == "ota") {
      performOTA();
    } else if (msg.startsWith("rank:")) {
      rankActive = true;              // Hold rank color until next enable/disable
      int rank = msg.substring(5).toInt();
      if      (rank == 1) setColor(0,   0,   255); // Blue   – rank 1
      else if (rank == 2) setColor(180,   0, 180); // Purple – rank 2
      else if (rank == 3) setColor(255, 255, 0);   // Yellow – rank 3
      else                setColor(255, 255, 255); // White  – rank > 3
    }
  }
}


// ── MQTT CONNECT (non-blocking helper) ────────────────────────────────────────
bool mqttConnect() {
  if (client.connect(myId.c_str(), "quiz/offline", 1, false, myId.c_str())) {
    client.subscribe(allTopic.c_str());
    client.subscribe(myTopic.c_str());
    client.publish("quiz/register", myId.c_str());
    client.publish("quiz/version",  (myId + "," + String(version) + "," + WiFi.localIP().toString()).c_str());
    setColor(0, 0, 255);  // Blue: connected
    digitalWrite(STATUS_LED_PIN, HIGH);  // Solid: connected
    return true;
  }
  return false;
}


// ── HELPERS ───────────────────────────────────────────────────────────────────
String getUniqueId() {
  uint64_t mac = ESP.getEfuseMac();
  char id[17];
  sprintf(id, "%016llX", mac);
  return String(id);  // ESP32 MAC address as unique ID
}


// ── SETUP ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, LOW);

  ledcSetup(0, 5000, 8); ledcAttachPin(RED_PIN,   0);  // channel 0 → RED
  ledcSetup(1, 5000, 8); ledcAttachPin(GREEN_PIN, 1);  // channel 1 → GREEN
  ledcSetup(2, 5000, 8); ledcAttachPin(BLUE_PIN,  2);  // channel 2 → BLUE

  // Init chirp + green
  digitalWrite(BUZZER_PIN, HIGH); delay(1); digitalWrite(BUZZER_PIN, LOW);
  setColor(0, 255, 0);  // Green: initialising
  delay(3000);

  myId     = getUniqueId();
  allTopic = "quiz/all";
  myTopic  = "quiz/" + myId;

  Serial.println("\n\n\n\n\n\n===================================");
  Serial.println("Firmware: " + String(version));
  Serial.println("ID:       " + myId);

  // WiFi – setAutoReconnect handles WiFi drops automatically
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(ssid, password);
  Serial.print("WiFi connecting to: " + String(ssid));
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nIP: " + WiFi.localIP().toString());
  setColor(255, 0, 0);  // Red: WiFi connected
  delay(3000);

  // NTP time sync
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer1, ntpServer2);
  struct tm timeinfo;
  Serial.print("NTP sync");
  while (!getLocalTime(&timeinfo)) { delay(500); Serial.print("."); }
  char buf[50];
  strftime(buf, sizeof(buf), "%A, %d %B %Y - %H:%M:%S", &timeinfo);
  Serial.println("\nTime: " + String(buf));
  setColor(255, 255, 255);  // White: time synced
  delay(3000);

  // MQTT – setKeepAlive once here, not inside the reconnect loop
  client.setServer(mqttServer, mqttPort);
  client.setKeepAlive(keepAlive);
  client.setCallback(callback);
  mqttConnect();  // Initial connect (blocking is fine in setup)

  // Ready beep (blocking delay is fine in setup)
  digitalWrite(BUZZER_PIN, HIGH); delay(1000); digitalWrite(BUZZER_PIN, LOW);
}


// ── MAIN LOOP ─────────────────────────────────────────────────────────────────
void loop() {
  updateBuzzer();  // Drive non-blocking buzzer state machine
  client.loop();

  if (!client.connected()) {
    // Flash status LED while disconnected
    if (millis() - blinkMs >= 500) {
      blinkMs    = millis();
      blinkState = !blinkState;
      digitalWrite(STATUS_LED_PIN, blinkState ? HIGH : LOW);
    }
    // Retry MQTT every 5 s – non-blocking, device stays fully responsive
    if (millis() - lastReconnectMs >= 5000) {
      lastReconnectMs = millis();
      mqttConnect();
    }
    return;  // Skip button/heartbeat while offline
  }

  // LED reflects button state – held on rank color until next enable/disable
  if (!rankActive) {
    setColor(enabled ? 0 : 255, enabled ? 255 : 0, 0);  // Green=enabled / Red=disabled
  }

  // Heartbeat
  if (millis() - lastHeartbeat >= hbInterval) {
    lastHeartbeat = millis();
    client.publish("quiz/heartbeat", myId.c_str());
  }

  // Button press
  if (enabled && digitalRead(BUTTON_PIN) == LOW && millis() - lastPress > (unsigned long)deBounceVal) {
    lastPress = millis();

    // Timestamp: prefer NTP unix time (ms), fall back to millis()
    unsigned long long pressTime;
    struct tm timeinfo;
    pressTime = getLocalTime(&timeinfo)
                  ? (unsigned long long)time(NULL) * 1000ULL
                  : (unsigned long long)millis();

    char pressStr[20];
    sprintf(pressStr, "%llu", pressTime);
    String payload = myId + "," + pressStr;

    if (client.publish("quiz/press", payload.c_str())) {
      enabled = false;  // Lock until reset/enable from server
    }
  }
}
