#!/usr/bin/env python3
r"""
arm_drive_watcher.py
Automatic Ripping Machine (A.R.M.) - Fast Optical Drive Ingest Loop

Monitors optical drive (Drive E: ASUS BW-16D1HT), triggers MakeMKV for bit-for-bit
demuxing into Node A scratch storage, and IMMEDIATELY EJECTS the drive tray upon
raw dump completion so the user can insert the next disc without waiting for
downstream AI upscaling or AV1 video encoding.
"""

import os
import sys
import time
import json
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import yaml
except ImportError:
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [ARM-Watcher] %(message)s"
)
logger = logging.getLogger("ARM-DriveWatcher")


class DriveWatcher:
    def __init__(self, config_path: Optional[str] = None, config_dict: Optional[Dict[str, Any]] = None, dry_run: bool = False):
        self.dry_run = dry_run
        if config_dict is not None:
            self.config = config_dict
        else:
            self.config = self._load_config(config_path)
        
        self.drive_letter = self.config.get("optical_drive", {}).get("letter", "E:")
        self.clean_drive = self.drive_letter.rstrip(":\\").upper()
        self.poll_interval = self.config.get("optical_drive", {}).get("poll_interval_seconds", 5)
        self.auto_eject = self.config.get("optical_drive", {}).get("auto_eject_immediately_on_dump", True)
        
        # Paths
        dirs = self.config.get("directories", {})
        self.incoming_dir = Path(dirs.get("incoming", r"D:\rips\incoming"))
        self.logs_dir = Path(dirs.get("logs", r"D:\rips\logs"))
        
        # Binaries
        self.makemkv_bin = self.config.get("makemkv", {}).get(
            "executable", r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
        )
        self.min_length = self.config.get("makemkv", {}).get("min_length_seconds", 900)
        self.disc_index = self.config.get("makemkv", {}).get("disc_index", "disc:0")
        
        # State
        self.last_seen_volume: Optional[str] = None
        self._ensure_dirs()

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        default_config = {
            "optical_drive": {"letter": "E:", "poll_interval_seconds": 5, "auto_eject_immediately_on_dump": True},
            "directories": {
                "scratch_root": r"D:\rips",
                "incoming": r"D:\rips\incoming",
                "processing": r"D:\rips\processing",
                "completed": r"D:\rips\completed",
                "logs": r"D:\rips\logs"
            },
            "makemkv": {
                "executable": r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
                "min_length_seconds": 900,
                "disc_index": "disc:0"
            },
            "integrations": {
                "gotify": {"enabled": False, "base_url": "https://notify.ryuns.net", "app_token": ""}
            }
        }
        if not config_path or not os.path.exists(config_path):
            return default_config
        
        if yaml is None:
            logger.warning("PyYAML not installed, using default configuration.")
            return default_config
            
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
            # Shallow merge
            default_config.update(user_config)
            return default_config

    def _ensure_dirs(self):
        if not self.dry_run:
            self.incoming_dir.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)

    def query_drive_volume(self) -> Optional[Dict[str, Any]]:
        """Queries Windows for optical drive status and volume label."""
        if self.dry_run:
            return {"ready": True, "label": "Avatar_Book_1_Disc_2", "size_bytes": 46366588928}

        ps_cmd = (
            f"Get-Volume -DriveLetter {self.clean_drive} -ErrorAction SilentlyContinue | "
            f"Select-Object DriveLetter, FileSystemLabel, Size, SizeRemaining | ConvertTo-Json"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout.strip())
                label = data.get("FileSystemLabel") or "UNKNOWN_DISC"
                size = data.get("Size") or 0
                if size > 0:
                    return {"ready": True, "label": label, "size_bytes": size}
            return None
        except Exception as e:
            logger.debug(f"Error querying drive volume: {e}")
            return None

    def eject_drive_tray(self) -> bool:
        """
        Executes immediate physical tray ejection via Windows Shell COM object.
        This unblocks the optical drive immediately after MakeMKV finishes dumping.
        """
        logger.info(f"Triggering IMMEDIATE tray ejection for Drive {self.clean_drive}: ...")
        if self.dry_run:
            logger.info("[DRY RUN] Tray ejected successfully.")
            return True

        eject_script = (
            f"(New-Object -COM Shell.Application).Namespace(17)"
            f".ParseName('{self.clean_drive}:').InvokeVerb('Eject')"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", eject_script],
                capture_output=True,
                text=True,
                timeout=15
            )
            if res.returncode == 0:
                logger.info(f"Tray on Drive {self.clean_drive}: EJECTED successfully.")
                return True
            else:
                logger.error(f"Failed to eject tray: {res.stderr}")
                return False
        except Exception as e:
            logger.error(f"Exception during tray ejection: {e}")
            return False

    def send_notification(self, title: str, message: str, priority: int = 5):
        """Dispatches alert via Gotify if enabled."""
        logger.info(f"[NOTIFICATION] {title} - {message}")
        gotify_cfg = self.config.get("integrations", {}).get("gotify", {})
        if not gotify_cfg.get("enabled"):
            return

        base_url = gotify_cfg.get("base_url")
        token = gotify_cfg.get("app_token")
        if not base_url or not token:
            return

        import urllib.request
        import urllib.parse
        
        url = f"{base_url.rstrip('/')}/message?token={token}"
        payload = json.dumps({
            "title": title,
            "message": message,
            "priority": priority
        }).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning(f"Failed to deliver Gotify alert: {e}")

    def build_makemkv_command(self, output_dir: Path) -> list:
        return [
            self.makemkv_bin,
            "mkv",
            self.disc_index,
            "all",
            str(output_dir),
            f"--minlength={self.min_length}"
        ]

    def process_disc(self, volume_info: Dict[str, Any]) -> bool:
        disc_label = volume_info["label"]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = self.incoming_dir / f"{disc_label}_{timestamp}"
        
        logger.info(f"Beginning raw bit-for-bit MakeMKV dump for '{disc_label}' -> {target_dir}")
        self.send_notification(
            title="💿 Optical Rip Initiated",
            message=f"Detected '{disc_label}'. Ripping raw streams to scratch storage...",
            priority=4
        )
        
        start_time = time.time()
        target_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.dry_run:
            cmd = self.build_makemkv_command(target_dir)
            log_file = self.logs_dir / f"makemkv_{disc_label}_{timestamp}.log"
            
            with open(log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STREQ, text=True)
                
            if proc.returncode != 0:
                logger.error(f"MakeMKV extraction failed with exit code {proc.returncode}. Log: {log_file}")
                self.send_notification(
                    title="❌ Rip Failed",
                    message=f"MakeMKV encountered an error while ripping '{disc_label}'. Check {log_file.name}",
                    priority=8
                )
                return False
        else:
            time.sleep(0.5)

        duration_sec = int(time.time() - start_time)
        duration_min = round(duration_sec / 60.0, 1)
        
        # Write drop manifest for the asynchronous compute queue
        manifest = {
            "disc_label": disc_label,
            "timestamp": timestamp,
            "size_bytes": volume_info["size_bytes"],
            "dump_duration_seconds": duration_sec,
            "status": "ready_for_compute",
            "created_at": datetime.now().isoformat()
        }
        manifest_path = target_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info(f"MakeMKV dump completed in {duration_min} minutes. Manifest written: {manifest_path}")

        # CRITICAL USER REQUIREMENT: EJECT IMMEDIATELY!
        if self.auto_eject:
            ejected = self.eject_drive_tray()
            self.send_notification(
                title="✨ Disc Ready & Tray Ejected",
                message=(
                    f"'{disc_label}' raw extraction finished in {duration_min} min.\n"
                    f"Drive {self.clean_drive}: tray has been opened. Ready for next disc!\n"
                    f"Asynchronous AI upscaling & AV1 encode queued in background."
                ),
                priority=6
            )
            return ejected

        return True

    def poll_once(self) -> bool:
        vol = self.query_drive_volume()
        if vol is None:
            if self.last_seen_volume is not None:
                logger.info(f"Drive {self.clean_drive}: is now empty.")
                self.last_seen_volume = None
            return False

        current_label = vol["label"]
        if current_label != self.last_seen_volume:
            logger.info(f"New disc detected: '{current_label}' ({vol['size_bytes'] / (1024**3):.1f} GB)")
            self.last_seen_volume = current_label
            return self.process_disc(vol)
        
        return False

    def run(self):
        logger.info(f"ARM Drive Watcher active. Monitoring Drive {self.clean_drive}: every {self.poll_interval}s...")
        try:
            while True:
                self.poll_once()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("ARM Drive Watcher stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="ARM Fast Optical Ingest Watcher")
    parser.add_argument("--config", default="work/projects/arm/arm_config.yaml", help="Path to config YAML")
    parser.add_argument("--dry-run", action="store_true", help="Simulate drive detection and extraction")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    watcher = DriveWatcher(config_path=args.config, dry_run=args.dry_run)
    if args.once:
        watcher.poll_once()
    else:
        watcher.run()


if __name__ == "__main__":
    main()
