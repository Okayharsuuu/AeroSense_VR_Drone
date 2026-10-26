import cv2
import threading
import time
import numpy as np
import json
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import torch
from ultralytics import YOLO
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor

# =====================================
# SETTINGS
# =====================================

WIDTH         = 640
HEIGHT        = 360
FPS_LIMIT     = 25

DEPTH_A       = 500.0
DEPTH_B       = 10.0
DEPTH_ALPHA   = 0.15

PERSON_CLASS_ID = 0

SAM2_CONFIG   = "configs/sam2.1/sam2.1_hiera_t.yaml"
SAM2_CKPT     = "/home/jetson/sam2/checkpoints/sam2.1_hiera_tiny.pt"
SAM2_FPS      = 5
SAM2_INTERVAL = 1.0 / SAM2_FPS

SAM2_CONTOUR_POINTS = 80

# ── Tracking & persistence ─────────────────────────────────────
TRACK_TTL             = 1.0   # seconds to keep a person alive after YOLO loses them
SAM2_REFRESH_INTERVAL = 0.8   # seconds between SAM2 corrections per track
SAM2_BOX_DRIFT_PX     = 15    # pixel centre drift that forces an early SAM2 refresh

# ── Optical flow (Lucas-Kanade) ────────────────────────────────
# We track FEATURE POINTS (goodFeaturesToTrack) inside the bounding box,
# not contour points directly.  From the feature motion we estimate an
# affine warp and apply it rigidly to the full SAM2 contour.
# This preserves the silhouette shape — only position/scale/rotation change.
FLOW_FPS      = 15
FLOW_INTERVAL = 1.0 / FLOW_FPS

LK_PARAMS = dict(
    winSize  = (21, 21),
    maxLevel = 3,
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.01)
)

# goodFeaturesToTrack params — strong corners inside the bounding box
GFT_PARAMS = dict(
    maxCorners    = 40,
    qualityLevel  = 0.01,
    minDistance   = 7,
    blockSize     = 7
)

# Minimum fraction of feature points that must survive LK for the
# affine estimate to be trusted.
FLOW_MIN_SURVIVAL = 0.4

# If the estimated affine scale deviates more than this from 1.0
# the warp is probably wrong — discard it and flag for SAM2.
FLOW_MAX_SCALE_CHANGE = 0.35

# =====================================
# SHARED STATE
# =====================================

latest_frame   = None
frame_lock     = threading.Lock()

latest_results = []
results_lock   = threading.Lock()

# ── Per-person tracking dict  ──────────────────────────────────
# keyed by YOLO track_id (int)
# {
#   "contour":    [x0,y0,x1,y1,...],   flat pixel coords
#   "box":        (x1,y1,x2,y2),
#   "depth_m":    float,
#   "label":      str,
#   "last_seen":  float,
#   "last_sam2":  float,
#   "sam2_box":   tuple or None,
#   "needs_sam2": bool,
# }
tracked_persons = {}
tracked_lock    = threading.Lock()

depth_ema = {}   # track_id -> smoothed depth (float)

# =====================================
# LOAD YOLO
# =====================================

print("[INFO] Loading YOLO model...")
yolo_model = YOLO("yolov8n.pt")
print("[INFO] YOLO loaded")

# =====================================
# LOAD MIDAS
# =====================================

print("[INFO] Loading MiDaS depth model...")
midas            = torch.hub.load("intel-isl/MiDaS", "MiDaS_small",
                                  trust_repo=True)
midas.eval()
midas_transforms = torch.hub.load("intel-isl/MiDaS", "transforms",
                                  trust_repo=True)
midas_transform  = midas_transforms.small_transform
device           = torch.device("cuda" if torch.cuda.is_available()
                                else "cpu")
midas            = midas.to(device)
print(f"[INFO] MiDaS loaded on {device}")

# =====================================
# GSTREAMER PIPELINE
# =====================================

gst_pipeline = (
    f"rtspsrc location=rtsp://192.168.144.25:8554/main.264 "
    f"latency=0 protocols=tcp ! "
    "rtph265depay ! "
    "h265parse ! "
    "nvv4l2decoder disable-dpb=true ! "
    "nvvidconv flip-method=2 ! "  # rotate 180 degrees: camera is mounted upside down
    f"video/x-raw,width={WIDTH},height={HEIGHT},format=BGRx ! "
    "videoconvert ! "
    "video/x-raw,format=BGR ! "
    "appsink drop=true max-buffers=1 sync=false"
)

print("[INFO] Opening GStreamer pipeline...")
cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)

if not cap.isOpened():
    print("[ERROR] Failed to open GStreamer stream — "
          "check Ethernet to A8 mini")
    import sys
    sys.exit(1)

print("[INFO] GStreamer stream connected")

# =====================================
# UDP IP BROADCAST
# =====================================

def broadcast_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        try:
            result   = subprocess.run(['ip', 'addr', 'show'],
                                      capture_output=True, text=True)
            local_ip = None
            for line in result.stdout.split('\n'):
                if ('inet ' in line
                        and '127.0.0.1'     not in line
                        and '192.168.144'   not in line):
                    ip = line.strip().split()[1].split('/')[0]
                    if not ip.startswith('100.'):
                        local_ip = ip
                        break
            if local_ip:
                msg = f"JETSON_IP:{local_ip}:8080".encode()
                sock.sendto(msg, ('<broadcast>', 9999))
        except Exception as e:
            print(f"[WARN] Broadcast error: {e}")
        time.sleep(2)

threading.Thread(target=broadcast_ip, daemon=True).start()

# =====================================
# DEPTH HELPERS
# =====================================

def get_depth_map(frame_bgr):
    frame_rgb    = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    input_tensor = midas_transform(frame_rgb).to(device)
    with torch.no_grad():
        depth = midas(input_tensor)
        depth = torch.nn.functional.interpolate(
            depth.unsqueeze(1),
            size=(frame_bgr.shape[0], frame_bgr.shape[1]),
            mode="bilinear",
            align_corners=False,
        ).squeeze().cpu().numpy()
    return depth


def raw_to_metric(raw_value):
    if raw_value <= 0:
        return None
    return round(DEPTH_A / (raw_value + DEPTH_B), 2)


def get_object_depth(depth_map, x1, y1, x2, y2, track_id):
    h, w    = depth_map.shape
    x1, x2  = max(0, x1), min(w, x2)
    y1, y2  = max(0, y1), min(h, y2)

    px  = max(1, int((x2 - x1) * 0.1))
    py  = max(1, int((y2 - y1) * 0.1))
    rx1 = min(x1 + px, x2 - 1)
    rx2 = max(x2 - px, rx1 + 1)
    ry1 = min(y1 + py, y2 - 1)
    ry2 = max(y2 - py, ry1 + 1)

    region = (depth_map[ry1:ry2, rx1:rx2]
              if (ry2 > ry1 and rx2 > rx1)
              else depth_map[(y1+y2)//2 : (y1+y2)//2+1,
                             (x1+x2)//2 : (x1+x2)//2+1])

    raw    = float(np.median(region)) if region.size > 0 else 0.0
    metric = raw_to_metric(raw)
    if metric is None:
        return None

    if track_id not in depth_ema:
        depth_ema[track_id] = metric
    else:
        depth_ema[track_id] = (DEPTH_ALPHA * metric
                               + (1.0 - DEPTH_ALPHA) * depth_ema[track_id])
    return round(depth_ema[track_id], 2)

# =====================================
# DRAW BOXES HELPER
# =====================================

def draw_detections(frame, detections):
    for det in detections:
        x1, y1, x2, y2 = det["x1"], det["y1"], det["x2"], det["y2"]
        label           = det["label"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        (tw, th), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame,
                      (x1, y1 - th - 8), (x1 + tw + 4, y1),
                      (0, 255, 0), -1)
        cv2.putText(frame, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return frame

# =====================================
# CONTOUR DEBUG DRAW HELPER
# =====================================

def draw_contours_debug(frame, detections):
    for det in detections:
        cv2.rectangle(frame,
                      (det["x1"], det["y1"]),
                      (det["x2"], det["y2"]),
                      (100, 100, 100), 1)
        contour = det.get("contour", [])
        if len(contour) >= 4:
            pts = np.array(contour, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(frame, [pts], isClosed=True,
                          color=(0, 255, 100), thickness=2)
        depth_m = det.get("depth_m", -1.0)
        label   = (f"{depth_m:.2f}m" if depth_m > 0
                   else det.get("label", ""))
        cv2.putText(frame, label,
                    (det["x1"], det["y1"] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 100), 1, cv2.LINE_AA)
    return frame

# =====================================
# FRAME CAPTURE THREAD
# =====================================

def capture_frames():
    global latest_frame
    while True:
        ret, frame = cap.read()
        if not ret:
            print("[WARN] Frame grab failed — retrying")
            time.sleep(0.01)
            continue
        with frame_lock:
            latest_frame = frame

threading.Thread(target=capture_frames, daemon=True).start()

# =====================================
# MIDAS THREAD  (fixed 5 FPS)
# =====================================

latest_depth_map = None
depth_map_lock   = threading.Lock()

MIDAS_FPS        = 5
MIDAS_INTERVAL   = 1.0 / MIDAS_FPS
_midas_fps_times = []

def midas_loop():
    global latest_depth_map, _midas_fps_times
    print(f"[INFO] MiDaS thread started — target {MIDAS_FPS} FPS")
    while True:
        loop_start = time.time()
        with frame_lock:
            frame = latest_frame
        if frame is not None:
            try:
                t0        = time.time()
                depth_map = get_depth_map(frame.copy())
                elapsed   = time.time() - t0
                with depth_map_lock:
                    latest_depth_map = depth_map
                _midas_fps_times.append(time.time())
                if len(_midas_fps_times) > 10:
                    _midas_fps_times.pop(0)
                if len(_midas_fps_times) >= 2:
                    span = _midas_fps_times[-1] - _midas_fps_times[0]
                    fps  = (len(_midas_fps_times) - 1) / span
                    print(f"[MiDaS] {fps:.1f} FPS  (inference {elapsed*1000:.0f}ms)")
            except Exception as e:
                print(f"[WARN] MiDaS error: {e}")
        elapsed_total = time.time() - loop_start
        sleep_time    = MIDAS_INTERVAL - elapsed_total
        if sleep_time > 0:
            time.sleep(sleep_time)

threading.Thread(target=midas_loop, daemon=True).start()

# =====================================
# OPTICAL FLOW THREAD  (affine warp on feature points)
# =====================================
#
# WHY NOT TRACK CONTOUR POINTS DIRECTLY
# ──────────────────────────────────────
# LK optical flow is designed for sparse corner/edge features.  Propagating
# 80 evenly-spaced boundary points causes:
#   • Points on smooth skin/clothing slide along the edge rather than with it
#   • Some points drift off the silhouette entirely
#   • The spacing between points changes — the polygon collapses or fans out
#
# WHAT WE DO INSTEAD
# ──────────────────
# 1. Detect strong corners (goodFeaturesToTrack) inside the bounding box.
#    These are actual image features that LK can track reliably.
# 2. Run LK on those feature points between the previous and current frame.
# 3. From the surviving (prev→curr) point pairs, estimate a partial affine
#    transform (translation + rotation + scale, no shear) using
#    estimateAffinePartial2D with RANSAC.
# 4. Apply that same 2×3 matrix to every contour point as a rigid warp.
#
# Result: the SAM2 silhouette shape is preserved perfectly.  It just
# translates/rotates/scales as a unit to follow the person's motion.
# SAM2 still corrects shape drift every SAM2_REFRESH_INTERVAL seconds.

prev_gray_frame  = None
prev_gray_lock   = threading.Lock()

# Per-track feature point cache: track_id -> Nx1x2 float32 array in prev frame
prev_features    = {}
prev_features_lock = threading.Lock()


def _get_features_in_box(gray, box):
    """Detect good features to track inside the bounding box region."""
    x1, y1, x2, y2 = box
    x1 = max(0, x1);  y1 = max(0, y1)
    x2 = min(gray.shape[1], x2);  y2 = min(gray.shape[0], y2)
    if x2 <= x1 or y2 <= y1:
        return None
    roi = gray[y1:y2, x1:x2]
    pts = cv2.goodFeaturesToTrack(roi, **GFT_PARAMS)
    if pts is None or len(pts) < 4:
        return None
    # Shift from ROI coords back to full-frame coords
    pts[:, :, 0] += x1
    pts[:, :, 1] += y1
    return pts.astype(np.float32)


def optical_flow_loop():
    global prev_gray_frame
    print("[INFO] Optical flow thread started (affine-warp mode)")

    while True:
        loop_start = time.time()

        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(FLOW_INTERVAL)
            continue

        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        with prev_gray_lock:
            prev_gray = prev_gray_frame

        if prev_gray is not None:
            with tracked_lock:
                tids = list(tracked_persons.keys())

            for tid in tids:
                with tracked_lock:
                    person = tracked_persons.get(tid)
                    if person is None:
                        continue
                    contour = list(person.get("contour", []))
                    box     = person.get("box")

                if len(contour) < 4 or box is None:
                    # No contour yet — just update feature cache for next frame
                    feats = _get_features_in_box(curr_gray, box) if box else None
                    with prev_features_lock:
                        if feats is not None:
                            prev_features[tid] = feats
                        else:
                            prev_features.pop(tid, None)
                    continue

                # ── Get or re-detect feature points in the box ────────
                with prev_features_lock:
                    p_feats = prev_features.get(tid)

                if p_feats is None or len(p_feats) < 4:
                    # No previous features — detect fresh ones in current frame
                    # and store for next cycle; nothing to warp this cycle
                    feats = _get_features_in_box(curr_gray, box)
                    with prev_features_lock:
                        if feats is not None:
                            prev_features[tid] = feats
                        else:
                            prev_features.pop(tid, None)
                    continue

                # ── LK flow on feature points ─────────────────────────
                try:
                    new_feats, status, _ = cv2.calcOpticalFlowPyrLK(
                        prev_gray, curr_gray, p_feats, None, **LK_PARAMS)
                except Exception as e:
                    print(f"[WARN] LK error tid={tid}: {e}")
                    with prev_features_lock:
                        prev_features.pop(tid, None)
                    continue

                if new_feats is None or status is None:
                    with prev_features_lock:
                        prev_features.pop(tid, None)
                    continue

                status_flat = status.flatten().astype(bool)
                survival    = float(np.sum(status_flat)) / len(status_flat)

                if survival < FLOW_MIN_SURVIVAL:
                    # Too many lost — flag for immediate SAM2 correction
                    with tracked_lock:
                        if tid in tracked_persons:
                            tracked_persons[tid]["needs_sam2"] = True
                    with prev_features_lock:
                        prev_features.pop(tid, None)
                    continue

                src_pts = p_feats[status_flat]        # Nx1x2
                dst_pts = new_feats[status_flat]      # Nx1x2

                # ── Estimate partial affine (translation+rotation+scale) ─
                # estimateAffinePartial2D uses RANSAC internally — robust to
                # a few bad feature correspondences.
                M, inliers = cv2.estimateAffinePartial2D(
                    src_pts, dst_pts,
                    method=cv2.RANSAC,
                    ransacReprojThreshold=3.0
                )

                if M is None:
                    with tracked_lock:
                        if tid in tracked_persons:
                            tracked_persons[tid]["needs_sam2"] = True
                    with prev_features_lock:
                        prev_features.pop(tid, None)
                    continue

                # Sanity-check: scale component should be close to 1.0
                # M = [[a, b, tx], [c, d, ty]]  scale ≈ sqrt(a²+b²)
                scale = float(np.sqrt(M[0, 0]**2 + M[0, 1]**2))
                if abs(scale - 1.0) > FLOW_MAX_SCALE_CHANGE:
                    # Degenerate warp — person probably ran behind occlusion
                    with tracked_lock:
                        if tid in tracked_persons:
                            tracked_persons[tid]["needs_sam2"] = True
                    with prev_features_lock:
                        prev_features.pop(tid, None)
                    continue

                # ── Apply affine warp to the full contour ─────────────
                # Contour is a flat [x0,y0,x1,y1,...] list.
                # Reshape to Nx1x2, apply M, reshape back.
                pts = np.array(contour, dtype=np.float32).reshape(-1, 1, 2)

                # cv2.transform applies a 2×2 rotation+scale part only.
                # We need the full 2×3 affine, so do it manually:
                #   new_x = M[0,0]*x + M[0,1]*y + M[0,2]
                #   new_y = M[1,0]*x + M[1,1]*y + M[1,2]
                xy    = pts.reshape(-1, 2)            # N×2
                ones  = np.ones((len(xy), 1), dtype=np.float32)
                xyz   = np.hstack([xy, ones])         # N×3
                warped = (M @ xyz.T).T                # N×2
                flat  = warped.reshape(-1).astype(int).tolist()

                # Clamp to frame bounds
                flat[0::2] = [max(0, min(WIDTH-1,  v)) for v in flat[0::2]]
                flat[1::2] = [max(0, min(HEIGHT-1, v)) for v in flat[1::2]]

                with tracked_lock:
                    if tid in tracked_persons:
                        tracked_persons[tid]["contour"] = flat

                # Update feature cache: use surviving tracked points in curr frame
                # Also add any newly detected features to keep the pool full
                new_pool = dst_pts.reshape(-1, 1, 2)
                extra    = _get_features_in_box(curr_gray, box)
                if extra is not None:
                    new_pool = np.vstack([new_pool, extra])
                    # Deduplicate by grid: keep at most GFT_PARAMS["maxCorners"]
                    if len(new_pool) > GFT_PARAMS["maxCorners"]:
                        new_pool = new_pool[:GFT_PARAMS["maxCorners"]]

                with prev_features_lock:
                    prev_features[tid] = new_pool.astype(np.float32)

        # Update prev_gray
        with prev_gray_lock:
            prev_gray_frame = curr_gray

        # Clean up feature cache for expired tracks
        with tracked_lock:
            alive = set(tracked_persons.keys())
        with prev_features_lock:
            for tid in list(prev_features.keys()):
                if tid not in alive:
                    prev_features.pop(tid, None)

        elapsed    = time.time() - loop_start
        sleep_time = FLOW_INTERVAL - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

threading.Thread(target=optical_flow_loop, daemon=True).start()

# =====================================
# SAM 2 THREAD  (correction-based)
# =====================================
# SAM2 runs for a person only when:
#   1. last_sam2 == 0  →  new track, never segmented yet
#   2. needs_sam2 == True  →  optical flow degraded
#   3. box centre drifted > SAM2_BOX_DRIFT_PX since last SAM2 run
#   4. time since last SAM2 > SAM2_REFRESH_INTERVAL  →  periodic correction

FRAME_DIFF_THRESH = 4.0
THUMB_W, THUMB_H  = 80, 45

def sam2_loop():
    print("[INFO] SAM2 thread starting — loading model...")
    sam2_model = build_sam2(SAM2_CONFIG, SAM2_CKPT, device=device)
    predictor  = SAM2ImagePredictor(sam2_model)
    print(f"[INFO] SAM2 loaded — correction-based, max {SAM2_FPS} FPS")

    last_thumb = None

    while True:
        loop_start = time.time()

        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(SAM2_INTERVAL)
            continue

        frame_copy = frame.copy()
        now        = time.time()

        # ── Frame-level gate ──────────────────────────────────────────
        thumb = cv2.resize(
            cv2.cvtColor(frame_copy, cv2.COLOR_BGR2GRAY),
            (THUMB_W, THUMB_H)
        ).astype(np.float32)

        if last_thumb is not None:
            diff = float(np.mean(np.abs(thumb - last_thumb)))
            if diff < FRAME_DIFF_THRESH:
                elapsed = time.time() - loop_start
                time.sleep(max(0, SAM2_INTERVAL - elapsed))
                continue

        # ── Decide which tracks need SAM2 this cycle ──────────────────
        with tracked_lock:
            snapshot = {tid: dict(p) for tid, p in tracked_persons.items()}

        persons_to_run = []
        for tid, person in snapshot.items():
            box = person.get("box")
            if box is None:
                continue

            never_run  = person["last_sam2"] == 0
            needs_flow = person.get("needs_sam2", False)
            time_due   = (now - person["last_sam2"]) > SAM2_REFRESH_INTERVAL

            box_drifted = False
            if person["sam2_box"] is not None:
                sx1, sy1, sx2, sy2 = person["sam2_box"]
                bx1, by1, bx2, by2 = box
                drift = (((bx1+bx2)/2 - (sx1+sx2)/2)**2
                         + ((by1+by2)/2 - (sy1+sy2)/2)**2) ** 0.5
                box_drifted = drift > SAM2_BOX_DRIFT_PX

            if never_run or needs_flow or time_due or box_drifted:
                persons_to_run.append((tid, box))

        if not persons_to_run:
            elapsed = time.time() - loop_start
            time.sleep(max(0, SAM2_INTERVAL - elapsed))
            continue

        # ── Encode frame once, run predict per person ─────────────────
        frame_rgb = cv2.cvtColor(frame_copy, cv2.COLOR_BGR2RGB)
        try:
            predictor.set_image(frame_rgb)
            last_thumb = thumb

            for tid, box in persons_to_run:
                np_box = np.array(box, dtype=np.float32)
                with torch.inference_mode():
                    masks, _, _ = predictor.predict(
                        box=np_box, multimask_output=False)

                mask = (masks[0] > 0).astype(np.uint8) * 255
                contours, _ = cv2.findContours(
                    mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not contours:
                    continue

                largest = max(contours, key=cv2.contourArea)
                n       = len(largest)
                if n >= 3:
                    indices = np.linspace(0, n-1, SAM2_CONTOUR_POINTS, dtype=int)
                    sampled = largest[indices]
                else:
                    sampled = largest

                flat = sampled.reshape(-1).tolist()

                with tracked_lock:
                    if tid in tracked_persons:
                        tracked_persons[tid]["contour"]    = flat
                        tracked_persons[tid]["last_sam2"]  = now
                        tracked_persons[tid]["sam2_box"]   = box
                        tracked_persons[tid]["needs_sam2"] = False

                # Reset feature cache so flow re-detects on the fresh contour
                with prev_features_lock:
                    prev_features.pop(tid, None)

            print(f"[SAM2] corrected {len(persons_to_run)} tracks")

        except Exception as e:
            print(f"[WARN] SAM2 error: {e}")

        elapsed = time.time() - loop_start
        time.sleep(max(0, SAM2_INTERVAL - elapsed))

threading.Thread(target=sam2_loop, daemon=True).start()

# =====================================
# INFERENCE THREAD  (YOLO tracking)
# =====================================

def inference_loop():
    global latest_results
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(0.01)
            continue

        frame = frame.copy()

        with depth_map_lock:
            depth_map = latest_depth_map

        results = yolo_model.track(
            frame,
            classes=[PERSON_CLASS_ID],
            persist=True,
            verbose=False
        )
        boxes = results[0].boxes
        now   = time.time()

        detected_ids = set()

        for box in boxes:
            if box.id is None:
                continue
            track_id = int(box.id[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            confidence = float(box.conf[0])

            depth_m = None
            if depth_map is not None:
                depth_m = get_object_depth(depth_map, x1, y1, x2, y2, track_id)

            label = (f"person {confidence:.0%} | {depth_m}m"
                     if depth_m is not None
                     else f"person {confidence:.0%}")

            detected_ids.add(track_id)

            with tracked_lock:
                if track_id not in tracked_persons:
                    tracked_persons[track_id] = {
                        "contour":    [],
                        "box":        (x1, y1, x2, y2),
                        "depth_m":    depth_m if depth_m is not None else -1.0,
                        "label":      label,
                        "last_seen":  now,
                        "last_sam2":  0.0,
                        "sam2_box":   None,
                        "needs_sam2": True,
                    }
                else:
                    tracked_persons[track_id]["box"]       = (x1, y1, x2, y2)
                    tracked_persons[track_id]["depth_m"]   = (depth_m if depth_m is not None
                                                              else tracked_persons[track_id]["depth_m"])
                    tracked_persons[track_id]["label"]     = label
                    tracked_persons[track_id]["last_seen"] = now

        # ── Expire stale tracks ───────────────────────────────────────
        with tracked_lock:
            expired = [tid for tid, p in tracked_persons.items()
                       if now - p["last_seen"] > TRACK_TTL]
            for tid in expired:
                tracked_persons.pop(tid, None)
                depth_ema.pop(tid, None)

        # ── Build latest_results ──────────────────────────────────────
        detections = []
        with tracked_lock:
            for tid, person in tracked_persons.items():
                bx = person["box"]
                detections.append({
                    "label":    person["label"],
                    "x1": bx[0], "y1": bx[1],
                    "x2": bx[2], "y2": bx[3],
                    "depth_m":  person["depth_m"],
                    "world_x":  0.0,
                    "world_y":  0.0,
                    "world_z":  0.0,
                    "tracking": True,
                    "contour":  list(person["contour"]),
                })

        with results_lock:
            latest_results = detections

threading.Thread(target=inference_loop, daemon=True).start()

# =====================================
# HTTP HANDLER
# =====================================

class StreamHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == '/detections':
            with results_lock:
                payload = json.dumps(
                    {"detections": list(latest_results)}
                ).encode()
            self.send_response(200)
            self.send_header('Content-Type',   'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control',  'no-cache')
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == '/contour_stream':
            print("[INFO] Client connected to /contour_stream")
            self.send_response(200)
            self.send_header('Content-type',
                             'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection',    'close')
            self.end_headers()

            last_send      = 0
            frame_interval = 1.0 / FPS_LIMIT

            try:
                while True:
                    now = time.time()
                    if now - last_send < frame_interval:
                        time.sleep(0.001)
                        continue
                    last_send = now

                    with frame_lock:
                        frame = latest_frame
                    if frame is None:
                        continue
                    frame = frame.copy()

                    with results_lock:
                        detections = list(latest_results)
                    draw_contours_debug(frame, detections)

                    success, jpeg = cv2.imencode(
                        '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
                    if not success:
                        continue

                    data = jpeg.tobytes()
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(data)}\r\n\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

            except Exception as e:
                print(f"[INFO] Debug client disconnected: {e}")
            return

        if self.path == '/debug':
            html = b"""<!DOCTYPE html>
<html>
<head>
    <title>Jetson Debug</title>
    <style>
        body { background:#111; color:#eee; font-family:monospace;
               margin:0; padding:10px; }
        h2   { color:#0f0; margin:4px 0; }
        .row { display:flex; gap:12px; flex-wrap:wrap; }
        .box { background:#1a1a1a; border:1px solid #333;
               border-radius:6px; padding:8px; }
        img  { display:block; max-width:640px; width:100%; }
        pre  { font-size:12px; max-height:400px; overflow-y:auto;
               margin:0; white-space:pre-wrap; word-break:break-all; }
        .tag { color:#0f0; }
    </style>
</head>
<body>
    <h2>&#x1F7E2; Jetson Vision Debug</h2>
    <div class="row">
        <div class="box">
            <div class="tag">contour_stream (SAM2 outlines)</div>
            <img src="/contour_stream">
        </div>
        <div class="box">
            <div class="tag">detections JSON
                <span id="age" style="color:#888;margin-left:8px"></span>
            </div>
            <pre id="json">loading...</pre>
        </div>
    </div>
    <script>
        async function poll() {
            try {
                const r   = await fetch('/detections');
                const txt = await r.text();
                const obj = JSON.parse(txt);
                obj.detections.forEach(d => {
                    if (d.contour && d.contour.length)
                        d.contour = '[' + (d.contour.length/2) + ' points]';
                });
                document.getElementById('json').textContent =
                    JSON.stringify(obj, null, 2);
                document.getElementById('age').textContent =
                    new Date().toLocaleTimeString();
            } catch(e) {
                document.getElementById('json').textContent = 'poll error: ' + e;
            }
        }
        poll();
        setInterval(poll, 500);
    </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header('Content-Type',   'text/html')
            self.send_header('Content-Length', str(len(html))  )
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path != '/stream':
            self.send_response(404)
            self.end_headers()
            return

        print("[INFO] Client connected to /stream")
        self.send_response(200)
        self.send_header('Content-type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Pragma',        'no-cache')
        self.send_header('Connection',    'close')
        self.end_headers()

        last_send      = 0
        frame_interval = 1.0 / FPS_LIMIT

        try:
            while True:
                now = time.time()
                if now - last_send < frame_interval:
                    time.sleep(0.001)
                    continue
                last_send = now

                with frame_lock:
                    frame = latest_frame
                if frame is None:
                    continue
                frame = frame.copy()

                with results_lock:
                    detections = list(latest_results)
                draw_detections(frame, detections)

                success, jpeg = cv2.imencode(
                    '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
                if not success:
                    continue

                data = jpeg.tobytes()
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(data)}\r\n\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

        except Exception as e:
            print(f"[INFO] Client disconnected: {e}")

    def log_message(self, format, *args):
        return


# =====================================
# SERVER
# =====================================

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads      = True
    allow_reuse_address = True


server = ThreadedHTTPServer(('0.0.0.0', 8080), StreamHandler)

print("[INFO] =================================")
print("[INFO] SERVER READY")
print("[INFO] Stream:     http://JETSON_IP:8080/stream")
print("[INFO] Detections: http://JETSON_IP:8080/detections")
print("[INFO] Debug:      http://JETSON_IP:8080/debug")
print("[INFO] =================================")

server.serve_forever()

