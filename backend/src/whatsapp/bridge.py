import os
import json
import subprocess
import threading
import queue
import time
from typing import Optional, Callable


class WhatsAppBridge:
    def __init__(self, auth_dir: Optional[str] = None,
                 phone_number: Optional[str] = None,
                 session_id: Optional[str] = None):
        self.auth_dir = auth_dir or os.path.expanduser(
            '~/.ai-agent-system/credentials/whatsapp/default'
        )
        self.phone_number = phone_number
        self.session_id = session_id
        self.gateway_script = os.path.join(os.path.dirname(__file__), 'gateway_v3.js')
        self.process: Optional[subprocess.Popen] = None
        self.event_queue = queue.Queue()
        self.callbacks = {
            'pairing_code': None,
            'connection': None,
            'message': None,
            'contacts': None,
            'history_messages': None,
            'agent_control': None,
            'error': None,
            'ack': None,
            'restart_requested': None,
        }
        self.is_running = False

        # ── Intentional-stop flag ────────────────────────────────────────────
        # Set BEFORE killing the process so _health_monitor does not treat the
        # death as an unexpected crash and attempt a restart.
        self._intentional_stop = threading.Event()

        # ── Restart-in-progress lock ──────────────────────────────────────────
        # Prevents _health_monitor and _monitor_stdout from both triggering
        # _attempt_restart simultaneously (double-restart bug).
        self._restart_in_progress = threading.Event()

        # ── Send queue for non-blocking bridge writes ─────────────────────────
        # FIX (critical): bridge.send_message/react/delete are called directly
        # from async coroutines running on the asyncio event loop.  The original
        # code called time.sleep(5) inside _attempt_restart from the BrokenPipe
        # error path — this blocked the entire event loop.
        #
        # Solution: writes go onto a thread-safe queue. A dedicated daemon
        # thread drains the queue so the event loop thread never blocks.
        self._send_queue: queue.Queue = queue.Queue()
        self._send_thread: Optional[threading.Thread] = None

        # Monitor threads — tracked so stop() can join them
        self._monitor_threads: list = []

        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5

    # ── Public API ────────────────────────────────────────────────────────────

    def on_event(self, event_type: str, callback: Callable):
        self.callbacks[event_type] = callback

    def start(self):
        # If already running, don't start another one
        if self.process and self.process.poll() is None:
            print("[Bridge] start() called but gateway is already running")
            return
            
        self.is_running = True

        self._intentional_stop.clear()

        try:
            result = subprocess.run(
                ['node', '--version'], capture_output=True, check=True, timeout=5
            )
            print(f"✅ Node.js detected: {result.stdout.decode().strip()}")
        except Exception as e:
            print(f"❌ ERROR: 'node' not found. Install Node.js. ({e})")
            return

        print(f"🚀 Starting WhatsApp Gateway at {self.gateway_script}")

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
                env=env,
            )
            self.is_running = True
            self.reconnect_attempts = 0

            # Drain the send queue (carries over from before restart)
            # Don't clear it — queued messages should still be delivered
            # after a restart once the socket reconnects.

            # Start the send drain thread
            self._send_thread = threading.Thread(
                target=self._drain_send_queue, daemon=True, name="wa-send"
            )
            self._send_thread.start()

            self._monitor_threads = []
            for target, name in [
                (self._monitor_stdout, "wa-stdout"),
                (self._monitor_stderr, "wa-stderr"),
                (self._health_monitor, "wa-health"),
            ]:
                t = threading.Thread(target=target, daemon=True, name=name)
                t.start()
                self._monitor_threads.append(t)

            print("✅ WhatsApp Gateway started successfully")
        except Exception as e:
            print(f"❌ FAILED to start WhatsApp Gateway: {e}")
            self.is_running = False

    def stop(self):
        """
        Stop the gateway.  Sets _intentional_stop BEFORE killing the process
        so _health_monitor never tries to restart after an explicit stop.
        """
        self._intentional_stop.set()
        self.is_running = False
        self._restart_in_progress.clear()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("⚠️  Bridge: process didn't terminate — killing forcefully")
                self.process.kill()
                self.process.wait()
            except Exception:
                pass
            finally:
                self.process = None
                print("🛑 WhatsApp Gateway stopped")

        # Unblock the send-drain thread
        self._send_queue.put(None)

        for t in self._monitor_threads:
            t.join(timeout=3)
        self._monitor_threads = []

    # ── Send helpers ──────────────────────────────────────────────────────────

    def _enqueue(self, command: dict):
        """
        Push a command onto the non-blocking send queue.

        FIX: The old code wrote directly to process.stdin inside the async
        event loop, and on BrokenPipeError called time.sleep(5) which froze
        the event loop.  Now all writes happen on the _drain_send_queue thread.
        Callers on the event loop just enqueue and return instantly.
        """
        if not self.is_running:
            raise RuntimeError("Gateway not running")
        self._send_queue.put(command)

    def _drain_send_queue(self):
        """Background thread: drain _send_queue and write to Node stdin."""
        while True:
            item = self._send_queue.get()
            if item is None:  # Sentinel — stop the thread
                break
            if self._intentional_stop.is_set():
                break
            # Wait until the process is alive and stdin is writable
            if not self.process or not self.is_running:
                continue
            try:
                self.process.stdin.write(json.dumps(item) + '\n')
                self.process.stdin.flush()
            except BrokenPipeError:
                # Process died — _health_monitor or _monitor_stdout will handle restart.
                # Don't sleep here: just stop draining.  The command is lost but
                # the restart will bring a fresh session.
                print("[Bridge] Send queue: broken pipe — waiting for restart")
                # Signal that restart is needed if not already in progress
                if not self._intentional_stop.is_set() and not self._restart_in_progress.is_set():
                    self._restart_in_progress.set()
                    threading.Thread(
                        target=self._attempt_restart, daemon=True, name="wa-restart"
                    ).start()
                # Pause sending until the process comes back
                while self.is_running and not self._intentional_stop.is_set():
                    if self.process and self.process.poll() is None:
                        break  # Process is alive again — resume
                    time.sleep(0.5)
            except Exception as e:
                print(f"[Bridge] Send error: {e}")

    def send_message(self, to: str, text: str,
                     media: Optional[str] = None, media_type: Optional[str] = None):
        self._enqueue({
            'type': 'send_message',
            'to': to,
            'text': text,
            'media': media,
            'mediaType': media_type,
            'id': int(time.time() * 1000),
        })

    def react(self, to: str, message_id: str, emoji: str):
        self._enqueue({
            'type': 'react',
            'to': to,
            'messageId': message_id,
            'emoji': emoji,
            'id': int(time.time() * 1000),
        })

    def delete(self, to: str, message_id: str):
        self._enqueue({
            'type': 'delete_message',
            'to': to,
            'messageId': message_id,
            'id': int(time.time() * 1000),
        })

    def get_contacts(self):
        if not self.is_running:
            return
        try:
            self._enqueue({'type': 'get_contacts', 'id': int(time.time() * 1000)})
        except Exception as e:
            print(f"[Bridge] Failed to request contacts: {e}")

    # ── Monitor threads ───────────────────────────────────────────────────────

    def _monitor_stdout(self):
        """
        Listen for JSON events from Node stdout.

        FIX: When readline() returns '' the process has died.  We now trigger
        a restart immediately rather than waiting up to 30 s for _health_monitor
        to notice.
        """
        for line in iter(self.process.stdout.readline, ''):
            if not line:
                break

            try:
                start = line.find('{')
                end = line.rfind('}')

                if start == -1 or end == -1 or end <= start:
                    raise json.JSONDecodeError("No JSON", line, 0)

                event = json.loads(line[start:end + 1])
                event_type = event.get('type')

                if event_type == 'restart_requested':
                    if not self._intentional_stop.is_set():
                        if not self._restart_in_progress.is_set():
                            self._restart_in_progress.set()
                            print(f"[Bridge] Node requested restart "
                                  f"(reason: {event.get('reason', 'unknown')})")
                            threading.Thread(
                                target=self._attempt_restart,
                                daemon=True,
                                name="wa-restart",
                            ).start()
                        else:
                            print("[Bridge] Node requested restart but one already in progress — skipping")
                    else:
                        print("[Bridge] Node restart_requested ignored (intentional stop)")
                    continue

                if event_type in self.callbacks and self.callbacks[event_type]:
                    try:
                        self.callbacks[event_type](event)
                    except Exception as e:
                        print(f"[Bridge] Callback error for {event_type}: {e}")
                else:
                    self.event_queue.put(event)

            except (json.JSONDecodeError, ValueError):
                clean = line.strip()
                if clean and len(clean) < 200 and not any(
                    n in clean for n in ["pino", "{", "Buffer", "pubKey", "privKey",
                                         "rootKey", "baseKey", "Closing session:"]
                ):
                    print(f"[Node] {clean}")

        # ── stdout EOF: process died ──────────────────────────────────────────
        # FIX: trigger restart immediately instead of waiting for _health_monitor
        if not self._intentional_stop.is_set() and not self._restart_in_progress.is_set():
            self._restart_in_progress.set()
            print("[Bridge] stdout closed — process died, triggering restart")
            self.is_running = False
            threading.Thread(
                target=self._attempt_restart, daemon=True, name="wa-restart"
            ).start()

    def _monitor_stderr(self):
        """Log Node stderr; extract pairing codes embedded in log lines."""
        for line in iter(self.process.stderr.readline, ''):
            if not line:
                break
            clean = line.strip()
            if not clean:
                continue
            if any(skip in clean for skip in ["Health", "Media", "Gateway alive"]):
                continue

            if "Pairing code requested successfully:" in clean:
                code = clean.split("Pairing code requested successfully:")[1].strip()
                if self.callbacks.get('pairing_code'):
                    try:
                        self.callbacks['pairing_code']({"code": code})
                    except Exception as e:
                        print(f"[Bridge] Pairing callback error: {e}")

            print(f"[Node] {clean}")

    def _health_monitor(self):
        """
        Secondary watchdog — catches cases where stdout/stderr close without
        emitting restart_requested (e.g. OOM kill, SIGKILL from OS).

        FIX: Snapshots self.process at the start of each 30 s cycle.  If the
        process object changed (because _attempt_restart already ran), the old
        snapshot no longer matches and we skip the cycle — no double restart.
        """
        while self.is_running and not self._intentional_stop.is_set():
            watched_process = self.process  # Snapshot

            for _ in range(30):
                if not self.is_running or self._intentional_stop.is_set():
                    return
                time.sleep(1)

            if self._intentional_stop.is_set():
                return

            # If the process object was replaced (restart happened), loop again
            if self.process is not watched_process:
                continue

            if self.process and self.process.poll() is not None:
                if self._intentional_stop.is_set():
                    return
                if self._restart_in_progress.is_set():
                    print("[Bridge] Health monitor: restart already in progress — skipping")
                    return
                self._restart_in_progress.set()
                print("❌ Health monitor: gateway process died unexpectedly")
                self.is_running = False
                threading.Thread(
                    target=self._attempt_restart, daemon=True, name="wa-restart"
                ).start()
                return

    def _attempt_restart(self):
        """
        Restart the gateway after a delay.

        FIX: All time.sleep calls happen on this dedicated restart thread, NOT
        on the asyncio event loop thread — eliminating the event loop blockage.
        """
        if self._intentional_stop.is_set():
            print("[Bridge] Restart suppressed — stop was intentional")
            self._restart_in_progress.clear()
            return

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print(f"❌ Max reconnect attempts ({self.max_reconnect_attempts}) reached. Giving up.")
            self._restart_in_progress.clear()
            return

        self.reconnect_attempts += 1
        print(f"🔄 Attempting restart ({self.reconnect_attempts}/{self.max_reconnect_attempts})...")

        # Backoff — safe here because we're on a daemon thread, not the event loop
        time.sleep(5)

        if self._intentional_stop.is_set():
            print("[Bridge] Restart aborted — stop was set during backoff")
            self._restart_in_progress.clear()
            return

        if self.process and self.process.poll() is None:
            print("[Bridge] Killing old process before restart...")
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except:
                pass
            finally:
                self.process = None

        self.start()
        self._restart_in_progress.clear()  # Clear AFTER start() so new monitor can set it

    def get_status(self):
        return {
            "connected": self.is_running and self.process and self.process.poll() is None,
            "reconnect_attempts": self.reconnect_attempts,
        }