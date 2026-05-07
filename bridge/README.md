# TSR Live Bridge

Shared Memory → WebSocket relay for Le Mans Ultimate.
Reads the rFactor 2 / LMU shared memory plugin (TheIronWolf v3.7+) and
broadcasts a JSON snapshot ~30 Hz to any WebSocket client.

## Quick start (use the prebuilt .exe)

1. Download `tsr_live_bridge.exe` from the latest [Release](../../releases) or
   from the latest [GitHub Actions run artifact](../../actions).
2. Make sure the rF2 shared memory plugin is enabled in
   `UserData/player/CustomPluginVariables.JSON`:
   ```json
   { "rFactor2SharedMemoryMapPlugin.dll": { " Enabled": 1 } }
   ```
3. Double-click `tsr_live_bridge.exe`.
4. Open TSR Command Center, go to **Live** tab, click
   *Connecter ws://localhost:8765*.

## Build it yourself

### From source on Windows

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --console --name tsr_live_bridge tsr_live_bridge.py
# Output: dist/tsr_live_bridge.exe
```

### Run without building (Python required)

```powershell
pip install -r requirements.txt
python tsr_live_bridge.py
```

## Variant: Racelab plugin

If you only have `RacelabLMUPlugin.dll` enabled, edit `tsr_live_bridge.py`
and switch to the `$lmuSMMP_Telemetry$` name (commented at the top of the file).

## What's exposed

JSON payload sent ~30 Hz per connected client:

| Key | Unit | Source |
|---|---|---|
| `speed_kmh` | km/h | local velocity magnitude |
| `rpm` / `rpm_max` | tr/min | engine |
| `gear` | -1..N | -1 = R, 0 = N |
| `fuel_l` | liters | tank |
| `throttle` / `brake` / `steering` / `clutch` | 0..1 | filtered inputs |
| `steer_torque` | Nm | steering shaft |
| `engine_temp` / `oil_temp` | °C | coolant + oil |
| `lap` | int | current lap |
| `elapsed` | s | session elapsed time |
| `vehicle` / `track` | string | identifiers |

## Notes

- Listens on `0.0.0.0:8765` so a tablet on the same Wi-Fi can connect (firewall must allow it).
- Player vehicle assumed to be index 0 (works for solo / most multiplayer cases).
  For full coverage, cross-reference with the scoring shared memory.
- The bridge needs to run on the **same Windows machine as LMU** because it reads OS-level shared memory.
