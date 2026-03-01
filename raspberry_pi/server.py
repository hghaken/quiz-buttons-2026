import paho.mqtt.client as mqtt
import threading
import time
import json
import os
from flask import Flask, render_template, request, redirect, url_for, send_file
from gpiozero import OutputDevice
import statistics
from paho.mqtt.client import CallbackAPIVersion
import matplotlib.pyplot as plt
import io
import base64

# =========================================================================== VERSIE & CONFIG =====
VERSION = "v1.0.5 (01-03-2026)"
BUZZER_PIN = 17

# Bestandspaden
CONFIG_FILE          = '/home/game/quiz/config.json'
PLAYER_NAMES_FILE    = '/home/game/quiz/player_names.json'
PLAYER_COLORS_FILE   = '/home/game/quiz/player_colors.json'
SCORES_FILE          = '/home/game/quiz/scores.json'
CURRENT_ROUND_FILE   = '/home/game/quiz/current_round.json'
ROUND_DESC_FILE      = '/home/game/quiz/round_descriptions.json'
JOKERS_FILE          = '/home/game/quiz/jokers.json'
CORRECT_ANSWERS_FILE = '/home/game/quiz/correct_answers.json'


# =========================================================================== BESTAND HELPERS =====
def load_json(filepath, default):
    """Load a JSON file, return default if it doesn't exist."""
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    """Save data to a JSON file."""
    with open(filepath, 'w') as f:
        json.dump(data, f)


# =========================================================================== DATA LADEN =====
config             = load_json(CONFIG_FILE, {})
player_names       = load_json(PLAYER_NAMES_FILE, {})
player_colors      = load_json(PLAYER_COLORS_FILE, {})
scores             = load_json(SCORES_FILE, {})
round_descriptions = load_json(ROUND_DESC_FILE, {})
correct_answers    = load_json(CORRECT_ANSWERS_FILE, {})
jokers             = load_json(JOKERS_FILE, {})

ANSWER_TIMEOUT      = config.get('answer_timeout', 30)
TOTAL_ROUNDS        = config.get('total_rounds', 10)
QUESTIONS_PER_ROUND = config.get('questions_per_round', 10)

try:
    current_round = load_json(CURRENT_ROUND_FILE, {}).get('current_round', 1)
    print(f"Loaded current round: {current_round}")
except Exception:
    current_round = 1
    print("No current_round.json - using default 1")


# =========================================================================== SAVE FUNCTIES =====
def save_scores():
    save_json(SCORES_FILE, scores)

def save_current_round():
    save_json(CURRENT_ROUND_FILE, {'current_round': current_round})

def save_round_descriptions():
    save_json(ROUND_DESC_FILE, round_descriptions)

def save_jokers():
    save_json(JOKERS_FILE, jokers)

def save_correct_answers():
    save_json(CORRECT_ANSWERS_FILE, correct_answers)

def save_player_colors():
    save_json(PLAYER_COLORS_FILE, player_colors)

def save_config():
    save_json(CONFIG_FILE, {
        'answer_timeout': ANSWER_TIMEOUT,
        'total_rounds': TOTAL_ROUNDS,
        'questions_per_round': QUESTIONS_PER_ROUND
    })

def load_jokers():
    global jokers
    jokers = load_json(JOKERS_FILE, {})

load_jokers()


# =========================================================================== FLASK APP =====
app = Flask(__name__)


# =========================================================================== GLOBALS =====
registered = set()
presses = []
disabled = set()
latencies = []
last_heartbeat = {}
button_versions = {}
button_ips = {}
lock = threading.RLock()
OFFLINE_TIMEOUT = 15
answer_end_time = None
timer_thread = None
current_questions_completed = 0
current_question_started = False
current_question_has_winner = False
buzzer = OutputDevice(BUZZER_PIN)


# =========================================================================== HULPFUNCTIES =====
def buzz_quizmaster(duration=0.5):
    def buzz_thread():
        buzzer.on()
        time.sleep(duration)
        buzzer.off()
    threading.Thread(target=buzz_thread).start()

def get_current_answerer():
    for ts, id in presses:
        if id not in disabled:
            return id
    return None

def reset_timer():
    global answer_end_time, timer_thread
    answer_end_time = None
    if timer_thread and timer_thread.is_alive():
        timer_thread.join(timeout=1)
    timer_thread = None

def lock_buttons():
    mqtt_client.publish("quiz/all", "lock")

def unlock_buttons():
    mqtt_client.publish("quiz/all", "unlock")

def start_answer_timer():
    global answer_end_time, timer_thread
    if ANSWER_TIMEOUT == 0:
        print("Answer timer disabled (timeout = 0)")
        answer_end_time = None
        return
    print(f"Starting answer timer for {ANSWER_TIMEOUT} seconds")
    answer_end_time = time.time() + ANSWER_TIMEOUT
    def timer_func():
        time.sleep(ANSWER_TIMEOUT)
        with lock:
            if presses:
                print("Timeout reached - buzzing")
                buzz_quizmaster(1.5)
    timer_thread = threading.Thread(target=timer_func, daemon=True)
    timer_thread.start()

def process_presses():
    print("Processing presses called")
    with lock:
        current = get_current_answerer()
        if current:
            print(f"Current answerer found: {current} - buzzing and starting timer")
            mqtt_client.publish(f"quiz/{current}", "buzz", qos=1)
            buzz_quizmaster()
            start_answer_timer()
        else:
            print("No current answerer - timer not started")

def clear_round_state():
    """Reset presses, disabled and latencies."""
    with lock:
        presses.clear()
        disabled.clear()
        latencies.clear()


# =========================================================================== MQTT =====
def on_connect(client, userdata, flags, rc, properties=None):
    print("Connected to MQTT")
    client.subscribe("quiz/#")
    client.publish("quiz/all", "disable")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode()
    print(f"Received {topic}: {payload}")

    if topic == "quiz/register":
        with lock:
            if payload not in registered:
                registered.add(payload)
                last_heartbeat[payload] = time.time()
                if payload not in scores:
                    scores[payload] = {}
                if payload not in jokers:
                    jokers[payload] = None
                    save_jokers()

    elif topic == "quiz/press":
        with lock:
            parts = payload.split(',')
            if len(parts) == 2:
                id = parts[0]
                try:
                    sent_ts = int(parts[1])
                    recv_ts = int(time.time() * 1000)
                    latency = recv_ts - sent_ts
                    if 0 < latency < 10000:
                        latencies.append(latency)
                        print(f"Latency for {id}: {latency} ms")
                except ValueError:
                    print("Invalid timestamp in payload")

                if id in registered and id not in disabled and id not in [p[1] for p in presses]:
                    presses.append((time.time(), id))
                    presses.sort()
                    # Send rank colors immediately as buttons press
                    press_ids = [p[1] for p in presses]
                    for i, btn_id in enumerate(press_ids):
                        mqtt_client.publish(f"quiz/{btn_id}", f"rank:{i + 1}")
                    if len([p for p in presses if p[1] not in disabled]) == 1:
                        process_presses()

    elif topic == "quiz/version":
        parts = payload.split(',', 2)
        if len(parts) >= 2:
            with lock:
                button_versions[parts[0]] = parts[1]
                if len(parts) == 3:
                    button_ips[parts[0]] = parts[2]

    elif topic == "quiz/heartbeat":
        with lock:
            if payload in registered:
                last_heartbeat[payload] = time.time()
                print(f"Heartbeat from {payload}")

    elif topic == "quiz/offline":
        with lock:
            if payload in registered:
                last_heartbeat[payload] = 0
                print(f"Offline detected (LWT): {payload}")

mqtt_client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect("localhost", 1883, 60)

def mqtt_loop():
    mqtt_client.loop_forever()


# =========================================================================== ROUTES =====
##### RESULTS.HTML #####
@app.route('/')
def results():
    with lock:
        reg_list = list(registered)
        press_order = [p[1] for p in presses if p[1] not in disabled]
        current_time = time.time()
        offline_status = {id: (current_time - last_heartbeat.get(id, 0) > OFFLINE_TIMEOUT) for id in reg_list}

        if latencies:
            avg_latency = statistics.mean(latencies)
            min_latency = min(latencies)
            max_latency = max(latencies)
        else:
            avg_latency = min_latency = max_latency = 0

        if presses and get_current_answerer() and answer_end_time:
            remaining_time = max(0, int(answer_end_time - current_time))
        else:
            remaining_time = 0

        player_totals = {id: sum(scores.get(id, {}).values()) for id in reg_list}
        press_timestamps = {p[1]: p[0] for p in presses}
        sorted_reg_list = [id for id in press_order if id in reg_list] + sorted([id for id in reg_list if id not in press_order], key=lambda id: player_names.get(id, id).lower())

    return render_template('results.html',
                           registered=sorted_reg_list,
                           disabled=disabled,
                           press_order=press_order,
                           avg_latency=avg_latency,
                           min_latency=min_latency,
                           max_latency=max_latency,
                           offline_status=offline_status,
                           player_names=player_names,
                           player_colors=player_colors,
                           player_totals=player_totals,
                           press_timestamps=press_timestamps,
                           remaining_time=remaining_time,
                           presses=presses,
                           current_round=current_round,
                           total_rounds=TOTAL_ROUNDS,
                           round_descriptions=round_descriptions,
                           jokers=jokers,
                           current_questions_completed=current_questions_completed,
                           questions_per_round=QUESTIONS_PER_ROUND,
                           current_question_started=current_question_started,
                           current_question_has_winner=current_question_has_winner,
                           correct_answers=correct_answers)

##### RESULTS.HTML - BUTTON FOUT (BlOKKEREN) #####
@app.route('/disable/<id>', methods=['POST'])
def disable(id):
    global answer_end_time
    with lock:
        if id not in disabled:
            disabled.add(id)
            presses[:] = [p for p in presses if p[1] != id]
            mqtt_client.publish(f"quiz/{id}", "disable")
            if not get_current_answerer():
                answer_end_time = None
            else:
                process_presses()
        reset_timer()
    return redirect(url_for('results'))

##### RESULTS.HTML - BUTTON PUNTEN TOEKENNEN #####
@app.route('/award/<id>', methods=['POST'])
def award(id):
    global answer_end_time, current_question_has_winner
    with lock:
        current = get_current_answerer()
        if current == id:
            points = int(request.form.get('points', 0))
            if points in [1, 2, 3]:
                buzz_quizmaster(0.1)
                round_str = str(current_round)
                if id not in scores:
                    scores[id] = {}
                if id not in correct_answers:
                    correct_answers[id] = {}
                multiplier = 2 if jokers.get(id) == round_str else 1
                scores[id][round_str] = scores[id].get(round_str, 0) + (points * multiplier)
                correct_answers[id][round_str] = correct_answers[id].get(round_str, 0) + 1
                print(f"Incrementing correct for {id} in round {round_str}")
                save_scores()
                save_correct_answers()
                reset_timer()
                current_question_has_winner = True
                # Send white to buttons that never pressed
                press_ids = [p[1] for p in presses]
                for btn_id in registered:
                    if btn_id not in press_ids:
                        mqtt_client.publish(f"quiz/{btn_id}", "rank:4")  # White: did not press
        presses.clear()
        disabled.clear()
        answer_end_time = None
    return redirect(url_for('results'))

##### RESULTS.HTML - BUTTON JOKER INZETTEN #####
@app.route('/set_joker/<id>', methods=['POST'])
def set_joker(id):
    with lock:
        if jokers.get(id) is None and current_questions_completed == 0 and not current_question_started:
            buzz_quizmaster(0.1)
            jokers[id] = str(current_round)
            save_jokers()
    return redirect(url_for('results'))

##### RESULTS.HTML - BUTTON VOLGENDE RONDE #####
@app.route('/increment_round', methods=['POST'])
def increment_round():
    global current_round, current_questions_completed, current_question_started, current_question_has_winner
    current_round = min(current_round + 1, TOTAL_ROUNDS)
    current_questions_completed = 0
    current_question_started = False
    current_question_has_winner = False
    lock_buttons()
    clear_round_state()
    save_current_round()
    return redirect(url_for('results'))

##### RESULTS.HTML - BUTTON START VRAAG #####
@app.route('/start_question', methods=['POST'])
def start_question():
    global current_question_started, current_question_has_winner
    current_question_started = True
    current_question_has_winner = False
    unlock_buttons()
    return redirect(url_for('results'))

##### RESULTS.HTML - BUTTON VOLGENDE VRAAG #####
@app.route('/next_question', methods=['POST'])
def next_question():
    global current_questions_completed, current_question_started, current_question_has_winner
    current_questions_completed = min(current_questions_completed + 1, QUESTIONS_PER_ROUND)
    current_question_started = False
    current_question_has_winner = False
    lock_buttons()
    clear_round_state()
    return redirect(url_for('results'))

#############################################

##### RESULTS.HTML - BUTTON RESET RONDE (GEEN PUNTEN) #####
@app.route('/reset', methods=['POST'])
def reset():
    global answer_end_time, current_question_has_winner
    buzz_quizmaster(0.1)
    with lock:
        presses.clear()
        latencies.clear()
        answer_end_time = None
        current_question_has_winner = False
        # Re-enable only buttons that did NOT answer wrong; wrong-answer buttons stay disabled (red)
        for btn_id in registered:
            if btn_id not in disabled:
                mqtt_client.publish(f"quiz/{btn_id}", "enable")
    return redirect(url_for('results'))


##### RESULTS.HTML - RESET RONDE TELLER #####
@app.route('/reset_round', methods=['POST'])
def reset_round():
    global current_round, current_questions_completed, correct_answers
    buzz_quizmaster(0.1)
    load_jokers()
    with lock:
        current_round = 1
        current_questions_completed = 0
        for player_id in list(jokers.keys()):
            jokers[player_id] = None
        correct_answers = {id: {} for id in registered}
        save_correct_answers()
        save_jokers()
        save_current_round()
    return redirect(url_for('results'))


##### RESULTS.HTML - BUTTON RESET ALLE SCORES #####
@app.route('/reset_scores', methods=['POST'])
def reset_scores():
    global scores, correct_answers, jokers, current_questions_completed, current_round, current_question_started, current_question_has_winner, answer_end_time
    buzz_quizmaster(0.1)
    with lock:
        current_questions_completed = 0
        current_question_started = False
        current_question_has_winner = False
        scores = {}
        correct_answers = {}
        jokers = {}
        current_round = 1
        answer_end_time = None
        clear_round_state()
        unlock_buttons()
        save_scores()
        save_correct_answers()
        save_jokers()
        save_current_round()
    return redirect(url_for('results'))


##### RESULTS.HTML - ALLE KNOPPEN ZOEKEN #####
@app.route('/reregister', methods=['POST'])
def reregister():
    buzz_quizmaster(0.1)
    mqtt_client.publish("quiz/all", "reregister")
    time.sleep(1.5)
    lock_buttons()
    time.sleep(2.0)
    lock_buttons()
    return redirect(url_for('results'))

##### RESULTS.HTML - SCORE OVERZICHT #####
@app.route('/score')
def score_overview():
    load_jokers()
    with lock:
        reg_list = list(registered)
        player_totals = [(id, sum(scores.get(id, {}).values())) for id in reg_list]
        sorted_players = sorted(player_totals, key=lambda x: x[1], reverse=True)

    # Generate score graph
    fig, ax = plt.subplots(figsize=(10, 6))
    rounds = list(range(1, TOTAL_ROUNDS + 1))
    for id in reg_list:
        player_scores = [scores.get(id, {}).get(str(r), 0) for r in rounds]
        cumulative = [sum(player_scores[:i+1]) for i in range(len(player_scores))]
        ax.plot(rounds, cumulative, label=player_names.get(id, id))

    ax.set_xlabel('Round')
    ax.set_ylabel('Cumulative Score')
    ax.set_title('Round-by-Round Cumulative Scores')
    ax.legend()
    ax.grid(True)

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    graph_url = 'data:image/png;base64,' + base64.b64encode(buf.read()).decode('utf-8')
    plt.close(fig)

    return render_template('score.html',
                           sorted_players=sorted_players,
                           player_names=player_names,
                           scores=scores,
                           total_rounds=TOTAL_ROUNDS,
                           round_descriptions=round_descriptions,
                           graph_url=graph_url,
                           jokers=jokers,
                           correct_answers=correct_answers)

##### RESULTS.HTML - SETUP PAGINA #####
@app.route('/setup')
def setup_page():
    sorted_registered = sorted(registered, key=lambda id: player_names.get(id, id).lower())
    return render_template('setup.html',
                           current_timeout=ANSWER_TIMEOUT,
                           total_rounds=TOTAL_ROUNDS,
                           questions_per_round=QUESTIONS_PER_ROUND,
                           round_descriptions=round_descriptions,
                           registered=sorted_registered,
                           player_names=player_names,
                           player_colors=player_colors,
                           button_versions=button_versions,
                           button_ips=button_ips,
                           version=VERSION)


@app.route('/set_timeout', methods=['POST'])
def set_timeout():
    global ANSWER_TIMEOUT
    ANSWER_TIMEOUT = int(request.form.get('timeout', 30))
    save_config()
    return redirect(url_for('setup_page'))


@app.route('/set_total_rounds', methods=['POST'])
def set_total_rounds():
    global TOTAL_ROUNDS
    TOTAL_ROUNDS = int(request.form.get('total_rounds', 10))
    save_config()
    return redirect(url_for('setup_page'))


@app.route('/set_questions_per_round', methods=['POST'])
def set_questions_per_round():
    global QUESTIONS_PER_ROUND
    QUESTIONS_PER_ROUND = int(request.form.get('questions_per_round', 10))
    save_config()
    return redirect(url_for('setup_page'))


@app.route('/set_round_descriptions', methods=['POST'])
def set_round_descriptions():
    global round_descriptions
    round_descriptions = {}
    for i in range(1, TOTAL_ROUNDS + 1):
        desc = request.form.get(f'desc_{i}', '').strip()
        if desc:
            round_descriptions[str(i)] = desc
    save_round_descriptions()
    return redirect(url_for('setup_page'))


@app.route('/set_player_names', methods=['POST'])
def set_player_names():
    with lock:
        for id in registered:
            name = request.form.get(f'name_{id}', '').strip()
            if name:
                player_names[id] = name
            elif id in player_names:
                del player_names[id]
            color = request.form.get(f'color_{id}', '').strip()
            if color:
                player_colors[id] = color
    save_json(PLAYER_NAMES_FILE, player_names)
    save_player_colors()
    return redirect(url_for('setup_page'))


##### OTA - FIRMWARE SERVEREN #####
FIRMWARE_PATH = '/home/game/quiz/firmware.bin'

@app.route('/firmware.bin')
def serve_firmware():
    if not os.path.exists(FIRMWARE_PATH):
        return "Firmware not found", 404
    return send_file(FIRMWARE_PATH, mimetype='application/octet-stream')

@app.route('/upload_firmware', methods=['POST'])
def upload_firmware():
    f = request.files.get('firmware')
    if f and f.filename.endswith('.bin'):
        f.save(FIRMWARE_PATH)
    return redirect(url_for('setup_page'))

@app.route('/ota_update', methods=['POST'])
def ota_update():
    target = request.form.get('target', 'all')
    if target == 'all':
        mqtt_client.publish("quiz/all", "ota")
    else:
        mqtt_client.publish(f"quiz/{target}", "ota")
    return redirect(url_for('setup_page'))


@app.route('/restart', methods=['POST'])
def restart_server():
    def do_restart():
        time.sleep(1)
        os.system("sudo systemctl restart quiz")
    threading.Thread(target=do_restart).start()
    return render_template('restart.html')


@app.route('/shutdown', methods=['POST'])
def shutdown_server():
    os.system("sudo shutdown -h now")
    return redirect(url_for('results'))


# =========================================================================== MAIN =====
if __name__ == '__main__':
    # Reset alle tellers bij opstarten
    scores = {}
    correct_answers = {}
    jokers = {}
    current_round = 1
    save_scores()
    save_correct_answers()
    save_jokers()
    save_current_round()
    # Reset knoppen en Server
    buzz_quizmaster(0.1)
    mqtt_thread = threading.Thread(target=mqtt_loop)
    mqtt_thread.start()
    time.sleep(2.0)
    buzz_quizmaster(0.1)
    mqtt_client.publish("quiz/all", "reregister")
    time.sleep(1.0)
    lock_buttons()
    buzz_quizmaster(1)
    app.run(host='0.0.0.0', port=5000)

