import os
import json
import subprocess
import threading
import queue
import time
from typing import Optional, Callable

class WhatsAppBridge:
    def __init__(self, auth_dir: Optional[str] = None, phone_number: Optional[str] = None, session_id: Optional[str] = None):
        self.auth_dir = auth_dir or os.path.expanduser('~/.ai-agent-system/credentials/whatsapp/default')
        self.phone_number = phone_number
        self.session_id = session_id
        self.gateway_script = os.path.join(os.path.dirname(__file__), 'gateway_v3.js')
        self.process: Optional[subprocess.Popen] = None
        self.event_queue = queue.Queue()
        self.callbacks = {
            'pairing_code': None,
            'connection': None,
            'message': None,
            'error': None,
            'ack': None
        }
        self.is_running = False
        self.health_check_thread = None
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5

    def on_event(self, event_type: str, callback: Callable):
        """Register a callback for a specific event type"""
        self.callbacks[event_type] = callback

    def start(self):
        """Start the Node.js gateway process"""
        if self.is_running:
            return

        # Health check for Node.js
        try:
            result = subprocess.run(['node', '--version'], capture_output=True, check=True, timeout=5)
            print(f"✅ Node.js detected: {result.stdout.decode().strip()}")
        except Exception as e:
            print("❌ ERROR: 'node' command not found. Please install Node.js.")
            return

        print(f"🚀 Starting WhatsApp Gateway at {self.gateway_script}")
        
        # Pass the folder name as the Session ID for R2 storage
        session_id = self.session_id or os.path.basename(self.auth_dir.rstrip('/'))
        env = os.environ.copy()
        env['WHATSAPP_SESSION_ID'] = session_id
        
        try:
            args = ['node', self.gateway_script, self.auth_dir]
            if self.phone_number:
                args.append(self.phone_number)

            self.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env
            )
            self.is_running = True
            self.reconnect_attempts = 0
            
            # Start monitoring threads
            threading.Thread(target=self._monitor_stdout, daemon=True).start()
            threading.Thread(target=self._monitor_stderr, daemon=True).start()
            threading.Thread(target=self._health_monitor, daemon=True).start()
            
            print("✅ WhatsApp Gateway started successfully")
        except Exception as e:
            print(f"❌ FAILED to start WhatsApp Gateway: {e}")
            self.is_running = False

    def stop(self):
        """Stop the gateway process"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
            finally:
                self.is_running = False
                print("🛑 WhatsApp Gateway stopped")

    def send_message(self, to: str, text: str, media: Optional[str] = None, media_type: Optional[str] = None):
        """Send a message through the gateway"""
        if not self.process or not self.is_running:
            raise RuntimeError("Gateway not running")

        command = {
            'type': 'send_message',
            'to': to,
            'text': text,
            'media': media,
            'mediaType': media_type,
            'id': int(time.time() * 1000)
        }
        
        try:
            self.process.stdin.write(json.dumps(command) + '\n')
            self.process.stdin.flush()
        except BrokenPipeError:
            print("❌ Bridge: Broken pipe, attempting restart...")
            self._attempt_restart()
            raise RuntimeError("Gateway connection lost")

    def react(self, to: str, message_id: str, emoji: str):
        """Send a reaction"""
        if not self.process or not self.is_running:
            raise RuntimeError("Gateway not running")
            
        command = {
            'type': 'react',
            'to': to,
            'messageId': message_id,
            'emoji': emoji,
            'id': int(time.time() * 1000)
        }
        
        try:
            self.process.stdin.write(json.dumps(command) + '\n')
            self.process.stdin.flush()
        except BrokenPipeError:
            print("❌ Bridge: Broken pipe, attempting restart...")
            self._attempt_restart()
            raise RuntimeError("Gateway connection lost")

    def delete(self, to: str, message_id: str):
        """Delete a message"""
        if not self.process or not self.is_running:
            raise RuntimeError("Gateway not running")
            
        command = {
            'type': 'delete_message',
            'to': to,
            'messageId': message_id,
            'id': int(time.time() * 1000)
        }
        
        try:
            self.process.stdin.write(json.dumps(command) + '\n')
            self.process.stdin.flush()
        except BrokenPipeError:
            print("❌ Bridge: Broken pipe, attempting restart...")
            self._attempt_restart()
            raise RuntimeError("Gateway connection lost")

    def get_contacts(self):
        """Request the full contact list from the gateway"""
        if not self.process or not self.is_running:
            return
            
        command = {
            'type': 'get_contacts',
            'id': int(time.time() * 1000)
        }
        
        try:
            self.process.stdin.write(json.dumps(command) + '\n')
            self.process.stdin.flush()
        except Exception as e:
            print(f"❌ Bridge: Failed to request contacts: {e}")

    def _monitor_stdout(self):
        """Listen for JSON events from Node.js stdout"""
        for line in iter(self.process.stdout.readline, ''):
            if not line:
                break
            
            try:
                # Extract JSON from line
                start = line.find('{')
                end = line.rfind('}')
                
                if start != -1 and end != -1 and end > start:
                    json_str = line[start:end+1]
                    event = json.loads(json_str)
                    event_type = event.get('type')
                    
                    # Dispatch to appropriate callback
                    if event_type in self.callbacks and self.callbacks[event_type]:
                        try:
                            self.callbacks[event_type](event)
                        except Exception as e:
                            print(f"[Bridge] Callback error for {event_type}: {e}")
                    else:
                        self.event_queue.put(event)
                else:
                    raise json.JSONDecodeError("No JSON found", line, 0)
                    
            except (json.JSONDecodeError, ValueError) as e:
                # Filter noise
                clean_line = line.strip()
                if clean_line and not any(noise in clean_line for noise in ["Closing session:", "pino", "{", "Buffer", "pubKey", "privKey", "rootKey", "baseKey"]):
                    if len(clean_line) < 200:
                        print(f"[Node] {clean_line}")

    def _monitor_stderr(self):
        """Log errors from Node.js stderr and parse non-JSON pairing codes."""
        for line in iter(self.process.stderr.readline, ''):
            if not line:
                break
            
            clean_line = line.strip()
            if clean_line:
                # Filter out routine logs
                if any(skip in clean_line for skip in ["Health", "Media", "Gateway alive"]):
                    continue
                
                # Check for Pairing Code printed to stderr by console.error
                if "Pairing code requested successfully:" in clean_line:
                    code = clean_line.split("Pairing code requested successfully:")[1].strip()
                    if self.callbacks.get('pairing_code'):
                        try:
                            self.callbacks['pairing_code']({"code": code})
                        except Exception as e:
                            print(f"[Bridge] Pairing callback error: {e}")
                
                print(f"[Node Error] {clean_line}")

    def _health_monitor(self):
        """Monitor gateway health and restart if needed"""
        while self.is_running:
            time.sleep(30)  # Check every 30 seconds
            
            if self.process and self.process.poll() is not None:
                # Process died
                print("❌ Gateway process died unexpectedly")
                self.is_running = False
                self._attempt_restart()

    def _attempt_restart(self):
        """Attempt to restart the gateway"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print(f"❌ Max reconnect attempts ({self.max_reconnect_attempts}) reached. Giving up.")
            return
        
        self.reconnect_attempts += 1
        print(f"🔄 Attempting restart ({self.reconnect_attempts}/{self.max_reconnect_attempts})...")
        
        time.sleep(5)  # Wait before restart
        self.start()

    def get_status(self):
        """Get gateway status"""
        return {
            "connected": self.is_running and self.process and self.process.poll() is None,
            "reconnect_attempts": self.reconnect_attempts
        }