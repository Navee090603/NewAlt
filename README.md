# ALT Multi-Stage Monitor (Step1 → Step2 → Step3 → Step4)

Production-ready Python automation for real-time ALT pipeline monitoring with **Microsoft SQL Server (SSMS)** persistence.

## Stages covered

1. **STEP1**: `C:\Users\karun\Downloads\ALT\01_VU\Altruista` (`.txt`)
2. **STEP2**: `C:\Users\karun\Downloads\ALT\Step2` (`.txt`)
3. **STEP3**: `C:\Users\karun\Downloads\ALT\Step_3` (`.edi`)
4. **STEP4**: `C:\Users\karun\Downloads\ALT\Step4` (`.edi`)

## Core workflow

- Matches only files containing today's `YYYYMMDD` in name.
- STEP2 is processed only if same logical file (same base name / stem) moved from STEP1.
- STEP3 is processed only if same logical file moved from STEP2.
- STEP4 is processed only if same logical file moved from STEP3.
- This prevents stage interference/confusion.

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

## SSMS (SQL Server) setup

### 1) Open SSMS and run schema script

Run:
- `sql/alt_monitor_schema.sql`

This script creates:
- `dbo.stage_file_state`
- `dbo.event_log`
- `dbo.usp_alt_stage_upsert_arrival`

### 2) Python dependency

```bash
pip install pyodbc
```

### 3) Connection string

Set environment variable:

```bash
set ALT_MSSQL_CONN_STR=Driver={ODBC Driver 17 for SQL Server};Server=YOUR_SERVER;Database=ALT_MONITOR;UID=YOUR_USER;PWD=YOUR_PASSWORD;TrustServerCertificate=yes;
```

Optional: if DB is already prepared in SSMS and you want to skip auto-DDL from Python:

```bash
set ALT_MSSQL_AUTO_SETUP=false
```

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
