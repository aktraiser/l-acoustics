CREATE OR ALTER VIEW vw_opportunities_with_validation AS
SELECT
    -- Opportunite
    o.id,
    o.article_url,
    o.article_title,
    o.publication_date,
    o.language,
    o.origin,

    -- Vertical / Market segment
    o.vertical,

    -- Venue
    o.venue_name,
    o.city,
    o.country,
    o.zone,
    o.venue_type,
    o.capacity,

    -- Projet
    o.project_type,
    o.project_phase,
    o.opening_year,
    o.opening_date,

    -- Financier
    o.investment,
    o.investment_currency,

    -- Stakeholders
    o.investor_owner_management,
    o.architect_consultant_contractor,

    -- Concurrents
    o.competitor_name_main,
    o.competitor_name_other,
    o.key_products_installed,
    o.system_integrator,
    o.other_key_players,

    -- Analyse
    o.evaluation_score,
    o.audit_opportunity,
    o.audit_opportunity_reason,

    -- Metadata opportunite
    o.crawled_at,
    o.loaded_at,
    o.ingestion_week,

    -- Deduplication
    o.is_duplicate,
    o.is_suspected_duplicate,
    o.duplicate_of,
    o.duplicate_score,
    CASE
        WHEN o.is_duplicate = 1 THEN 'DOUBLON'
        WHEN o.is_suspected_duplicate = 1 THEN 'ZONE_GRISE'
        WHEN o.is_duplicate = 0 THEN 'UNIQUE'
        ELSE 'NON_TRAITE'
    END AS dedup_status,

    -- RAE/RSM (par pays) - une ligne par saler
    s.who AS saler_name,
    s.email AS saler_email,
    s.role AS saler_role,
    s.sales_zone,
    s.sales_region,
    s.country_code,

    -- BD (par verticale) - une ligne par BD
    bd.who AS bd_name,
    bd.email AS bd_email,

    -- VALIDATION (depuis SQL DATABASE - cross-database query)
    -- is_validated: -1=PENDING, 1=OK, 0=KO (jamais NULL pour Power BI)
    COALESCE(v.is_validated, -1) AS is_validated,
    CASE
        WHEN v.is_validated = 1 THEN 'OK'
        WHEN v.is_validated = 0 THEN 'KO'
        ELSE 'PENDING'
    END AS validation_status,
    v.validated_by,
    v.validation_date,
    v.validation_comment,

    -- Email tracking
    v.email_sent_at,
    CASE
        WHEN v.email_sent_at IS NOT NULL THEN 'SENT'
        ELSE 'NOT_SENT'
    END AS email_status

FROM weak_signals_lakehouse.dbo.landing_feedly_opportunities o
LEFT JOIN weak_signals_lakehouse.dbo.landing_salers s
    ON TRIM(LOWER(o.country)) = TRIM(LOWER(s.country))
LEFT JOIN weak_signals_lakehouse.dbo.landing_salers_bd bd
    ON TRIM(LOWER(o.vertical)) = TRIM(LOWER(bd.market_segment))
-- CROSS-DATABASE JOIN vers SQL Database (writeback Power Apps)
-- Note: Dans Fabric, utiliser le nom exact visible dans sys.databases
LEFT JOIN weak_signals_sqldb.dbo.validations v
    ON o.id = v.opportunity_id
WHERE o.audit_opportunity = 1
  AND (o.is_duplicate = 0 OR o.is_duplicate IS NULL);  -- Exclut les doublons confirmes
GO


-- =============================================================================
-- VUE AUDIT: Doublons exclus (pour monitoring)
-- =============================================================================
-- Cette vue montre les doublons confirmes pour audit/debug
-- -----------------------------------------------------------------------------
CREATE OR ALTER VIEW vw_duplicates_audit AS
SELECT
    o.id,
    o.article_title,
    o.venue_name,
    o.city,
    o.country,
    o.ingestion_week,
    o.is_duplicate,
    o.is_suspected_duplicate,
    o.duplicate_of,
    o.duplicate_score,
    -- Article original (si doublon)
    orig.article_title AS original_article_title,
    orig.venue_name AS original_venue_name,
    orig.ingestion_week AS original_ingestion_week
FROM weak_signals_lakehouse.dbo.landing_feedly_opportunities o
LEFT JOIN weak_signals_lakehouse.dbo.landing_feedly_opportunities orig
    ON o.duplicate_of = orig.id
WHERE o.is_duplicate = 1 OR o.is_suspected_duplicate = 1;
GO

-- =============================================================================
-- VUE STATS: Statistiques de deduplication par semaine
-- =============================================================================
CREATE OR ALTER VIEW vw_dedup_stats_weekly AS
SELECT
    ingestion_week,
    COUNT(*) AS total_articles,
    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) AS doublons_confirmes,
    SUM(CASE WHEN is_suspected_duplicate = 1 THEN 1 ELSE 0 END) AS zone_grise,
    SUM(CASE WHEN is_duplicate = 0 AND (is_suspected_duplicate = 0 OR is_suspected_duplicate IS NULL) THEN 1 ELSE 0 END) AS uniques,
    SUM(CASE WHEN is_duplicate IS NULL THEN 1 ELSE 0 END) AS non_traites,
    ROUND(100.0 * SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_doublons
FROM weak_signals_lakehouse.dbo.landing_feedly_opportunities
WHERE audit_opportunity = 1
GROUP BY ingestion_week;
GO