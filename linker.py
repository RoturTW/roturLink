from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/link', methods=['GET'])
def link():
    return true

if __name__ == '__main__':
    app.run(port=5000)