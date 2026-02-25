/*
Run this script in SSMS against your target database (e.g., ALT_MONITOR).
It is idempotent and safe to re-run.
*/

IF OBJECT_ID('dbo.stage_file_state', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.stage_file_state (
        stage_name NVARCHAR(20) NOT NULL,
        file_name NVARCHAR(260) NOT NULL,
        logical_name NVARCHAR(260) NOT NULL,
        file_ext NVARCHAR(10) NOT NULL,
        file_size_bytes BIGINT NOT NULL,
        arrival_ts_ist DATETIME2 NOT NULL,
        last_seen_ts_ist DATETIME2 NOT NULL,
        moved_ts_ist DATETIME2 NULL,
        stuck_alert_sent BIT NOT NULL CONSTRAINT DF_stage_file_state_stuck DEFAULT 0,
        large_file BIT NOT NULL CONSTRAINT DF_stage_file_state_large DEFAULT 0,
        sla_breach_sent BIT NOT NULL CONSTRAINT DF_stage_file_state_sla DEFAULT 0,
        last_large_update_ts_ist DATETIME2 NULL,
        active BIT NOT NULL CONSTRAINT DF_stage_file_state_active DEFAULT 1,
        CONSTRAINT PK_stage_file_state PRIMARY KEY(stage_name, file_name)
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.stage_file_state') AND name = 'IX_stage_file_state_stage_active')
BEGIN
    CREATE INDEX IX_stage_file_state_stage_active ON dbo.stage_file_state(stage_name, active);
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.stage_file_state') AND name = 'IX_stage_file_state_stage_arrival')
BEGIN
    CREATE INDEX IX_stage_file_state_stage_arrival ON dbo.stage_file_state(stage_name, arrival_ts_ist);
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.stage_file_state') AND name = 'IX_stage_file_state_stage_logical_moved')
BEGIN
    CREATE INDEX IX_stage_file_state_stage_logical_moved ON dbo.stage_file_state(stage_name, logical_name, moved_ts_ist);
END;

IF OBJECT_ID('dbo.event_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_log (
        id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_event_log PRIMARY KEY,
        event_ts_ist DATETIME2 NOT NULL,
        level NVARCHAR(20) NOT NULL,
        event_type NVARCHAR(100) NOT NULL,
        stage_name NVARCHAR(20) NULL,
        file_name NVARCHAR(260) NULL,
        details NVARCHAR(MAX) NULL
    );
END;

-- Stored procedure used by Python monitor for upsert behavior
IF OBJECT_ID('dbo.usp_alt_stage_upsert_arrival', 'P') IS NULL
    EXEC('CREATE PROCEDURE dbo.usp_alt_stage_upsert_arrival AS BEGIN SET NOCOUNT ON; END');
GO
ALTER PROCEDURE dbo.usp_alt_stage_upsert_arrival
    @stage_name NVARCHAR(20),
    @file_name NVARCHAR(260),
    @logical_name NVARCHAR(260),
    @file_ext NVARCHAR(10),
    @file_size_bytes BIGINT,
    @arrival_ts_ist DATETIME2,
    @last_seen_ts_ist DATETIME2,
    @large_file BIT
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE dbo.stage_file_state
    SET file_size_bytes = @file_size_bytes,
        last_seen_ts_ist = @last_seen_ts_ist,
        active = 1
    WHERE stage_name = @stage_name
      AND file_name = @file_name;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO dbo.stage_file_state (
            stage_name, file_name, logical_name, file_ext,
            file_size_bytes, arrival_ts_ist, last_seen_ts_ist,
            large_file, active
        )
        VALUES (
            @stage_name, @file_name, @logical_name, @file_ext,
            @file_size_bytes, @arrival_ts_ist, @last_seen_ts_ist,
            @large_file, 1
        );
    END
END;
GO
