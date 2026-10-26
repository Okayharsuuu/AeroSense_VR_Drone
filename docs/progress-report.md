# AeroSense VR Drone: Progress Report

Oct 27, 2026 · Aman Sharma

## Goal

A SIYI A8 mini gimbal camera and an NVIDIA Jetson ride on the drone, with ArduPilot as the flight controller. The operator wears a Meta Quest 3 and sees the camera's live video, and the gimbal turns with their head. The Jetson detects people in the video, and the headset's passthrough mode shows their outlines placed in the operator's surroundings.

The first milestone is the camera feed in VR with head-controlled gimbal, running from the drone.

## Where we are now

The first milestone works on the bench. The headset shows the camera's live video, and the gimbal follows head movement in the correct direction on both axes. The Jetson, camera and mount are fitted on the drone. Flight testing comes next.

## What's done

| Area | What we did | Result |
| --- | --- | --- |
| Camera link | Connected the A8 mini to the Jetson over Ethernet | Jetson at 192.168.144.30 reaches the camera at 192.168.144.25 with no packet loss |
| Video stream | Decoded the camera's RTSP stream with the Jetson's hardware decoder | H.265, 1280×720, 25 fps, no lost packets |
| Gimbal control | Wrote `gimbal_test.py` to send SDK commands and read the replies | Gimbal reaches commanded angles (yaw 31.9°, pitch −30°) and centres on command |
| Upside-down mount | Added `YAW_SIGN` and `PITCH_SIGN` to `gimbal_controller.py`; rotated the video 180° on the Jetson | Gimbal follows the headset on both axes; picture is upright |
| Network | Put the Jetson, Quest and laptop on one Tailscale network; switched the Jetson's firewall backend to iptables-legacy | All three connect directly over the local network |
| Network delay | Found laptop Wi-Fi power saving was adding 100–250 ms of jitter | Steady 22 ms after turning power saving off |
| Video server | Ran `a8_stream_server.py` on the Jetson | Live feed with person boxes at port 8080 |
| Unity setup | Installed Unity 2022.3.62f2 with Android support on Ubuntu 24.04 | Project opens and builds |
| Linux build fixes | Removed an unused web-view package; patched a Meta XR SDK 201 bug with `tools/patch_meta_sdk_linux.sh` | Both blocked building on Linux; neither affects the app |
| Quest app | Set the Jetson IP; named the app VR Drone Aerosense (`com.aerosense.vrdrone`) | Installed and running on the Quest |
| Headset test | Full loop with the Quest | Video in VR; gimbal follows head movement |
| Mount | Designed and printed the Jetson and battery mount (`cad/`) | Fitted on the drone |

## Addresses

| Device | Address | Notes |
| --- | --- | --- |
| Jetson (Tailscale) | 100.93.247.80 | Video on port 8080, gimbal commands on UDP 5005 |
| Jetson (to camera) | 192.168.144.30 on `enP8p1s0` | Static address on the Ethernet port |
| A8 mini camera | 192.168.144.25 | RTSP on 8554, SDK control on UDP 37260 |
| Quest 3 (Tailscale) | 100.83.197.49 | Tailscale must be on before launching the app |

## What's pending

**On the drone**

- [ ] Start both Jetson scripts automatically at boot, since there is no SSH access in flight
- [ ] Pick a field network: a phone hotspot or small router that travels with the drone; Tailscale also needs internet on both ends
- [ ] Power the Jetson and camera from the drone battery and check flight time with the added weight
- [ ] Connect the ArduPilot flight controller to the Jetson for drone position and attitude
- [ ] Check frame rate and temperature with YOLO, MiDaS and SAM2 running on the Jetson in flight

**Passthrough from a moving drone**

The person outlines currently assume the camera stays still. The Quest app asks the Jetson for the camera's position at `/camera_pose`, but the Jetson server doesn't serve that endpoint yet.

- [ ] Serve the drone's real position and attitude from ArduPilot at `/camera_pose`
- [ ] Calibrate MiDaS depth properly, since distances are approximate today
- [ ] Make the Jetson IP a setting in the app instead of hardcoded in four scripts

Two things to avoid: don't accept Unity's offer to upgrade the Meta XR SDK past 201, and don't open the project with Unity 6.
