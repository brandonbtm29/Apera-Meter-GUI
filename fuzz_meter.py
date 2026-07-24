import serial
import serial.tools.list_ports
import time
import string
import sys

def find_apera_meter():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    print("Scanning COM ports for Apera meter...")
    for p in ports:
        try:
            ts = serial.Serial(p, 115200, timeout=0.5)
            ts.setDTR(False)
            ts.setRTS(False)
            ts.write(b'&OK#\r\n')
            time.sleep(0.3)
            resp = ts.read(ts.in_waiting)
            ts.close()
            if b'<SH' in resp or b'<SL' in resp or b'#>' in resp or b'<' in resp:
                print(f"Found meter on {p}!")
                return p
        except Exception:
            pass
    return None

def fuzz_meter(port):
    print(f"\n[!] WARNING: Fuzzing can potentially cause the meter to factory reset, erase calibration, or enter a locked state.")
    resp = input("Are you sure you want to proceed? (yes/no): ")
    if resp.strip().lower() != 'yes':
        print("Aborting.")
        return

    ser = serial.Serial(port, 115200, timeout=0.1)
    ser.setDTR(False)
    ser.setRTS(False)
    
    print("\n--- Starting Fuzzing ---")
    print("Testing all 2-letter combinations (e.g., &AA# to &ZZ#)...")
    
    known_commands = ['OK', 'WA', 'DT']
    alphabet = string.ascii_uppercase

    for first in alphabet:
        for second in alphabet:
            cmd_str = f"{first}{second}"
            if cmd_str in known_commands:
                continue
                
            packet = f"&{cmd_str}#\r\n".encode('ascii')
            
            # Clear input buffer
            ser.read(ser.in_waiting)
            
            # Send command
            ser.write(packet)
            time.sleep(0.05) # Wait for potential response
            
            if ser.in_waiting > 0:
                resp = ser.read(ser.in_waiting)
                # Ignore the standard data pushes if they happen exactly now,
                # though it's hard to filter. We just print everything.
                print(f"[+] Sent: {packet.strip()} | Received: {resp}")
                
                # Sleep a little longer if we got a hit to let it finish sending
                time.sleep(0.2)
                if ser.in_waiting > 0:
                    print(f"    Extra data: {ser.read(ser.in_waiting)}")

    print("--- Fuzzing Complete ---")
    ser.close()

if __name__ == "__main__":
    print("Apera Meter Fuzzer")
    port = find_apera_meter()
    if not port:
        print("Could not detect Apera meter. Is it plugged in and turned on?")
        sys.exit(1)
        
    fuzz_meter(port)
