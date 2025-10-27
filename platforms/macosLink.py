import subprocess, sys, logging, os, threading, time, json, platform, asyncio, concurrent.futures
from functools import wraps, lru_cache
from urllib.parse import urlparse
from collections import defaultdict

CONFIG = {
    "allowed_modules": ["system", "cpu", "memory", "disk", "network", "bluetooth", "battery", "temperature"],
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

if platform.system() != "Darwin":
    print("[roturLink] This script requires macOS")
    sys.exit(1)

logging.getLogger().setLevel(logging.ERROR)
if "--debug" not in sys.argv:
    sys.stdout = sys.stderr = type('NullWriter', (), {'write': lambda s,x: None, 'flush': lambda s: None})()

def ensure_module_installed(module_name, pip_package=None, brew_package=None, verbose=True):
    try:
        return __import__(module_name)
    except ImportError:
        if verbose:
            print(f"Module '{module_name}' not found. Installing...")
    
    package_to_install = pip_package or module_name
    try:
        if verbose:
            print(f"Installing via pip: {package_to_install}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", package_to_install, "--break-system-packages"],
            check=True,
            capture_output=True,
            text=True
        )
        if verbose:
            print(f"Successfully installed {package_to_install}")
        return __import__(module_name)
    except (subprocess.CalledProcessError, FileNotFoundError, ImportError) as e:
        if verbose:
            print(f"Installation failed: {e}")
    
    if brew_package:
        try:
            if verbose:
                print(f"Trying homebrew: {brew_package}")
            subprocess.run(
                ["brew", "install", brew_package],
                check=True,
                capture_output=True,
                text=True
            )
            if verbose:
                print(f"Successfully installed {brew_package}")
            return __import__(module_name)
        except (subprocess.CalledProcessError, FileNotFoundError, ImportError) as e:
            if verbose:
                print(f"Brew installation failed: {e}")
    
    if verbose:
        print(f"Failed to install '{module_name}'")

    sys.exit(1)

CORS = ensure_module_installed("flask_cors").CORS
psutil = ensure_module_installed("psutil")
requests = ensure_module_installed("requests")
flask = ensure_module_installed("flask")
websockets = ensure_module_installed("websockets")
bluetooth = ensure_module_installed("bleak")

try:
    import plistlib
    PLIST_AVAILABLE = True
except ImportError:
    PLIST_AVAILABLE = False

from flask import Flask, request, jsonify, Response

app = Flask(__name__)
if CORS:
    CORS(app, resources={r"/*": {"origins": "*"}})

METRICS_INTERVAL = 5.0
BLUETOOTH_INTERVAL = 30.0
USB_SCAN_INTERVAL = 15.0
USB_MONITOR_INTERVAL = 10.0
HEARTBEAT_INTERVAL = 10
WIFI_UPDATE_INTERVAL = 45.0
DRIVE_BROADCAST_INTERVAL = 120.0
BASIC_METRICS_INTERVAL = 10.0
DISK_UPDATE_INTERVAL = 30.0
BATTERY_UPDATE_INTERVAL = 60.0

ORIGINS_URL = "https://link.rotur.dev/allowed.json"

system_metrics_cache = {
    "cpu_percent": 0,
    "memory": {},
    "disk": {},
    "network": {},
    "battery": {},
    "bluetooth": [],
    "wifi": {},
    "brightness": 0,
    "volume": 0,
    "drives": [],
    "last_update": 0,
    "last_usb_scan": 0,
    "last_drive_check": 0,
    "last_basic_update": 0,
    "last_disk_update": 0,
    "last_battery_update": 0,
    "last_bluetooth_scan": 0,
    "last_usb_broadcast": 0
}

connected_clients = set()
ALLOWED_ORIGINS = CONFIG["allowed_origins"].copy()
BLUETOOTH_DEVICES = {}
PAIRED_BLUETOOTH_DEVICES = {}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
command_cache = {}
CACHE_TTL = 5.0

def fetch_allowed_origins():
    global ALLOWED_ORIGINS
    try:
        response = requests.get(ORIGINS_URL, timeout=5)
        origins_data = response.json()
        if isinstance(origins_data, dict) and "origins" in origins_data:
            ALLOWED_ORIGINS = list(set(origins_data["origins"] + CONFIG["allowed_origins"]))
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Origins fetch error: {e}")

def is_origin_allowed(origin):
    if not origin:
        return False
    return origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:") or origin in ALLOWED_ORIGINS

def run_command(cmd, timeout=5, shell=False, cache_key=None):
    if cache_key:
        cached = command_cache.get(cache_key)
        if cached and time.time() - cached["timestamp"] < CACHE_TTL:
            return cached["result"]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell
        )
        output = {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode
        }
        
        if cache_key:
            command_cache[cache_key] = {"result": output, "timestamp": time.time()}
        
        return output
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout ({timeout}s)", "stdout": "", "stderr": ""}
    except FileNotFoundError:
        return {"success": False, "error": f"Command not found: {cmd[0] if isinstance(cmd, list) else cmd}", "stdout": "", "stderr": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "stdout": "", "stderr": ""}

async def run_async(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, func, *args)

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

@lru_cache(maxsize=1)
def get_system_info_cached():
    bluetooth_available = False
    
    try:
        result = run_command(["system_profiler", "SPBluetoothDataType"], timeout=5, cache_key="bluetooth_check")
        if result["success"]:
            stdout_lower = result["stdout"].lower()
            bluetooth_available = any(
                indicator in stdout_lower for indicator in [
                    "state: on", "powered: yes", "discoverable: yes",
                    "connectable: yes", "controller state: on", "bluetooth power: on"
                ]
            )
    except Exception:
        pass
    
    if not bluetooth_available:
        try:
            result = run_command(
                ["defaults", "read", "/Library/Preferences/com.apple.Bluetooth", "ControllerPowerState"],
                timeout=3,
                cache_key="bluetooth_power"
            )
            bluetooth_available = result["success"] and result["stdout"].strip() == "1"
        except Exception:
            pass
    
    if not bluetooth_available:
        try:
            import bleak
            bluetooth_available = True
        except ImportError:
            bluetooth_available = False
    
    return {
        "platform": {
            "system": "macOS",
            "architecture": platform.machine(),
            "version": platform.mac_ver()[0]
        },
        "cpu": {
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True)
        },
        "bluetooth": {
            "available": bluetooth_available,
            "backend": "bleak"
        },
        "memory": {
            "total_gb": round(psutil.virtual_memory().total / (1024**3), 2)
        },
        "hostname": platform.node(),
    }

def get_system_info():
    try:
        return get_system_info_cached()
    except:
        get_system_info_cached.cache_clear()
        return get_system_info_cached()

def get_paired_bluetooth_devices():
    try:
        result = run_command(["system_profiler", "SPBluetoothDataType"], timeout=10, cache_key="bluetooth_paired")
        if not result["success"]:
            return []
        
        paired_devices = []
        lines = result["stdout"].split("\n")
        current_device = None
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            if stripped.endswith(":") and not any(x in stripped for x in ["Bluetooth:", "Devices", "Services", "Software", "Versions", "Interfaces"]):
                device_name = stripped[:-1].strip()
                if device_name and len(device_name) > 0 and not device_name.startswith("Controller"):
                    current_device = {
                        "name": device_name,
                        "address": "",
                        "paired": True,
                        "connected": False,
                        "type": "Unknown",
                        "rssi": 0
                    }
                    
                    for j in range(i + 1, min(i + 20, len(lines))):
                        detail = lines[j].strip()
                        
                        if detail.endswith(":") and ":" not in detail[:-1]:
                            break
                        
                        if "Address:" in detail:
                            current_device["address"] = detail.split(":", 1)[-1].strip()
                        elif "Connected:" in detail:
                            connected_str = detail.split(":", 1)[-1].strip().lower()
                            current_device["connected"] = connected_str in ["yes", "true"]
                        elif "Type:" in detail or "Device Type:" in detail:
                            current_device["type"] = detail.split(":", 1)[-1].strip()
                        elif "Minor Type:" in detail and current_device["type"] == "Unknown":
                            current_device["type"] = detail.split(":", 1)[-1].strip()
                    
                    if current_device["address"]:
                        paired_devices.append(current_device)
                        PAIRED_BLUETOOTH_DEVICES[current_device["address"]] = current_device
        
        return paired_devices
        
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Paired Bluetooth error: {e}")
        return []

async def scan_bluetooth_devices():
    try:
        if not connected_clients:
            return list(BLUETOOTH_DEVICES.values())
        
        current_time = time.time()
        if (current_time - system_metrics_cache.get("last_bluetooth_scan", 0) < 30.0 and BLUETOOTH_DEVICES):
            return list(BLUETOOTH_DEVICES.values())
            
        from bleak import BleakScanner
        discovered_devices = await asyncio.wait_for(
            BleakScanner.discover(timeout=2.0),
            timeout=3.0
        )
        
        devices = []
        for device in discovered_devices:
            rssi = -90
            if hasattr(device, 'rssi'):
                rssi = device.rssi
            
            device_info = {
                "name": device.name or "Unknown Device",
                "address": device.address,
                "rssi": rssi,
                "paired": device.address in PAIRED_BLUETOOTH_DEVICES,
                "connected": False,
                "nearby": True,
                "last_seen": current_time
            }
            
            if device.address in PAIRED_BLUETOOTH_DEVICES:
                device_info["connected"] = PAIRED_BLUETOOTH_DEVICES[device.address].get("connected", False)
            
            devices.append(device_info)
            BLUETOOTH_DEVICES[device.address] = device_info
        
        old_devices = [addr for addr, dev in BLUETOOTH_DEVICES.items() 
                      if current_time - dev.get("last_seen", 0) > 120]
        for addr in old_devices:
            del BLUETOOTH_DEVICES[addr]
            
        system_metrics_cache["last_bluetooth_scan"] = current_time
        return devices
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Bluetooth scan error: {e}")
        return list(BLUETOOTH_DEVICES.values())

async def connect_bluetooth_device(address):
    try:
        script = f'''
        tell application "System Events"
            tell process "SystemUIServer"
                tell (menu bar item 1 of menu bar 1 where description is "bluetooth")
                    click
                    delay 0.5
                end tell
            end tell
        end tell
        '''
        
        result = run_command(["blueutil", "--connect", address], timeout=15)
        if result["success"]:
            await asyncio.sleep(2)
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Connected to {address}"}
        
        connect_script = f'''
        tell application "System Events"
            tell process "Bluetooth"
                connect "{address}"
            end tell
        end tell
        '''
        
        result = run_command(["osascript", "-e", connect_script], timeout=15)
        if result["success"]:
            await asyncio.sleep(2)
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Connected to {address}"}
        
        return {"success": False, "error": "Connection failed. Install blueutil: brew install blueutil"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

async def disconnect_bluetooth_device(address):
    try:
        result = run_command(["blueutil", "--disconnect", address], timeout=10)
        if result["success"]:
            await asyncio.sleep(1)
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Disconnected from {address}"}
        
        disconnect_script = f'''
        tell application "System Events"
            tell process "Bluetooth"
                disconnect "{address}"
            end tell
        end tell
        '''
        
        result = run_command(["osascript", "-e", disconnect_script], timeout=10)
        if result["success"]:
            await asyncio.sleep(1)
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Disconnected from {address}"}
        
        return {"success": False, "error": "Disconnection failed"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

async def pair_bluetooth_device(address):
    try:
        result = run_command(["blueutil", "--pair", address], timeout=30)
        if result["success"]:
            await asyncio.sleep(3)
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Paired with {address}"}
        
        return {"success": False, "error": "Pairing failed. Install blueutil: brew install blueutil"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

async def unpair_bluetooth_device(address):
    try:
        result = run_command(["blueutil", "--unpair", address], timeout=15)
        if result["success"]:
            if address in PAIRED_BLUETOOTH_DEVICES:
                del PAIRED_BLUETOOTH_DEVICES[address]
            get_paired_bluetooth_devices()
            return {"success": True, "message": f"Unpaired {address}"}
        
        return {"success": False, "error": "Unpairing failed"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

def parse_wifi_channel(channel_str):
    try:
        parts = channel_str.split()
        channel = int(parts[0])
        frequency = 5 if "5GHz" in channel_str else 2.4
        return channel, frequency
    except (ValueError, IndexError):
        return 0, 0

def calculate_signal_quality(rssi):
    return max(0, min(100, (rssi + 100) * 2))

def get_wifi_info_sync():
    try:
        wifi_info = {"connected": False, "ssid": "Unknown", "signal_strength": 0, "channel": 0, "frequency": 0, "scan": []}
        
        result = run_command(["system_profiler", "SPAirPortDataType"], timeout=10, cache_key="wifi_profiler")
        if not result["success"]:
            return {"error": "WiFi unavailable", "connected": False, "scan": []}
        
        lines = result["stdout"].split("\n")
        in_current_network = False
        in_other_networks = False
        current_ssid = None
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            if "Status: Connected" in line:
                wifi_info["connected"] = True
            
            if "Current Network Information:" in line:
                in_current_network = True
                in_other_networks = False
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.endswith(":"):
                        current_ssid = next_line[:-1]
                        wifi_info["ssid"] = current_ssid
                continue
            
            if "Other Local Wi-Fi Networks:" in line:
                in_other_networks = True
                in_current_network = False
                continue
            
            if in_current_network and current_ssid:
                if "Signal / Noise:" in stripped:
                    try:
                        parts = stripped.split(":")[-1].strip().split("/")
                        rssi_str = parts[0].strip().replace("dBm", "").strip()
                        rssi = int(rssi_str)
                        wifi_info["signal_strength"] = calculate_signal_quality(rssi)
                    except (ValueError, IndexError):
                        pass
                elif "Channel:" in stripped:
                    try:
                        channel_info = stripped.split(":")[-1].strip()
                        channel, freq = parse_wifi_channel(channel_info)
                        wifi_info["channel"] = channel
                        wifi_info["frequency"] = freq
                    except Exception:
                        pass
            
            if in_other_networks and stripped.endswith(":") and not any(x in stripped for x in ["PHY Mode:", "Channel:", "Network Type:", "Security:", "Signal / Noise:"]):
                ssid = stripped[:-1].strip()
                if ssid and len(ssid) > 0:
                    network_info = {"ssid": ssid, "signal_strength": 0, "channel": 0, "frequency": 0, "security": "Unknown", "connected": False}
                    
                    for j in range(i + 1, min(i + 10, len(lines))):
                        detail = lines[j].strip()
                        
                        if detail.endswith(":") and ":" not in detail[:-1]:
                            break
                        
                        if "Signal / Noise:" in detail:
                            try:
                                parts = detail.split(":")[-1].strip().split("/")
                                rssi_str = parts[0].strip().replace("dBm", "").strip()
                                rssi = int(rssi_str)
                                network_info["signal_strength"] = calculate_signal_quality(rssi)
                            except (ValueError, IndexError):
                                pass
                        elif "Channel:" in detail:
                            try:
                                channel_info = detail.split(":")[-1].strip()
                                channel, freq = parse_wifi_channel(channel_info)
                                network_info["channel"] = channel
                                network_info["frequency"] = freq
                            except Exception:
                                pass
                        elif "Security:" in detail:
                            network_info["security"] = detail.split(":")[-1].strip()
                    
                    network_info["connected"] = (ssid == current_ssid)
                    wifi_info.setdefault("scan", []).append(network_info)
        
        if wifi_info.get("scan"):
            seen_ssids = {}
            unique_networks = []
            for net in wifi_info["scan"]:
                ssid = net["ssid"]
                if ssid not in seen_ssids or net["signal_strength"] > seen_ssids[ssid]["signal_strength"]:
                    if ssid in seen_ssids:
                        unique_networks.remove(seen_ssids[ssid])
                    seen_ssids[ssid] = net
                    unique_networks.append(net)
            
            unique_networks.sort(key=lambda x: x["signal_strength"], reverse=True)
            wifi_info["scan"] = unique_networks[:20]
        
        return wifi_info
        
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] WiFi error: {e}")
        return {"error": str(e), "connected": False, "scan": []}

async def get_wifi_info():
    return await run_async(get_wifi_info_sync)

def get_brightness_sync():
    result = run_command(["brightness", "-l"], timeout=2)
    if result["success"]:
        try:
            for line in result["stdout"].split("\n"):
                if "brightness" in line.lower():
                    brightness_val = float(line.split()[-1])
                    return {"brightness": int(brightness_val * 100), "available": True}
        except (ValueError, IndexError):
            pass
    
    return {"brightness": 50, "available": False, "error": "Install with: brew install brightness"}

async def get_brightness():
    return await run_async(get_brightness_sync)

async def set_brightness(percentage):
    percentage = max(1, min(100, int(percentage)))
    result = await run_async(run_command, ["brightness", str(percentage / 100.0)])
    
    if result["success"]:
        return {"success": True, "brightness": percentage}
    
    return {"success": False, "error": "Install brightness: brew install brightness"}

def get_volume_sync():
    try:
        script = "output volume of (get volume settings)"
        result = run_command(["osascript", "-e", script], timeout=2)
        if result["success"]:
            volume = int(result["stdout"])
            
            mute_script = "output muted of (get volume settings)"
            mute_result = run_command(["osascript", "-e", mute_script], timeout=2)
            muted = mute_result["success"] and mute_result["stdout"].strip().lower() == "true"
            
            return {"volume": volume, "muted": muted, "available": True}
    except (ValueError, Exception) as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] Volume error: {e}")
    
    return {"volume": 0, "muted": True, "available": False, "error": "Volume control unavailable"}

async def get_volume():
    return await run_async(get_volume_sync)

def set_volume_sync(percentage):
    percentage = max(0, min(100, int(percentage)))
    script = f"set volume output volume {percentage}"
    result = run_command(["osascript", "-e", script], timeout=2)
    return {"success": result["success"], "volume": percentage} if result["success"] else {"success": False, "error": "Failed"}

async def set_volume(percentage):
    return await run_async(set_volume_sync, percentage)

def toggle_mute_sync():
    mute_script = "output muted of (get volume settings)"
    mute_result = run_command(["osascript", "-e", mute_script], timeout=2)
    
    if mute_result["success"]:
        current_muted = mute_result["stdout"].strip().lower() == "true"
        new_muted = not current_muted
        
        toggle_script = f"set volume output muted {str(new_muted).lower()}"
        result = run_command(["osascript", "-e", toggle_script], timeout=2)
        
        if result["success"]:
            return {"success": True, "muted": new_muted}
    
    return {"success": False, "error": "Failed"}

async def toggle_mute():
    return await run_async(toggle_mute_sync)

def mount_usb_drive(device_path):
    result = run_command(["diskutil", "mount", device_path], timeout=30)
    if result["success"]:
        if "mounted at" in result["stdout"]:
            mount_point = result["stdout"].split("mounted at")[-1].strip()
            return {"success": True, "mount_point": mount_point, "message": f"Mounted at {mount_point}"}
        return {"success": True, "message": "Mounted successfully"}
    
    return {"success": False, "error": result.get("error", "Mount failed")}

def safely_remove_usb(device_path):
    result = run_command(["diskutil", "unmount", device_path], timeout=30)
    if result["success"]:
        return {"success": True, "message": f"Safely removed {device_path}"}
    
    return {"success": False, "error": result.get("error", "Unmount failed")}

def get_usb_drives(force_scan=False):
    current_time = time.time()
    if not force_scan and (current_time - system_metrics_cache.get("last_usb_scan", 0)) < USB_SCAN_INTERVAL:
        return system_metrics_cache.get("drives", [])
    
    try:
        usb_drives = []
        volumes_path = "/Volumes"

        if not os.path.exists(volumes_path):
            return []

        ls_result = run_command(["ls", "-1", volumes_path], timeout=2)
        if not ls_result.get("success"):
            try:
                volume_names = [name for name in os.listdir(volumes_path) if not name.startswith('.')]
            except OSError:
                return []
        else:
            volume_names = [name for name in ls_result["stdout"].splitlines() if name and not name.startswith('.')]

        blocked_volumes = {"Macintosh HD", "System", "Data", "Preboot", "Recovery", "VM"}

        for volume_name in volume_names:
            if volume_name in blocked_volumes:
                continue
                
            volume_path = os.path.join(volumes_path, volume_name)
            if not os.path.isdir(volume_path):
                continue

            try:
                device_node = ""
                size_gb = 0.0
                filesystem = "unknown"

                df_result = run_command(["df", "-k", volume_path], timeout=2)
                if df_result.get("success"):
                    lines = df_result["stdout"].splitlines()
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        if len(parts) >= 6:
                            device_node = parts[0]
                            try:
                                blocks_1k = int(parts[1])
                                size_gb = round((blocks_1k * 1024) / (1024**3), 2)
                            except ValueError:
                                pass

                du_result = run_command(["diskutil", "info", volume_path], timeout=2)
                if du_result.get("success"):
                    for line in du_result["stdout"].splitlines():
                        line = line.strip()
                        if not device_node and line.startswith("Device Node:"):
                            device_node = line.split(":", 1)[1].strip()
                        if line.startswith("File System Personality:"):
                            filesystem = line.split(":", 1)[1].strip() or filesystem

                if not size_gb:
                    try:
                        st = os.statvfs(volume_path)
                        total_bytes = st.f_frsize * st.f_blocks
                        size_gb = round(total_bytes / (1024**3), 2)
                    except Exception:
                        pass

                device_node = device_node or f"/dev/disk_for_{volume_name}"

                device_info = {
                    "device_node": device_node,
                    "name": volume_name,
                    "size_gb": size_gb,
                    "files": [],
                    "mount_points": [{
                        "device": device_node,
                        "mount_point": volume_path,
                        "mount_name": volume_name,
                        "filesystem": filesystem
                    }]
                }

                if len(usb_drives) < 3:
                    try:
                        device_info["files"] = list_directory_contents(volume_path)
                    except Exception:
                        device_info["files"] = []

                usb_drives.append(device_info)

            except Exception as e:
                if "--debug" in sys.argv:
                    print(f"[roturLink] Error processing {volume_name}: {e}")
                continue

        system_metrics_cache["drives"] = usb_drives
        system_metrics_cache["last_usb_scan"] = current_time
        return usb_drives

    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[roturLink] USB drives error: {e}")
        return system_metrics_cache.get("drives", [])

def list_directory_contents(path):
    try:
        if not os.path.exists(path) or not os.path.isdir(path):
            return []
        
        items = []
        try:
            entries = os.listdir(path)
        except (OSError, PermissionError):
            return []
            
        entries.sort(key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        
        for entry in entries[:100]:
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
        if "--debug" in sys.argv:
            print(f"[roturLink] List dir error: {e}")
        return []

def read_file_content(file_path, max_size=1024*1024):
    try:
        if not os.path.exists(file_path) or not os.path.isfile(file_path) or not os.access(file_path, os.R_OK):
            return {"error": "File not accessible"}
        
        file_size = os.path.getsize(file_path)
        if file_size > max_size:
            return {"error": f"File too large (max {max_size//1024}KB)"}
        
        try:
            with open(file_path, 'rb') as f:
                sample = f.read(1024)
                text_chars = bytes(range(32, 127)) + b'\n\r\t\f\b'
                is_text = not bool(sample.translate(None, text_chars))
        except Exception:
            is_text = False
        
        if is_text:
            for encoding in ['utf-8', 'latin-1', 'cp1252']:
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        return {"content": f.read(), "type": "text", "size": file_size, "encoding": encoding}
                except UnicodeDecodeError:
                    continue
        
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
    except Exception:
        return False

async def broadcast_to_all_clients(message):
    if not connected_clients:
        return
    disconnected = []
    for client in list(connected_clients):
        if not await send_to_client(client, message):
            disconnected.append(client)
    for client in disconnected:
        connected_clients.discard(client)

async def update_and_broadcast_metrics():
    while True:
        try:
            if not connected_clients:
                await asyncio.sleep(10.0)
                continue
            
            current_time = time.time()
            
            if current_time - system_metrics_cache.get("last_basic_update", 0) > BASIC_METRICS_INTERVAL:
                try:
                    system_metrics_cache["cpu_percent"] = psutil.cpu_percent(interval=None)
                    
                    memory = psutil.virtual_memory()
                    system_metrics_cache["memory"] = {
                        "total": memory.total,
                        "used": memory.used,
                        "percent": memory.percent
                    }
                    
                    network = psutil.net_io_counters()
                    system_metrics_cache["network"] = {
                        "sent": network.bytes_sent,
                        "received": network.bytes_recv
                    }
                    
                    system_metrics_cache["last_basic_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv:
                        print(f"[roturLink] Basic metrics error: {e}")
            
            if current_time - system_metrics_cache.get("last_disk_update", 0) > DISK_UPDATE_INTERVAL:
                try:
                    disk = psutil.disk_usage("/")
                    system_metrics_cache["disk"] = {
                        "total": disk.total,
                        "used": disk.used,
                        "percent": disk.percent
                    }
                    system_metrics_cache["last_disk_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv:
                        print(f"[roturLink] Disk metrics error: {e}")
            
            if current_time - system_metrics_cache.get("last_battery_update", 0) > BATTERY_UPDATE_INTERVAL:
                try:
                    if hasattr(psutil, "sensors_battery"):
                        battery = psutil.sensors_battery()
                        if battery:
                            system_metrics_cache["battery"] = {
                                "percent": round(battery.percent, 1),
                                "plugged": battery.power_plugged
                            }
                    system_metrics_cache["last_battery_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv:
                        print(f"[roturLink] Battery error: {e}")
            
            if connected_clients:
                try:
                    message = {"cmd": "metrics_update", "val": get_system_metrics()}
                    asyncio.create_task(broadcast_to_all_clients(message))
                except Exception as e:
                    if "--debug" in sys.argv:
                        print(f"[roturLink] Broadcast error: {e}")
                
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[roturLink] Metrics error: {e}")
        
        await asyncio.sleep(METRICS_INTERVAL)

async def update_and_broadcast_bluetooth():
    while True:
        try:
            paired_devices = await asyncio.to_thread(get_paired_bluetooth_devices)
            nearby_devices = await asyncio.wait_for(scan_bluetooth_devices(), timeout=3.0)
            
            all_devices = {}
            for device in paired_devices:
                all_devices[device["address"]] = device
            
            for device in nearby_devices:
                addr = device["address"]
                if addr in all_devices:
                    all_devices[addr].update({
                        "rssi": device["rssi"],
                        "nearby": True,
                        "last_seen": device["last_seen"]
                    })
                else:
                    all_devices[addr] = device
            
            devices_list = list(all_devices.values())
            system_metrics_cache["bluetooth"] = devices_list
            
            if connected_clients:
                message = {
                    "cmd": "bluetooth_update",
                    "val": {
                        "bluetooth": {
                            "devices": devices_list,
                            "paired": [d for d in devices_list if d.get("paired")],
                            "nearby": [d for d in devices_list if d.get("nearby")],
                            "connected": [d for d in devices_list if d.get("connected")],
                            "count": len(devices_list),
                            "timestamp": time.time()
                        }
                    }
                }
                asyncio.create_task(broadcast_to_all_clients(message))
                
        except asyncio.TimeoutError:
            if "--debug" in sys.argv:
                print("[roturLink] Bluetooth scan timeout")
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[roturLink] Bluetooth error: {e}")
                
        await asyncio.sleep(BLUETOOTH_INTERVAL)

async def update_and_broadcast_wifi():
    while True:
        try:
            if not connected_clients:
                await asyncio.sleep(60.0)
                continue
            
            try:
                wifi_info = await asyncio.wait_for(get_wifi_info(), timeout=8.0)
                system_metrics_cache["wifi"] = wifi_info
                
                if connected_clients:
                    message = {
                        "cmd": "wifi_update",
                        "val": {
                            "wifi": wifi_info,
                            "timestamp": time.time()
                        }
                    }
                    asyncio.create_task(broadcast_to_all_clients(message))
                    
            except asyncio.TimeoutError:
                if "--debug" in sys.argv:
                    print("[roturLink] WiFi scan timeout")
                    
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[roturLink] WiFi error: {e}")
        
        await asyncio.sleep(WIFI_UPDATE_INTERVAL)

def get_drive_identifiers(drives):
    return {drive.get("device_node", "") for drive in drives if drive.get("device_node")}

async def monitor_usb_drives():
    previous_drives = set()
    initial_sent = False
    
    while True:
        try:
            if not connected_clients:
                await asyncio.sleep(60.0)
                previous_drives = set()
                initial_sent = False
                continue
                
            current_drives = await asyncio.to_thread(get_usb_drives, True)
            current_identifiers = get_drive_identifiers(current_drives)
            
            if not initial_sent and connected_clients:
                try:
                    message = {
                        "cmd": "drives_update",
                        "val": {
                            "drives": current_drives,
                            "change_type": "initial"
                        }
                    }
                    asyncio.create_task(broadcast_to_all_clients(message))
                    initial_sent = True
                except Exception:
                    pass

            if previous_drives and current_identifiers != previous_drives:
                removed_drives = previous_drives - current_identifiers
                added_drives = current_identifiers - previous_drives
                
                if removed_drives or added_drives:
                    if "--debug" in sys.argv:
                        if removed_drives:
                            print(f"[roturLink] Drives removed: {removed_drives}")
                        if added_drives:
                            print(f"[roturLink] Drives added: {added_drives}")
                    
                    updated_drives = await asyncio.to_thread(get_usb_drives, True)
                    if connected_clients:
                        change_type = "removal" if removed_drives else "addition"
                        message = {
                            "cmd": "drives_update",
                            "val": {
                                "drives": updated_drives,
                                "change_type": change_type
                            }
                        }
                        asyncio.create_task(broadcast_to_all_clients(message))
            
            previous_drives = current_identifiers
            system_metrics_cache["last_drive_check"] = time.time()
            
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[roturLink] USB monitor error: {e}")
        
        await asyncio.sleep(USB_MONITOR_INTERVAL)

async def update_and_broadcast_drives():
    while True:
        try:
            if connected_clients:
                drives = await asyncio.to_thread(get_usb_drives)
                message = {
                    "cmd": "drives_update",
                    "val": {
                        "drives": drives,
                        "change_type": "periodic"
                    }
                }
                asyncio.create_task(broadcast_to_all_clients(message))
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[roturLink] Periodic drives update error: {e}")
        await asyncio.sleep(DRIVE_BROADCAST_INTERVAL)

async def handle_command(websocket, message):
    try:
        if isinstance(message, str):
            message = json.loads(message)
        
        cmd = message.get("cmd")
        
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
        elif cmd == "bluetooth_scan":
            await send_to_client(websocket, {"cmd": "bluetooth_ack", "val": {"status": "scanning"}})
            nearby = await scan_bluetooth_devices()
            paired = await asyncio.to_thread(get_paired_bluetooth_devices)
            await send_to_client(websocket, {"cmd": "bluetooth_scan_response", "val": {"nearby": nearby, "paired": paired}})
        elif cmd == "bluetooth_connect":
            address = message.get("val", {}).get("address")
            if not address:
                await send_to_client(websocket, {"cmd": "error", "val": {"message": "Address required"}})
            else:
                await send_to_client(websocket, {"cmd": "bluetooth_ack", "val": {"status": "connecting", "address": address}})
                result = await connect_bluetooth_device(address)
                await send_to_client(websocket, {"cmd": "bluetooth_connect_response", "val": result})
        elif cmd == "bluetooth_disconnect":
            address = message.get("val", {}).get("address")
            if not address:
                await send_to_client(websocket, {"cmd": "error", "val": {"message": "Address required"}})
            else:
                await send_to_client(websocket, {"cmd": "bluetooth_ack", "val": {"status": "disconnecting", "address": address}})
                result = await disconnect_bluetooth_device(address)
                await send_to_client(websocket, {"cmd": "bluetooth_disconnect_response", "val": result})
        elif cmd == "bluetooth_pair":
            address = message.get("val", {}).get("address")
            if not address:
                await send_to_client(websocket, {"cmd": "error", "val": {"message": "Address required"}})
            else:
                await send_to_client(websocket, {"cmd": "bluetooth_ack", "val": {"status": "pairing", "address": address}})
                result = await pair_bluetooth_device(address)
                await send_to_client(websocket, {"cmd": "bluetooth_pair_response", "val": result})
        elif cmd == "bluetooth_unpair":
            address = message.get("val", {}).get("address")
            if not address:
                await send_to_client(websocket, {"cmd": "error", "val": {"message": "Address required"}})
            else:
                await send_to_client(websocket, {"cmd": "bluetooth_ack", "val": {"status": "unpairing", "address": address}})
                result = await unpair_bluetooth_device(address)
                await send_to_client(websocket, {"cmd": "bluetooth_unpair_response", "val": result})
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
        await websocket.close()
        return
    
    connected_clients.add(websocket)
    
    try:
        await send_to_client(websocket, {"cmd": "handshake", "val": {"server": "rotur-websocket", "version": "1.0.0"}})
        await send_to_client(websocket, {"cmd": "system_info", "val": get_system_info()})
        await send_to_client(websocket, {"cmd": "metrics", "val": get_system_metrics()})
        
        try:
            drives_now = await asyncio.to_thread(get_usb_drives)
            await send_to_client(websocket, {"cmd": "drives_update", "val": {"drives": drives_now, "change_type": "initial"}})
        except Exception:
            pass
        
        async for message in websocket:
            asyncio.create_task(handle_command(websocket, message))
    except Exception:
        pass
    finally:
        connected_clients.discard(websocket)

async def start_websocket_server():
    fetch_allowed_origins()
    asyncio.create_task(update_and_broadcast_metrics())
    asyncio.create_task(update_and_broadcast_bluetooth())
    asyncio.create_task(update_and_broadcast_wifi())
    asyncio.create_task(monitor_usb_drives())
    asyncio.create_task(update_and_broadcast_drives())
    
    async with websockets.serve(handler, "127.0.0.1", 5002, ping_interval=None):
        if "--debug" in sys.argv:
            print("[roturLink] WebSocket server at ws://127.0.0.1:5002")
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
        response.headers.update({
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
            'Access-Control-Max-Age': '86400'
        })
        return response
        
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "URL parameter missing"}), 400
    
    try:
        headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'content-length', 'connection']}
        response = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            params=request.args,
            data=request.get_data(),
            timeout=10,
            allow_redirects=True
        )
        
        proxy_response = Response(response.content)
        if 'Content-Type' in response.headers:
            proxy_response.headers['Content-Type'] = response.headers['Content-Type']
        proxy_response.headers.update({
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
            'Access-Control-Allow-Headers': '*'
        })
        return proxy_response, response.status_code
        
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

def validate_usb_path(path):
    usb_drives = get_usb_drives()
    allowed_paths = [mp["mount_point"] for drive in usb_drives for mp in drive.get("mount_points", [])]
    full_path = path if path.startswith('/') else f"/{path}"
    return any(full_path.startswith(ap) for ap in allowed_paths), full_path

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

@create_endpoint("/bluetooth/devices")
def bluetooth_devices():
    paired = get_paired_bluetooth_devices()
    nearby = list(BLUETOOTH_DEVICES.values())
    
    all_devices = {}
    for device in paired:
        all_devices[device["address"]] = device
    for device in nearby:
        addr = device["address"]
        if addr in all_devices:
            all_devices[addr].update({"rssi": device["rssi"], "nearby": True})
        else:
            all_devices[addr] = device
    
    return jsonify({
        "devices": list(all_devices.values()),
        "paired": paired,
        "nearby": nearby
    })

@create_endpoint("/bluetooth/scan", ["POST"])
def bluetooth_scan():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        nearby = loop.run_until_complete(scan_bluetooth_devices())
        return jsonify({"success": True, "devices": nearby})
    finally:
        loop.close()

@create_endpoint("/bluetooth/connect", ["POST"])
def bluetooth_connect():
    data = request.get_json()
    if not data or "address" not in data:
        return jsonify({"error": "Address required"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(connect_bluetooth_device(data["address"]))
        return jsonify(result)
    finally:
        loop.close()

@create_endpoint("/bluetooth/disconnect", ["POST"])
def bluetooth_disconnect():
    data = request.get_json()
    if not data or "address" not in data:
        return jsonify({"error": "Address required"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(disconnect_bluetooth_device(data["address"]))
        return jsonify(result)
    finally:
        loop.close()

@create_endpoint("/bluetooth/pair", ["POST"])
def bluetooth_pair():
    data = request.get_json()
    if not data or "address" not in data:
        return jsonify({"error": "Address required"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(pair_bluetooth_device(data["address"]))
        return jsonify(result)
    finally:
        loop.close()

@create_endpoint("/bluetooth/unpair", ["POST"])
def bluetooth_unpair():
    data = request.get_json()
    if not data or "address" not in data:
        return jsonify({"error": "Address required"}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(unpair_bluetooth_device(data["address"]))
        return jsonify(result)
    finally:
        loop.close()

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
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)