# ALT Multi-Stage Monitor (Step1 → Step2 → Step3 → Step4) - No SQL Version

Production-ready Python automation for real-time ALT pipeline monitoring with optimized **file-based state + detailed logs**.

## Stages covered

1. **STEP1**: `C:\Users\karun\Downloads\ALT\01_VU\Altruista` (`.txt`)
2. **STEP2**: `C:\Users\karun\Downloads\ALT\Step2` (`.txt`)
3. **STEP3**: `C:\Users\karun\Downloads\ALT\Step_3` (`.edi`)
4. **STEP4**: `C:\Users\karun\Downloads\ALT\Step4` (`.edi`)

## Core workflow

- Matches only files containing today's `YYYYMMDD` in filename.
- STEP2 starts only after same logical file moved from STEP1.
- STEP3 starts only after same logical file moved from STEP2.
- STEP4 starts only after same logical file moved from STEP3.
- Prevents stage interference/confusion.

## Alerts and notifications

For each stage:
- Arrival mail to Team1.
- Moved-out mail to Team1.
- Stuck alert (>5 min) to internal team.
- Large-file path (>=700 MB):
  - suppress normal stuck alert,
  - SLA breach alert at 2.5 hours,
  - progress mail every 30 minutes,
  - completion mail when moved.

STEP1 business-window control:
- IST window: **06:00 to 08:30**
- Expected count: **2 files**
- Missing-file alert sent once/day after 08:30 if count is short.

## Persistence and logging (No SQL)

- Runtime state file: `state/monitor_state.json`
- Rotating operational log: `logs/monitor.log`
- Structured event audit log: `logs/events.jsonl`

This provides restart-safe continuity and detailed process traceability without database dependency.

## Optimization done

- Uses `os.scandir` for faster directory polling.
- Uses atomic state writes (`.tmp` + replace) to avoid corruption.
- Uses rotating logs to avoid unlimited growth.
- Uses in-memory state object with periodic checkpoint save.

## Mail setup

- SMTP Port: **25**
- From: `Naveen.Thiyagasundaram@caresource.com`
- Internal: `Hari.shankaran@caresource.com`
- Team1: `Thamaraipriya.Madhaiyan@caresource.com`

## Run

```bash
python monitor_alt_files.py
```

Run continuously (VS Code terminal / scheduler / service wrapper).
