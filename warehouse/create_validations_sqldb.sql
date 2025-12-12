CREATE TABLE validations (
    opportunity_id      NVARCHAR(255) PRIMARY KEY,  -- PK obligatoire pour Power Apps
    is_validated        INT NULL,                    -- 1=OK, 0=KO, NULL=PENDING
    validated_by        NVARCHAR(255) NULL,
    validation_date     DATETIME2 NULL,
    validation_comment  NVARCHAR(MAX) NULL,

    -- Infos pour Data Activator (email)
    article_url         NVARCHAR(2048) NULL,
    article_title       NVARCHAR(500) NULL,
    vertical            NVARCHAR(100) NULL,
    venue_name          NVARCHAR(255) NULL,
    city                NVARCHAR(100) NULL,
    country             NVARCHAR(100) NULL,
    evaluation_score    INT NULL,
    audit_opportunity_reason NVARCHAR(MAX) NULL,
    saler_email         NVARCHAR(255) NULL,
    saler_name          NVARCHAR(255) NULL,

    -- Semaine d'ingestion (pour filtrage hebdomadaire)
    ingestion_week      NVARCHAR(10) NULL,           -- Format: "2024-W49"

    -- Deduplication (info contextuelle)
    is_duplicate        BIT NULL,                    -- 1=doublon, 0=unique
    is_suspected_duplicate BIT NULL,                 -- 1=zone grise
    duplicate_score     FLOAT NULL,                  -- Score similarité (0.0-1.0)

    -- Email tracking
    email_sent_at       DATETIME2 NULL,              -- NULL = pas encore envoyé

    -- Metadata
    created_at          DATETIME2 DEFAULT GETDATE(),
    updated_at          DATETIME2 DEFAULT GETDATE()
);

-- =============================================================================
-- INDEX pour les requêtes fréquentes
-- =============================================================================
CREATE INDEX IX_validations_ingestion_week ON validations(ingestion_week);
CREATE INDEX IX_validations_is_validated ON validations(is_validated);
CREATE INDEX IX_validations_email_sent ON validations(email_sent_at) WHERE email_sent_at IS NULL;
