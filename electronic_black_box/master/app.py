from flask import Flask, jsonify, render_template, request
import threading
from modbus_worker import (
    start_modbus_worker, connections, presence,
    send_modbus_command, get_status
)

app = Flask(__name__, static_folder='static', template_folder='templates')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/data')
def data():
    status = get_status()
    return jsonify({
        "connections": connections,
        "presence": presence,
        "active": status["active"],
        "last_trigger_ts": status["last_trigger_ts"],
    })

@app.route('/command', methods=['POST'])
def command():
    data = request.get_json(force=True)
    mac = data.get("mac"); action = data.get("action")
    if not mac or not action:
        return jsonify({"error": "MAC oder Aktion fehlt"}), 400
    ok = send_modbus_command(mac, action)
    return (jsonify({"status": "ok"}) if ok
            else (jsonify({"error": "Fehler bei Modbus-Befehl"}), 500))

if __name__ == '__main__':
    threading.Thread(target=start_modbus_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5050, debug=True, use_reloader=False)
