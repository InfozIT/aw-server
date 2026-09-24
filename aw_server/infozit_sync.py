import threading
import time
import logging
import uuid
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error
import json
import subprocess
import platform
import socket
import getpass
from aw_core.config import load_config_toml

logger = logging.getLogger(__name__)

class InfozITSyncThread(threading.Thread):
    def __init__(self, api, interval_seconds=60):
        super().__init__(daemon=True)
        self.api = api
        self.interval = interval_seconds
        self.last_sync = datetime.now(timezone.utc) - timedelta(seconds=interval_seconds)
        self.backend_url = "https://2ml9h49p-9002.inc1.devtunnels.ms/api/tracker/sync/events"
        
        # Get the system MAC address formatted as XX:XX:XX:XX:XX:XX
        mac_num = uuid.getnode()
        self.mac_address = ':'.join(('%012X' % mac_num)[i:i+2] for i in range(0, 12, 2))
        self.hardware_id = self._get_hardware_uuid()
        
        # System & User Info
        self.os_type = platform.system()
        self.os_version = platform.release()
        self.hostname = socket.gethostname()
        try:
            self.os_user = getpass.getuser()
        except Exception:
            self.os_user = "unknown"

    def _get_hardware_uuid(self):
        os_type = platform.system()
        try:
            if os_type == "Windows":
                output = subprocess.check_output('wmic csproduct get uuid', shell=True).decode().split('\n')[1].strip()
                return output if output else "unknown-windows-uuid"
            elif os_type == "Darwin":
                output = subprocess.check_output("/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice | grep 'IOPlatformUUID'", shell=True).decode()
                parts = output.split('"')
                if len(parts) >= 4:
                    return parts[3]
                return "unknown-mac-uuid"
            elif os_type == "Linux":
                with open('/etc/machine-id', 'r') as f:
                    return f.read().strip()
        except Exception as e:
            logger.error(f"Failed to get hardware UUID: {e}")
            
        return "unknown-hardware-uuid"

    def run(self):
        logger.info(f"InfozIT Sync Thread started, interval: {self.interval}s")
        while True:
            try:
                time.sleep(self.interval)
                self.sync_events()
            except Exception as e:
                logger.error(f"Error in InfozIT sync loop: {e}")

    def sync_events(self):
        # Read the latest config to see if activation_key is set
        config = load_config_toml("aw-server", '[server]\nactivation_key=""')
        activation_key = config.get("server", {}).get("activation_key", "")

        if not activation_key:
            return

        now = datetime.now(timezone.utc)
        start = self.last_sync
        end = now

        # Get buckets that match the window and afk watchers
        buckets = self.api.get_buckets()
        target_buckets = []
        for bucket_id, bucket_info in buckets.items():
            if bucket_info.get("client", "").startswith("aw-watcher-window") or \
               bucket_info.get("client", "").startswith("aw-watcher-afk"):
                target_buckets.append(bucket_id)

        all_events = []
        for bucket_id in target_buckets:
            try:
                # get_events takes (bucket_id, limit, start, end)
                events = self.api.get_events(bucket_id, start=start, end=end)
                for ev in events:
                    # Append some context to the event for the backend
                    client_name = buckets[bucket_id].get("client", "unknown")
                    formatted_event = {
                        "timestamp": ev.get("timestamp"),
                        "duration": ev.get("duration", 0),
                        "bucket": client_name,
                    }
                    if client_name.startswith("aw-watcher-window"):
                        data = ev.get("data", {})
                        formatted_event["app"] = data.get("app", "unknown")
                        formatted_event["title"] = data.get("title", "")
                        if "url" in data:
                            formatted_event["url"] = data.get("url")
                        if "incognito" in data:
                            formatted_event["incognito"] = data.get("incognito")
                        formatted_event["is_afk"] = False
                    elif client_name.startswith("aw-watcher-afk"):
                        status = ev.get("data", {}).get("status", "")
                        formatted_event["app"] = "System"
                        formatted_event["title"] = f"User is {status}"
                        formatted_event["is_afk"] = (status == "afk")

                    all_events.append(formatted_event)
            except Exception as e:
                logger.warning(f"Failed to fetch events for bucket {bucket_id}: {e}")

        if not all_events:
            self.last_sync = now
            return

        # POST to backend
        try:
            req = urllib.request.Request(self.backend_url, method="POST")
            req.add_header('Content-Type', 'application/json')
            req.add_header('x-activation-key', activation_key)
            req.add_header('x-mac-address', self.mac_address)
            req.add_header('x-hardware-id', self.hardware_id)
            req.add_header('x-os-type', self.os_type)
            req.add_header('x-os-version', self.os_version)
            req.add_header('x-hostname', self.hostname)
            req.add_header('x-os-user', self.os_user)
            
            payload = json.dumps({"events": all_events}).encode('utf-8')
            
            with urllib.request.urlopen(req, data=payload, timeout=10) as response:
                if response.status == 200:
                    logger.debug(f"Synced {len(all_events)} events successfully.")
                    self.last_sync = now
                else:
                    logger.error(f"Sync failed with status {response.status}")
        except urllib.error.HTTPError as e:
            logger.error(f"HTTPError during sync: {e.code} - {e.read().decode('utf-8')}")
        except Exception as e:
            logger.error(f"Failed to send events to backend: {e}")
