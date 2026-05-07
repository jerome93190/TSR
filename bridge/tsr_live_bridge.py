#!/usr/bin/env python3
"""
TSR Live Bridge — Shared Memory -> WebSocket relay for Le Mans Ultimate.

Reads BOTH the Scoring and Telemetry shared memories from the rF2/LMU
plugin (TheIronWolf v3.7+), finds the player by mIsPlayer flag, and
broadcasts a full snapshot to any WebSocket client at ~10 Hz.

Payload contains:
  - session: track, type, time, weather, num vehicles
  - player: telemetry (speed, RPM, gear, fuel, pedals)
  - vehicles: list of all cars with live timing (place, gap, last lap, etc.)

Compatible with:
  - rF2SMMP plugin     ($rFactor2SMMP_*$)
  - Racelab variant    ($lmuSMMP_*$)  (uncomment NAMES_LMU below)

Listens on ws://0.0.0.0:8765 (LAN-accessible).
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import struct
import sys
import time
from ctypes import (
    c_double, c_long, c_ulong, c_char, c_byte, c_ubyte, c_short, c_ushort,
    c_uint, c_float, Structure,
)

try:
    import websockets
except ImportError:
    print("[!] Missing dependency. Run: pip install websockets")
    sys.exit(1)


# ============================================================
# WIN32 SHARED MEMORY (OpenFileMapping + MapViewOfFile)
# We use the Win32 API directly because Python's mmap.mmap(-1, ..., tagname)
# *creates* the region if it doesn't exist (instead of failing) — and
# requires a length that exactly matches the existing region's size.
# OpenFileMapping returns NULL if the region doesn't exist (clean detection),
# and MapViewOfFile with size=0 maps the whole region whatever its size.
# ============================================================
if sys.platform == "win32":
    from ctypes import wintypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    _kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.MapViewOfFile.restype = ctypes.c_void_p
    _kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    _kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    _kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    FILE_MAP_READ = 0x0004
else:
    _kernel32 = None
    FILE_MAP_READ = 0


class SharedMemView:
    """Read-only view onto an *existing* named shared memory region."""

    def __init__(self, name: str):
        self.name = name
        self.handle = None
        self.addr = None
        if _kernel32 is None:
            raise OSError("Win32 API only available on Windows")
        h = _kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not h:
            err = ctypes.get_last_error()
            raise FileNotFoundError(f"OpenFileMapping({name}) error={err}")
        addr = _kernel32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not addr:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(h)
            raise OSError(f"MapViewOfFile({name}) error={err}")
        self.handle = h
        self.addr = addr

    def read(self, offset: int, length: int) -> bytes:
        return ctypes.string_at(self.addr + offset, length)

    def close(self):
        if self.addr:
            _kernel32.UnmapViewOfFile(self.addr)
            self.addr = None
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass


# ============================================================
# CONFIG
# ============================================================
NAMES_RF2 = {
    "telemetry": r"$rFactor2SMMP_Telemetry$",
    "scoring":   r"$rFactor2SMMP_Scoring$",
}
NAMES_LMU = {
    "telemetry": r"$lmuSMMP_Telemetry$",
    "scoring":   r"$lmuSMMP_Scoring$",
}

WS_HOST  = "0.0.0.0"
WS_PORT  = 8765
TICK_HZ  = 10  # broadcast rate; scoring info doesn't change faster than this anyway


# ============================================================
# SHARED MEMORY STRUCTS — rF2 plugin v3.7+ layout
# Source: github.com/TheIronWolfModding/rFactor2SharedMemoryMapPlugin
# All structs use _pack_ = 4 to match the plugin's #pragma pack(push, 4)
# ============================================================
class rF2Vec3(Structure):
    _pack_ = 4
    _fields_ = [("x", c_double), ("y", c_double), ("z", c_double)]


# --- TELEMETRY (only the head we need for HUD) -----------------
class rF2VehicleTelemetryHead(Structure):
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


# Stride from the start of one rF2VehicleTelemetry to the next.
# Full struct (incl. 4 wheels) is documented at 7416 bytes.
TELEMETRY_VEHICLE_STRIDE = 7416


# --- SCORING ---------------------------------------------------
class rF2VehicleScoring(Structure):
    _pack_ = 4
    _fields_ = [
        ("mID",                  c_long),
        ("mDriverName",          c_char * 32),
        ("mVehicleName",         c_char * 64),
        ("mTotalLaps",           c_short),
        ("mSector",              c_byte),
        ("mFinishStatus",        c_byte),
        ("mLapDist",             c_double),
        ("mPathLateral",         c_double),
        ("mTrackEdge",           c_double),
        ("mBestSector1",         c_double),
        ("mBestSector2",         c_double),
        ("mBestLapTime",         c_double),
        ("mLastSector1",         c_double),
        ("mLastSector2",         c_double),
        ("mLastLapTime",         c_double),
        ("mCurSector1",          c_double),
        ("mCurSector2",          c_double),
        ("mNumPitstops",         c_short),
        ("mNumPenalties",        c_short),
        ("mIsPlayer",            c_ubyte),
        ("mControl",             c_byte),
        ("mInPits",              c_ubyte),
        ("mPlace",               c_ubyte),
        ("mVehicleClass",        c_char * 32),
        ("mTimeBehindNext",      c_double),
        ("mLapsBehindNext",      c_long),
        ("mTimeBehindLeader",    c_double),
        ("mLapsBehindLeader",    c_long),
        ("mLapStartET",          c_double),
        ("mPos",                 rF2Vec3),
        ("mLocalVel",            rF2Vec3),
        ("mLocalAccel",          rF2Vec3),
        ("mOri",                 rF2Vec3 * 3),
        ("mLocalRot",            rF2Vec3),
        ("mLocalRotAccel",       rF2Vec3),
        ("mHeadlights",          c_ubyte),
        ("mPitState",            c_ubyte),
        ("mServerScored",        c_ubyte),
        ("mIndividualPhase",     c_ubyte),
        ("mQualification",       c_long),
        ("mTimeIntoLap",         c_double),
        ("mEstimatedLapTime",    c_double),
        ("mPitGroup",            c_char * 24),
        ("mFlag",                c_ubyte),
        ("mUnderYellow",         c_ubyte),
        ("mCountLapFlag",        c_ubyte),
        ("mInGarageStall",       c_ubyte),
        ("mUpgradePack",         c_ubyte * 16),
        ("mPitLapDist",          c_float),
        ("mBestLapSector1",      c_float),
        ("mBestLapSector2",      c_float),
        ("mExpansion",           c_ubyte * 48),
    ]


class rF2ScoringInfo(Structure):
    _pack_ = 4
    _fields_ = [
        ("mTrackName",            c_char * 64),
        ("mSession",              c_long),
        ("mCurrentET",            c_double),
        ("mEndET",                c_double),
        ("mMaxLaps",              c_long),
        ("mLapDist",              c_double),
        ("mResultsStreamPtr",     c_ubyte * 8),  # opaque pointer in mem
        ("mNumVehicles",          c_long),
        ("mGamePhase",            c_ubyte),
        ("mYellowFlagState",      c_byte),
        ("mSectorFlag",           c_byte * 3),
        ("mStartLight",           c_ubyte),
        ("mNumRedLights",         c_ubyte),
        ("mInRealtime",           c_ubyte),
        ("mPlayerName",           c_char * 32),
        ("mPlrFileName",          c_char * 64),
        ("mDarkCloud",            c_double),
        ("mRaining",              c_double),
        ("mAmbientTemp",          c_double),
        ("mTrackTemp",            c_double),
        ("mWind",                 rF2Vec3),
        ("mMinPathWetness",       c_double),
        ("mMaxPathWetness",       c_double),
        ("mGameMode",             c_ubyte),
        ("mIsPasswordProtected",  c_ubyte),
        ("mServerPort",           c_ushort),
        ("mServerPublicIP",       c_uint),
        ("mMaxPlayers",           c_long),
        ("mServerName",           c_char * 32),
        ("mStartET",              c_float),
        ("mAvgPathWetness",       c_double),
        ("mExpansion",            c_ubyte * 200),
    ]


MAX_MAPPED_VEHICLES = 128


class rF2Scoring(Structure):
    _pack_ = 4
    _fields_ = [
        ("mVersionUpdateBegin",   c_ulong),
        ("mVersionUpdateEnd",     c_ulong),
        ("mBytesUpdatedHint",     c_long),
        ("mScoringInfo",          rF2ScoringInfo),
        ("mVehicles",             rF2VehicleScoring * MAX_MAPPED_VEHICLES),
    ]


# Decoded constants ------------------------------------------------
SESSION_NAMES = {
    0: "Test Day",
    1: "Practice 1", 2: "Practice 2", 3: "Practice 3", 4: "Practice 4",
    5: "Qualify 1", 6: "Qualify 2", 7: "Qualify 3", 8: "Qualify 4",
    9: "Warm-up",
    10: "Race 1", 11: "Race 2", 12: "Race 3", 13: "Race 4",
}
GAME_PHASE_NAMES = {
    0: "Garage", 1: "Recon", 2: "Formation", 3: "Countdown",
    4: "Green Flag", 5: "Full-Course Yellow", 6: "Stopped", 7: "Finished",
}
CONTROL_NAMES = {0: "nobody", 1: "player", 2: "AI", 3: "remote", 4: "replay", -1: "?"}


# ============================================================
# READERS
# ============================================================
def safe_str(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("latin-1", errors="ignore").strip()


def read_scoring(mm: SharedMemView) -> dict | None:
    """Read the entire Scoring shared memory. Returns dict or None if not ready."""
    raw = mm.read(0, ctypes.sizeof(rF2Scoring))
    sc = rF2Scoring.from_buffer_copy(raw)
    if sc.mVersionUpdateBegin != sc.mVersionUpdateEnd or sc.mScoringInfo.mNumVehicles < 1:
        return None
    info = sc.mScoringInfo
    n = max(0, min(info.mNumVehicles, MAX_MAPPED_VEHICLES))
    vehicles = []
    for i in range(n):
        v = sc.mVehicles[i]
        vehicles.append({
            "id":          v.mID,
            "place":       v.mPlace,
            "name":        safe_str(v.mDriverName),
            "vehicle":     safe_str(v.mVehicleName),
            "class":       safe_str(v.mVehicleClass),
            "lap":         v.mTotalLaps,
            "sector":      v.mSector,
            "best_lap":    v.mBestLapTime if v.mBestLapTime > 0 else None,
            "best_s1":    v.mBestSector1 if v.mBestSector1 > 0 else None,
            "best_s2":    (v.mBestSector2 - v.mBestSector1) if v.mBestSector2 > 0 and v.mBestSector1 > 0 else None,
            "last_lap":    v.mLastLapTime if v.mLastLapTime > 0 else None,
            "last_s1":     v.mLastSector1 if v.mLastSector1 > 0 else None,
            "last_s2":     (v.mLastSector2 - v.mLastSector1) if v.mLastSector2 > 0 and v.mLastSector1 > 0 else None,
            "cur_s1":      v.mCurSector1 if v.mCurSector1 > 0 else None,
            "pits":        v.mNumPitstops,
            "penalties":   v.mNumPenalties,
            "is_player":   bool(v.mIsPlayer),
            "control":     CONTROL_NAMES.get(v.mControl, "?"),
            "in_pits":     bool(v.mInPits),
            "in_garage":   bool(v.mInGarageStall),
            "gap_next_t":  v.mTimeBehindNext if v.mTimeBehindNext > 0 else 0,
            "gap_next_l":  v.mLapsBehindNext,
            "gap_lead_t":  v.mTimeBehindLeader if v.mTimeBehindLeader > 0 else 0,
            "gap_lead_l":  v.mLapsBehindLeader,
            "finish":      v.mFinishStatus,
            "headlights":  bool(v.mHeadlights),
        })
    return {
        "track":        safe_str(info.mTrackName),
        "session":      SESSION_NAMES.get(info.mSession, f"S{info.mSession}"),
        "session_id":   info.mSession,
        "phase":        GAME_PHASE_NAMES.get(info.mGamePhase, f"P{info.mGamePhase}"),
        "phase_id":     info.mGamePhase,
        "current_et":   info.mCurrentET,
        "end_et":       info.mEndET,
        "max_laps":     info.mMaxLaps,
        "lap_dist":     info.mLapDist,
        "in_realtime":  bool(info.mInRealtime),
        "yellow":       info.mYellowFlagState,
        "sector_flag":  list(info.mSectorFlag),
        "player_name":  safe_str(info.mPlayerName),
        "weather":      {
            "rain":     info.mRaining,
            "clouds":   info.mDarkCloud,
            "ambient":  info.mAmbientTemp,
            "track":    info.mTrackTemp,
            "min_wet":  info.mMinPathWetness,
            "max_wet":  info.mMaxPathWetness,
            "avg_wet":  info.mAvgPathWetness,
        },
        "vehicles":     vehicles,
    }


def read_telemetry_for_player(mm: SharedMemView, player_id: int, num_vehicles: int) -> dict | None:
    """Read Telemetry, find vehicle matching player_id (by mID)."""
    update_begin, update_end, num_t = struct.unpack("<IIi", mm.read(0, 12))
    if update_begin != update_end or num_t < 1:
        return None
    n = max(0, min(num_t, num_vehicles, MAX_MAPPED_VEHICLES))
    head_size = ctypes.sizeof(rF2VehicleTelemetryHead)
    for i in range(n):
        offset = 12 + i * TELEMETRY_VEHICLE_STRIDE
        raw = mm.read(offset, head_size)
        v = rF2VehicleTelemetryHead.from_buffer_copy(raw)
        if v.mID == player_id:
            speed = (v.mLocalVel.x ** 2 + v.mLocalVel.y ** 2 + v.mLocalVel.z ** 2) ** 0.5 * 3.6
            return {
                "vehicle":      safe_str(v.mVehicleName),
                "track":        safe_str(v.mTrackName),
                "lap":          v.mLapNumber,
                "elapsed":      round(v.mElapsedTime, 3),
                "gear":         v.mGear,
                "rpm":          round(v.mEngineRPM, 0),
                "rpm_max":      round(v.mEngineMaxRPM, 0),
                "speed_kmh":    round(speed, 2),
                "fuel_l":       round(v.mFuel, 3),
                "throttle":     round(v.mFilteredThrottle, 3),
                "brake":        round(v.mFilteredBrake, 3),
                "steering":     round(v.mFilteredSteering, 3),
                "clutch":       round(v.mFilteredClutch, 3),
                "steer_torque": round(v.mSteeringShaftTorque, 2),
                "engine_temp":  round(v.mEngineWaterTemp, 1),
                "oil_temp":     round(v.mEngineOilTemp, 1),
            }
    return None


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


def try_open_pair():
    """Try rF2 names first, fall back to LMU names. Returns (scoring_view, tele_view, label) or all None."""
    diagnostics = []
    for label, names in [("rFactor2 (TheIronWolf)", NAMES_RF2), ("Racelab/LMU", NAMES_LMU)]:
        try:
            mm_s = SharedMemView(names["scoring"])
        except FileNotFoundError as e:
            diagnostics.append(f"  - {label}: scoring NOT FOUND ({names['scoring']})")
            continue
        except OSError as e:
            diagnostics.append(f"  - {label}: scoring error: {e}")
            continue
        try:
            mm_t = SharedMemView(names["telemetry"])
        except Exception as e:
            mm_s.close()
            diagnostics.append(f"  - {label}: scoring OK but telemetry error: {e}")
            continue
        print(f"[OK] Connected to {label}")
        print(f"     scoring   = {names['scoring']}")
        print(f"     telemetry = {names['telemetry']}")
        return mm_s, mm_t, label
    for line in diagnostics:
        print(line)
    return None, None, None


async def broadcast_loop():
    mm_s = mm_t = None
    label = None
    last_attempt = 0.0
    last_warn = 0.0

    while True:
        now = time.time()

        if mm_s is None and now - last_attempt > 2:
            last_attempt = now
            mm_s, mm_t, label = try_open_pair()
            if mm_s is None and now - last_warn > 5:
                print("[..] Plugin not detected. Checks:")
                print("     1. Is LMU running?")
                print("     2. Is rFactor2SharedMemoryMapPlugin64.dll in LMU/Plugins/ ?")
                print("     3. In UserData/player/CustomPluginVariables.JSON, do you have:")
                print('        "rFactor2SharedMemoryMapPlugin.dll": { " Enabled":1 }')
                print("     4. Have you restarted LMU after enabling the plugin?")
                print("     5. Are you in a session (not just at the menu) ?")
                last_warn = now
                if CLIENTS:
                    payload = json.dumps({
                        "error": "shared_memory_unavailable",
                        "message": "Bridge connecté, mais shared memory du plugin introuvable. Vérifie que LMU est lancé, plugin activé, et que tu es DANS une session (track chargé, pas le menu).",
                        "ts": now,
                    })
                    await asyncio.gather(*[c.send(payload) for c in CLIENTS], return_exceptions=True)

        if mm_s is not None and CLIENTS:
            try:
                scoring = read_scoring(mm_s)
                if scoring is not None:
                    # Find player by mIsPlayer
                    player_idx = next((i for i, v in enumerate(scoring["vehicles"]) if v["is_player"]), None)
                    player_telemetry = None
                    if player_idx is not None:
                        player_id = scoring["vehicles"][player_idx]["id"]
                        try:
                            player_telemetry = read_telemetry_for_player(mm_t, player_id, len(scoring["vehicles"]))
                        except Exception:
                            player_telemetry = None
                    payload_obj = {
                        "ok": True,
                        "ts": now,
                        "source": label,
                        "session": {
                            "track":       scoring["track"],
                            "type":        scoring["session"],
                            "phase":       scoring["phase"],
                            "current_et":  scoring["current_et"],
                            "end_et":      scoring["end_et"],
                            "max_laps":    scoring["max_laps"],
                            "in_realtime": scoring["in_realtime"],
                            "weather":     scoring["weather"],
                            "yellow":      scoring["yellow"],
                            "num_vehicles": len(scoring["vehicles"]),
                            "player_name": scoring["player_name"],
                        },
                        "vehicles":  scoring["vehicles"],
                        "player":    player_telemetry,
                    }
                    payload = json.dumps(payload_obj)
                    await asyncio.gather(*[c.send(payload) for c in CLIENTS], return_exceptions=True)
            except Exception as e:
                print(f"[!] read error: {e}")
                try: mm_s.close()
                except Exception: pass
                try: mm_t.close()
                except Exception: pass
                mm_s = mm_t = None

        await asyncio.sleep(1.0 / TICK_HZ)


async def main():
    print("=" * 64)
    print("  TSR Live Bridge")
    print("  Shared Memory -> WebSocket relay for Le Mans Ultimate")
    print("=" * 64)
    print(f"  WebSocket  : ws://localhost:{WS_PORT}  (also LAN at 0.0.0.0:{WS_PORT})")
    print(f"  Tick rate  : {TICK_HZ} Hz")
    print(f"  Reads      : Scoring (all cars) + Telemetry (player)")
    print()
    print("  -> Open the TSR Command Center, Live tab, click 'Connecter'")
    print("  -> Press Ctrl+C to stop")
    print("=" * 64)

    async with websockets.serve(handler, WS_HOST, WS_PORT):
        await broadcast_loop()


if __name__ == "__main__":
    # Sanity checks
    print(f"[size] rF2VehicleTelemetryHead = {ctypes.sizeof(rF2VehicleTelemetryHead)} bytes")
    print(f"[size] rF2VehicleScoring       = {ctypes.sizeof(rF2VehicleScoring)} bytes")
    print(f"[size] rF2ScoringInfo          = {ctypes.sizeof(rF2ScoringInfo)} bytes")
    print(f"[size] rF2Scoring              = {ctypes.sizeof(rF2Scoring)} bytes")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stop] Bridge stopped.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
        sys.exit(1)
