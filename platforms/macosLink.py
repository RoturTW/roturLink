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

if platform.system() != "Darwin":
    print("[roturLink] This script is made for macOS. Running on non-macOS systems may not work as expected.")
    sys.exit(1)

def request_macos_permissions():
    """Request all necessary macOS permissions upfront to avoid popup spam"""
    print("[roturLink] Requesting macOS system permissions...")
    
    permissions_needed = []
    
    # Check Bluetooth availability
    bluetooth_result = run_command(["defaults", "read", "/Library/Preferences/com.apple.Bluetooth", "ControllerPowerState"], timeout=2)
    if not bluetooth_result["success"]:
        permissions_needed.append("Bluetooth access (system preferences)")
    
    # Check location services (required for WiFi scanning)
    location_script = '''
    tell application "System Events"
        try
            do shell script "system_profiler SPAirPortDataType" with administrator privileges
            return "granted"
        on error
            return "denied"
        end try
    end tell
    '''
    
    # Test if we can access system information that requires permissions
    diskutil_result = run_command(["diskutil", "list"], timeout=5)
    if not diskutil_result["success"]:
        permissions_needed.append("Disk access (diskutil)")
    
    # Test brightness control
    brightness_result = run_command(["brightness", "-l"], timeout=2)
    if not brightness_result["success"]:
        permissions_needed.append("Brightness control (install: brew install brightness)")
    
    if permissions_needed:
        print("[roturLink] The following permissions/tools are needed for full functionality:")
        for permission in permissions_needed:
            print(f"  - {permission}")
        print("[roturLink] Some features may be limited without these permissions.")
        print("[roturLink] You may see permission dialogs - please allow access for full functionality.")
        print()
    
    # Pre-authorize common operations to trigger permission dialogs early
    try:
        # Trigger any location/WiFi permission dialogs
        run_command(["system_profiler", "SPAirPortDataType"], timeout=10)
        # Trigger Bluetooth permission dialog
        run_command(["system_profiler", "SPBluetoothDataType"], timeout=5)
    except:
        pass
    
    return True

logging.getLogger().setLevel(logging.ERROR)
sys.stdout = sys.stderr = type('NullWriter', (), {'write': lambda s,x: None, 'flush': lambda s: None})() if "--debug" not in sys.argv else sys.stdout

def ensure_module_installed(module_name, brew_package=None, pip_package=None):
    try: 
        return __import__(module_name)
    except ImportError:
        if pip_package:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pip_package], check=True, capture_output=True)
                return __import__(module_name)
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        if brew_package:
            try:
                subprocess.run(["brew", "install", brew_package], check=True, capture_output=True)
                return __import__(module_name)
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

# Install dependencies
CORS = ensure_module_installed("flask_cors", pip_package="flask-cors").CORS
psutil = ensure_module_installed("psutil", pip_package="psutil")
requests = ensure_module_installed("requests", pip_package="requests")
flask = ensure_module_installed("flask", pip_package="flask")
websockets = ensure_module_installed("websockets", pip_package="websockets")
bluetooth = ensure_module_installed("bleak", pip_package="bleak")

# Optional modules for macOS
OSASCRIPT_AVAILABLE = True  # Always available on macOS
try:
    import plistlib
    PLIST_AVAILABLE = True
except ImportError:
    PLIST_AVAILABLE = False

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
    # Check Bluetooth availability using multiple methods
    bluetooth_available = False
    
    # Method 1: Check if Bluetooth is powered on using system_profiler
    try:
        result = run_command(["system_profiler", "SPBluetoothDataType"], timeout=5)
        if result["success"]:
            stdout = result["stdout"].lower()
            # Look for various indicators that Bluetooth is working
            bluetooth_available = any(indicator in stdout for indicator in [
                "state: on",
                "powered: yes", 
                "discoverable: yes",
                "connectable: yes",
                "controller state: on",
                "bluetooth power: on"
            ])
    except Exception:
        pass
    
    # Method 2: Try using defaults to check Bluetooth state
    if not bluetooth_available:
        try:
            result = run_command(["defaults", "read", "/Library/Preferences/com.apple.Bluetooth", "ControllerPowerState"], timeout=3)
            if result["success"]:
                bluetooth_available = result["stdout"].strip() == "1"
        except Exception:
            pass
    
    # Method 3: Check if bleak can detect Bluetooth adapter
    if not bluetooth_available:
        try:
            import asyncio
            from bleak import BleakScanner
            # Try a very quick Bluetooth test
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                devices = loop.run_until_complete(asyncio.wait_for(BleakScanner.discover(timeout=0.5), timeout=2.0))
                bluetooth_available = True  # If we can scan, Bluetooth is available
            except:
                pass
            finally:
                loop.close()
        except Exception:
            pass
    
    # Method 4: Final fallback - assume available if we can import bleak
    if not bluetooth_available:
        try:
            import bleak
            bluetooth_available = True  # If bleak imported, assume Bluetooth hardware exists
        except ImportError:
            bluetooth_available = False
    
    return {
        "platform": {"system": "macOS", "architecture": platform.machine(), "version": platform.mac_ver()[0]},
        "cpu": {"cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(logical=True)},
        "bluetooth": {"available": bluetooth_available, "backend": "bleak"},
        "memory": {"total_gb": round(psutil.virtual_memory().total / (1024**3), 2)},
        "hostname": platform.node(),
    }

def get_wifi_info_sync():
    try:
        wifi_info = {"connected": False, "ssid": "Unknown", "signal_strength": 0, "scan": []}
        
        # Method 1: Try using system_profiler (more reliable, requires permissions)
        try:
            result = run_command(["system_profiler", "SPAirPortDataType"], timeout=10)
            if result["success"] and "Current Network Information" in result["stdout"]:
                lines = result["stdout"].split("\n")
                for i, line in enumerate(lines):
                    if "Current Network Information" in line:
                        # Look for SSID in the next few lines
                        for j in range(i + 1, min(i + 10, len(lines))):
                            if lines[j].strip().endswith(":") and not ":" in lines[j].strip()[:-1]:
                                ssid = lines[j].strip()[:-1]
                                if ssid and ssid != "Current Network Information":
                                    wifi_info["connected"] = True
                                    wifi_info["ssid"] = ssid
                                    wifi_info["signal_strength"] = 75  # Default strength
                                    break
                        break
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] system_profiler WiFi error: {e}")
        
        # Method 2: Try airport command if system_profiler didn't work
        if not wifi_info["connected"]:
            try:
                result = run_command(["/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport", "-I"], timeout=5)
                if result["success"]:
                    lines = result["stdout"].split("\n")
                    for line in lines:
                        if "SSID:" in line:
                            ssid = line.split("SSID:")[1].strip()
                            if ssid:
                                wifi_info["connected"] = True
                                wifi_info["ssid"] = ssid
                        elif "agrCtlRSSI:" in line:
                            try:
                                rssi = int(line.split("agrCtlRSSI:")[1].strip())
                                wifi_info["signal_strength"] = max(0, min(100, (rssi + 100) * 2))
                            except ValueError:
                                pass
            except Exception as e:
                if "--debug" in sys.argv: print(f"[roturLink] airport -I error: {e}")
        
        # Method 3: Try networksetup as fallback
        if not wifi_info["connected"]:
            try:
                result = run_command(["networksetup", "-getairportnetwork", "en0"], timeout=5)
                if result["success"] and "Current Wi-Fi Network:" in result["stdout"]:
                    ssid = result["stdout"].replace("Current Wi-Fi Network:", "").strip()
                    if ssid and ssid != "You are not associated with an AirPort network.":
                        wifi_info["connected"] = True
                        wifi_info["ssid"] = ssid
                        wifi_info["signal_strength"] = 50  # Default strength
            except Exception as e:
                if "--debug" in sys.argv: print(f"[roturLink] networksetup error: {e}")
        
        # Get nearby networks (only if we have permissions and tools working)
        nearby_networks = []
        try:
            # Try airport scan with shorter timeout
            scan_result = run_command(["/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport", "-s"], timeout=8)
            if scan_result["success"] and scan_result["stdout"].strip():
                lines = scan_result["stdout"].split("\n")[1:]  # Skip header
                seen_ssids = set()
                
                for line in lines:
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 3:
                            ssid = parts[0]
                            if ssid and ssid not in seen_ssids and not ssid.startswith("*"):
                                seen_ssids.add(ssid)
                                try:
                                    rssi = int(parts[2])
                                    signal_strength = max(0, min(100, (rssi + 100) * 2))
                                    
                                    network_info = {
                                        "ssid": ssid,
                                        "signal_strength": signal_strength,
                                        "frequency": 0,  # Not easily available
                                        "connected": ssid == wifi_info.get("ssid", "")
                                    }
                                    nearby_networks.append(network_info)
                                except (ValueError, IndexError):
                                    continue
                
                # Sort by signal strength
                nearby_networks.sort(key=lambda x: x["signal_strength"], reverse=True)
                wifi_info["scan"] = nearby_networks[:20]  # Limit to top 20
            else:
                # If scanning failed, provide helpful message
                wifi_info["scan"] = []
                if "--debug" in sys.argv:
                    print("[roturLink] WiFi scanning unavailable - may need location permissions or WiFi hardware")
                    
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] WiFi scan error: {e}")
            wifi_info["scan"] = []
        
        return wifi_info
        
    except Exception as e:
        error_msg = str(e)
        if "operation couldn't be completed" in error_msg.lower() or "permission" in error_msg.lower():
            error_msg = "WiFi access requires location permissions. Please allow in System Preferences > Security & Privacy > Privacy > Location Services"
        return {"error": error_msg, "connected": False, "scan": []}

async def get_wifi_info():
    return await run_async(get_wifi_info_sync)

def get_brightness_sync():
    # Try using brightness command line tool if available
    result = run_command(["brightness", "-l"])
    if result["success"]:
        try:
            # Parse brightness output
            lines = result["stdout"].split("\n")
            for line in lines:
                if "brightness" in line.lower():
                    brightness_val = float(line.split()[-1])
                    return {"brightness": int(brightness_val * 100), "available": True}
        except (ValueError, IndexError):
            pass
    
    # Fallback to osascript
    script = 'tell application "System Events" to tell appearance preferences to get dark mode'
    result = run_command(["osascript", "-e", script])
    if result["success"]:
        # This is a fallback - we can't easily get brightness without additional tools
        return {"brightness": 50, "available": False, "error": "Brightness control requires additional tools"}
    
    return {"brightness": 0, "available": False, "error": "Brightness control not available"}

async def get_brightness():
    return await run_async(get_brightness_sync)

async def set_brightness(percentage):
    percentage = max(1, min(100, int(percentage)))
    
    # Try using brightness command if available
    result = await run_async(run_command, ["brightness", str(percentage / 100.0)])
    if result["success"]:
        return {"success": True, "brightness": percentage}
    
    # Fallback message
    return {"success": False, "error": "Brightness control requires 'brightness' command line tool. Install with: brew install brightness"}

def get_volume_sync():
    try:
        # Get volume using osascript
        script = "output volume of (get volume settings)"
        result = run_command(["osascript", "-e", script])
        if result["success"]:
            volume = int(result["stdout"])
            
            # Check if muted
            mute_script = "output muted of (get volume settings)"
            mute_result = run_command(["osascript", "-e", mute_script])
            muted = mute_result["success"] and mute_result["stdout"].strip().lower() == "true"
            
            return {"volume": volume, "muted": muted, "available": True}
    except (ValueError, Exception) as e:
        if "--debug" in sys.argv: print(f"[roturLink] Volume error: {e}")
    
    return {"volume": 0, "muted": True, "available": False, "error": "Volume control not available"}

async def get_volume():
    return await run_async(get_volume_sync)

def set_volume_sync(percentage):
    percentage = max(0, min(100, int(percentage)))
    script = f"set volume output volume {percentage}"
    result = run_command(["osascript", "-e", script])
    return {"success": result["success"], "volume": percentage} if result["success"] else {"success": False, "error": "Volume set failed"}

async def set_volume(percentage):
    return await run_async(set_volume_sync, percentage)

def toggle_mute_sync():
    # Get current mute status
    mute_script = "output muted of (get volume settings)"
    mute_result = run_command(["osascript", "-e", mute_script])
    
    if mute_result["success"]:
        current_muted = mute_result["stdout"].strip().lower() == "true"
        new_muted = not current_muted
        
        # Toggle mute
        toggle_script = f"set volume output muted {str(new_muted).lower()}"
        result = run_command(["osascript", "-e", toggle_script])
        
        if result["success"]:
            return {"success": True, "muted": new_muted}
    
    return {"success": False, "error": "Mute toggle failed"}

async def toggle_mute():
    return await run_async(toggle_mute_sync)

def mount_usb_drive(device_path):
    # On macOS, drives are typically auto-mounted
    # This function will attempt to mount if not already mounted
    result = run_command(["diskutil", "mount", device_path], timeout=30)
    if result["success"]:
        # Parse diskutil output to get mount point
        if "mounted at" in result["stdout"]:
            mount_point = result["stdout"].split("mounted at")[-1].strip()
            return {"success": True, "mount_point": mount_point, "message": f"Mounted at {mount_point}"}
        return {"success": True, "message": "Drive mounted successfully"}
    
    return {"success": False, "error": result.get("error", "Mount failed")}

def get_unmounted_usb_devices():
    # On macOS, unmounted devices are not easily accessible via /Volumes
    # This function returns empty list as most USB devices are auto-mounted
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

        # Use `ls /Volumes` as requested to enumerate mounted volumes
        ls_result = run_command(["ls", "-1", volumes_path])
        if not ls_result.get("success"):
            # Fallback to Python listing
            volume_names = [name for name in os.listdir(volumes_path) if not name.startswith('.')]
        else:
            volume_names = [name for name in ls_result["stdout"].splitlines() if name and not name.startswith('.')]

        # System/internal volumes we don't expose
        blocked_volumes = {"Macintosh HD", "System", "Data", "Preboot", "Recovery", "VM"}

        for volume_name in volume_names:
            volume_path = os.path.join(volumes_path, volume_name)
            if not os.path.isdir(volume_path):
                continue
            if volume_name in blocked_volumes:
                continue

            try:
                device_node = ""
                size_gb = 0.0
                filesystem = "unknown"

                # Prefer fast: get device and size via df
                df_result = run_command(["df", "-k", volume_path])
                if df_result.get("success"):
                    lines = df_result["stdout"].splitlines()
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        # Expected: Filesystem, 1024-blocks, Used, Available, Capacity, iused, ifree, %iused, Mounted on
                        if len(parts) >= 6:
                            device_node = parts[0]
                            try:
                                blocks_1k = int(parts[1])
                                size_gb = round((blocks_1k * 1024) / (1024**3), 2)
                            except ValueError:
                                pass

                # Filesystem type via diskutil (fast enough per volume)
                if PLIST_AVAILABLE:
                    du_result = run_command(["diskutil", "info", "-plist", volume_path])
                    if du_result.get("success") and du_result.get("stdout"):
                        try:
                            plist_data = plistlib.loads(du_result["stdout"].encode("utf-8"))
                            filesystem = plist_data.get("FilesystemType") or plist_data.get("FilesystemName", filesystem)
                            if not device_node:
                                device_node = plist_data.get("DeviceNode", device_node)
                            if not size_gb:
                                # Try TotalSize or VolumeTotalSpace (bytes)
                                total_bytes = plist_data.get("TotalSize") or plist_data.get("VolumeTotalSpace")
                                if isinstance(total_bytes, int) and total_bytes > 0:
                                    size_gb = round(total_bytes / (1024**3), 2)
                        except Exception:
                            pass
                else:
                    # Human-readable fallback parsing
                    du_result = run_command(["diskutil", "info", volume_path])
                    if du_result.get("success"):
                        for line in du_result["stdout"].splitlines():
                            line = line.strip()
                            if not device_node and line.startswith("Device Node:"):
                                device_node = line.split(":", 1)[1].strip()
                            if line.startswith("File System Personality:"):
                                filesystem = line.split(":", 1)[1].strip() or filesystem

                # As a last resort, statvfs for size
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

                # Populate top-level files for a few drives only
                if len(usb_drives) < 3:
                    try:
                        device_info["files"] = list_directory_contents(volume_path)
                    except Exception:
                        device_info["files"] = []

                usb_drives.append(device_info)

            except Exception as e:
                if "--debug" in sys.argv:
                    print(f"[roturLink] Error processing volume {volume_name}: {e}")
                continue

        system_metrics_cache["drives"] = usb_drives
        system_metrics_cache["last_usb_scan"] = current_time
        return usb_drives

    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] USB drives error: {e}")
        return system_metrics_cache.get("drives", [])

async def scan_bluetooth_devices():
    try:
        # Skip Bluetooth scanning if no clients are connected
        if not connected_clients:
            return list(BLUETOOTH_DEVICES.values())
        
        # Use cached results if recent
        current_time = time.time()
        if (current_time - system_metrics_cache.get("last_bluetooth_scan", 0) < 30.0 and 
            BLUETOOTH_DEVICES):
            return list(BLUETOOTH_DEVICES.values())
            
        from bleak import BleakScanner
        discovered_devices = await BleakScanner.discover(timeout=1.0)  # Very short timeout
        
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
        
        # Update cache less aggressively
        for device in devices:
            BLUETOOTH_DEVICES[device["address"]] = device
            
        system_metrics_cache["last_bluetooth_scan"] = current_time
        return devices
    except Exception as e:
        if "--debug" in sys.argv: print(f"[roturLink] Bluetooth error: {e}")
        return list(BLUETOOTH_DEVICES.values())

def list_directory_contents(path):
    try:
        if not os.path.exists(path) or not os.path.isdir(path):
            return []
        
        items = []
        entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        
        for entry in entries:
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
            # Only update metrics if we have connected clients
            if not connected_clients:
                await asyncio.sleep(10.0)  # Long sleep when no clients
                continue
            
            # Very basic metrics only
            current_time = time.time()
            
            # Basic system metrics with longer intervals
            if current_time - system_metrics_cache.get("last_basic_update", 0) > 10.0:
                try:
                    # Non-blocking CPU
                    system_metrics_cache["cpu_percent"] = psutil.cpu_percent(interval=None)
                    
                    # Memory (fast)
                    memory = psutil.virtual_memory()
                    system_metrics_cache["memory"] = {"total": memory.total, "used": memory.used, "percent": memory.percent}
                    
                    # Network (fast)
                    network = psutil.net_io_counters()
                    system_metrics_cache["network"] = {"sent": network.bytes_sent, "received": network.bytes_recv}
                    
                    system_metrics_cache["last_basic_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv: print(f"[roturLink] Basic metrics error: {e}")
            
            # Very infrequent disk check (slow operation)
            if current_time - system_metrics_cache.get("last_disk_update", 0) > 30.0:
                try:
                    disk = psutil.disk_usage("/")
                    system_metrics_cache["disk"] = {"total": disk.total, "used": disk.used, "percent": disk.percent}
                    system_metrics_cache["last_disk_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv: print(f"[roturLink] Disk metrics error: {e}")
            
            # Battery check very infrequently
            if current_time - system_metrics_cache.get("last_battery_update", 0) > 60.0:
                try:
                    if hasattr(psutil, "sensors_battery") and psutil.sensors_battery():
                        battery = psutil.sensors_battery()
                        system_metrics_cache["battery"] = {"percent": round(battery.percent, 1), "plugged": battery.power_plugged}
                    system_metrics_cache["last_battery_update"] = current_time
                except Exception as e:
                    if "--debug" in sys.argv: print(f"[roturLink] Battery error: {e}")
            
            # Only broadcast if we have clients
            if connected_clients:
                try:
                    message = {"cmd": "metrics_update", "val": get_system_metrics()}
                    asyncio.create_task(broadcast_to_all_clients(message))
                except Exception as e:
                    if "--debug" in sys.argv: print(f"[roturLink] Broadcast error: {e}")
                
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] Metrics error: {e}")
        
        # Longer sleep to reduce CPU usage
        await asyncio.sleep(5.0)

async def update_and_broadcast_bluetooth():
    while True:
        try:
            # Much longer sleep when no clients
            if not connected_clients:
                await asyncio.sleep(30.0)
                continue
                
            # Only scan occasionally and with timeout
            devices = await asyncio.wait_for(scan_bluetooth_devices(), timeout=3.0)
            system_metrics_cache["bluetooth"] = devices
            
            if connected_clients:
                message = {"cmd": "bluetooth_update", "val": {"bluetooth": {"devices": devices, "count": len(devices), "timestamp": time.time()}}}
                asyncio.create_task(broadcast_to_all_clients(message))
                
                # Very infrequent USB updates
                current_time = time.time()
                if current_time - system_metrics_cache.get("last_usb_broadcast", 0) > 120.0:  # Every 2 minutes
                    try:
                        drives = await asyncio.wait_for(asyncio.to_thread(get_usb_drives), timeout=5.0)
                        message = {"cmd": "drives_update", "val": {"drives": drives, "change_type": "periodic"}}
                        asyncio.create_task(broadcast_to_all_clients(message))
                        system_metrics_cache["last_usb_broadcast"] = current_time
                    except Exception as e:
                        if "--debug" in sys.argv: print(f"[roturLink] USB broadcast error: {e}")
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] Bluetooth error: {e}")
                
        await asyncio.sleep(30.0)  # Much longer sleep

async def update_and_broadcast_wifi():
    while True:
        try:
            # Much longer sleep when no clients
            if not connected_clients:
                await asyncio.sleep(60.0)
                continue
            
            # Only update WiFi info occasionally with timeout
            try:
                wifi_info = await asyncio.wait_for(get_wifi_info(), timeout=8.0)
                system_metrics_cache["wifi"] = wifi_info
                
                if connected_clients:
                    message = {"cmd": "wifi_update", "val": {"wifi": wifi_info, "timestamp": time.time()}}
                    asyncio.create_task(broadcast_to_all_clients(message))
                    
            except Exception as e:
                if "--debug" in sys.argv: print(f"[roturLink] WiFi update error: {e}")
                
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] WiFi update error: {e}")
        
        # Update WiFi much less frequently (every 45 seconds)
        await asyncio.sleep(45.0)

def get_drive_identifiers(drives):
    return set(drive.get("device_node", "") for drive in drives if drive.get("device_node"))

async def monitor_usb_drives():
    previous_drives = set()
    initial_sent = False
    
    while True:
        try:
            # Skip monitoring entirely if no clients connected
            if not connected_clients:
                await asyncio.sleep(60.0)  # Long sleep when no clients
                continue
                
            # Run in thread to avoid blocking
            current_drives = await asyncio.to_thread(get_usb_drives, True)
            current_identifiers = get_drive_identifiers(current_drives)
            
            # Send initial list once when clients are connected
            if not initial_sent and connected_clients:
                try:
                    message = {"cmd": "drives_update", "val": {"drives": current_drives, "change_type": "initial"}}
                    asyncio.create_task(broadcast_to_all_clients(message))
                    initial_sent = True
                except Exception:
                    pass

            # Only notify on actual changes
            if previous_drives and current_identifiers != previous_drives:
                removed_drives = previous_drives - current_identifiers
                added_drives = current_identifiers - previous_drives
                
                if removed_drives or added_drives:
                    if "--debug" in sys.argv:
                        if removed_drives: print(f"[roturLink] USB drives removed: {removed_drives}")
                        if added_drives: print(f"[roturLink] USB drives added: {added_drives}")
                    
                    # Get updated drives and broadcast
                    updated_drives = await asyncio.to_thread(get_usb_drives, True)
                    if connected_clients:
                        message = {"cmd": "drives_update", "val": {"drives": updated_drives, "change_type": "removal" if removed_drives else "addition"}}
                        asyncio.create_task(broadcast_to_all_clients(message))
            
            previous_drives = current_identifiers
            system_metrics_cache["last_drive_check"] = time.time()
            
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] USB monitor error: {e}")
        
        # Much longer sleep
        await asyncio.sleep(15.0)

async def update_and_broadcast_drives():
    """Periodically broadcast current drives to keep UIs in sync."""
    while True:
        try:
            if connected_clients:
                drives = await asyncio.to_thread(get_usb_drives)
                message = {"cmd": "drives_update", "val": {"drives": drives, "change_type": "periodic"}}
                asyncio.create_task(broadcast_to_all_clients(message))
        except Exception as e:
            if "--debug" in sys.argv: print(f"[roturLink] update_and_broadcast_drives error: {e}")
        await asyncio.sleep(30.0)

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
        # Push current drives immediately on connect
        try:
            drives_now = await asyncio.to_thread(get_usb_drives)
            await send_to_client(websocket, {"cmd": "drives_update", "val": {"drives": drives_now, "change_type": "initial"}})
        except Exception:
            pass
        
        async for message in websocket:
            asyncio.create_task(handle_command(websocket, message))
    except:
        pass
    finally:
        connected_clients.discard(websocket)

async def start_websocket_server():
    fetch_allowed_origins()
    asyncio.create_task(update_and_broadcast_metrics())
    # Temporarily disable expensive functions to isolate CPU issue
    # asyncio.create_task(update_and_broadcast_bluetooth())
    # asyncio.create_task(update_and_broadcast_wifi())
    asyncio.create_task(monitor_usb_drives())
    asyncio.create_task(update_and_broadcast_drives())
    
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
    # Match Linux approach: only allow paths under known mounted drive mount_points
    usb_drives = get_usb_drives()
    allowed_paths = [mp["mount_point"] for drive in usb_drives for mp in drive.get("mount_points", [])]
    full_path = f"/{path}" if not path.startswith('/') else path
    return any(full_path.startswith(ap) for ap in allowed_paths), full_path

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

# CPU usage monitoring to reduce load
def should_skip_expensive_operations():
    """Check if we should skip expensive operations due to high CPU usage"""
    try:
        current_cpu = psutil.cpu_percent(interval=None)
        # Skip expensive operations if CPU is over 80%
        return current_cpu > 80.0
    except:
        return False

if __name__ == "__main__":
    # Skip permission request for now to reduce startup CPU
    # request_macos_permissions()
    
    fetch_allowed_origins()
    threading.Thread(target=run_websocket_server, daemon=True).start()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
