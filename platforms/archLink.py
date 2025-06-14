import subprocess, sys, logging, os, threading, time, json, platform, asyncio, concurrent.futures
from functools import wraps
from urllib.parse import urlparse

# Configuration from link.conf
CONFIG = {
    "allowed_modules": [
        "system",
        "cpu", 
        "memory",
        "disk",
        "network",
        "bluetooth",
        "battery",
        "temperature"
    ],
    "allowed_origins": [
        "https://turbowarp.org",
        "https://origin.mistium.com", 
        "http://localhost:5001",
        "http://localhost:5002",
        "http://localhost:3000",
        "http://127.0.0.1:5001",
        "http://127.0.0.1:5002",
        "http://127.0.0.1:3000"
    ]
}

if platform.system() != "Linux":
    print("[roturLink] This script is made for Arch Linux. Running on non-Linux systems may not work as expected.")
    sys.exit(1)

logging.getLogger().setLevel(logging.ERROR)
sys.stdout = sys.stderr = type('NullWriter', (), {'write': lambda s,x: None, 'flush': lambda s: None})() if "--debug" not in sys.argv else sys.stdout

def ensure_module_installed(module_name, arch_package=None):
    try: 
        return __import__(module_name)
    except ImportError:
        if arch_package:
            try:
                subprocess.run(["sudo", "pacman", "-S", "--noconfirm", arch_package], check=True, capture_output=True)
                return __import__(module_name)
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

# Install dependencies
CORS = ensure_module_installed("flask_cors", "python-flask-cors").CORS
psutil = ensure_module_installed("psutil", "python-psutil")
requests = ensure_module_installed("requests", "python-requests")
flask = ensure_module_installed("flask", "python-flask")
websockets = ensure_module_installed("websockets", "python-websockets")
bluetooth = ensure_module_installed("bleak", "python-bleak")
pyudev = ensure_module_installed("pyudev", "python-pyudev")

# Add helper function for parent device traversal
from pyudev import Context

context = Context()

def get_parent_device(device):
    try:
        parent = device.parent
        return parent
    except Exception:
        return None

# Optional modules
PULSEAUDIO_AVAILABLE = bool(ensure_module_installed("pulsectl", "python-pulsectl"))
try:
    import gi
    gi.require_version('NM', '1.0')
    from gi.repository import NM
    NETWORKMANAGER_AVAILABLE = True
except:
    NETWORKMANAGER_AVAILABLE = bool(ensure_module_installed("gi", "networkmanager python-gobject"))

from flask import Flask, request, jsonify, Response

app = Flask(__name__)
sys.modules["flask.cli"].show_server_banner = lambda *x: None
CORS(app, resources={r"/*": {"origins": "*"}})

# Configuration
METRICS_INTERVAL, BLUETOOTH_INTERVAL, USB_SCAN_INTERVAL, USB_MONITOR_INTERVAL, HEARTBEAT_INTERVAL = 1.0, 5.0, 10.0, 2.0, 10
ORIGINS_URL = "https://link.rotur.dev/allowed.json"

system_metrics_cache = {"cpu_percent": 0, "memory": {}, "disk": {}, "network": {}, "battery": {}, "bluetooth": [], "wifi": {}, "brightness": 0, "volume": 0, "drives": [], "last_update": 0, "last_usb_scan": 0, "last_drive_check": 0}
connected_clients = set()
ALLOWED_ORIGINS = CONFIG["allowed_origins"].copy()
BLUETOOTH_DEVICES = {}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

def fetch_allowed_origins():
    global ALLOWED_ORIGINS
    try:
        response = requests.get(ORIGINS_URL, timeout=5)
        origins_data = response.json()
        if isinstance(origins_data, dict) and "origins" in origins_data:
            ALLOWED_ORIGINS = origins_data["origins"] + CONFIG["allowed_origins"]
    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] Origins fetch error: {e}")

def is_origin_allowed(origin):
    return origin.startswith("http://localhost:") or origin in ALLOWED_ORIGINS

def run_command(cmd, timeout=5, shell=False):
    """Unified command runner with error handling"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)
        return {"success": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip(), "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timeout ({timeout}s)"}
    except FileNotFoundError:
        return {"success": False, "error": f"Command not found: {cmd[0] if isinstance(cmd, list) else cmd}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def run_async(func, *args):
    """Run blocking function in executor"""
    return await asyncio.get_event_loop().run_in_executor(executor, func, *args)

def get_system_metrics():
    return {
        "cpu": {"percent": system_metrics_cache["cpu_percent"]},
        "memory": system_metrics_cache["memory"],
        "disk": system_metrics_cache["disk"],
        "network": system_metrics_cache["network"],
        "battery": system_metrics_cache.get("battery", {}),
        "wifi": system_metrics_cache.get("wifi", {}),
        "brightness": system_metrics_cache.get("brightness", 0),
        "volume": system_metrics_cache.get("volume", {"level": 0, "muted": False}),
        "drives": system_metrics_cache.get("drives", []),
        "timestamp": time.time(),
    }

def get_system_info():
    bluetooth_available = "Controller" in run_command(["bluetoothctl", "show"]).get("stdout", "")
    return {
        "platform": {"system": "Arch Linux", "architecture": platform.machine()},
        "cpu": {"cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(logical=True)},
        "bluetooth": {"available": bluetooth_available, "backend": "bleak"},
        "memory": {"total_gb": round(psutil.virtual_memory().total / (1024**3), 2)},
        "hostname": platform.node(),
    }

def get_wifi_info_sync():
    if not NETWORKMANAGER_AVAILABLE:
        return {"error": "NetworkManager not available", "connected": False, "scan": []}
    
    try:
        client = NM.Client.new(None)
        wifi_info = {"connected": False, "ssid": "Unknown", "signal_strength": 0, "scan": []}
        
        # Get connected network info
        for conn in client.get_active_connections():
            if conn.get_connection_type() == '802-11-wireless':
                settings = conn.get_connection()
                wifi_settings = settings.get_setting_wireless()
                
                # Get signal strength
                signal_strength = 0
                for device in client.get_devices():
                    if device.get_device_type() == NM.DeviceType.WIFI:
                        active_ap = device.get_active_access_point()
                        if active_ap:
                            signal_strength = active_ap.get_strength()
                            break

                wifi_info.update({
                    "connected": True,
                    "ssid": wifi_settings.get_ssid().get_data().decode('utf-8') if wifi_settings.get_ssid() else "Unknown",
                    "signal_strength": signal_strength
                })
                break
        
        # Get nearby networks
        nearby_networks = []
        for device in client.get_devices():
            if device.get_device_type() == NM.DeviceType.WIFI:
                try:
                    # Request fresh scan
                    device.request_scan_async(None, None, None)
                    
                    # Get access points
                    access_points = device.get_access_points()
                    seen_ssids = set()
                    
                    for ap in access_points:
                        ssid_bytes = ap.get_ssid()
                        if not ssid_bytes:
                            continue
                            
                        try:
                            ssid = ssid_bytes.get_data().decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                            
                        if ssid and ssid not in seen_ssids:
                            seen_ssids.add(ssid)
                            
                            network_info = {
                                "ssid": ssid,
                                "signal_strength": ap.get_strength(),
                                "frequency": ap.get_frequency(),
                                "connected": ssid == wifi_info.get("ssid", "")
                            }
                            nearby_networks.append(network_info)
                            
                except Exception as e:
                    if "--debug" in sys.argv: 
                        print(f"[roturLink] WiFi scan error: {e}")
                    continue
                break
        
        # Sort by signal strength
        nearby_networks.sort(key=lambda x: x["signal_strength"], reverse=True)
        wifi_info["scan"] = nearby_networks[:20]  # Limit to top 20
        
        return wifi_info
        
    except Exception as e:
        return {"error": str(e), "connected": False, "scan": []}

async def get_wifi_info():
    return await run_async(get_wifi_info_sync)

def get_brightness_sync():
    result = run_command(["brightnessctl", "get"])
    max_result = run_command(["brightnessctl", "max"])
    
    if result["success"] and max_result["success"]:
        try:
            current, maximum = int(result["stdout"]), int(max_result["stdout"])
            return {"brightness": int((current / maximum) * 100), "current": current, "max": maximum, "available": True}
        except ValueError:
            pass
    
    percent_result = run_command(["brightnessctl", "-m"])
    if percent_result["success"]:
        try:
            percentage = int(percent_result["stdout"].split(',')[4].replace('%', ''))
            return {"brightness": percentage, "available": True}
        except (IndexError, ValueError):
            pass
    
    return {"brightness": 0, "available": False, "error": "brightnessctl not available"}

async def get_brightness():
    return await run_async(get_brightness_sync)

async def set_brightness(percentage):
    percentage = max(1, min(100, int(percentage)))
    result = await run_async(run_command, ["brightnessctl", "set", f"{percentage}%"])
    return {"success": result["success"], "brightness": percentage} if result["success"] else {"success": False, "error": result.get("error", "brightnessctl failed")}

def get_volume_sync():
    if PULSEAUDIO_AVAILABLE:
        try:
            import pulsectl
            with pulsectl.Pulse('roturLink-volume') as pulse:
                sinks = pulse.sink_list()
                if sinks:
                    sink = sinks[0]
                    return {"volume": int(sink.volume.value_flat * 100), "muted": sink.mute == 1, "available": True}
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] PulseAudio error: {e}")
    
    # Fallback to amixer
    result = run_command(["amixer", "get", "Master"])
    if result["success"]:
        import re
        match = re.search(r'\[(\d+)%\]', result["stdout"])
        if match:
            return {"volume": int(match.group(1)), "muted": "[off]" in result["stdout"], "available": True}
    
    return {"volume": 0, "muted": True, "available": False, "error": "No audio control available"}

async def get_volume():
    return await run_async(get_volume_sync)

def set_volume_sync(percentage):
    percentage = max(0, min(100, int(percentage)))
    if PULSEAUDIO_AVAILABLE:
        try:
            import pulsectl
            with pulsectl.Pulse('roturLink-volume') as pulse:
                sinks = pulse.sink_list()
                if sinks:
                    pulse.volume_set(sinks[0], pulsectl.PulseVolumeInfo(percentage / 100.0))
                    return {"success": True, "volume": percentage}
        except Exception:
            pass
    
    result = run_command(["amixer", "set", "Master", f"{percentage}%"])
    return {"success": result["success"], "volume": percentage} if result["success"] else {"success": False, "error": "Volume set failed"}

async def set_volume(percentage):
    return await run_async(set_volume_sync, percentage)

def toggle_mute_sync():
    if PULSEAUDIO_AVAILABLE:
        try:
            import pulsectl
            with pulsectl.Pulse('roturLink-volume') as pulse:
                sinks = pulse.sink_list()
                if sinks:
                    sink = sinks[0]
                    pulse.mute(sink, not sink.mute)
                    return {"success": True, "muted": not sink.mute}
        except Exception:
            pass
    
    result = run_command(["amixer", "set", "Master", "toggle"])
    if result["success"]:
        volume_info = get_volume_sync()
        return {"success": True, "muted": volume_info.get("muted", False)}
    return {"success": False, "error": "Mute toggle failed"}

async def toggle_mute():
    return await run_async(toggle_mute_sync)

def mount_usb_drive(device_path):
    result = run_command(["udisksctl", "mount", "-b", device_path], timeout=30)
    if result["success"]:
        output = result["stdout"]
        if "Mounted" in output and "at" in output:
            mount_point = output.split("at")[-1].strip()
            return {"success": True, "mount_point": mount_point, "message": f"Mounted at {mount_point}"}
        return {"success": True, "message": "Drive mounted successfully"}
    
    # Fallback to manual mount
    if "not found" in result.get("error", ""):
        mount_point = f"/mnt/usb_{os.path.basename(device_path)}"
        os.makedirs(mount_point, exist_ok=True)
        manual_result = run_command(["sudo", "mount", device_path, mount_point], timeout=30)
        return {"success": manual_result["success"], "mount_point": mount_point, "message": f"Mounted at {mount_point}"} if manual_result["success"] else {"success": False, "error": "Manual mount failed"}
    
    return {"success": False, "error": result.get("error", "Mount failed")}

def get_unmounted_usb_devices():
    try:
        context = pyudev.Context()
        unmounted_devices = []
        
        # Get mounted devices
        mounted_devices = set()
        try:
            with open('/proc/mounts', 'r') as f:
                mounted_devices = {line.split()[0] for line in f if line.split()[0].startswith('/dev/')}
        except Exception:
            pass
        
        # Find USB devices
        for device in context.list_devices(subsystem='block'):
            device_node = device.device_node
            if not device_node or device_node in mounted_devices:
                continue
                
            # Check if USB
            device_path = device.device_path
            is_usb = '/usb' in device_path or 'usb' in device.properties.get('ID_BUS', '').lower()
            
            if not is_usb:
                # Check parent chain
                current = device
                for _ in range(10):
                    current = get_parent_device(current)
                    if current is None:
                        break
                    if current.subsystem == 'usb':
                        is_usb = True
                        break
            
            if is_usb and device.properties.get('ID_FS_TYPE'):
                device_info = {
                    "device_node": device_node,
                    "name": device.properties.get('ID_FS_LABEL') or device.properties.get('ID_MODEL', 'Unknown USB Drive'),
                    "filesystem": device.properties.get('ID_FS_TYPE', 'unknown'),
                    "size_gb": 0
                }
                
                # Get size
                try:
                    size_path = f"/sys/block/{device.sys_name.rstrip('0123456789')}/size"
                    if os.path.exists(size_path):
                        with open(size_path, 'r') as f:
                            sectors = int(f.read().strip())
                            device_info["size_gb"] = round(sectors * 512 / (1024**3), 2)
                except Exception:
                    pass
                
                unmounted_devices.append(device_info)
        
        return unmounted_devices
    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] USB devices error: {e}")
        return []

def auto_mount_usb_drives():
    unmounted = get_unmounted_usb_devices()
    mounted_results = []
    
    for device in unmounted:
        if "--debug" in sys.argv: print(f"[roturLink] Auto-mounting: {device['name']}")
        result = mount_usb_drive(device['device_node'])
        mounted_results.append({"device": device, "mount_result": result})
        
        if "--debug" in sys.argv:
            status = "Success" if result.get("success") else "Failed"
            print(f"[roturLink] {status}: {result.get('message', result.get('error', ''))}")
    
    return mounted_results

def safely_remove_usb(device_path):
    result = run_command(["udisksctl", "unmount", "-b", device_path], timeout=30)
    if result["success"]:
        return {"success": True, "message": f"Safely removed {device_path}"}
    
    # Fallback
    if "not found" in result.get("error", ""):
        manual_result = run_command(["sudo", "umount", device_path], timeout=30)
        return {"success": manual_result["success"], "message": f"Safely removed {device_path}"} if manual_result["success"] else {"success": False, "error": "Manual unmount failed"}
    
    return {"success": False, "error": result.get("error", "Unmount failed")}

def get_usb_drives(force_scan=False):
    current_time = time.time()
    if not force_scan and (current_time - system_metrics_cache.get("last_usb_scan", 0)) < USB_SCAN_INTERVAL:
        return system_metrics_cache.get("drives", [])
    
    auto_mount_usb_drives()
    
    try:
        context = pyudev.Context()
        usb_drives = []
        
        for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
            device_path = device.device_path
            is_usb = '/usb' in device_path or 'usb' in device.properties.get('ID_BUS', '').lower()
            
            if not is_usb:
                current = device
                for _ in range(10):
                    current = get_parent_device(current)
                    if current is None:
                        break
                    if current.subsystem == 'usb':
                        is_usb = True
                        break
            
            if is_usb:
                device_info = {
                    "device_node": device.device_node,
                    "name": device.properties.get('ID_MODEL', 'Unknown'),
                    "size_gb": 0,
                    "files": [],
                    "mount_points": []
                }
                
                # Get size
                try:
                    size_path = f"/sys/block/{device.sys_name}/size"
                    if os.path.exists(size_path):
                        with open(size_path, 'r') as f:
                            sectors = int(f.read().strip())
                            device_info["size_gb"] = round(sectors * 512 / (1024**3), 2)
                except Exception:
                    pass
                
                # Get mount points
                mount_points = []
                try:
                    with open('/proc/mounts', 'r') as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 2 and parts[0].startswith(device.device_node):
                                mount_point = parts[1].replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')
                                mount_name = os.path.basename(mount_point) if mount_point != '/' else 'root'
                                mount_points.append({
                                    "device": parts[0],
                                    "mount_point": mount_point,
                                    "mount_name": mount_name,
                                    "filesystem": parts[2] if len(parts) > 2 else "unknown"
                                })
                except Exception:
                    pass
                
                device_info["mount_points"] = mount_points
                
                if mount_points:
                    label = device.properties.get('ID_FS_LABEL', '')
                    device_info["name"] = label or mount_points[0]["mount_name"]
                    
                    if len(usb_drives) < 3:  # Limit file operations
                        try:
                            device_info["files"] = list_directory_contents(mount_points[0]["mount_point"])
                        except Exception:
                            device_info["files"] = []
                    
                    usb_drives.append(device_info)
        
        system_metrics_cache["drives"] = usb_drives
        system_metrics_cache["last_usb_scan"] = current_time
        return usb_drives
    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] USB drives error: {e}")
        return system_metrics_cache.get("drives", [])

async def scan_bluetooth_devices():
    try:
        from bleak import BleakScanner
        discovered_devices = await BleakScanner.discover(timeout=2.0)
        current_time = time.time()
        
        devices = []
        for device in discovered_devices:
            rssi = -90
            if hasattr(device, 'advertisement_data') and hasattr(device.advertisement_data, 'rssi'):
                rssi = device.advertisement_data.rssi
            
            device_info = {
                "name": device.name or "Unknown",
                "address": device.address,
                "rssi": rssi,
                "last_seen": current_time
            }
            devices.append(device_info)
        
        BLUETOOTH_DEVICES.clear()
        for device in devices:
            BLUETOOTH_DEVICES[device["address"]] = device
            
        return devices
    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] Bluetooth error: {e}")
        return list(BLUETOOTH_DEVICES.values())

def list_directory_contents(path, max_files=50):
    try:
        if not os.path.exists(path) or not os.path.isdir(path):
            return []
        
        items = []
        entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        
        for entry in entries[:max_files]:
            try:
                full_path = os.path.join(path, entry)
                if not os.path.exists(full_path):
                    continue
                
                is_dir = os.path.isdir(full_path)
                item = {
                    "name": entry,
                    "type": "directory" if is_dir else "file",
                    "readable": os.access(full_path, os.R_OK),
                    "writable": os.access(full_path, os.W_OK),
                    "path": full_path,
                }
                
                try:
                    stat_info = os.stat(full_path)
                    item["modified"] = int(stat_info.st_mtime)
                    item["permissions"] = oct(stat_info.st_mode)[-3:]
                    
                    if is_dir:
                        try:
                            item["size"] = len(os.listdir(full_path))
                        except (OSError, PermissionError):
                            item["size"] = 0
                    else:
                        item["size"] = stat_info.st_size
                        item["extension"] = os.path.splitext(entry)[1].lower()
                except (OSError, PermissionError):
                    item.update({"size": 0, "modified": 0, "permissions": "000"})
                    if not is_dir:
                        item["extension"] = ""
                
                items.append(item)
            except (OSError, PermissionError):
                continue
        
        return items
    except Exception as e:
        return {"error": str(e)}

def read_file_content(file_path, max_size=1024*1024):
    try:
        if not os.path.exists(file_path) or not os.path.isfile(file_path) or not os.access(file_path, os.R_OK):
            return {"error": "File not accessible"}
        
        file_size = os.path.getsize(file_path)
        if file_size > max_size:
            return {"error": f"File too large (max {max_size//1024}KB)"}
        
        # Check if text file
        try:
            with open(file_path, 'rb') as f:
                sample = f.read(1024)
                is_text = not bool(sample.translate(None, bytes(range(32, 127)) + b'\n\r\t\f\b'))
        except:
            is_text = False
        
        if is_text:
            for encoding in ['utf-8', 'latin-1']:
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        return {"content": f.read(), "type": "text", "size": file_size, "encoding": encoding}
                except UnicodeDecodeError:
                    continue
        
        # Binary file
        import base64
        with open(file_path, 'rb') as f:
            content = f.read()
        return {"content": base64.b64encode(content).decode('utf-8'), "type": "binary", "size": file_size, "encoding": "base64"}
            
    except Exception as e:
        return {"error": str(e)}

def write_file_content(file_path, content, content_type="text", encoding="utf-8"):
    try:
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            return {"error": "Directory does not exist"}
        
        if content_type == "text":
            with open(file_path, 'w', encoding=encoding) as f:
                f.write(content)
        elif content_type == "binary":
            import base64
            binary_content = base64.b64decode(content)
            with open(file_path, 'wb') as f:
                f.write(binary_content)
        else:
            return {"error": "Invalid content type"}
        
        return {"success": True, "message": f"File written: {file_path}"}
    except Exception as e:
        return {"error": str(e)}

def create_directory(path):
    try:
        if os.path.exists(path):
            return {"error": "Directory already exists"}
        os.makedirs(path, exist_ok=True)
        return {"success": True, "message": f"Directory created: {path}"}
    except Exception as e:
        return {"error": str(e)}

def delete_file_or_directory(path):
    try:
        if not os.path.exists(path):
            return {"error": "Path does not exist"}
        
        if os.path.isfile(path):
            os.remove(path)
            return {"success": True, "message": f"File deleted: {path}"}
        elif os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
            return {"success": True, "message": f"Directory deleted: {path}"}
        return {"error": "Unknown file type"}
    except Exception as e:
        return {"error": str(e)}

async def send_to_client(ws, message):
    try:
        await ws.send(json.dumps(message))
        return True
    except:
        return False

async def broadcast_to_all_clients(message):
    if not connected_clients: return
    disconnected = [client for client in connected_clients if not await send_to_client(client, message)]
    for client in disconnected:
        connected_clients.discard(client)

async def update_and_broadcast_metrics():
    while True:
        try:
            # Quick metrics
            system_metrics_cache["cpu_percent"] = psutil.cpu_percent(interval=0.05)
            memory = psutil.virtual_memory()
            system_metrics_cache["memory"] = {"total": memory.total, "used": memory.used, "percent": memory.percent}
            disk = psutil.disk_usage("/")
            system_metrics_cache["disk"] = {"total": disk.total, "used": disk.used, "percent": disk.percent}
            network = psutil.net_io_counters()
            system_metrics_cache["network"] = {"sent": network.bytes_sent, "received": network.bytes_recv}
            
            if hasattr(psutil, "sensors_battery") and psutil.sensors_battery():
                battery = psutil.sensors_battery()
                system_metrics_cache["battery"] = {"percent": round(battery.percent, 1), "plugged": battery.power_plugged}
            
            # Slower operations with rate limiting
            current_time = time.time()
            tasks = []
            
            if current_time - system_metrics_cache.get("last_wifi_update", 0) > 10.0:
                tasks.append(get_wifi_info())
                
            if current_time - system_metrics_cache.get("last_controls_update", 0) > 5.0:
                tasks.extend([get_brightness(), get_volume()])
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                result_index = 0
                if current_time - system_metrics_cache.get("last_wifi_update", 0) > 10.0:
                    if not isinstance(results[result_index], Exception):
                        system_metrics_cache["wifi"] = results[result_index]
                        system_metrics_cache["last_wifi_update"] = current_time
                    result_index += 1
                    
                if current_time - system_metrics_cache.get("last_controls_update", 0) > 5.0:
                    if result_index < len(results) and not isinstance(results[result_index], Exception):
                        brightness_info = results[result_index]
                        system_metrics_cache["brightness"] = brightness_info.get("brightness", 0)
                    result_index += 1
                    
                    if result_index < len(results) and not isinstance(results[result_index], Exception):
                        volume_info = results[result_index]
                        system_metrics_cache["volume"] = {"level": volume_info.get("volume", 0), "muted": volume_info.get("muted", False)}
                    
                    system_metrics_cache["last_controls_update"] = current_time
            
            if connected_clients:
                asyncio.create_task(broadcast_to_all_clients({"cmd": "metrics_update", "val": get_system_metrics()}))
                
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] Metrics error: {e}")
        
        await asyncio.sleep(METRICS_INTERVAL)

async def update_and_broadcast_bluetooth():
    while True:
        if connected_clients:
            devices = await scan_bluetooth_devices()
            system_metrics_cache["bluetooth"] = devices
            await broadcast_to_all_clients({"cmd": "bluetooth_update", "val": {"bluetooth": {"devices": devices, "count": len(devices), "timestamp": time.time()}}})
            
            current_time = time.time()
            if current_time - system_metrics_cache.get("last_usb_broadcast", 0) > USB_SCAN_INTERVAL * 2:
                drives = get_usb_drives()
                await broadcast_to_all_clients({"cmd": "drives_update", "val": {"drives": drives, "change_type": "periodic"}})
                system_metrics_cache["last_usb_broadcast"] = current_time
                
        await asyncio.sleep(BLUETOOTH_INTERVAL)

def get_drive_identifiers(drives):
    return set(drive.get("device_node", "") for drive in drives if drive.get("device_node"))

async def monitor_usb_drives():
    previous_drives = set()
    
    while True:
        try:
            current_drives = get_usb_drives(force_scan=True)
            current_identifiers = get_drive_identifiers(current_drives)
            
            if previous_drives and current_identifiers != previous_drives:
                removed_drives = previous_drives - current_identifiers
                added_drives = current_identifiers - previous_drives
                
                if removed_drives or added_drives:
                    if "--debug" in sys.argv:
                        if removed_drives: print(f"[roturLink] USB drives removed: {removed_drives}")
                        if added_drives: print(f"[roturLink] USB drives added: {added_drives}")
                    
                    updated_drives = get_usb_drives(force_scan=True)
                    if connected_clients:
                        await broadcast_to_all_clients({"cmd": "drives_update", "val": {"drives": updated_drives, "change_type": "removal" if removed_drives else "addition"}})
            
            previous_drives = current_identifiers
            system_metrics_cache["last_drive_check"] = time.time()
            
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] USB monitor error: {e}")
        
        await asyncio.sleep(USB_MONITOR_INTERVAL)

async def handle_command(websocket, message):
    try:
        if isinstance(message, str):
            message = json.loads(message)
        
        cmd = message.get("cmd")
        
        # Command handlers with immediate responses
        command_handlers = {
            "ping": lambda: {"cmd": "pong", "val": {"timestamp": time.time()}},
            "get_metrics": lambda: {"cmd": "metrics", "val": get_system_metrics()},
            "get_system_info": lambda: {"cmd": "system_info", "val": get_system_info()},
        }
        
        if cmd in command_handlers:
            await send_to_client(websocket, command_handlers[cmd]())
        elif cmd == "brightness_get":
            brightness_info = await get_brightness()
            await send_to_client(websocket, {"cmd": "brightness_response", "val": brightness_info})
        elif cmd == "brightness_set":
            brightness = message.get("val", 100.0)
            await send_to_client(websocket, {"cmd": "brightness_ack", "val": {"brightness": brightness, "status": "setting"}})
            result = await set_brightness(brightness)
            await send_to_client(websocket, {"cmd": "brightness_response", "val": result})
        elif cmd == "volume_get":
            volume_info = await get_volume()
            await send_to_client(websocket, {"cmd": "volume_response", "val": volume_info})
        elif cmd == "volume_set":
            volume = message.get("val", 50)
            await send_to_client(websocket, {"cmd": "volume_ack", "val": {"volume": volume, "status": "setting"}})
            result = await set_volume(volume)
            await send_to_client(websocket, {"cmd": "volume_response", "val": result})
        elif cmd == "volume_mute":
            await send_to_client(websocket, {"cmd": "volume_ack", "val": {"status": "toggling_mute"}})
            result = await toggle_mute()
            await send_to_client(websocket, {"cmd": "volume_response", "val": result})
        else:
            await send_to_client(websocket, {"cmd": "error", "val": {"message": f"Unknown command: {cmd}"}})
    
    except json.JSONDecodeError:
        await send_to_client(websocket, {"cmd": "error", "val": {"message": "Invalid JSON"}})
    except Exception as e:
        await send_to_client(websocket, {"cmd": "error", "val": {"message": str(e)}})

async def handler(websocket):
    origin = websocket.request.headers.get("origin", "")
    client_ip = websocket.remote_address[0] if hasattr(websocket, 'remote_address') else "unknown"
    
    if not (client_ip in ("127.0.0.1", "::1") or is_origin_allowed(origin)):
        return
    
    connected_clients.add(websocket)
    
    try:
        await send_to_client(websocket, {"cmd": "handshake", "val": {"server": "rotur-websocket", "version": "1.0.0"}})
        await send_to_client(websocket, {"cmd": "system_info", "val": get_system_info()})
        await send_to_client(websocket, {"cmd": "metrics", "val": get_system_metrics()})
        
        async for message in websocket:
            asyncio.create_task(handle_command(websocket, message))
    except:
        pass
    finally:
        connected_clients.discard(websocket)

async def start_websocket_server():
    fetch_allowed_origins()
    asyncio.create_task(update_and_broadcast_metrics())
    asyncio.create_task(update_and_broadcast_bluetooth())
    asyncio.create_task(monitor_usb_drives())
    
    async with websockets.serve(handler, "127.0.0.1", 5002, ping_interval=None):
        if "--debug" in sys.argv: print("[roturLink] WebSocket server at ws://127.0.0.1:5002")
        await asyncio.Future()

def run_websocket_server():
    asyncio.run(start_websocket_server())

def isAllowed(request):
    origin = request.headers.get("Origin", "")
    return request.remote_addr in ("127.0.0.1", "::1") or is_origin_allowed(origin)

def create_endpoint(path, methods=["GET"]):
    def decorator(func):
        @app.route(path, methods=methods)
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not isAllowed(request):
                return jsonify({"error": "Access denied"}), 403
            return func(*args, **kwargs)
        return wrapper
    return decorator

# Simplified HTTP endpoints
@app.route("/rotur", methods=["GET"])
def ping():
    return "true", 200, {'Access-Control-Allow-Origin': '*'}

@create_endpoint("/sysinfo")
def sysinfo():
    return jsonify(get_system_info())

@create_endpoint("/proxy", ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def proxy():
    if request.method == "OPTIONS":
        response = app.response_class("", 200)
        response.headers.update({'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS', 'Access-Control-Max-Age': '86400'})
        return response
        
    url = request.args.get('url')
    if not url: 
        return jsonify({"error": "URL parameter missing"}), 400
    
    try:
        headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'content-length', 'connection']}
        response = requests.request(method=request.method, url=url, headers=headers, params=request.args, data=request.get_data(), timeout=10, allow_redirects=True)
        
        proxy_response = Response(response.content)
        if 'Content-Type' in response.headers:
            proxy_response.headers['Content-Type'] = response.headers['Content-Type']
        proxy_response.headers.update({'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS', 'Access-Control-Allow-Headers': '*'})
        return proxy_response, response.status_code
        
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# Consolidated endpoints with path validation
def validate_usb_path(path):
    usb_drives = get_usb_drives()
    allowed_paths = [mp["mount_point"] for drive in usb_drives for mp in drive.get("mount_points", [])]
    full_path = f"/{path}" if not path.startswith('/') else path
    return any(full_path.startswith(allowed_path) for allowed_path in allowed_paths), full_path

# USB and file system endpoints
@create_endpoint("/usb/drives")
def usb_drives():
    return jsonify({"drives": get_usb_drives()})

@create_endpoint("/usb/remove", ["POST"])
def usb_remove():
    data = request.get_json()
    if not data or "device" not in data:
        return jsonify({"error": "Device path required"}), 400
    return jsonify(safely_remove_usb(data["device"]))

@create_endpoint("/usb/mount", ["POST"])
def mount_usb():
    data = request.get_json()
    if not data or "device" not in data:
        return jsonify({"error": "Device path required"}), 400
    return jsonify(mount_usb_drive(data["device"]))

@create_endpoint("/usb/unmounted")
def get_unmounted_usb():
    return jsonify({"unmounted_devices": get_unmounted_usb_devices()})

@create_endpoint("/fs/list/<path:directory_path>")
def list_directory_endpoint(directory_path):
    is_allowed, full_path = validate_usb_path(directory_path)
    if not is_allowed:
        return jsonify({"error": "Access denied - path not in mounted USB drive"}), 403
    contents = list_directory_contents(full_path)
    return jsonify({"path": full_path, "contents": contents})

@create_endpoint("/fs/read/<path:file_path>")
def read_file_endpoint(file_path):
    is_allowed, full_path = validate_usb_path(file_path)
    if not is_allowed:
        return jsonify({"error": "Access denied - path not in mounted USB drive"}), 403
    max_size = request.args.get('max_size', 1024*1024, type=int)
    return jsonify(read_file_content(full_path, max_size))

@create_endpoint("/fs/write/<path:file_path>", ["POST"])
def write_file_endpoint(file_path):
    is_allowed, full_path = validate_usb_path(file_path)
    if not is_allowed:
        return jsonify({"error": "Access denied - path not in mounted USB drive"}), 403
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "Content required"}), 400
    return jsonify(write_file_content(full_path, data["content"], data.get("type", "text"), data.get("encoding", "utf-8")))

@create_endpoint("/fs/mkdir/<path:directory_path>", ["POST"])
def create_dir_endpoint(directory_path):
    is_allowed, full_path = validate_usb_path(directory_path)
    if not is_allowed:
        return jsonify({"error": "Access denied - path not in mounted USB drive"}), 403
    return jsonify(create_directory(full_path))

@create_endpoint("/fs/delete/<path:target_path>", ["DELETE"])
def delete_path_endpoint(target_path):
    is_allowed, full_path = validate_usb_path(target_path)
    if not is_allowed:
        return jsonify({"error": "Access denied - path not in mounted USB drive"}), 403
    return jsonify(delete_file_or_directory(full_path))

# Control endpoints
@create_endpoint("/volume/get")
def volume_info():
    return jsonify(get_volume_sync())

@create_endpoint("/volume/set/<volume>")
def volume_set_endpoint(volume):
    return jsonify(set_volume_sync(volume))

@create_endpoint("/volume/mute", ["POST"])
def volume_mute_endpoint():
    return jsonify(toggle_mute_sync())

if __name__ == "__main__":
    fetch_allowed_origins()
    threading.Thread(target=run_websocket_server, daemon=True).start()
    app.run(host="127.0.0.1", port=5001, debug=False)