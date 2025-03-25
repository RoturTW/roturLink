import subprocess, sys, logging, os, threading, time, json, platform, asyncio
from functools import wraps

# Disable all logging and redirecting stdout/stderr if not in debug mode
logging.getLogger().setLevel(logging.ERROR)
log = logging.getLogger("werkzeug")
log.disabled = True
log.setLevel(logging.ERROR)

class NullWriter:
    def write(self, s): pass
    def flush(self): pass

if "--debug" not in sys.argv:
    sys.stdout = sys.stderr = NullWriter()

def ensure_module_installed(module_name, package_name=None):
    try:
        return __import__(module_name)
    except ImportError:
        subprocess.call([sys.executable, "-m", "pip", "install", package_name or module_name])
        return __import__(module_name)

# Import required modules
CORS = ensure_module_installed("flask_cors", "flask-cors").CORS
psutil = ensure_module_installed("psutil")
requests = ensure_module_installed("requests")
flask = ensure_module_installed("flask")
websockets = ensure_module_installed("websockets")
from flask import Flask, request, jsonify

app = Flask(__name__)
CORS(app, origins=["https://turbowarp.org", "https://origin.mistium.com", 
                  "http://localhost:5001", "http://localhost:5002"])

# System metrics cache with optimized data collection
system_metrics_cache = {
    "cpu_percent": 0, "cpu_percent_per_core": [], "last_update": 0,
    "updating": False, "last_detailed_update": 0, "memory": {},
    "disk": {}, "network": {}, "battery": {}
}

# WebSocket connection management
connected_clients = set()
HEARTBEAT_INTERVAL = 10  # seconds

def get_system_metrics():
    """Get current system metrics efficiently"""
    now = time.time()
    
    # Use cached data if fresh (< 200ms old)
    if now - system_metrics_cache["last_update"] < 0.2:
        return {
            "cpu": {
                "percent": system_metrics_cache["cpu_percent"],
                "per_core": system_metrics_cache["cpu_percent_per_core"],
            },
            "memory": system_metrics_cache.get("memory", {}),
            "disk": system_metrics_cache.get("disk", {}),
            "network": system_metrics_cache.get("network", {}),
            "battery": system_metrics_cache.get("battery", {}),
            "timestamp": now,
        }
    
    system_metrics_cache["last_update"] = now
    return {
        "cpu": {
            "percent": system_metrics_cache["cpu_percent"],
            "per_core": system_metrics_cache["cpu_percent_per_core"],
        },
        "memory": system_metrics_cache["memory"],
        "disk": system_metrics_cache["disk"],
        "network": system_metrics_cache["network"],
        "battery": system_metrics_cache.get("battery", {}),
        "timestamp": now,
    }

# WebSocket helper functions
async def send_to_client(ws, message):
    """Send a message to a specific client"""
    try:
        await ws.send(json.dumps(message))
        return True
    except websockets.exceptions.ConnectionClosed:
        if "--debug" in sys.argv:
            print(f"[roturLink] Connection closed when trying to send message")
        return False
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Error sending message: {str(e)}")
        return False

async def handler(websocket):
    """WebSocket connection handler"""
    # Get client info
    client_ip = websocket.remote_address[0] if hasattr(websocket, 'remote_address') else "unknown"
    if "--debug" in sys.argv:
        print(f"[roturLink] New connection from {client_ip}")
    
    # Add to connected clients
    connected_clients.add(websocket)
    
    try:
        # Send handshake message
        await send_to_client(websocket, {
            "cmd": "handshake",
            "val": {
                "server": "rotur-websocket",
                "version": "1.0.0"
            }
        })
        
        # Send initial system metrics
        metrics = get_system_metrics()
        await send_to_client(websocket, {
            "cmd": "metrics",
            "val": metrics
        })
        
        # Start sending metrics periodically
        while True:
            try:
                # Send metrics update
                await send_to_client(websocket, {
                    "cmd": "metrics",
                    "val": get_system_metrics()
                })
                
                # Sleep before next update
                await asyncio.sleep(0.25)
                
            except websockets.exceptions.ConnectionClosed:
                if "--debug" in sys.argv:
                    print(f"[roturLink] Connection closed by {client_ip}")
                break
            
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Error handling connection: {str(e)}")
    finally:
        # Clean up
        heartbeat_task.cancel()
        if websocket in connected_clients:
            connected_clients.remove(websocket)
            if "--debug" in sys.argv:
                print(f"[roturLink] Client {client_ip} removed. {len(connected_clients)} clients remaining")

async def start_websocket_server():
    """Start the WebSocket server"""
    try:
        async with websockets.serve(handler, "127.0.0.1", 5002, ping_interval=None):
            if "--debug" in sys.argv:
                print(f"[roturLink] WebSocket server running at ws://127.0.0.1:5002")
            # Keep the server running
            await asyncio.Future()
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Error starting WebSocket server: {str(e)}")

def run_websocket_server():
    """Run the WebSocket server in the event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_websocket_server())

# Start WebSocket server thread
threading.Thread(target=run_websocket_server, daemon=True).start()

def cache_result(timeout=5):
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
    allowed_origins = ["https://origin.mistium.com", "https://turbowarp.org"]
    is_local = client_ip in ("127.0.0.1", "::1")
    return any(allowed_domain in origin for allowed_domain in allowed_origins) or is_local

@app.route("/rotur", methods=["GET"])
def ping():
    return "true", 200

@app.route("/run", methods=["GET"])
def run():
    if not isAllowed(request): return {"status": "error", "message": "Unauthorized request"}, 403
    command = request.args.get("command")
    if not command: return {"status": "error", "message": "No command provided"}, 400

    try:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
@cache_result(timeout=1)
def sysObj():
    if not isAllowed(request): return {"status": "error", "message": "Unauthorized request"}, 403
    try:
        metrics = get_system_metrics()
        return {"status": "success", "data": metrics}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route("/eval", methods=["GET"])
def eval_code():
    if not isAllowed(request): return {"status": "error", "message": "Unauthorized request"}, 403
    code = request.args.get("code")
    if not code: return {"status": "error", "message": "No code provided"}, 400
    try:
        return {"status": "success", "return": eval(code)}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route("/proxy", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def proxy():
    if not isAllowed(request): return {"status": "error", "message": "Unauthorized request"}, 403
    url = request.args.get("url")
    if not url: return {"status": "error", "message": "No URL provided"}, 400

    try:
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        params = request.args.to_dict()
        params.pop("url", None)
        
        response = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            params=params,
            data=request.get_data() if not request.is_json else None,
            json=request.json if request.is_json else None,
            cookies=request.cookies,
            allow_redirects=True,
        )
        return response.text, response.status_code, {k: v for k, v in response.headers.items()}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route("/sysinfo", methods=["GET"])
@cache_result(timeout=30)
def system_info():
    if not isAllowed(request): return {"status": "error", "message": "Unauthorized request"}, 403
    try:
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
            "cpu": {
                "processor": platform.processor(),
                "cpu_cores": psutil.cpu_count(logical=False),
                "cpu_threads": psutil.cpu_count(logical=True),
            },
            "memory": {
                "total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                "total": psutil.virtual_memory().total,
            },
            "hostname": platform.node(),
            "ip_addresses": [addr.address for iface in psutil.net_if_addrs().values() 
                            for addr in iface if addr.family == 2][:2],
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

def update_cache():
    while True:
        system_metrics_cache["cpu_percent"] = psutil.cpu_percent(interval=1)
        system_metrics_cache["cpu_percent_per_core"] = psutil.cpu_percent(percpu=True, interval=0.5)

        memory = psutil.virtual_memory()
        system_metrics_cache["memory"] = {
            "total": memory.total, "available": memory.available,
            "percent": memory.percent, "used": memory.used, "free": memory.free
        }
        
        disk = psutil.disk_usage("/")
        system_metrics_cache["disk"] = {
            "total": disk.total, "used": disk.used,
            "free": disk.free, "percent": disk.percent
        }
        
        network = psutil.net_io_counters()
        system_metrics_cache["network"] = {
            "sent": network.bytes_sent, "received": network.bytes_recv,
            "packets_sent": network.packets_sent, "packets_received": network.packets_recv
        }

        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery:
                system_metrics_cache["battery"] = {
                    "percent": battery.percent if hasattr(battery, "percent") else 0,
                    "power_plugged": battery.power_plugged if hasattr(battery, "power_plugged") else False
                }

if __name__ == "__main__":
    # Silence Flask's startup messages and run the app
    sys.modules["flask.cli"].show_server_banner = lambda *x: None

    # Start a background thread to update system metrics cache
    threading.Thread(target=lambda: asyncio.run(update_cache()), daemon=True).start()

    app.run(host="127.0.0.1", port=5001, debug=False)
