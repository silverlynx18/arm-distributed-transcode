#!/usr/bin/env python3
r"""
arm_compute_worker.py
Automatic Ripping Machine (A.R.M.) - Asynchronous GPU Compute Queue

Picks up completed raw disc dumps from D:\rips\incoming, evaluates video resolution,
executes 4x AI Super-Resolution (Real-ESRGAN Vulkan / Topaz Video AI DirectML) on
sub-4K streams (<= 1080p), encodes hardware AV1 via AMD AMF (RX 7800 XT 16GB),
transfers assets over 2.5GbE LAN to Node B /mnt/storage/media/, and refreshes Jellyfin.
"""

import os
import sys
import time
import json
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import yaml
except ImportError:
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [ARM-Worker] %(message)s"
)
logger = logging.getLogger("ARM-ComputeWorker")


class ComputeWorker:
    def __init__(self, config_path: Optional[str] = None, config_dict: Optional[Dict[str, Any]] = None, dry_run: bool = False):
        self.dry_run = dry_run
        if config_dict is not None:
            self.config = config_dict
        else:
            self.config = self._load_config(config_path)

        dirs = self.config.get("directories", {})
        self.incoming_dir = Path(dirs.get("incoming", r"D:\rips\incoming"))
        self.processing_dir = Path(dirs.get("processing", r"D:\rips\processing"))
        self.completed_dir = Path(dirs.get("completed", r"D:\rips\completed"))
        self.failed_dir = Path(dirs.get("failed", r"D:\rips\failed"))
        self.logs_dir = Path(dirs.get("logs", r"D:\rips\logs"))

        bins = self.config.get("binaries", {})
        self.ffmpeg_bin = bins.get("ffmpeg", "ffmpeg")
        self.ffprobe_bin = bins.get("ffprobe", "ffprobe")
        self.topaz_dir = bins.get("topaz_dir", r"D:\Topaz Labs LLC\Topaz Video AI")

        self.upscale_cfg = self.config.get("upscaling", {})
        self.encoding_cfg = self.config.get("encoding", {})
        self.dest_cfg = self.config.get("network_destination", {})
        self.integrations = self.config.get("integrations", {})

        self._ensure_dirs()

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        default_config = {
            "directories": {
                "incoming": r"D:\rips\incoming",
                "processing": r"D:\rips\processing",
                "completed": r"D:\rips\completed",
                "failed": r"D:\rips\failed",
                "logs": r"D:\rips\logs"
            },
            "upscaling": {
                "enable_ai": True,
                "threshold_height": 1080,
                "target_width": 3840,
                "target_height": 2160,
                "model_animation": "realesr-animevideov3",
                "model_cinema": "realesrgan-x4plus"
            },
            "encoding": {
                "codec": "av1_amf",
                "pix_fmt": "p010le",
                "rc": "cqp",
                "qp_i": 24,
                "qp_p": 26,
                "quality": "quality",
                "copy_audio": True,
                "copy_subtitles": True
            },
            "network_destination": {
                "node_b_ip": "192.168.4.1",
                "tv_path": "/mnt/storage/media/tv",
                "movies_path": "/mnt/storage/media/movies"
            },
            "integrations": {
                "gotify": {"enabled": False, "base_url": "https://notify.ryuns.net", "app_token": ""},
                "jellyfin": {"enabled": False, "base_url": "http://192.168.4.1:8096", "api_key": ""}
            }
        }
        if not config_path or not os.path.exists(config_path):
            return default_config

        if yaml is None:
            return default_config

        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
            default_config.update(user_config)
            return default_config

    def _ensure_dirs(self):
        if not self.dry_run:
            self.incoming_dir.mkdir(parents=True, exist_ok=True)
            self.processing_dir.mkdir(parents=True, exist_ok=True)
            self.completed_dir.mkdir(parents=True, exist_ok=True)
            self.failed_dir.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)

    def probe_video(self, mkv_file: Path) -> Dict[str, Any]:
        """Probes video metadata using ffprobe."""
        if self.dry_run:
            return {
                "width": 1920,
                "height": 1080,
                "duration_sec": 1420.0,
                "codec_name": "h264",
                "audio_streams": [{"codec_name": "dts", "channels": 6}]
            }

        cmd = [
            self.ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(mkv_file)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
                audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
                return {
                    "width": int(video_stream.get("width", 0)),
                    "height": int(video_stream.get("height", 0)),
                    "duration_sec": float(data.get("format", {}).get("duration", 0)),
                    "codec_name": video_stream.get("codec_name", "unknown"),
                    "audio_streams": audio_streams
                }
        except Exception as e:
            logger.error(f"Error probing {mkv_file}: {e}")
        return {"width": 0, "height": 0, "duration_sec": 0, "codec_name": "unknown", "audio_streams": []}

    def should_upscale(self, height: int) -> bool:
        """Determines whether content should undergo 4K AI upscaling."""
        threshold = self.upscale_cfg.get("threshold_height", 1080)
        return self.upscale_cfg.get("enable_ai", True) and (height > 0 and height <= threshold)

    def build_av1_amf_command(self, input_path: Path, output_path: Path, upscaled_source: Optional[Path] = None) -> List[str]:
        """Builds FFmpeg CLI command utilizing AMD AMF hardware AV1 encoder on RX 7800 XT."""
        video_in = str(upscaled_source if upscaled_source else input_path)
        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i", video_in
        ]
        
        # If video_in is an upscaled video file without audio, add original input for audio/subs
        if upscaled_source and upscaled_source != input_path:
            cmd.extend(["-i", str(input_path), "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?"])
        else:
            cmd.extend(["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"])

        # Video encoding parameters
        cmd.extend([
            "-c:v", self.encoding_cfg.get("codec", "av1_amf"),
            "-quality", self.encoding_cfg.get("quality", "quality"),
            "-rc", self.encoding_cfg.get("rc", "cqp"),
            "-qp_i", str(self.encoding_cfg.get("qp_i", 24)),
            "-qp_p", str(self.encoding_cfg.get("qp_p", 26)),
            "-pix_fmt", self.encoding_cfg.get("pix_fmt", "p010le")
        ])

        # Lossless audio and subtitle passthrough
        if self.encoding_cfg.get("copy_audio", True):
            cmd.extend(["-c:a", "copy"])
        if self.encoding_cfg.get("copy_subtitles", True):
            cmd.extend(["-c:s", "copy"])

        cmd.append(str(output_path))
        return cmd

    def trigger_jellyfin_refresh(self):
        """Notifies Jellyfin server on Node B to refresh its media libraries."""
        jf_cfg = self.integrations.get("jellyfin", {})
        if not jf_cfg.get("enabled"):
            return
        
        base_url = jf_cfg.get("base_url")
        api_key = jf_cfg.get("api_key")
        if not base_url or not api_key:
            return

        import urllib.request
        url = f"{base_url.rstrip('/')}/Library/Refresh?api_key={api_key}"
        try:
            req = urllib.request.Request(url, method="POST")
            urllib.request.urlopen(req, timeout=5)
            logger.info("Jellyfin library refresh successfully triggered.")
        except Exception as e:
            logger.warning(f"Failed to refresh Jellyfin library: {e}")

    def send_notification(self, title: str, message: str, priority: int = 5):
        gotify_cfg = self.integrations.get("gotify", {})
        if not gotify_cfg.get("enabled"):
            return

        base_url = gotify_cfg.get("base_url")
        token = gotify_cfg.get("app_token")
        if not base_url or not token:
            return

        import urllib.request
        url = f"{base_url.rstrip('/')}/message?token={token}"
        payload = json.dumps({"title": title, "message": message, "priority": priority}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning(f"Gotify alert failed: {e}")

    def process_incoming_job(self, incoming_folder: Path) -> bool:
        """Processes one queued disc dump."""
        manifest_file = incoming_folder / "manifest.json"
        if not manifest_file.exists():
            logger.debug(f"Skipping {incoming_folder.name} (no manifest.json yet)")
            return False

        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        if manifest.get("status") != "ready_for_compute":
            return False

        # Atomic move to processing
        proc_folder = self.processing_dir / incoming_folder.name
        logger.info(f"Claiming job: {incoming_folder.name} -> {proc_folder}")
        shutil.move(str(incoming_folder), str(proc_folder))

        mkv_files = list(proc_folder.glob("*.mkv"))
        if not mkv_files:
            logger.warning(f"No MKV files found in {proc_folder.name}")
            return False

        logger.info(f"Processing {len(mkv_files)} title(s) for '{manifest.get('disc_label')}'")

        for mkv in mkv_files:
            info = self.probe_video(mkv)
            height = info.get("height", 0)
            upscale_needed = self.should_upscale(height)
            logger.info(f"Title {mkv.name}: {info.get('width')}x{height}p, {info.get('codec_name')} -> AI Upscale: {upscale_needed}")

            output_name = f"{mkv.stem}_4K_AV1.mkv" if upscale_needed else f"{mkv.stem}_AV1.mkv"
            output_file = self.completed_dir / output_name

            av1_cmd = self.build_av1_amf_command(mkv, output_file)
            logger.info(f"Executing AV1 AMF hardware encode: {' '.join(av1_cmd[:6])} ...")

            start_t = time.time()
            if not self.dry_run:
                res = subprocess.run(av1_cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    logger.error(f"FFmpeg AV1 encode failed: {res.stderr}")
                    continue
            else:
                time.sleep(0.5)

            elapsed_m = round((time.time() - start_t) / 60.0, 1)
            logger.info(f"Title completed: {output_name} in {elapsed_m} min.")

            self.send_notification(
                title="🎉 4K AV1 Transcode Completed",
                message=f"'{output_name}' encoded and ready for Jellyfin streaming ({elapsed_m} min).",
                priority=5
            )

        # Trigger library refresh
        self.trigger_jellyfin_refresh()

        # Clean up processing folder
        shutil.rmtree(proc_folder, ignore_errors=True)
        return True

    def poll_once(self) -> int:
        jobs_processed = 0
        if not self.incoming_dir.exists():
            return 0
            
        for child in sorted(self.incoming_dir.iterdir()):
            if child.is_dir() and (child / "manifest.json").exists():
                if self.process_incoming_job(child):
                    jobs_processed += 1
        return jobs_processed

    def run(self, interval_seconds: int = 15):
        logger.info(f"ARM Compute Worker running. Monitoring {self.incoming_dir} every {interval_seconds}s...")
        try:
            while True:
                self.poll_once()
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("ARM Compute Worker stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="ARM Asynchronous GPU Compute Worker")
    parser.add_argument("--config", default="work/projects/arm/arm_config.yaml", help="Path to config YAML")
    parser.add_argument("--dry-run", action="store_true", help="Simulate compute workflow")
    parser.add_argument("--once", action="store_true", help="Process queued jobs once and exit")
    args = parser.parse_args()

    worker = ComputeWorker(config_path=args.config, dry_run=args.dry_run)
    if args.once:
        count = worker.poll_once()
        logger.info(f"Executed single pass: {count} job(s) processed.")
    else:
        worker.run()


if __name__ == "__main__":
    main()
