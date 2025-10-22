# roturLink

A cross-platform system monitoring and control server that provides real-time access to system metrics through both HTTP and WebSocket APIs.

## Supported Platforms

- **Arch Linux** (`platforms/archLink.py`) - Full featured implementation
- **macOS** (`platforms/macosLink.py`) - Native macOS implementation

## Quick Start

### Arch Linux
```bash
cd platforms/
python archLink.py
```

### macOS
```bash
cd platforms/
python macosLink.py
```

The server will start on:
- HTTP API: `http://127.0.0.1:5001`
- WebSocket: `ws://127.0.0.1:5002`

## Available Modules

| Module | Arch Linux | macOS | Description |
|--------|------------|-------|-------------|
| **System** | ✅ | ✅ | Basic system information and hardware details |
| **CPU** | ✅ | ✅ | Real-time CPU usage monitoring |
| **Memory** | ✅ | ✅ | RAM usage statistics |
| **Disk** | ✅ | ✅ | Storage usage information |
| **Network** | ✅ | ✅ | Network I/O statistics |
| **WiFi** | ✅ | ✅ | WiFi status and network scanning |
| **Bluetooth** | ✅ | ✅ | Bluetooth device discovery |
| **Battery** | ✅ | ✅ | Battery status (if available) |
| **Brightness** | ✅ | ⚠️ | Display brightness control |
| **Volume** | ✅ | ✅ | Audio volume control |
| **USB Drives** | ✅ | ✅ | USB drive management and file access |
| **Temperature** | 🚧 | 🚧 | System temperature (planned) |

✅ = Fully implemented  
⚠️ = Requires additional tools  
🚧 = Planned/partial implementation

## Key Features

- **Real-time Monitoring**: WebSocket-based live system metrics
- **Cross-platform**: Platform-specific optimizations for Arch Linux and macOS  
- **Secure Access**: CORS protection and origin validation
- **USB File Access**: Safe file operations on mounted USB drives
- **System Control**: Brightness and volume adjustment
- **Bluetooth Scanning**: Low-energy device discovery
- **WiFi Management**: Network scanning and connection status

## Documentation

For detailed implementation information, see [PLATFORM_DOCUMENTATION.md](PLATFORM_DOCUMENTATION.md)

## Configuration

Both platforms use the same configuration from `link.conf`:

```json
{
    "allowed_modules": ["system", "cpu", "memory", "disk", "network", "bluetooth", "battery", "temperature"],
    "allowed_origins": ["https://turbowarp.org", "https://origin.mistium.com", "http://localhost:5001", "http://localhost:5002", "http://localhost:3000", "http://127.0.0.1:5001", "http://127.0.0.1:5002", "http://127.0.0.1:3000"]
}
```

## Dependencies

### Common Dependencies
```bash
pip install flask flask-cors psutil requests websockets bleak
```

### Arch Linux Specific
```bash
sudo pacman -S python-flask python-flask-cors python-psutil python-requests python-websockets python-bleak
sudo pacman -S python-pulsectl python-pyudev networkmanager python-gobject
sudo pacman -S brightnessctl bluez-utils alsa-utils udisks2
```

### macOS Specific
```bash
pip install flask flask-cors psutil requests websockets bleak
brew install brightness  # Optional, for brightness control
```

## API Usage

### HTTP Endpoints
```bash
# System information
curl http://127.0.0.1:5001/sysinfo

# USB drives
curl http://127.0.0.1:5001/usb/drives

# Volume control
curl http://127.0.0.1:5001/volume/get
curl -X POST http://127.0.0.1:5001/volume/set/50
```

### WebSocket Commands
```javascript
const ws = new WebSocket('ws://127.0.0.1:5002');

// Get system metrics
ws.send(JSON.stringify({cmd: 'get_metrics'}));

// Set brightness
ws.send(JSON.stringify({cmd: 'brightness_set', val: 75}));

// Set volume
ws.send(JSON.stringify({cmd: 'volume_set', val: 50}));
```

## Security

- **Origin Validation**: Configurable CORS origins
- **Local Access**: Automatic localhost access
- **Path Restriction**: USB file operations only
- **System Protection**: No access to system directories

## License

MIT
