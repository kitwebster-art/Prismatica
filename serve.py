#!/usr/bin/env python3
"""Tiny static server for the Prismatica app.

allow_reuse_address ensures the watchdog can rebind immediately after a crash
or kill instead of waiting out the OS's TIME_WAIT window. Without it we hit
"OSError: Address already in use" on quick restarts.
"""
import http.server
import json
import os
import subprocess
import socketserver
import sys
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8899"))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIRECTORY)

RENDER_JOBS = {}


def slugify(value):
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "lens")).strip("_").lower()
    return text or "lens"


def analyze_render_file(video_path):
    """Scan a rendered MP4 for abrupt jumps, held frames, or washed-out output."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.exists(candidate):
                ffmpeg = candidate
                break
    if not ffmpeg:
        return {
            "ok": False,
            "passed": False,
            "error": "ffmpeg is not available, so the render file could not be scanned",
        }
    if not video_path or not os.path.exists(video_path):
        return {"ok": False, "passed": False, "error": "render file not found"}

    width = 256
    height = 256
    frame_size = width * height * 3
    cmd = [
        ffmpeg,
        "-v", "error",
        "-i", video_path,
        "-vf", f"scale={width}:{height}:flags=area,format=rgb24",
        "-f", "rawvideo",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    prev = None
    diffs = []
    tile_diffs = []
    luma_means = []
    luma_stdevs = []
    sat_means = []
    bright_ratios = []
    frames = 0
    while True:
        chunk = proc.stdout.read(frame_size)
        if not chunk or len(chunk) < frame_size:
            break
        if prev is not None:
            total = 0
            for a, b in zip(chunk, prev):
                total += abs(a - b)
            frame_diff = total / (frame_size * 255)
            diffs.append(frame_diff)
            # Local tile scan catches encoder macroblock corruption where only
            # one part of the frame glitches. Whole-frame diff can miss this.
            tile = 32
            max_tile = 0.0
            for ty in range(0, height, tile):
                for tx in range(0, width, tile):
                    tile_total = 0
                    tile_count = 0
                    y_end = min(height, ty + tile)
                    x_end = min(width, tx + tile)
                    for py in range(ty, y_end):
                        row = (py * width + tx) * 3
                        row_end = (py * width + x_end) * 3
                        for k in range(row, row_end):
                            tile_total += abs(chunk[k] - prev[k])
                            tile_count += 1
                    if tile_count:
                        max_tile = max(max_tile, tile_total / (tile_count * 255))
            tile_diffs.append(max_tile)

        luma_sum = 0.0
        luma_sq_sum = 0.0
        sat_sum = 0.0
        bright_count = 0
        pixels = width * height
        for j in range(0, len(chunk), 3):
            r = chunk[j] / 255.0
            g = chunk[j + 1] / 255.0
            b = chunk[j + 2] / 255.0
            y = 0.2126 * r + 0.7152 * g + 0.0722 * b
            luma_sum += y
            luma_sq_sum += y * y
            mx = max(r, g, b)
            mn = min(r, g, b)
            if mx > 0:
                sat_sum += (mx - mn) / mx
            if y > 0.92:
                bright_count += 1
        luma_mean = luma_sum / pixels
        luma_var = max(0.0, (luma_sq_sum / pixels) - (luma_mean * luma_mean))
        luma_means.append(luma_mean)
        luma_stdevs.append(luma_var ** 0.5)
        sat_means.append(sat_sum / pixels)
        bright_ratios.append(bright_count / pixels)

        prev = chunk
        frames += 1
    _, stderr = proc.communicate(timeout=30)
    if proc.returncode != 0:
        return {
            "ok": False,
            "passed": False,
            "error": (stderr.decode("utf-8", "replace") or "ffmpeg scan failed")[-1000:],
        }
    if len(diffs) < 2:
        return {"ok": False, "passed": False, "error": "not enough frames to scan", "frames": frames}

    sorted_diffs = sorted(diffs)
    def median_of(values):
        values = sorted(values)
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

    mid = len(sorted_diffs) // 2
    median = sorted_diffs[mid] if len(sorted_diffs) % 2 else (sorted_diffs[mid - 1] + sorted_diffs[mid]) / 2
    mean = sum(diffs) / len(diffs)
    median_luma = median_of(luma_means)
    median_contrast = median_of(luma_stdevs)
    median_sat = median_of(sat_means)
    median_bright = median_of(bright_ratios)
    spike_threshold = max(0.04, median * 5)
    tile_median = median_of(tile_diffs) if tile_diffs else 0
    tile_spike_threshold = max(0.16, tile_median * 4, median * 12)
    hold_threshold = max(0.0008, median * 0.08)
    spikes = [(i + 1, d) for i, d in enumerate(diffs) if d > spike_threshold]
    tile_spikes = [
        (i + 1, d)
        for i, d in enumerate(tile_diffs)
        if d > tile_spike_threshold
    ]
    holds = []
    run = []
    # Ignore the opening frames: sequence exports can intentionally start from a
    # held or very slowly easing camera pose. Only report sustained freezes once
    # the test clip is underway.
    for i, d in enumerate(diffs):
        frame_no = i + 1
        if frame_no < 20 or d >= hold_threshold:
            if len(run) >= 6:
                holds.extend(run)
            run = []
            continue
        run.append((frame_no, d))
    if len(run) >= 6:
        holds.extend(run)
    washed_out = (
        median_luma > 0.82
        and median_contrast < 0.18
        and median_sat < 0.18
        and median_bright > 0.45
    )
    passed = not spikes and not tile_spikes and not holds and not washed_out
    return {
        "ok": True,
        "passed": passed,
        "washedOut": washed_out,
        "frames": frames,
        "medianDiff": round(median, 6),
        "meanDiff": round(mean, 6),
        "maxDiff": round(max(diffs), 6),
        "medianTileDiff": round(tile_median, 6),
        "maxTileDiff": round(max(tile_diffs) if tile_diffs else 0, 6),
        "medianLuma": round(median_luma, 6),
        "medianContrast": round(median_contrast, 6),
        "medianSaturation": round(median_sat, 6),
        "medianBrightPixels": round(median_bright, 6),
        "spikeThreshold": round(spike_threshold, 6),
        "tileSpikeThreshold": round(tile_spike_threshold, 6),
        "holdThreshold": round(hold_threshold, 6),
        "spikes": [{"frame": i, "diff": round(d, 6)} for i, d in spikes[:20]],
        "tileSpikes": [{"frame": i, "diff": round(d, 6)} for i, d in tile_spikes[:20]],
        "holds": [{"frame": i, "diff": round(d, 6)} for i, d in holds[:20]],
    }


def repair_render_file(video_path, analysis):
    """Replace detected glitch frames with blends of their clean neighbours."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not os.path.exists(ffmpeg) and shutil.which("ffmpeg") is None:
        return {"ok": False, "error": "ffmpeg is not available for repair"}
    if not video_path or not os.path.exists(video_path):
        return {"ok": False, "error": "render file not found for repair"}
    bad = set()
    for key in ("spikes", "tileSpikes"):
        for item in (analysis or {}).get(key, []) or []:
            try:
                bad.add(int(item.get("frame")))
            except Exception:
                pass
    bad = sorted(f for f in bad if f > 1)
    if not bad:
        return {"ok": False, "error": "no repairable frames reported"}

    fps = "30"
    if os.path.exists(ffprobe):
        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=r_frame_rate",
                    "-of", "default=nokey=1:noprint_wrappers=1", video_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            rate = (probe.stdout or "").strip().splitlines()[0]
            if "/" in rate:
                a, b = rate.split("/", 1)
                fps_val = float(a) / max(1.0, float(b))
                fps = f"{fps_val:.6f}".rstrip("0").rstrip(".")
            elif rate:
                fps = rate
        except Exception:
            fps = "30"

    try:
        from PIL import Image
    except Exception as e:
        return {"ok": False, "error": f"Pillow unavailable for repair: {e}"}

    with tempfile.TemporaryDirectory(prefix="prismatica-repair-") as td:
        pattern = os.path.join(td, "frame_%06d.png")
        extract = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", video_path, pattern],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if extract.returncode != 0:
            return {"ok": False, "error": (extract.stderr or "frame extraction failed")[-1000:]}
        frame_files = sorted(f for f in os.listdir(td) if f.startswith("frame_") and f.endswith(".png"))
        frame_count = len(frame_files)
        if frame_count < 3:
            return {"ok": False, "error": "not enough frames extracted for repair"}

        groups = []
        group = []
        for f in bad:
            if not group or f == group[-1] + 1:
                group.append(f)
            else:
                groups.append(group)
                group = [f]
        if group:
            groups.append(group)

        repaired = []
        for group in groups:
            start, end = group[0], group[-1]
            prev_no = max(1, start - 1)
            next_no = min(frame_count, end + 1)
            if prev_no >= start or next_no <= end:
                continue
            prev_path = os.path.join(td, f"frame_{prev_no:06d}.png")
            next_path = os.path.join(td, f"frame_{next_no:06d}.png")
            with Image.open(prev_path).convert("RGB") as prev_img, Image.open(next_path).convert("RGB") as next_img:
                span = len(group) + 1
                for offset, frame_no in enumerate(group, start=1):
                    alpha = offset / span
                    repaired_img = Image.blend(prev_img, next_img, alpha)
                    repaired_img.save(os.path.join(td, f"frame_{frame_no:06d}.png"))
                    repaired.append(frame_no)

        if not repaired:
            return {"ok": False, "error": "no frames could be repaired"}

        repaired_path = os.path.join(td, "repaired.mp4")
        encode = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", fps,
                "-i", pattern,
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "8",
                "-g", "1",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                repaired_path,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if encode.returncode != 0:
            return {"ok": False, "error": (encode.stderr or "repair encode failed")[-1000:]}
        os.replace(repaired_path, video_path)
        return {"ok": True, "frames": repaired, "count": len(repaired), "fps": fps}


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # Prismatica is changing quickly during fabrication/render debugging.
        # Force Chrome to fetch the current local build instead of reusing an
        # older cached index.html that may still contain a broken render patch.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def build_clean_step(self, state_payload=None):
        try:
            script = os.path.join(DIRECTORY, "exports", "build-fabricator-clean-step.py")
            python_candidates = [
                os.path.join(DIRECTORY, ".venv", "bin", "python"),
                sys.executable,
                os.path.expanduser("~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"),
            ]
            py = next((candidate for candidate in python_candidates if os.path.exists(candidate)), sys.executable)
            cmd = [py, script]
            tmp_path = None
            if state_payload is not None:
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                    json.dump(state_payload, tmp)
                    tmp_path = tmp.name
                cmd += ["--state-json", tmp_path]
            result = subprocess.run(
                cmd,
                cwd=DIRECTORY,
                capture_output=True,
                text=True,
                timeout=420,
                check=False,
            )
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if result.returncode != 0:
                raise RuntimeError(result.stderr or result.stdout or "clean STEP build failed")

            rel_dir = "exports/saved/fabricator_clean_step"
            abs_dir = os.path.join(DIRECTORY, rel_dir)
            generated = re.findall(r"(/[^\n\r]+/PV2-step-v(\d{3})\.step)", result.stdout)
            if not generated:
                raise RuntimeError("clean STEP build did not report a versioned STEP file")
            step_path, version_text = generated[-1]
            base = f"PV2-step-v{version_text}"
            step_name = f"{base}.step"
            zip_name = f"{base}.zip"
            readme_name = f"{base}-readme.txt"
            qa_name = f"{base}-qa.json"
            pdf_name = f"{base}-validation.pdf"
            preview_name = f"{base}-preview.obj"
            zip_path = os.path.join(abs_dir, zip_name)
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(step_path, arcname=step_name)
                zf.write(os.path.join(abs_dir, readme_name), arcname=readme_name)
                qa_path = os.path.join(abs_dir, qa_name)
                if os.path.exists(qa_path):
                    zf.write(qa_path, arcname=qa_name)
                pdf_path = os.path.join(abs_dir, pdf_name)
                if os.path.exists(pdf_path):
                    zf.write(pdf_path, arcname=pdf_name)
                preview_path = os.path.join(abs_dir, preview_name)
                if os.path.exists(preview_path):
                    zf.write(preview_path, arcname=preview_name)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": True,
                "stepUrl": f"/{rel_dir}/{step_name}",
                "zipUrl": f"/{rel_dir}/{zip_name}",
                "path": os.path.join(abs_dir, step_name),
                "zipPath": zip_path,
                "filename": zip_name,
                "stepFilename": step_name,
                "log": result.stdout,
            }).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    def render_background(self, payload):
        tmp_path = None
        try:
            script = os.path.join(DIRECTORY, "exports", "render-background.js")
            node = os.path.expanduser(
                "~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
            )
            node_modules = os.path.expanduser(
                "~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
            )
            if not os.path.exists(node):
                node = "node"

            payload = dict(payload or {})
            payload.setdefault("appUrl", f"http://127.0.0.1:{PORT}/")
            payload.setdefault("outputDir", os.path.join(os.path.expanduser("~"), "Downloads"))
            payload.setdefault("timeoutMs", 60 * 60 * 1000)
            wait_for_completion = bool(payload.pop("wait", False))
            analyze_after_render = bool(payload.pop("analyze", False))
            job_id = f"render-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            job_dir = os.path.join(DIRECTORY, "exports", "saved", "render_jobs")
            os.makedirs(job_dir, exist_ok=True)
            progress_path = os.path.join(job_dir, f"{job_id}.json")
            stdout_path = os.path.join(job_dir, f"{job_id}.stdout.txt")
            stderr_path = os.path.join(job_dir, f"{job_id}.stderr.txt")
            payload["progressPath"] = progress_path
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump({"ok": True, "jobId": job_id, "state": "queued", "progress": 0, "status": "queued"}, f)

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                json.dump(payload, tmp)
                tmp_path = tmp.name

            env = os.environ.copy()
            if os.path.exists(node_modules):
                env["NODE_PATH"] = node_modules
            stdout_f = open(stdout_path, "w", encoding="utf-8")
            stderr_f = open(stderr_path, "w", encoding="utf-8")
            process = subprocess.Popen(
                [node, script, tmp_path],
                cwd=DIRECTORY,
                text=True,
                stdout=stdout_f,
                stderr=stderr_f,
                env=env,
            )
            RENDER_JOBS[job_id] = {
                "process": process,
                "progressPath": progress_path,
                "stdoutPath": stdout_path,
                "stderrPath": stderr_path,
                "payloadPath": tmp_path,
                "analyze": analyze_after_render,
                "startedAt": time.time(),
            }

            if wait_for_completion:
                try:
                    process.wait(timeout=max(60, int(payload["timeoutMs"] / 1000) + 30))
                finally:
                    stdout_f.close()
                    stderr_f.close()
                data = self.render_status_data(job_id)
            else:
                data = {"ok": True, "jobId": job_id, "state": "running", "progress": 0, "status": "starting render…"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
        finally:
            if wait_for_completion and tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def render_status_data(self, job_id):
        job = RENDER_JOBS.get(job_id)
        if not job:
            progress_path = os.path.join(
                DIRECTORY, "exports", "saved", "render_jobs", f"{os.path.basename(job_id)}.json"
            )
            if os.path.exists(progress_path):
                try:
                    with open(progress_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
                data.setdefault("jobId", job_id)
                if data.get("state") == "running":
                    data["ok"] = False
                    data["state"] = "error"
                    data["error"] = "render server restarted before this job completed; please start the render again"
                return data
            return {"ok": False, "error": "unknown render job"}

        data = {"ok": True, "jobId": job_id, "state": "running", "progress": 0, "status": "running…"}
        try:
            with open(job["progressPath"], "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass

        process = job["process"]
        rc = process.poll()
        if rc is None:
            # The browser-side renderer writes "done" to the progress file just
            # before the Node wrapper exits. Keep the job non-final here so
            # scan/analyse callers do not observe a completed render without
            # the server-side frame analysis attached.
            if data.get("state") == "done":
                data["state"] = "finalizing"
                if job.get("analyze") and not data.get("analysis"):
                    data["progress"] = min(0.995, float(data.get("progress") or 0.995))
                    data["status"] = "render complete · scanning frames…"
            data.setdefault("state", "running")
            return data

        data["returnCode"] = rc
        if rc == 0:
            data.setdefault("state", "done")
            data["state"] = "done"
            data["progress"] = 1
            if job.get("analyze") and data.get("path") and not data.get("analysis"):
                analysis = analyze_render_file(data.get("path"))
                data["analysis"] = analysis
                if analysis.get("ok"):
                    repair_note = ""
                    if data.get("repair", {}).get("ok"):
                        repair_note = f"repaired {data['repair'].get('count', 0)} frame(s) · "
                    data["status"] = (
                        f"{data.get('status', 'render complete')} · "
                        f"{repair_note}"
                        f"scan {'passed' if analysis.get('passed') else 'possible glitch'} "
                        f"({analysis.get('frames', 0)} frames scanned)"
                    )
                try:
                    with open(job["progressPath"], "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                except Exception:
                    pass
        else:
            data["ok"] = False
            data["state"] = "error"
            try:
                with open(job["stderrPath"], "r", encoding="utf-8") as f:
                    stderr_tail = f.read()[-4000:]
                    if data.get("error"):
                        data["stderr"] = stderr_tail
                    else:
                        data["error"] = stderr_tail or "background render failed"
            except Exception:
                data.setdefault("error", "background render failed")

        try:
            os.remove(job["payloadPath"])
        except OSError:
            pass
        return data

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/build-clean-step":
            self.build_clean_step()
            return
        if parsed.path == "/api/render-status":
            qs = parse_qs(parsed.query)
            data = self.render_status_data((qs.get("id") or [""])[0])
            self.send_response(200 if data.get("ok") else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/render-background":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                self.render_background(payload)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if self.path == "/api/build-clean-step":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                self.build_clean_step(payload.get("state", payload))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if self.path != "/api/save-export":
            self.send_error(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            filename = os.path.basename(payload.get("filename", ""))
            content = payload.get("content", "")
            if not filename.endswith((".step", ".stp", ".stl", ".md", ".png")):
                raise ValueError("unsupported export type")
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
            if not filename:
                raise ValueError("empty filename")

            export_dir = os.path.join(DIRECTORY, "exports", "saved")
            os.makedirs(export_dir, exist_ok=True)
            path = os.path.join(export_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "path": path}).encode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))


HOST = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT") else "127.0.0.1"

with ReusableTCPServer((HOST, PORT), Handler) as httpd:
    print(f"Serving {DIRECTORY} at http://{HOST}:{PORT}")
    sys.stdout.flush()
    httpd.serve_forever()
