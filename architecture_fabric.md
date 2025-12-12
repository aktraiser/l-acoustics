# Architecture Fabric - Pipeline Weak Signals

Cette doc décrit comment on a construit le pipeline de collecte et d'analyse des opportunités Feedly dans Microsoft Fabric.

## Vue d'ensemble

### Le flux en bref

```
Azure Functions (ingestion)
        ↓
landing_feedly_opportunities (on garde tout l'historique)
        ↓
deduplicate_weekly.ipynb (dédup via ai.similarity)
        ↓
weekly_opportunities (les opportunités uniques de la semaine)
        ↓
Export Excel → Les commerciaux valident → Power BI
```

### Schéma complet

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                 AZURE FUNCTIONS                                     │
│                          (func-lac-opportunities-premium)                           │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                     │
│  Feedly API ──► APIM ──► feed_ingest ──► q-raw-events                              │
│                                              │                                      │
│                                              ▼                                      │
│                                       enrich_event (Agent IA lac-weak-signals)     │
│                                              │                                      │
│                                              ▼                                      │
│                                       Azure AI Search (index)                       │
│                                              │                                      │
│                                              ▼                                      │
│                                       analyze_event (Agent IA lac-analyst-leads)   │
│                                              │                                      │
│                                              ▼                                      │
│                                       q-opportunities                               │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                    MICROSOFT FABRIC (Market Research Dev - LBO)                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                     │
│  ┌─────────────────────────────────────┐    ┌─────────────────────────────────────┐│
│  │         LAKEHOUSE                   │    │        SQL DATABASE                 ││
│  │  (weak_signals_lakehouse)           │    │    (weak_signals_sqldb)             ││
│  ├─────────────────────────────────────┤    ├─────────────────────────────────────┤│
│  │ Tables/                             │    │                                     ││
│  │ ├── landing_feedly_opportunities    │    │ validations (table)                 ││
│  │ ├── landing_salers                  │    │                                     ││
│  │ ├── landing_salers_bd               │    │                                     ││
│  │ └── landing_validations             │    │                                     ││
│  │                                     │    │                                     ││
│  │ Files/                              │    │                                     ││
│  │ └── weak_signals_validation.xlsx ◄──┼────┤ (sync via notebook)                 ││
│  │     (un seul fichier)               │    │                                     ││
│  └─────────────────────────────────────┘    └─────────────────────────────────────┘│
│              │                                              │                       │
│              └──────────────────┬───────────────────────────┘                       │
│                                 ▼                                                   │
│                    ┌─────────────────────────────────────┐                          │
│                    │           WAREHOUSE                 │                          │
│                    │    (weak_signals_warehouse)         │                          │
│                    │       Vues cross-database           │                          │
│                    ├─────────────────────────────────────┤                          │
│                    │ vw_opportunities_with_validation    │◄── Power BI              │
│                    │ vw_opportunities_complete           │                          │
│                    │ vw_salers_stats / vw_bd_stats       │                          │
│                    └─────────────────────────────────────┘                          │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Comment fonctionne la validation Excel

On a simplifié au max le cycle de validation. Avant on avait Data Activator, Graph API, deux fichiers Excel... c'était trop fragile. Maintenant c'est beaucoup plus simple :

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                          BOUCLE EXCEL ↔ FABRIC                                      │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                     │
│  1. EXPORT (chaque lundi)                                                           │
│     ┌─────────────────────────────────────────────────────────────────────────┐    │
│     │ sync_validations_excel.ipynb                                            │    │
│     │                                                                         │    │
│     │ - Récupère l'Excel existant (s'il existe)                               │    │
│     │ - Ajoute les nouvelles opportunités de la semaine                       │    │
│     │ - Garde les validations déjà faites (pas d'écrasement)                  │    │
│     │ - Sauvegarde le fichier                                                 │    │
│     └─────────────────────────────────────────────────────────────────────────┘    │
│                                 │                                                   │
│                                 ▼                                                   │
│  2. VALIDATION (les commerciaux dans Excel)                                         │
│     ┌─────────────────────────────────────────────────────────────────────────┐    │
│     │ weak_signals_validation.xlsx (dans Lakehouse Files/)                    │    │
│     │                                                                         │    │
│     │ Comment y accéder :                                                     │    │
│     │ - OneLake File Explorer (app Windows) → ça monte comme un dossier       │    │
│     │ - Télécharger/uploader depuis l'interface Fabric                        │    │
│     │ - Plus tard on mettra un shortcut SharePoint                            │    │
│     │                                                                         │    │
│     │ Ce que fait le commercial :                                             │    │
│     │ - is_validated : 1 si OK, 0 si KO                                       │    │
│     │ - validation_comment : s'il veut ajouter un commentaire                 │    │
│     │ - validated_by : son email                                              │    │
│     └─────────────────────────────────────────────────────────────────────────┘    │
│                                 │                                                   │
│                                 ▼                                                   │
│  3. IMPORT + NOTIF (tous les jours à 8h)                                            │
│     ┌─────────────────────────────────────────────────────────────────────────┐    │
│     │ sync_validations_excel.ipynb (mode import)                              │    │
│     │                                                                         │    │
│     │ - Lit l'Excel                                                           │    │
│     │ - Repère les nouvelles validations (is_validated rempli                 │    │
│     │   mais email_sent_at encore vide)                                       │    │
│     │ - Sync vers Lakehouse + SQL Database                                    │    │
│     │ - Envoie les emails aux commerciaux                                     │    │
│     │ - Met à jour email_sent_at                                              │    │
│     └─────────────────────────────────────────────────────────────────────────┘    │
│                                 │                                                   │
│                                 ▼                                                   │
│  4. REPORTING                                                                       │
│     Power BI tape sur vw_opportunities_with_validation → Dashboard OK/KO/PENDING    │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### Pourquoi c'est mieux maintenant

| | Avant | Maintenant |
|---|---|---|
| Fichiers Excel | 2 fichiers (les gens se trompaient) | 1 seul fichier |
| Composants | 5 trucs (Data Activator, Graph API...) | 2 trucs (Notebook + SMTP) |
| Notifications | Temps réel mais ça plantait souvent | 1x par jour mais ça marche |
| Debug | Galère | Facile, on a les logs du notebook |
| Fiabilité | ~70% | ~90% |

### Le fichier Excel

Un seul fichier : `weak_signals_validation.xlsx` dans `/lakehouse/default/Files/`

Le merge est "intelligent" :
- Les nouvelles opportunités arrivent avec les colonnes de validation vides
- Si une ligne est déjà validée, on n'y touche pas
- Les commerciaux éditent directement ce fichier

### Colonnes de l'Excel

| Colonne | Type | Qui remplit | C'est quoi |
|---|---|---|---|
| `id` | String | Auto | L'ID de l'opportunité |
| `article_title` | String | Auto | Titre de l'article |
| `article_url` | String | Auto | Lien vers l'article |
| `venue_name` | String | Auto | Nom du lieu/venue |
| `city` | String | Auto | Ville |
| `country` | String | Auto | Pays |
| `vertical` | String | Auto | Le segment de marché |
| `evaluation_score` | Int | Auto | Score de l'IA (0-100) |
| `audit_opportunity_reason` | String | Auto | Pourquoi l'IA pense que c'est intéressant |
| `saler_name` | String | Auto | Nom du commercial assigné |
| `saler_email` | String | Auto | Email du commercial |
| `is_validated` | Int | **Commercial** | 1=OK, 0=KO, vide=pas encore traité |
| `validation_comment` | String | **Commercial** | Commentaire (optionnel) |
| `validated_by` | String | **Commercial** | Email de celui qui valide |
| `email_sent_at` | Timestamp | Auto | Quand l'email a été envoyé |

### Le notebook de sync

`sync_validations_excel.ipynb` fait tout. Il a 3 modes :
- `export` : Ajoute les nouvelles opportunités dans l'Excel
- `import` : Lit l'Excel, sync vers le Lakehouse/SQL, envoie les mails
- `full_sync` : Les deux d'un coup

### Envoi des emails

On envoie les emails directement depuis le notebook. Pas besoin de Data Activator.

```python
# Quand on détecte une nouvelle validation
for validation in new_validations:
    send_email(
        to=validation.saler_email,
        subject=f"Opportunité validée: {validation.venue_name}",
        body=format_email(validation)
    )
    update_email_sent_at(validation.id)
```

**Config SMTP** (dans local.settings.json ou les variables Fabric) :

| Variable | Description |
|---|---|
| `SMTP_HOST` | Le serveur (ex: smtp.office365.com) |
| `SMTP_PORT` | Le port (587 pour TLS) |
| `SMTP_USER` | L'email qui envoie |
| `SMTP_PASSWORD` | Le mot de passe |

**Ou avec SendGrid** :

| Variable | Description |
|---|---|
| `SENDGRID_API_KEY` | La clé API |
| `SENDGRID_FROM` | L'email vérifié |

---

## Le Lakehouse

`weak_signals_lakehouse` stocke toutes les données brutes en tables Delta.

### Les tables

| Table | D'où ça vient | C'est quoi | Notebook |
|---|---|---|---|
| `landing_feedly_opportunities` | Azure AI Search | Les opportunités enrichies par l'IA (36 colonnes) | `landing_feedly_opportunities.ipynb` |
| `landing_salers` | Excel (feuille RAE_RSM) | Les commerciaux par **pays** | `landing_salers.ipynb` |
| `landing_salers_bd` | Excel (feuille BD) | Les BD par **verticale** | `landing_salers.ipynb` |
| `landing_validations` | Excel validation | Les validations OK/KO | `import_validations_from_excel.ipynb` |

### Structure de landing_feedly_opportunities

| Catégorie | Colonnes |
|---|---|
| **ID** | `id` |
| **Article** | `article_url`, `article_title`, `article_content`, `publication_date`, `language`, `origin`, `entities`, `topics` |
| **Vertical** | `vertical` |
| **Venue** | `venue_name`, `city`, `country`, `zone`, `venue_type`, `capacity` |
| **Projet** | `project_type`, `project_phase`, `opening_year`, `opening_date` |
| **Financier** | `investment`, `investment_currency` |
| **Stakeholders** | `investor_owner_management`, `architect_consultant_contractor` |
| **Concurrents** | `competitor_name_main`, `competitor_name_other`, `key_products_installed`, `system_integrator`, `other_key_players` |
| **Analyse** | `evaluation_score`, `audit_opportunity`, `audit_opportunity_reason` |
| **Metadata** | `crawled_at`, `loaded_at`, `updated_at`, `ingestion_week` |
| **Déduplication** | `is_duplicate`, `duplicate_of`, `duplicate_score`, `is_suspected_duplicate` |

> `ingestion_week` (format `2024-W49`) est ajouté automatiquement par le notebook d'ingestion. C'est pratique pour filtrer par semaine.

### Structure de landing_salers (par pays)

| Colonne | Type | C'est quoi |
|---|---|---|
| `sales_zone` | String | Zone (EMEA, APAC, AMERICAS) |
| `sales_region` | String | Région |
| `country_code` | String | Code pays (FR, US...) |
| `country` | String | Nom du pays |
| `who` | String | Nom du commercial |
| `role` | String | RAE ou RSM |
| `email` | String | Son email |
| `loaded_at` | Timestamp | Quand on l'a chargé |
| `updated_at` | Timestamp | Dernière màj |

### Structure de landing_salers_bd (par verticale)

| Colonne | Type | C'est quoi |
|---|---|---|
| `market_segment` | String | La verticale (Live Events, Install...) |
| `who` | String | Nom du BD |
| `role` | String | BD |
| `email` | String | Email |
| `loaded_at` | Timestamp | Date de chargement |
| `updated_at` | Timestamp | Dernière màj |

---

## La SQL Database

`weak_signals_sqldb` stocke les validations. C'est là que Power Apps tape directement (pas via Azure Functions).

### Table validations

| Colonne | Type | C'est quoi |
|---|---|---|
| `opportunity_id` | NVARCHAR(255) | **Clé primaire** |
| `is_validated` | INT | 1=OK, 0=KO, NULL=pas encore fait |
| `validated_by` | NVARCHAR(255) | Qui a validé |
| `validation_date` | DATETIME2 | Quand |
| `validation_comment` | NVARCHAR(MAX) | Commentaire |
| `article_url` | NVARCHAR(2048) | URL de l'article |
| `article_title` | NVARCHAR(500) | Titre |
| `vertical` | NVARCHAR(100) | Segment |
| `venue_name` | NVARCHAR(255) | Nom du lieu |
| `city` | NVARCHAR(100) | Ville |
| `country` | NVARCHAR(100) | Pays |
| `evaluation_score` | INT | Score IA |
| `audit_opportunity_reason` | NVARCHAR(MAX) | Justification IA |
| `saler_email` | NVARCHAR(255) | Email du commercial à notifier |
| `saler_name` | NVARCHAR(255) | Nom du commercial |
| `email_sent_at` | DATETIME2 | Quand l'email est parti (NULL si pas encore) |
| `created_at` | DATETIME2 | Création |
| `updated_at` | DATETIME2 | Màj |

### Qui y accède

- **Power Apps** : Lecture/écriture directe via connecteur SQL
- **Warehouse** : Lecture via cross-database query

---

## Le Warehouse

`weak_signals_warehouse` expose les vues SQL pour Power BI. Il ne stocke rien lui-même, il fait juste des jointures cross-database entre le Lakehouse et la SQL Database.

### Les vues

| Vue | Elle fait quoi | Pour quoi |
|---|---|---|
| `vw_opportunities_with_salers` | Opportunités + RAE/RSM (par pays) | Jointure simple |
| `vw_opportunities_with_bd` | Opportunités + BD (par verticale) | Jointure simple |
| `vw_opportunities_complete` | Opportunités + RAE/RSM + BD | Vue complète (sans validation) |
| `vw_opportunities_by_country` | Agrégé par pays avec les salers concaténés | Dashboard |
| `vw_salers_stats` | Stats par commercial | KPIs |
| `vw_bd_stats` | Stats par BD | KPIs |
| `vw_countries_without_salers` | Les pays sans commercial | Pour voir la couverture |
| `vw_verticals_without_bd` | Les verticales sans BD | Pareil |
| `vw_opportunities_with_validation` | **La vue principale** - Tout + OK/KO | **Power BI** |

### Comment ça jointe

```sql
-- RAE/RSM : on jointe par PAYS
JOIN weak_signals_lakehouse.dbo.landing_salers ON opportunities.country = salers.country

-- BD : on jointe par VERTICALE
JOIN weak_signals_lakehouse.dbo.landing_salers_bd ON opportunities.vertical = salers_bd.market_segment

-- Validations : on jointe par ID
JOIN weak_signals_sqldb.dbo.validations ON opportunities.id = validations.opportunity_id
```

Du coup :
- Un **RAE/RSM** voit toutes les opportunités de son pays, quelle que soit la verticale
- Un **BD** voit toutes les opportunités de sa verticale, quel que soit le pays
- Zohar Pajela et Andre Pichette sont dans les deux tables (profils hybrides)

---

## Les notebooks

| Notebook | Il fait quoi | Quand |
|---|---|---|
| `landing_feedly_opportunities.ipynb` | Charge les opportunités depuis AI Search + ajoute `ingestion_week` | Chaque lundi (CRON) |
| `deduplicate_weekly.ipynb` | Détecte les doublons avec `ai.similarity` | Chaque lundi après l'ingestion |
| `landing_salers.ipynb` | Charge les commerciaux depuis l'Excel | Quand l'Excel change |
| `sync_validations_excel.ipynb` | Sync Excel ↔ Fabric + envoie les emails | Tous les jours |

### Modes de chargement

| Mode | Ce qu'il fait | Quand l'utiliser |
|---|---|---|
| `one_time` | OVERWRITE | Premier déploiement |
| `incremental` | MERGE (upsert) | CRON hebdo |
| `full_refresh` | OVERWRITE + VACUUM | De temps en temps pour nettoyer |

### Comment on ajoute ingestion_week

```python
from datetime import datetime
from pyspark.sql.functions import lit

# Semaine au format ISO (ex: "2024-W49")
current_week = datetime.now().strftime("%Y-W%W")

# On ajoute la colonne
df = df.withColumn("ingestion_week", lit(current_week))

# Et on enregistre
df.write.format("delta").mode("append").saveAsTable("landing_feedly_opportunities")
```

Ça permet de :
- Filtrer les articles de la semaine pour la dédup
- Exporter que les opportunités de la semaine dans l'Excel
- Suivre l'historique par semaine

---

## La déduplication

### Le principe

Chaque semaine, on compare les nouveaux articles à tout l'historique avec `ai.similarity()`. Comme ça on détecte quand le même événement est couvert par plusieurs sources.

### Prérequis

- Fabric Runtime 1.3 ou plus (pour les AI Functions)
- Un modèle d'embedding configuré dans Fabric

### Les colonnes de dédup

| Colonne | Type | C'est quoi |
|---|---|---|
| `is_duplicate` | Boolean | `true` si doublon confirmé (score >= 0.90) |
| `is_suspected_duplicate` | Boolean | `true` si c'est pas sûr (entre 0.85 et 0.90) |
| `duplicate_of` | String | ID de l'article original (le plus vieux) |
| `duplicate_score` | Float | Score de similarité (0 à 1) |

### Les seuils

| Score | C'est quoi | Ce qu'on fait |
|---|---|---|
| >= 0.90 | Doublon sûr | `is_duplicate = true`, pas dans l'Excel |
| 0.85 - 0.90 | Pas sûr | `is_suspected_duplicate = true`, dans l'Excel mais à vérifier |
| < 0.85 | Unique | Pas de flag, dans l'Excel |

### Le texte qu'on compare

```python
text_for_embedding = f"{article_title} {venue_name} {city} {country}"
```

On a choisi ça parce que :
- C'est très discriminant (même projet = même lieu + même ville)
- C'est moins bruité que le contenu complet
- Ça coûte moins cher à calculer

Si jamais on rate trop de doublons, on pourra ajouter un bout du contenu :
```python
text_for_embedding = f"{article_title} {venue_name} {city} {country} {content[:500]}"
```

### Le notebook de dédup

```python
from datetime import datetime

current_week = datetime.now().strftime("%Y-W%W")

# 1. Les articles de cette semaine
new_articles = spark.sql(f"""
    SELECT * FROM landing_feedly_opportunities
    WHERE ingestion_week = '{current_week}'
      AND is_duplicate IS NULL
""")

# 2. L'historique
historical = spark.sql(f"""
    SELECT id, article_title, venue_name, city, country
    FROM landing_feedly_opportunities
    WHERE ingestion_week != '{current_week}'
""")

# 3. On prépare le texte à comparer
from pyspark.sql.functions import col, lit, concat_ws

new_articles = new_articles.withColumn(
    "text_to_compare",
    concat_ws(" ", col("article_title"), col("venue_name"), col("city"), col("country"))
)

historical = historical.withColumn(
    "text_to_compare",
    concat_ws(" ", col("article_title"), col("venue_name"), col("city"), col("country"))
)

# 4. On calcule la similarité
results = spark.sql("""
    SELECT
        n.id as new_id,
        h.id as historical_id,
        ai.similarity(n.text_to_compare, h.text_to_compare) as similarity_score
    FROM new_articles n
    CROSS JOIN historical h
""")

# 5. On garde le meilleur match pour chaque article
best_matches = results.groupBy("new_id").agg(
    max("similarity_score").alias("max_score"),
    first("historical_id").alias("duplicate_of")
)

# 6. On applique les seuils
for row in best_matches.collect():
    if row.max_score >= 0.90:
        # Doublon confirmé
        update_article(row.new_id,
            is_duplicate=True,
            duplicate_of=row.duplicate_of,
            duplicate_score=row.max_score)
    elif row.max_score >= 0.85:
        # Zone grise
        update_article(row.new_id,
            is_suspected_duplicate=True,
            duplicate_of=row.duplicate_of,
            duplicate_score=row.max_score)
    else:
        # Unique
        update_article(row.new_id,
            is_duplicate=False,
            is_suspected_duplicate=False)
```

### L'export Excel après dédup

```python
from datetime import datetime

current_week = datetime.now().strftime("%Y-W%W")

# On exporte les uniques + la zone grise
opportunities_to_export = spark.sql(f"""
    SELECT * FROM landing_feedly_opportunities
    WHERE ingestion_week = '{current_week}'
      AND (is_duplicate = false OR is_duplicate IS NULL)
""")

# Onglet principal : les opportunités uniques
write_excel_sheet(opportunities_to_export.filter("is_suspected_duplicate = false"), "Opportunités")

# Onglet pour vérification : la zone grise
write_excel_sheet(opportunities_to_export.filter("is_suspected_duplicate = true"), "A vérifier (doublons potentiels)")

# Onglet audit : les doublons qu'on a exclus
duplicates = spark.sql(f"""
    SELECT * FROM landing_feedly_opportunities
    WHERE ingestion_week = '{current_week}'
      AND is_duplicate = true
""")
write_excel_sheet(duplicates, "Doublons exclus")
```

### Le planning CRON

```
# Lundi 6h00 - On récupère les nouvelles opportunités
landing_feedly_opportunities.ipynb (mode incremental)

# Lundi 6h30 - On déduplique
deduplicate_weekly.ipynb

# Lundi 7h00 - On exporte vers Excel
sync_validations_excel.ipynb (mode export)

# Tous les jours 8h00 - On importe les validations + envoi des emails
sync_validations_excel.ipynb (mode import)
```

### Requêtes de monitoring

```sql
-- Stats de dédup de la semaine
SELECT
    ingestion_week,
    COUNT(*) as total_articles,
    SUM(CASE WHEN is_duplicate = true THEN 1 ELSE 0 END) as doublons_confirmes,
    SUM(CASE WHEN is_suspected_duplicate = true THEN 1 ELSE 0 END) as zone_grise,
    SUM(CASE WHEN is_duplicate = false AND is_suspected_duplicate = false THEN 1 ELSE 0 END) as uniques
FROM landing_feedly_opportunities
WHERE ingestion_week = '2024-W49'
GROUP BY ingestion_week

-- Les doublons détectés (pour ajuster les seuils si besoin)
SELECT
    a.article_title as article_original,
    b.article_title as article_doublon,
    b.duplicate_score,
    b.ingestion_week
FROM landing_feedly_opportunities a
JOIN landing_feedly_opportunities b ON a.id = b.duplicate_of
WHERE b.ingestion_week = '2024-W49'
ORDER BY b.duplicate_score DESC

-- L'historique par semaine
SELECT
    ingestion_week,
    COUNT(*) as total,
    SUM(CASE WHEN is_duplicate = true THEN 1 ELSE 0 END) as doublons
FROM landing_feedly_opportunities
GROUP BY ingestion_week
ORDER BY ingestion_week DESC
```

---

## Les fichiers SQL

| Fichier | C'est quoi |
|---|---|
| `warehouse/create_validations.sql` | Crée la table validations + la vue cross-database |
| `warehouse/create_views_warehouse.sql` | Crée les vues du Warehouse |

---

## Power BI

### Source de données

On se connecte au Warehouse et on utilise `vw_opportunities_with_validation`.

### Ce qu'on peut afficher

- Toutes les infos de l'opportunité (venue, projet, budget, concurrents...)
- `saler_name`, `saler_email`, `saler_role` (le RAE/RSM du pays)
- `bd_name`, `bd_email` (le BD de la verticale)
- `validation_status` (OK, KO, PENDING)
- `validated_by`, `validation_date`, `validation_comment`

### Filtres utiles

- `audit_opportunity = true` : les opportunités validées par l'IA
- `validation_status = 'PENDING'` : celles qui restent à traiter
- `zone` : par région
- `vertical` : par marché

---

## Récap du flux de validation

```
1. Notebook sync_validations_excel.ipynb (mode export - chaque lundi)
         │
         ▼
   Lakehouse → weak_signals_validation.xlsx (on ajoute les nouvelles)
         │
         ▼
2. Les commerciaux modifient l'Excel
   (via OneLake File Explorer ou download/upload Fabric)
         │
         ▼
3. Ils remplissent : is_validated (1/0), validation_comment, validated_by
         │
         ▼
4. Notebook sync_validations_excel.ipynb (mode import - tous les jours)
         │
         ▼
   Excel → landing_validations (Lakehouse) + validations (SQL Database)
         │
         ▼
5. Le notebook détecte les nouvelles validations
   (is_validated rempli mais email_sent_at vide)
         │
         ▼
6. Envoi email via SMTP/SendGrid
         │
         ▼
7. On met à jour email_sent_at partout
         │
         ▼
8. Power BI affiche le tout via vw_opportunities_with_validation
```

---

## Maintenance

### Quoi rafraîchir quand

| Donnée | Fréquence | Quoi faire |
|---|---|---|
| Opportunités | Chaque semaine | Lancer `landing_feedly_opportunities.ipynb` en mode `incremental` |
| Commerciaux | Quand l'Excel change | Mettre à jour l'Excel puis lancer `landing_salers.ipynb` |
| Sync Excel | Tous les jours | Lancer `sync_validations_excel.ipynb` en mode `full_sync` |

### Le planning

```
# Lundi 6h00 - Ingestion
landing_feedly_opportunities.ipynb (mode incremental)

# Lundi 7h00 - Export Excel
sync_validations_excel.ipynb (mode export)

# Tous les jours 8h00 - Import des validations + emails
sync_validations_excel.ipynb (mode import)
```

### Ce qu'il faut surveiller

- `vw_countries_without_salers` : les pays pas couverts
- `vw_verticals_without_bd` : les verticales pas couvertes
- `vw_salers_stats` et `vw_bd_stats` : les stats
- Les opportunités sans email envoyé : `SELECT * FROM validations WHERE is_validated=1 AND email_sent_at IS NULL`
- Les logs du notebook `sync_validations_excel.ipynb` si les mails ne partent pas
