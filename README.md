# 🏊 Pool Manager

Application web de suivi et de gestion d'une piscine au brome. Saisie des mesures, calcul des doses de traitement, historique avec graphiques, alertes Telegram et intégration Home Assistant.

---

## Fonctionnalités

| Domaine | Détail |
|---|---|
| **Mesures** | Saisie de pH, brome, dureté (TH), alcalinité (TAC) et température |
| **Recommandations** | Diagnostic automatique hors-norme avec conseil de traitement |
| **Calculateur** | Calcul de dose par produit selon le volume de la piscine |
| **Traitements** | Historique des produits ajoutés, délai avant retest configurable par produit |
| **Historique** | Graphiques d'évolution + stats mensuelles et comparaison annuelle (YoY) |
| **Export** | Sauvegarde CSV / JSON importable |
| **Notifications** | Alertes Telegram (rappels périodiques + seuils critiques) |
| **Home Assistant** | Push vers un capteur HA via webhook configurable |
| **API** | Endpoint JSON pour page d'accueil domotique (token Bearer) |
| **PWA** | Service worker, manifest — installable sur mobile |
| **Audit** | Journal d'audit des actions sensibles (ajout/suppression/import) |

---

## Stack technique

- **Python 3.11+** — Flask 2.3, APScheduler 3.10, Requests
- **SQLite** — base de données locale en mode WAL
- **Bootstrap 5** — interface responsive
- **Chart.js** — graphiques d'évolution
- **systemd** — déploiement en service sur Debian/Ubuntu

---

## Installation (Debian / Ubuntu)

```bash
# Cloner ou copier les sources dans un répertoire
git clone <url-du-repo> pool
cd pool

# Installation complète (crée l'utilisateur système, le service systemd, le venv)
sudo bash install.sh
```

L'application démarre automatiquement sur le port **5124** et s'active au démarrage du système.

### Mise à jour

```bash
sudo bash install.sh --update
```

---

## Démarrage rapide (développement)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows : venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Accès : [http://localhost:5124](http://localhost:5124)

---

## Configuration

Toute la configuration se fait depuis l'interface **⚙️ Paramètres**.  
Les paramètres sont stockés dans la base SQLite ; le port et la clé secrète Flask dans le fichier `.env`.

### Sections disponibles

| Section | Contenu |
|---|---|
| **Votre piscine** | Volume (litres), capteur HA, référence home page |
| **⚙️ Application** | Port d'écoute (modifiable sans toucher au code) |
| **Telegram** | Token bot + Chat ID, rappels périodiques, délai de mesure |
| **Rappels** | Fréquence et heure des rappels automatiques |
| **Home Assistant** | URL webhook, token, rate-limit configurable |

### Variables d'environnement (`.env`)

| Variable | Description | Défaut |
|---|---|---|
| `SECRET_KEY` | Clé secrète Flask (générée automatiquement) | — |
| `PORT` | Port d'écoute | `5124` |

Le fichier `.env` est créé automatiquement par `install.sh`. En développement, il est chargé par `_load_dotenv()` au démarrage si `SECRET_KEY` n'est pas déjà dans l'environnement.

> **Sécurité** : `.env` et `.secret_key` sont dans `.gitignore`. Ne les commitez jamais.

---

## Structure du projet

```
pool/
├── app.py                  # Application Flask principale
├── recommendations.py      # Valeurs idéales et calcul des doses
├── requirements.txt
├── install.sh              # Script d'installation systemd
├── .env                    # Clé secrète + port (hors dépôt)
├── pool.db                 # Base SQLite (hors dépôt)
├── templates/
│   ├── base.html
│   ├── index.html          # Tableau de bord / saisie de mesure
│   ├── history.html        # Historique + graphiques + stats
│   ├── calculator.html     # Calculateur de doses
│   └── settings.html       # Configuration
└── static/
    ├── style.css
    ├── sw.js               # Service worker PWA
    ├── manifest.json
    └── uploads/            # Photos des mesures (hors dépôt)
```

---

## API

### `GET /api/homepage`

Retourne le dernier relevé pour une intégration domotique (widget, dashboard…).

**Authentification** : token Bearer dans le header `Authorization` ou paramètre `?token=`.

```bash
curl -H "Authorization: Bearer <token>" http://localhost:5124/api/homepage
```

```json
{
  "ph": 7.4,
  "bromine": 3.8,
  "hardness": 280,
  "alkalinity": 95,
  "temperature": 27.5,
  "measured_at": "2026-05-03T14:30:00",
  "status": { "ph": "ok", "bromine": "ok", ... }
}
```

Le token se configure dans **Paramètres → Votre piscine → Token API homepage**.

---

## Commandes utiles (après installation systemd)

```bash
systemctl status pool-manager       # état du service
systemctl restart pool-manager      # redémarrer
journalctl -u pool-manager -f       # logs en direct
systemctl stop pool-manager         # arrêter
```

---

## Licence

Usage personnel — aucune licence open-source définie.
