import subprocess
import sys
import logging
import os
import threading
import time
import json
from functools import wraps
import platform

# Disable all logging
logging.getLogger().setLevel(logging.ERROR)
log = logging.getLogger("werkzeug")
log.disabled = True
log.setLevel(logging.ERROR)


# Redirect stdout and stderr to devnull
class NullWriter:
    def write(self, s):
        pass

    def flush(self):
        pass


# Only redirect if not in debug mode
if not "--debug" in sys.argv:
    sys.stdout = NullWriter()
    sys.stderr = NullWriter()


def ensure_module_installed(module_name, package_name=None):
    """Ensures a module is installed, and imports it if available."""
    if package_name is None:
        package_name = module_name

    try:
        return __import__(module_name)
    except ImportError:
        subprocess.call([sys.executable, "-m", "pip", "install", package_name])
        return __import__(module_name)


# Import required modules
CORS = ensure_module_installed("flask_cors", "flask-cors").CORS
psutil = ensure_module_installed("psutil")
requests = ensure_module_installed("requests")
flask = ensure_module_installed("flask")
Flask = flask.Flask
request = flask.request
jsonify = flask.jsonify

app = Flask(__name__)
CORS(
    app,
    origins=[
        "https://turbowarp.org",
        "https://origin.mistium.com",
        "http://localhost:5001",
    ],
)

# Add a global cache for system metrics
system_metrics_cache = {"cpu_percent": 0, "last_update": 0, "updating": False}


def background_metrics_updater():
    """Update system metrics in the background"""
    while True:
        # Update CPU percent with interval=0 for a snapshot
        system_metrics_cache["cpu_percent"] = psutil.cpu_percent(interval=1)
        system_metrics_cache["last_update"] = time.time()
        time.sleep(4)  # Update every 5 seconds (1s for measuring + 4s sleep)


# Start the background metrics updater thread
metrics_thread = threading.Thread(target=background_metrics_updater, daemon=True)
metrics_thread.start()


def cache_result(timeout=5):
    """Cache function results for a specified timeout period"""

    def decorator(func):
        cache = {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            key = json.dumps((args, sorted(kwargs.items())))

            if key in cache and now - cache[key]["timestamp"] < timeout:
                return cache[key]["result"]

            result = func(*args, **kwargs)
            cache[key] = {"result": result, "timestamp": now}
            return result

        return wrapper

    return decorator


def isAllowed(request):
    origin = request.headers.get("Origin", "")
    client_ip = request.remote_addr

    # Removed print statement to disable logging

    allowed_origins = ["https://origin.mistium.com", "https://turbowarp.org"]
    is_local = client_ip == "127.0.0.1" or client_ip == "::1"
    return (
        any(allowed_domain in origin for allowed_domain in allowed_origins) or is_local
    )


@app.route("/rotur", methods=["GET"])
def ping():
    return "true", 200


@app.route("/run", methods=["GET"])
def run():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    command = request.args.get("command")
    if not command:
        return {"status": "error", "message": "No command provided"}, 400

    try:
        process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()

        return {
            "status": "success",
            "data": {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode,
            },
        }, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route("/sysobj", methods=["GET"])
@cache_result(timeout=1)  # Cache results for 1 second
def sysObj():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    try:
        # Get memory info once to avoid multiple system calls
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        network = psutil.net_io_counters()

        return {
            "status": "success",
            "data": {
                "cpu": {
                    "count": psutil.cpu_count(),
                    "physical_count": psutil.cpu_count(logical=False),
                    "percent": system_metrics_cache["cpu_percent"],
                    "brand": platform.processor(),
                },
                "memory": {
                    "total": memory.total,
                    "available": memory.available,
                    "percent": memory.percent,
                    "used": memory.used,
                    "free": memory.free,
                },
                "disk": {
                    "total": disk.total,
                    "used": disk.used,
                    "free": disk.free,
                    "percent": disk.percent,
                },
                "network": {
                    "sent": network.bytes_sent,
                    "received": network.bytes_recv,
                    "packets_sent": network.packets_sent,
                    "packets_received": network.packets_recv,
                },
                "battery": psutil.sensors_battery(),
                "timestamp": time.time(),
            },
        }, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route("/eval", methods=["GET"])
def eval_code():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    code = request.args.get("code")
    if not code:
        return {"status": "error", "message": "No code provided"}, 400

    try:
        result = eval(code)
        return {"status": "success", "return": result}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route(
    "/proxy", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
def proxy():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    url = request.args.get("url")
    if not url:
        return {"status": "error", "message": "No URL provided"}, 400

    try:
        method = request.method

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in ["host"]
        }

        data = request.get_data()
        json_data = request.json if request.is_json else None

        params = request.args.to_dict()
        params.pop("url", None)

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            data=data if not json_data else None,
            json=json_data,
            cookies=request.cookies,
            allow_redirects=True,
        )

        return (
            response.text,
            response.status_code,
            {key: value for key, value in response.headers.items()},
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route("/sysinfo", methods=["GET"])
@cache_result(timeout=30)  # Cache results for 30 seconds
def system_info():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    try:
        # System and platform info
        system_info = {
            "python": {
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
                "compiler": platform.python_compiler(),
            },
            "platform": {
                "system": platform.system(),
                "node": platform.node(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "architecture": platform.machine(),
            },
            "hardware": {
                "processor": platform.processor(),
                "cpu_cores": psutil.cpu_count(logical=False),
                "cpu_threads": psutil.cpu_count(logical=True),
                "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
            },
            "hostname": platform.node(),
            "ip_addresses": [
                addr.address
                for iface in psutil.net_if_addrs().values()
                for addr in iface
                if addr.family == 2
            ][:2],
            "environment": {
                "user": os.getenv("USER") or os.getenv("USERNAME"),
                "home": os.path.expanduser("~"),
                "path_separator": os.path.sep,
                "line_separator": os.linesep,
            },
        }

        return {"status": "success", "data": system_info}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


if __name__ == "__main__":
    # Silence Flask's startup messages completely
    cli = sys.modules["flask.cli"]
    cli.show_server_banner = lambda *x: None

    # Run the Flask app with all logging disabled
    app.run(host="127.0.0.1", port=5001, debug=False)
