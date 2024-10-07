import subprocess
import sys

def install_and_import(package):
    try:
        __import__(package)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        __import__(package)

# List of required packages
required_packages = [
    "threading",
    "tkinter",
    "flask",
    "psutil"
]

# Install and import required packages
for package in required_packages:
    install_and_import(package)

import threading
from tkinter import Tk, Label, Button, Checkbutton, IntVar, messagebox, PhotoImage
from flask import Flask, request
import psutil
import subprocess
import os
import sys
import random
import string

app = Flask(__name__)

TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'token.txt')

def save_token(token):
    with open(TOKEN_FILE, 'w') as f:
        f.write(token)

def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    return None

def generate_token(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

ROTUR_TOKEN = load_token() or generate_token()
save_token(ROTUR_TOKEN)

request_count = 0

deny_run = False
deny_processes = False
deny_stat = False
deny_userinfo = False

@app.before_request
def before_request():
    global request_count
    request_count += 1

@app.route('/link', methods=['GET'])
def link():
    if request.args.get('token') == ROTUR_TOKEN:
        return "Link successful"
    else:
        return "Link failed"

@app.route('/stats', methods=['GET'])
def stat():
    if deny_stat:
        return "Stat access denied"
    if request.args.get('token') == ROTUR_TOKEN:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_info = psutil.virtual_memory()
        disk_usage = psutil.disk_usage('/')

        stats = {
            "cpu_usage": cpu_usage,
            "memory_total": memory_info.total,
            "memory_used": memory_info.used,
            "memory_free": memory_info.free,
            "disk_total": disk_usage.total,
            "disk_used": disk_usage.used,
            "disk_free": disk_usage.free,
            "disk_percent": disk_usage.percent,
            "disk_io_counters": psutil.disk_io_counters()._asdict()
        }

        return stats
    else:
        return "Stat failed"

@app.route('/processes', methods=['GET'])
def processes():
    if deny_processes:
        return "Processes access denied"
    if request.args.get('token') == ROTUR_TOKEN:
        processes = []
        for process in psutil.process_iter():
            processes.append(process.as_dict(attrs=['pid', 'name', 'username', 'cpu_percent', 'memory_percent']))

        return processes
    else:
        return "Processes failed"

@app.route('/run', methods=['GET'])
def run():
    if deny_run:
        return "Run access denied"
    if request.args.get('token') == ROTUR_TOKEN:
        command = request.args.get('command')
        if command:
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            return {
                "stdout": stdout.decode('utf-8'),
                "stderr": stderr.decode('utf-8')
            }
        else:
            return "No command provided"
    else:
        return "Run failed"

@app.route('/userinfo', methods=['GET'])
def userinfo():
    if deny_userinfo:
        return "User info access denied"
    if request.args.get('token') == ROTUR_TOKEN:
        return {
            "user": psutil.users()
        }
    else:
        return "User info failed"

def run_flask():
    app.run(host='127.0.0.1', port=5001)

def start_flask_thread():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

def add_to_startup():
    if sys.platform == "win32":
        startup_folder = os.path.join(os.getenv('APPDATA'), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        script_path = os.path.abspath(__file__)
        bat_path = os.path.join(startup_folder, "start_linker.bat")
        with open(bat_path, "w") as bat_file:
            bat_file.write(f'python "{script_path}"')
    elif sys.platform == "darwin":
        plist_content = f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.user.roturLink</string>
            <key>ProgramArguments</key>
            <array>
                <string>python3</string>
                <string>{os.path.abspath(__file__)}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
        </dict>
        </plist>
        """
        plist_path = os.path.expanduser('~/Library/LaunchAgents/com.user.roturLink.plist')
        with open(plist_path, 'w') as plist_file:
            plist_file.write(plist_content)
        subprocess.call(['launchctl', 'load', plist_path])
    elif sys.platform == "linux":
        autostart_dir = os.path.expanduser('~/.config/autostart')
        os.makedirs(autostart_dir, exist_ok=True)
        desktop_entry = f"""
        [Desktop Entry]
        Type=Application
        Exec=python3 {os.path.abspath(__file__)}
        Hidden=false
        NoDisplay=false
        X-GNOME-Autostart-enabled=true
        Name=Rotur Link
        """
        desktop_path = os.path.join(autostart_dir, 'rotur_link.desktop')
        with open(desktop_path, 'w') as desktop_file:
            desktop_file.write(desktop_entry)

def is_startup_enabled():
    if sys.platform == "win32":
        startup_folder = os.path.join(os.getenv('APPDATA'), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        bat_path = os.path.join(startup_folder, "start_linker.bat")
        return os.path.exists(bat_path)
    elif sys.platform == "darwin":
        plist_path = os.path.expanduser('~/Library/LaunchAgents/com.user.roturLink.plist')
        return os.path.exists(plist_path)
    elif sys.platform == "linux":
        desktop_path = os.path.join(os.path.expanduser('~/.config/autostart'), 'rotur_link.desktop')
        return os.path.exists(desktop_path)
    return False

def create_tkinter_app():
    root = Tk()
    root.title("Rotur Link")

    # Set the application icon
    icon_url = "http://rotur.dev/Rotur%20Logo.png"
    icon_path = os.path.join(os.path.dirname(__file__), "Rotur_Logo.png")
    if not os.path.exists(icon_path):
        import urllib.request
        urllib.request.urlretrieve(icon_url, icon_path)
    root.iconphoto(False, PhotoImage(file=icon_path))

    if not is_startup_enabled() and messagebox.askyesno("Run on Boot", "Do you want to run this application on boot?"):
        add_to_startup()

    label = Label(root, text="Flask server is running on: http://127.0.0.1:5001", anchor='w', justify='left')
    label.pack(pady=10, padx=10, anchor='w')

    token_label = Label(root, text=f"Token: {ROTUR_TOKEN}", anchor='w', justify='left')
    token_label.pack(pady=5, padx=10, anchor='w')

    request_count_label = Label(root, text=f"Total Requests: {request_count}", anchor='w', justify='left')
    request_count_label.pack(pady=5, padx=10, anchor='w')

    def update_deny_flags():
        global deny_run, deny_processes, deny_stat, deny_userinfo
        deny_run = run_var.get() == 1
        deny_processes = processes_var.get() == 1
        deny_stat = stat_var.get() == 1
        deny_userinfo = userinfo_var.get() == 1

    def update_request_count_label():
        request_count_label.config(text=f"Total Requests: {request_count}")
        root.after(1000, update_request_count_label)

    def regenerate_token():
        global ROTUR_TOKEN
        ROTUR_TOKEN = generate_token()
        save_token(ROTUR_TOKEN)
        token_label.config(text=f"Token: {ROTUR_TOKEN}")

    def copy_token():
        root.clipboard_clear()
        root.clipboard_append(ROTUR_TOKEN)
        messagebox.showinfo("Copied", "Token copied to clipboard")

    run_var = IntVar(value=0)
    processes_var = IntVar(value=0)
    stat_var = IntVar(value=0)
    userinfo_var = IntVar(value=0)

    run_check = Checkbutton(root, text="Deny Run: Prevents execution of commands", variable=run_var, command=update_deny_flags, anchor='w', justify='left')
    run_check.pack(pady=5, padx=10, anchor='w')

    processes_check = Checkbutton(root, text="Deny Processes: Prevents access to process list", variable=processes_var, command=update_deny_flags, anchor='w', justify='left')
    processes_check.pack(pady=5, padx=10, anchor='w')

    stat_check = Checkbutton(root, text="Deny Stat: Prevents access to system stats", variable=stat_var, command=update_deny_flags, anchor='w', justify='left')
    stat_check.pack(pady=5, padx=10, anchor='w')

    userinfo_check = Checkbutton(root, text="Deny User Info: Prevents access to user info", variable=userinfo_var, command=update_deny_flags, anchor='w', justify='left')
    userinfo_check.pack(pady=5, padx=10, anchor='w')

    regenerate_button = Button(root, text="Regenerate Token", command=regenerate_token)
    regenerate_button.pack(pady=10, padx=10, anchor='w')

    copy_button = Button(root, text="Copy Token", command=copy_token)
    copy_button.pack(pady=10, padx=10, anchor='w')

    button = Button(root, text="Exit", command=root.quit)
    button.pack(pady=10, padx=10, anchor='w')

    root.after(1000, update_request_count_label)
    root.mainloop()

if __name__ == '__main__':
    start_flask_thread()
    create_tkinter_app()