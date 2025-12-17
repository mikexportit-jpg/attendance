# nfc_scanner.py
import serial
import requests

def main():
    port = 'COM4'
    baud = 9600
    url = "http://127.0.0.1:5000/attendance/scan"  # Your Flask endpoint

    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            print(f"📡 Listening on {port} at {baud} baud...")

            while True:
                data = ser.read(7)  # Read 7 bytes per scan (adjust if needed)
                if data:
                    uid_hex = data.hex().upper()
                    print(f"🆔 Tag scanned: {uid_hex}")

                    try:
                        response = requests.post(url, data={'uid': uid_hex})
                        print("🌐 Status Code:", response.status_code)
                        print("📄 Response:", response.text)
                    except Exception as e:
                        print("❌ Could not send to Flask:", e)

    except Exception as e:
        print("❌ Serial error:", e)

if __name__ == "__main__":
    main()
