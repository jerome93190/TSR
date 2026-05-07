#!/usr/bin/env python3
"""
TSR Live Bridge — Shared Memory -> WebSocket relay for Le Mans Ultimate.

Reads the rFactor 2 / LMU shared memory plugin (TheIronWolf v3.7+) and
broadcasts a JSON snapshot ~30 Hz to any WebSocket client (browser, tablet).

Compatible with:
  - rF2SMMP plugin: $rFactor2SMMP_Telemetry$
  - Racelab variant: $lmuSMMP_Telemetry$  (uncomment below)

Listens on ws://0.0.0.0:8765 (LAN-accessible by default).
"""

import asyncio
import ctypes
import json
import mmap
import sys
import time
from ctypes import (
    c_double, c_long, c_ulong, c_char, c_ubyte, c_float,
    c_int, c_uint, Structure,
)

try:
    import websockets
except ImportError:
    print("[!] Missing dependency. Run: pip install websockets")
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================
TELEMETRY_NAME = r"$rFactor2SMMP_Telemetry$"
# TELEMETRY_NAME = r"$lmuSMMP_Telemetry$"  # uncomment for Racelab variant

WS_HOST = "0.0.0.0"
WS_PORT = 8765
TICK_HZ = 30


# ============================================================
# SHARED MEMORY STRUCTURES (rF2 plugin v3.7+)
# Source: github.com/TheIronWolfModding/rFactor2SharedMemoryMapPlugin
# We only declare fields up to mFuel/mEngineMaxRPM — enough for HUD demo.
# ctypes computes correct offsets up to the last declared field.
# ============================================================
class rF2Vec3(Structure):
    _pack_ = 4
    _fields_ = [("x", c_double), ("y", c_double), ("z", c_double)]


class rF2VehicleTelemetryHead(Structure):
    """Subset of rF2VehicleTelemetry — fields up to mEngineMaxRPM."""
    _pack_ = 4
    _fields_ = [
        ("mID",                   c_long),
        ("mDeltaTime",            c_double),
        ("mElapsedTime",          c_double),
        ("mLapNumber",            c_long),
        ("mLapStartET",           c_double),
        ("mVehicleName",          c_char * 64),
        ("mTrackName",            c_char * 64),
        ("mPos",                  rF2Vec3),
        ("mLocalVel",             rF2Vec3),
        ("mLocalAccel",           rF2Vec3),
        ("mOri",                  rF2Vec3 * 3),
        ("mLocalRot",             rF2Vec3),
        ("mLocalRotAccel",        rF2Vec3),
        ("mGear",                 c_long),
        ("mEngineRPM",            c_double),
        ("mEngineWaterTemp",      c_double),
        ("mEngineOilTemp",        c_double),
        ("mClutchRPM",            c_double),
        ("mUnfilteredThrottle",   c_double),
        ("mUnfilteredBrake",      c_double),
        ("mUnfilteredSteering",   c_double),
        ("mUnfilteredClutch",     c_double),
        ("mFilteredThrottle",     c_double),
        ("mFilteredBrake",        c_double),
        ("mFilteredSteering",     c_double),
        ("mFilteredClutch",       c_double),
        ("mSteeringShaftTorque",  c_double),
        ("mFront3rdDeflection",   c_double),
        ("mRear3rdDeflection",    c_double),
        ("mFrontWingHeight",      c_double),
        ("mFrontRideHeight",      c_double),
        ("mRearRideHeight",       c_double),
        ("mDrag",                 c_double),
        ("mFrontDownforce",       c_double),
        ("mRearDownforce",        c_double),
        ("mFuel",                 c_double),
        ("mEngineMaxRPM",         c_double),
    ]


# Full rF2VehicleTelemetry struct is ~7416 bytes (incl. 4 wheels of ~272 bytes each).
# We only read the head portion.
FULL_VEHICLE_TELEMETRY_SIZE = 7416

# rF2Telemetry header: 2x ULONG + 1x LONG + padding = 12 bytes
TELE_HEADER_FIELDS = "<IIi"  # mVersionUpdateBegin, mVersionUpdateEnd, mNumVehicles
import struct as struct_mod
TELE_HEADER_SIZE = struct_mod.calcsize(TELE_HEADER_FIELDS)


# ============================================================
# SHARED MEMORY READER
# ============================================================
def open_shared_memory():
    """Open the named shared memory region. Raises on failure."""
    # 8MB is more than enough — full struct is ~950KB
    return mmap.mmap(-1, 8 * 1024 * 1024, TELEMETRY_NAME)


def read_snapshot(mm):
    """Read one snapshot of the player vehicle telemetry."""
    mm.seek(0)
    header_raw = mm.read(TELE_HEADER_SIZE)
    update_begin, update_end, num_vehicles = struct_mod.unpack(TELE_HEADER_FIELDS, header_raw)

    if num_vehicles < 1 or update_begin != update_end:
        # Either no session active, or the writer is mid-update — skip
        return None

    # Player vehicle is typically index 0; if not, we'd cross-reference with scoring's
    # mPlayerVehicleIndex. For HUD demo, index 0 works for single-player and most cases.
    vehicle_offset = TELE_HEADER_SIZE
    mm.seek(vehicle_offset)
    raw = mm.read(ctypes.sizeof(rF2VehicleTelemetryHead))
    veh = rF2VehicleTelemetryHead.from_buffer_copy(raw)

    # Speed magnitude from local velocity (m/s -> km/h)
    vx, vy, vz = veh.mLocalVel.x, veh.mLocalVel.y, veh.mLocalVel.z
    speed_kmh = (vx * vx + vy * vy + vz * vz) ** 0.5 * 3.6

    return {
        "vehicle":     veh.mVehicleName.decode("latin-1", errors="ignore").strip("\x00 "),
        "track":       veh.mTrackName.decode("latin-1", errors="ignore").strip("\x00 "),
        "lap":         veh.mLapNumber,
        "elapsed":     round(veh.mElapsedTime, 3),
        "gear":        veh.mGear,
        "rpm":         round(veh.mEngineRPM, 0),
        "rpm_max":     round(veh.mEngineMaxRPM, 0),
        "speed_kmh":   round(speed_kmh, 2),
        "fuel_l":      round(veh.mFuel, 3),
        "throttle":    round(veh.mFilteredThrottle, 3),
        "brake":       round(veh.mFilteredBrake, 3),
        "steering":    round(veh.mFilteredSteering, 3),
        "clutch":      round(veh.mFilteredClutch, 3),
        "steer_torque": round(veh.mSteeringShaftTorque, 2),
        "engine_temp": round(veh.mEngineWaterTemp, 1),
        "oil_temp":    round(veh.mEngineOilTemp, 1),
    }


# ============================================================
# WEBSOCKET SERVER
# ============================================================
CLIENTS = set()


async def handler(ws):
    CLIENTS.add(ws)
    print(f"[+] Client connected ({len(CLIENTS)} total) — {ws.remote_address}")
    try:
        async for _ in ws:
            pass
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)
        print(f"[-] Client disconnected ({len(CLIENTS)} total)")


async def broadcast_loop():
    mm = None
    last_attempt = 0.0
    last_warn = 0.0

    while True:
        now = time.time()

        if mm is None and now - last_attempt > 2:
            last_attempt = now
            try:
                mm = open_shared_memory()
                print(f"[OK] Shared memory mapped: {TELEMETRY_NAME}")
            except OSError as e:
                if now - last_warn > 5:
                    print(f"[..] Waiting for plugin (LMU not running?): {e}")
                    last_warn = now
                if CLIENTS:
                    payload = json.dumps({
                        "error": "shared_memory_unavailable",
                        "message": "LMU plugin not active yet — start a session in game.",
                        "ts": now,
                    })
                    await asyncio.gather(*[c.send(payload) for c in CLIENTS], return_exceptions=True)

        if mm is not None and CLIENTS:
            try:
                data = read_snapshot(mm)
                if data is not None:
                    data["ts"] = now
                    payload = json.dumps(data)
                    await asyncio.gather(*[c.send(payload) for c in CLIENTS], return_exceptions=True)
            except Exception as e:
                print(f"[!] read error: {e}")
                try:
                    mm.close()
                except Exception:
                    pass
                mm = None

        await asyncio.sleep(1.0 / TICK_HZ)


async def main():
    print("=" * 60)
    print("  TSR Live Bridge")
    print("  Shared Memory -> WebSocket relay for Le Mans Ultimate")
    print("=" * 60)
    print(f"  Listening on ws://localhost:{WS_PORT}")
    print(f"  LAN access:   ws://<this-machine-ip>:{WS_PORT}")
    print(f"  Reading from: {TELEMETRY_NAME}")
    print(f"  Tick rate:    {TICK_HZ} Hz")
    print()
    print("  -> Open the TSR Command Center in your browser (Live tab)")
    print("  -> Click 'Connecter ws://localhost:8765'")
    print("  -> Press Ctrl+C to stop")
    print("=" * 60)

    async with websockets.serve(handler, WS_HOST, WS_PORT):
        await broadcast_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stop] Bridge stopped.")
    except Exception as e:
        print(f"\n[fatal] {e}")
        input("Press Enter to exit...")
        sys.exit(1)
