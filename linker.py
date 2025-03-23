import subprocess
import sys
from flask import Flask, request, jsonify

try:
    from flask_cors import CORS
except ImportError:
    subprocess.call([sys.executable, "-m", "pip", "install", "flask-cors"])
    from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://turbowarp.org", "https://origin.mistium.com", "http://localhost:5001"])

def isAllowed(request):
    origin = request.headers.get('Origin', '')
    client_ip = request.remote_addr
    print(f"Request from: {origin}, IP: {client_ip}")
    
    allowed_origins = ['https://origin.mistium.com', 'https://turbowarp.org']
    is_local = client_ip == '127.0.0.1' or client_ip == '::1'
    return any(allowed_domain in origin for allowed_domain in allowed_origins) or is_local

@app.route('/run', methods=['GET'])
def run():
    if not isAllowed(request):
        return {"status": "error", "message": "Unauthorized request"}, 403

    command = request.args.get('command')
    if not command:
        return {"status": "error", "message": "No command provided"}, 400
    
    try:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        
        return {
            "status": "success",
            "data": {
                "stdout": stdout.decode('utf-8', errors='replace'),
                "stderr": stderr.decode('utf-8', errors='replace'),
                "exit_code": process.returncode
            }
        }, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001)
