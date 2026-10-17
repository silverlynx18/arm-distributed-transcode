"""
Unit tests for Automatic Ripping Machine (A.R.M.) Decoupled Ingest Pipeline.
Verifies immediate tray ejection, resolution branching, AMF encoding parameters,
and queue state management.
"""

import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from arm_drive_watcher import DriveWatcher
from arm_compute_worker import ComputeWorker


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        dirs = {
            "incoming": base / "incoming",
            "processing": base / "processing",
            "completed": base / "completed",
            "logs": base / "logs"
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
            
        config = {
            "optical_drive": {
                "letter": "E:",
                "poll_interval_seconds": 1,
                "auto_eject_immediately_on_dump": True
            },
            "directories": {
                "incoming": str(dirs["incoming"]),
                "processing": str(dirs["processing"]),
                "completed": str(dirs["completed"]),
                "logs": str(dirs["logs"])
            },
            "makemkv": {
                "executable": "makemkvcon64.exe",
                "min_length_seconds": 900,
                "disc_index": "disc:0"
            },
            "upscaling": {
                "enable_ai": True,
                "threshold_height": 1080
            },
            "encoding": {
                "codec": "av1_amf",
                "pix_fmt": "p010le",
                "rc": "cqp",
                "qp_i": 24,
                "qp_p": 26,
                "copy_audio": True,
                "copy_subtitles": True
            }
        }
        yield {"base": base, "dirs": dirs, "config": config}


def test_drive_watcher_makemkv_command(temp_workspace):
    watcher = DriveWatcher(config_dict=temp_workspace["config"], dry_run=True)
    target = temp_workspace["dirs"]["incoming"] / "test_disc"
    cmd = watcher.build_makemkv_command(target)
    
    assert "makemkvcon64.exe" in cmd[0]
    assert cmd[1] == "mkv"
    assert cmd[2] == "disc:0"
    assert cmd[3] == "all"
    assert str(target) in cmd[4]
    assert "--minlength=900" in cmd[5]


def test_immediate_tray_eject_on_dump_completion(temp_workspace):
    """
    CRITICAL REQUIREMENT: Verify tray eject occurs immediately when MakeMKV finishes,
    before any video compute starts, so the user can insert the next disc.
    """
    watcher = DriveWatcher(config_dict=temp_workspace["config"], dry_run=True)
    
    with patch.object(watcher, "eject_drive_tray", return_value=True) as mock_eject, \
         patch.object(watcher, "send_notification") as mock_notify:
        
        vol_info = {
            "ready": True,
            "label": "Avatar_Disc_2",
            "size_bytes": 46000000000
        }
        success = watcher.process_disc(vol_info)
        assert success is True
        
        # Verify immediate tray ejection was called
        mock_eject.assert_called_once()
        
        # Verify manifest was dropped in incoming
        incoming_dirs = list(temp_workspace["dirs"]["incoming"].glob("Avatar_Disc_2_*"))
        assert len(incoming_dirs) == 1
        manifest_file = incoming_dirs[0] / "manifest.json"
        assert manifest_file.exists()
        
        with open(manifest_file, "r") as f:
            manifest = json.load(f)
        assert manifest["disc_label"] == "Avatar_Disc_2"
        assert manifest["status"] == "ready_for_compute"


def test_compute_worker_resolution_branching(temp_workspace):
    worker = ComputeWorker(config_dict=temp_workspace["config"], dry_run=True)
    
    # 480p DVD -> Upscale
    assert worker.should_upscale(480) is True
    # 720p HD -> Upscale
    assert worker.should_upscale(720) is True
    # 1080p Blu-ray -> Upscale
    assert worker.should_upscale(1080) is True
    # 1440p -> Native Pass (exceeds 1080p threshold)
    assert worker.should_upscale(1440) is False
    # 2160p 4K UHD Blu-ray -> Native Pass
    assert worker.should_upscale(2160) is False


def test_compute_worker_av1_amf_command_generation(temp_workspace):
    worker = ComputeWorker(config_dict=temp_workspace["config"], dry_run=True)
    in_file = temp_workspace["dirs"]["processing"] / "movie.mkv"
    out_file = temp_workspace["dirs"]["completed"] / "movie_4K_AV1.mkv"
    
    cmd = worker.build_av1_amf_command(in_file, out_file)
    cmd_str = " ".join(cmd)
    
    assert "-c:v av1_amf" in cmd_str
    assert "-pix_fmt p010le" in cmd_str
    assert "-qp_i 24" in cmd_str
    assert "-qp_p 26" in cmd_str
    assert "-c:a copy" in cmd_str
    assert "-c:s copy" in cmd_str
    assert str(out_file) in cmd_str


def test_compute_worker_job_lifecycle(temp_workspace):
    worker = ComputeWorker(config_dict=temp_workspace["config"], dry_run=True)
    
    # Stage an incoming job
    job_dir = temp_workspace["dirs"]["incoming"] / "Avatar_Disc_2_20261016_010000"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a dummy MKV file
    mkv_path = job_dir / "title00.mkv"
    mkv_path.write_bytes(b"dummy mkv content")
    
    # Create manifest
    manifest = {
        "disc_label": "Avatar_Disc_2",
        "timestamp": "20261016_010000",
        "size_bytes": 46000000000,
        "status": "ready_for_compute"
    }
    with open(job_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)
        
    with patch.object(worker, "probe_video", return_value={"width": 1920, "height": 1080, "codec_name": "h264"}), \
         patch.object(worker, "trigger_jellyfin_refresh") as mock_jf, \
         patch.object(worker, "send_notification") as mock_notify:
         
        processed = worker.poll_once()
        assert processed == 1
        mock_jf.assert_called_once()
        mock_notify.assert_called()
        
        # Verify incoming job directory is gone (cleaned up)
        assert not job_dir.exists()
