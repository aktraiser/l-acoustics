# Feedly Collector

Ce projet collecte des articles depuis Feedly, les enrichit avec de l'IA, et detecte des opportunites commerciales pour L-Acoustics.

## Comment ca marche ?

Le pipeline fonctionne en 3 etapes :

1. **Ingestion** : On recupere les articles depuis Feedly via l'API Management
2. **Enrichissement** : Un agent IA extrait les infos business (lieu, projet, concurrents...)
3. **Analyse** : Un second agent IA score chaque article et detecte les opportunites

```
Feedly --> /api/feed --> q-raw-events --> enrich_event --> q-enriched-events --> analyze_event
                                                |                                      |
                                                v                                      v
                                          AI Search (upsert)                    AI Search (merge)
                                                                                       |
                                                                                       v
                                                                            q-opportunities (si pertinent)
```

La deduplication et l'export Excel sont geres dans Microsoft Fabric (voir [architecture_fabric.md](architecture_fabric.md)).

## Les fonctions

### Ingestion : `GET /api/feed`

Recupere les articles Feedly et les envoie dans la queue.

| Parametre | Defaut | Description |
|-----------|--------|-------------|
| `count` | 50 | Nombre d'articles |
| `hours` | 24 | Fenetre de temps |

```bash
curl "http://localhost:7071/api/feed?count=10&hours=72"
```

### Enrichissement : `enrich_event`

Se declenche automatiquement quand un message arrive dans `q-raw-events`.

L'agent `lac-weak-signals` extrait :
- Le segment de marche (Live Events, Hospitality, Corporate...)
- Les infos du lieu (nom, ville, pays, capacite...)
- Les details du projet (type, phase, budget, date d'ouverture...)
- Les concurrents mentionnes et leurs produits
- Les parties prenantes (investisseurs, architectes, integrateurs)

Le document enrichi est stocke dans Azure AI Search.

### Analyse : `analyze_event`

Se declenche quand un message arrive dans `q-enriched-events`.

L'agent `lac-analyst-leads` evalue :
- Un score de 0 a 100
- Si c'est une opportunite ou non
- La justification

Exemple de reponse :
```json
{
  "evaluationScore": 85,
  "auditOpportunity": true,
  "auditOpportunityReason": "Nouveau stade de 50,000 places en phase de conception, budget 500M EUR, pas encore de fournisseur audio selectionne."
}
```

### Health check : `GET /api/health`

Verifie que tout est bien configure.

## Installation

### Ce qu'il faut

- Python 3.9+
- Azure Functions Core Tools v4
- Azure CLI

### Demarrer en local

```bash
# Environnement virtuel
python -m venv .venv
source .venv/bin/activate  # ou .venv\Scripts\activate sur Windows

# Dependances
pip install -r requirements.txt

# Connexion Azure
az login

# Lancement
func start
```

### Configuration

Copier `local.settings.json.example` vers `local.settings.json` et renseigner :

```json
{
  "Values": {
    "FEEDLY_APIM_URL": "https://lac-apim-agentic.azure-api.net/pipeline",
    "FEEDLY_STREAM_ID": "enterprise/lacoustics/category/...",
    "APIM_SUBSCRIPTION_KEY": "votre-cle",

    "AI_SEARCH_ENDPOINT": "https://lac-feedly-search.search.windows.net",
    "AI_SEARCH_INDEX": "raw_events",
    "AI_SEARCH_KEY": "votre-cle",

    "AI_PROJECT_ENDPOINT": "https://lac-agentic-leads.services.ai.azure.com/api/projects/proj-default",
    "AI_PROJECT_KEY": "votre-cle",
    "AI_AGENT_NAME": "lac-weak-signals",
    "AI_ANALYST_AGENT_NAME": "lac-analyst-leads",

    "SERVICEBUS_CONNECTION": "Endpoint=sb://sb-lac-opportunities.servicebus.windows.net/;..."
  }
}
```

## Scripts utiles

```bash
# Recreer l'index AI Search
python recreate_index.py

# Traiter tous les articles non enrichis
python process_all_articles.py
```

## Deploiement

```bash
# Creer la Function App
az functionapp create \
  --resource-group rg-lac-agentic \
  --name func-lac-feedly-collector \
  --storage-account stlacopportunities \
  --consumption-plan-location westeurope \
  --runtime python \
  --runtime-version 3.9 \
  --functions-version 4

# Deployer
func azure functionapp publish func-lac-feedly-collector

# Configurer les variables
az functionapp config appsettings set \
  --name func-lac-feedly-collector \
  --resource-group rg-lac-agentic \
  --settings @appsettings.json
```

## Appeler les agents manuellement

```python
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

project_client = AIProjectClient(
    endpoint="https://lac-agentic-leads.services.ai.azure.com/api/projects/proj-default",
    credential=DefaultAzureCredential(),
)

agent = project_client.agents.get(agent_name="lac-weak-signals")
openai_client = project_client.get_openai_client()

response = openai_client.responses.create(
    input=[{"role": "user", "content": "Ton contenu ici"}],
    extra_body={"agent": {"name": agent["name"], "type": "agent_reference"}},
)

print(response.output_text)
```

## Ressources Azure

| Ressource | Nom | Role |
|-----------|-----|------|
| API Management | lac-apim-agentic | Proxy Feedly |
| Service Bus | sb-lac-opportunities | Queues de messages |
| AI Search | lac-feedly-search | Stockage des articles |
| AI Foundry | lac-agentic-leads | Agents IA |

## Commandes rapides

```bash
# Activer l'environnement
source .venv/bin/activate

# Se connecter a Azure
az login

# Lancer en local
func start

# Tester avec 5 articles
curl "http://localhost:7071/api/feed?count=5"

# Redeployer sur Azure
func azure functionapp publish func-lac-opportunities
```

## Logs

Les logs sont dans la console Azure Functions ou Application Insights :

```
feed_ingest: 10 articles envoyes dans q-raw-events
enrich_event: Article abc123 enrichi et indexe
analyze_event: Article abc123 - score=65, opportunity=False
```

---

Projet interne L-Acoustics
