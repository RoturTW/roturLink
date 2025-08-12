# roturLink Platform Documentation

## Overview

roturLink provides cross-platform system monitoring and control capabilities through both HTTP REST APIs and WebSocket connections. This document details the implementation differences between the Arch Linux (`archLink.py`) and macOS (`macosLink.py`) versions.

## Architecture

Both implementations follow the same architectural pattern:

- **Flask HTTP Server** (port 5001): REST API endpoints
- **WebSocket Server** (port 5002): Real-time bidirectional communication
- **Background Tasks**: Continuous system monitoring and data collection
- **Thread Pool Executor**: Async operations for non-blocking system calls

## Configuration

Both platforms use an embedded configuration:

```python
CONFIG = {
    "allowed_modules": [
        "system", "cpu", "memory", "disk", "network", 
        "bluetooth", "battery", "temperature"
    ],
    "allowed_origins": [
        "https://turbowarp.org", "https://origin.mistium.com",
        "http://localhost:5001", "http://localhost:5002", "http://localhost:3000",
        "http://127.0.0.1:5001", "http://127.0.0.1:5002", "http://127.0.0.1:3000"
    ]
}
```

## Module Implementations

### 1. System Information Module

**Purpose**: Provides basic system and hardware information

#### Arch Linux Implementation

- **Platform Detection**: Uses `platform.system() == "Linux"`
- **Bluetooth Check**: `bluetoothctl show` command
- **Dependencies**: Native Linux commands

#### macOS Implementation  

- **Platform Detection**: Uses `platform.system() == "Darwin"`
- **Bluetooth Check**: `system_profiler SPBluetoothDataType`
- **Version Info**: Includes macOS version via `platform.mac_ver()[0]`
- **Dependencies**: Native macOS system commands

**Common Data Returned**:

```json
{
    "platform": {"system": "macOS/Arch Linux", "architecture": "arm64/x86_64"},
    "cpu": {"cores": 8, "threads": 16},
    "bluetooth": {"available": true, "backend": "bleak"},
    "memory": {"total_gb": 16.0},
    "hostname": "hostname"
}
```

### 2. CPU Module

**Purpose**: Real-time CPU usage monitoring

#### Both Platforms

- **Implementation**: `psutil.cpu_percent(interval=0.05)`
- **Update Frequency**: 1 second
- **Thread Safety**: Uses system metrics cache

**Data Returned**:

```json
{"cpu": {"percent": 25.4}}
```

### 3. Memory Module

**Purpose**: RAM usage statistics

Both Platforms

- **Implementation**: `psutil.virtual_memory()`
- **Metrics**: Total, used, percentage
- **Update Frequency**: 1 second

**Data Returned**:

```json
{
    "memory": {
        "total": 17179869184,
        "used": 8589934592, 
        "percent": 50.0
    }
}
```

### 4. Disk Module

**Purpose**: Storage usage information

Both Platforms

- **Implementation**: `psutil.disk_usage("/")`
- **Scope**: Root filesystem only
- **Update Frequency**: 1 second

**Data Returned**:

```json
{
    "disk": {
        "total": 1000000000000,
        "used": 500000000000,
        "percent": 50.0
    }
}
```

### 5. Network Module

**Purpose**: Network I/O statistics

Both Platforms

- **Implementation**: `psutil.net_io_counters()`
- **Metrics**: Bytes sent/received (cumulative)
- **Update Frequency**: 1 second

**Data Returned**:

```json
{
    "network": {
        "sent": 1234567890,
        "received": 9876543210
    }
}
```

### 6. WiFi Module

**Purpose**: WiFi connection status and nearby network scanning

Arch Linux

- **Backend**: NetworkManager via `gi.repository.NM`
- **Current Connection**: NM.Client active connections
- **Network Scanning**: `device.request_scan_async()`
- **Signal Strength**: Access point RSSI values
- **Dependencies**: `python-networkmanager`, `python-gobject`

macOS

- **Backend**: Airport framework command-line tool
- **Current Connection**: `/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport -I`
- **Network Scanning**: `airport -s`
- **Signal Strength**: RSSI to percentage conversion: `max(0, min(100, (rssi + 100) * 2))`
- **Dependencies**: Native macOS airport utility

**Data Returned**:

```json
{
    "wifi": {
        "connected": true,
        "ssid": "NetworkName",
        "signal_strength": 85,
        "scan": [
            {"ssid": "Network1", "signal_strength": 90, "frequency": 2412, "connected": false},
            {"ssid": "Network2", "signal_strength": 75, "frequency": 5180, "connected": false}
        ]
    }
}
```

### 7. Bluetooth Module

**Purpose**: Bluetooth device discovery and monitoring

Both Platforms

- **Backend**: `bleak` (cross-platform Bluetooth Low Energy library)
- **Implementation**: `BleakScanner.discover(timeout=2.0)`
- **Update Frequency**: 5 seconds
- **Device Caching**: Maintains `BLUETOOTH_DEVICES` dictionary

**Data Returned**:

```json
{
    "bluetooth": {
        "devices": [
            {"name": "Device Name", "address": "AA:BB:CC:DD:EE:FF", "rssi": -45, "last_seen": 1640995200}
        ],
        "count": 1,
        "timestamp": 1640995200
    }
}
```

### 8. Battery Module

**Purpose**: Battery status and power information

Both Platforms

- **Implementation**: `psutil.sensors_battery()`
- **Availability**: Only on devices with batteries
- **Metrics**: Percentage, charging status

**Data Returned**:

```json
{
    "battery": {
        "percent": 85.5,
        "plugged": true
    }
}
```

### 9. Brightness Control Module

**Purpose**: Display brightness monitoring and control

Arch Linux

- **Read Brightness**: `brightnessctl get` and `brightnessctl max`
- **Set Brightness**: `brightnessctl set {percentage}%`
- **Fallback**: Machine-readable format with `brightnessctl -m`
- **Dependencies**: `brightnessctl` package

macOS

- **Read Brightness**: `brightness -l` command (requires installation)
- **Set Brightness**: `brightness {decimal_value}`
- **Fallback**: Reports unavailable with installation instructions
- **Dependencies**: `brightness` (install via `brew install brightness`)

**API Endpoints**:

- `GET /brightness/get` - WebSocket: `brightness_get`
- `POST /brightness/set/{percentage}` - WebSocket: `brightness_set`

### 10. Volume Control Module

**Purpose**: Audio volume monitoring and control

Arch Linux

- **Primary Backend**: PulseAudio via `pulsectl`
- **Fallback**: ALSA via `amixer` commands
- **Operations**: Get/set volume, mute toggle
- **Dependencies**: `python-pulsectl`, `alsa-utils`

macOS

- **Backend**: AppleScript via `osascript`
- **Get Volume**: `output volume of (get volume settings)`
- **Set Volume**: `set volume output volume {percentage}`
- **Mute Control**: `set volume output muted {true/false}`
- **Dependencies**: Native macOS (osascript always available)

**API Endpoints**:

- `GET /volume/get` - WebSocket: `volume_get`
- `POST /volume/set/{percentage}` - WebSocket: `volume_set`
- `POST /volume/mute` - WebSocket: `volume_mute`

### 11. USB Drive Module

**Purpose**: USB drive detection, mounting, and file system access

Arch Linux

- **Device Detection**: `pyudev` for hardware enumeration
- **Mount Operations**: `udisksctl mount/unmount` with `sudo mount` fallback
- **File System**: Direct `/proc/mounts` parsing
- **Auto-mounting**: Automatic mounting of detected unmounted devices
- **Dependencies**: `python-pyudev`, `udisks2`

macOS

- **Device Detection**: `/Volumes/` directory listing
- **Mount Operations**: `diskutil mount/unmount`
- **File System**: `diskutil info` for device details
- **Volume Filtering**: Excludes system volumes (Macintosh HD, System, Data, etc.)
- **Dependencies**: Native macOS `diskutil`

**Security**: Path validation restricts access to mounted USB drives only

**API Endpoints**:

- `GET /usb/drives` - List mounted drives
- `GET /usb/unmounted` - List unmounted devices
- `POST /usb/mount` - Mount device
- `POST /usb/remove` - Safely unmount device

### 12. File System Module

**Purpose**: File operations on USB drives

Both Platforms

- **Directory Listing**: `os.listdir()` with metadata
- **File Reading**: Text/binary detection with encoding support
- **File Writing**: Text and base64 binary support
- **Operations**: Create directory, delete files/directories

**Security**: All operations restricted to validated USB drive paths

**API Endpoints**:

- `GET /fs/list/{path}` - List directory contents
- `GET /fs/read/{path}` - Read file content
- `POST /fs/write/{path}` - Write file content
- `POST /fs/mkdir/{path}` - Create directory
- `DELETE /fs/delete/{path}` - Delete file/directory

### 13. Temperature Module

**Purpose**: System temperature monitoring

#### Implementation Status

- **Arch Linux**: Not currently implemented (placeholder in config)
- **macOS**: Not currently implemented (placeholder in config)
- **Future**: Could use `psutil.sensors_temperatures()` on Linux, `powermetrics` on macOS

## WebSocket Communication

### Connection Flow

1. **Handshake**: Server sends version info
2. **Initial Data**: System info and current metrics
3. **Real-time Updates**: Periodic metric broadcasts
4. **Command Handling**: Bidirectional command/response

### Message Format

```json
{
    "cmd": "command_name",
    "val": { /* command-specific data */ }
}
```

### Background Tasks

- **Metrics Update**: 1-second interval for CPU, memory, disk, network
- **Bluetooth Scan**: 5-second interval
- **USB Monitor**: 2-second interval for drive changes
- **WiFi Update**: 10-second interval
- **Controls Update**: 5-second interval for brightness/volume

## Security Features

### Origin Validation

- **CORS**: Configurable allowed origins
- **Local Access**: Automatic localhost/127.0.0.1 access
- **Remote Fetching**: Dynamic origin list from `https://link.rotur.dev/allowed.json`

### Path Validation

- **USB Access Only**: File operations restricted to mounted USB drives
- **System Protection**: Blocks access to system volumes/directories
- **Sandboxing**: No access to user directories or system files

## Dependencies Summary

### Arch Linux

- **System**: `pacman` package manager integration
- **Required**: `python-flask`, `python-flask-cors`, `python-psutil`, `python-requests`, `python-websockets`, `python-bleak`
- **Optional**: `python-pulsectl`, `python-pyudev`, `python-networkmanager`, `python-gobject`
- **Tools**: `brightnessctl`, `bluetoothctl`, `amixer`, `udisksctl`

### macOS

- **System**: Homebrew and pip integration
- **Required**: `flask`, `flask-cors`, `psutil`, `requests`, `websockets`, `bleak`
- **Optional**: `brightness` (via Homebrew)
- **Tools**: `diskutil`, `osascript`, `airport`, `system_profiler`

## Error Handling

### Command Execution

- **Timeouts**: 5-second default timeout for system commands
- **Fallbacks**: Multiple implementation strategies per platform
- **Graceful Degradation**: Services continue if individual modules fail

### WebSocket Management

- **Connection Tracking**: Automatic cleanup of disconnected clients
- **Exception Isolation**: Individual command failures don't crash server
- **Broadcast Safety**: Failed sends automatically remove dead connections

## Performance Considerations

### Caching Strategy

- **Metrics Cache**: Prevents excessive system calls
- **Rate Limiting**: Different update frequencies for different data types
- **Lazy Loading**: USB file listings only for first 3 drives

### Async Operations

- **Thread Pool**: Non-blocking system command execution
- **Background Tasks**: Separate coroutines for different monitoring tasks
- **Efficient Broadcasting**: Single message to multiple WebSocket clients

## Development Guidelines

### Adding New Modules

1. Add module name to `CONFIG["allowed_modules"]`
2. Implement platform-specific functions
3. Add caching strategy if needed
4. Create WebSocket command handlers
5. Add HTTP endpoints if required
6. Update this documentation

### Platform-Specific Implementation

1. Create separate functions for each platform
2. Use `run_command()` wrapper for system calls
3. Implement graceful fallbacks
4. Test with and without optional dependencies
5. Document required tools and packages

### Testing Considerations

- **Multi-platform**: Test on both Arch Linux and macOS
- **Permission Levels**: Test with and without sudo/admin access
- **Hardware Variations**: Test on systems with/without batteries, Bluetooth, etc.
- **Network Conditions**: Test WiFi scanning in various environments
- **USB Devices**: Test with different filesystem types and sizes
