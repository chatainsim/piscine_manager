import csv
import io
import logging
import os
import json
import sys
import atexit
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, url_for)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from recommendations import IDEAL_RANGES, get_ideal_ranges, get_recommendations, get_status

# ─── App setup ───────────────────────────────────────────────────────────────

app = Flask(__name__)

_SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
_ENV_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
_DEFAULT_PORT    = 5124


def _load_dotenv() -> None:
    """Charge .env dans os.environ pour les clés non encore définies.
    Utile en run direct (python app.py) ; systemd peuple déjà l'env via EnvironmentFile."""
    if not os.path.isfile(_ENV_FILE):
        return
    try:
        with open(_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                if k and k not in os.environ:
                    os.environ[k] = v.strip()
    except OSError:
        pass


_load_dotenv()


def _get_env_port() -> int:
    """Lit PORT dans .env (valeur active après prochain redémarrage)."""
    if os.path.isfile(_ENV_FILE):
        try:
            with open(_ENV_FILE) as f:
                for line in f:
                    if line.strip().startswith('PORT='):
                        return int(line.partition('=')[2].strip())
        except (OSError, ValueError):
            pass
    return _DEFAULT_PORT


def _save_env_port(port: int) -> None:
    """Met à jour ou insère PORT=<port> dans .env."""
    lines: list[str] = []
    found = False
    if os.path.isfile(_ENV_FILE):
        with open(_ENV_FILE) as f:
            for line in f:
                if line.strip().startswith('PORT='):
                    lines.append(f'PORT={port}\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'PORT={port}\n')
    with open(_ENV_FILE, 'w') as f:
        f.writelines(lines)
    try:
        os.chmod(_ENV_FILE, 0o640)
    except OSError:
        pass


def _warn_if_secret_key_not_ignored() -> None:
    """Vérifie que .secret_key est bien dans .gitignore avant de l'écrire sur disque.
    N'est appelée qu'au premier démarrage (création du fichier). Avertit sur stderr
    sans bloquer le démarrage — les déploiements sans git ne sont pas pénalisés."""
    gitignore = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.gitignore')
    if not os.path.isfile(gitignore):
        print(
            "[pool] AVERTISSEMENT : aucun .gitignore trouvé. Ajoutez '.secret_key' à votre "
            ".gitignore avant de committer pour ne pas exposer la clé secrète.",
            file=sys.stderr,
        )
        return
    try:
        with open(gitignore) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except OSError:
        return
    covered = any(
        pat in ('.secret_key', '*.key', '*secret*', '.secret*')
        for pat in lines
    )
    if not covered:
        print(
            "[pool] AVERTISSEMENT : '.secret_key' n'est pas dans .gitignore. "
            "Ajoutez-le pour éviter de committer la clé secrète Flask.",
            file=sys.stderr,
        )


def _load_secret_key() -> str:
    """Retourne la clé secrète dans cet ordre de priorité :
    1. Variable d'env SECRET_KEY
    2. Fichier .secret_key (créé automatiquement au premier démarrage)
    """
    env_key = os.environ.get('SECRET_KEY', '')
    if env_key:
        return env_key
    if os.path.isfile(_SECRET_KEY_FILE):
        key = open(_SECRET_KEY_FILE).read().strip()
        if key:
            return key
    # Premier démarrage sans clé configurée — génération automatique
    key = secrets.token_hex(32)
    _warn_if_secret_key_not_ignored()
    try:
        with open(_SECRET_KEY_FILE, 'w') as f:
            f.write(key)
        try:
            os.chmod(_SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
        print(
            f"[pool] Clé secrète générée automatiquement → {_SECRET_KEY_FILE}",
            file=sys.stderr,
        )
    except OSError as e:
        print(f"[pool] Impossible d'écrire .secret_key ({e}) — clé en mémoire uniquement.", file=sys.stderr)
    return key

app.secret_key = _load_secret_key()

_active_port: int = int(os.environ.get('PORT', _DEFAULT_PORT))

DATABASE = os.path.join(os.path.dirname(__file__), 'pool.db')
PHOTOS_DIR  = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
scheduler = BackgroundScheduler(daemon=True)

app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB max par requête (photos)

# Délai d'attente par défaut (heures) avant de retester l'eau après un traitement.
# Utilisé uniquement quand le produit n'a pas de délai propre (champ wait_hours de la
# table products). Pour ajuster par produit : Calculateur → fiche produit → "Délai avant retest".
WAIT_HOURS = 6
SCHEMA_VERSION = 13  # incrémenter à chaque nouvelle migration

logger = logging.getLogger(__name__)

_bg_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='pool-bg')


def _bg_submit(fn, *args):
    """Soumet fn(*args) au pool de fond et logue toute exception non capturée."""
    def _on_done(fut):
        exc = fut.exception()
        if exc:
            logger.error('Tâche de fond %s a levé une exception : %s', fn.__name__, exc, exc_info=exc)
    _bg_pool.submit(fn, *args).add_done_callback(_on_done)


def _flash_error(user_msg: str, log_msg: str | None = None) -> None:
    """Journalise l'exception courante et affiche un flash 'danger' à l'utilisateur."""
    logger.exception(log_msg or user_msg)
    flash(user_msg, 'danger')


def _check_bearer_token(setting_key: str, fallback_token: str = ''):
    """Valide le token Bearer (Authorization header ou fallback).
    Retourne None si OK, sinon une réponse JSON 401 prête à retourner.
    """
    s        = get_settings()
    expected = s.get(setting_key, '')
    auth     = request.headers.get('Authorization', '')
    token    = auth[7:].strip() if auth.startswith('Bearer ') else fallback_token
    if not expected or not secrets.compare_digest(token, expected):
        return jsonify({'error': 'Token invalide ou manquant'}), 401
    return None


# Liste des préfixes que le service worker ne doit pas mettre en cache.
# Source unique — servie via /api/sw-skip-list pour éviter la désynchronisation.
SW_SKIP_PREFIXES = [
    '/add', '/edit', '/delete', '/api/', '/backup', '/treatments',
    '/products', '/regenerate', '/test-telegram', '/test-ha-push',
    '/test-homepage', '/settings', '/sw.js',
]

# Cache settings — invalidé par save_setting() et backup_import()
_settings_cache: dict | None = None
_settings_lock  = threading.Lock()

# Fix #3 — bornes physiques des paramètres
BOUNDS = {
    'ph':          (3,    12),
    'bromine':     (0,    50),
    'hardness':    (0,  2000),
    'alkalinity':  (0,  1000),
    'temperature': (-10,  60),
}

# Feature 6 — seuils critiques pour alertes Telegram immédiates
CRITICAL_THRESHOLDS_BROME = {
    'ph':         {'low': 6.5,  'high': 8.5,  'label': 'pH',              'unit': ''},
    'bromine':    {'low': 1.0,  'high': 8.0,  'label': 'Brome',           'unit': ' ppm'},
    'hardness':   {'low': 100,  'high': 800,  'label': 'Dureté (TH)',     'unit': ' ppm'},
    'alkalinity': {'low': 50,   'high': 200,  'label': 'Alcalinité (TAC)','unit': ' ppm'},
}
CRITICAL_THRESHOLDS_CHLORE = {
    'ph':         {'low': 6.5,  'high': 8.5,  'label': 'pH',              'unit': ''},
    'bromine':    {'low': 0.5,  'high': 5.0,  'label': 'Chlore',          'unit': ' ppm'},
    'hardness':   {'low': 100,  'high': 800,  'label': 'Dureté (TH)',     'unit': ' ppm'},
    'alkalinity': {'low': 50,   'high': 200,  'label': 'Alcalinité (TAC)','unit': ' ppm'},
}
CRITICAL_THRESHOLDS = CRITICAL_THRESHOLDS_BROME  # backward compat


def _parse_param(name, value_str):
    """Parse et valide une valeur de paramètre. Retourne float ou None."""
    if not value_str:
        return None
    val = float(value_str)
    lo, hi = BOUNDS[name]
    if not (lo <= val <= hi):
        raise ValueError(f"{name} hors limites ({lo}–{hi}) : {val}")
    return val


def _send_critical_alert(measurement):
    """Envoie une alerte Telegram si un paramètre dépasse un seuil critique."""
    try:
        s = get_settings()
        token   = s.get('telegram_token', '')
        chat_id = s.get('telegram_chat_id', '')
        if not token or not chat_id:
            return

        pool_type  = s.get('pool_type', 'brome')
        thresholds_map = CRITICAL_THRESHOLDS_CHLORE if pool_type == 'chlore' else CRITICAL_THRESHOLDS_BROME
        alerts = []
        for key, thresholds in thresholds_map.items():
            val = measurement.get(key)
            if val is None:
                continue
            label = thresholds['label']
            unit  = thresholds['unit']
            if val < thresholds['low']:
                alerts.append(f"🔴 <b>{label}</b> : {val}{unit} — trop bas (seuil critique : {thresholds['low']}{unit})")
            elif val > thresholds['high']:
                alerts.append(f"🔴 <b>{label}</b> : {val}{unit} — trop haut (seuil critique : {thresholds['high']}{unit})")

        if not alerts:
            return

        date_str = measurement.get('measured_at', '')[:16].replace('T', ' ')
        msg = (
            f"🚨 <b>ALERTE — Piscine Manager</b>\n\n"
            f"Mesure du <b>{date_str}</b> : valeur(s) critique(s) détectée(s) !\n\n"
            + '\n'.join(alerts) +
            "\n\n⚠️ Corrigez l'eau dès que possible."
        )
        send_telegram(token, chat_id, msg)
    except Exception:
        logger.exception('_send_critical_alert error')


def _send_to_ha(measurement):
    """Envoie pH, désinfectant, TH et TAC vers l'API States de Home Assistant (si le push est activé)."""
    try:
        s = get_settings()
        if s.get('ha_push_enabled', 'true') == 'false':
            return
        ha_url   = s.get('ha_push_url',   '').rstrip('/')
        ha_token = s.get('ha_push_token', '')
        if not ha_url or not ha_token:
            return

        headers = {
            'Authorization': f'Bearer {ha_token}',
            'Content-Type':  'application/json',
        }

        pool_type    = s.get('pool_type', 'brome')
        san_label_ha = 'Chlore Piscine' if pool_type == 'chlore' else 'Brome Piscine'
        entities = [
            ('ph',        'sensor.pool_manager_ph',        '',    'pH Piscine',     'ph'),
            ('bromine',   'sensor.pool_manager_bromine',   'ppm', san_label_ha,     None),
            ('hardness',  'sensor.pool_manager_hardness',  'ppm', 'Dureté TH Piscine',       None),
            ('alkalinity','sensor.pool_manager_alkalinity','ppm', 'Alcalinité TAC Piscine',  None),
        ]

        for key, entity_id, unit, friendly_name, device_class in entities:
            val = measurement.get(key)
            if val is None:
                continue
            attrs = {'unit_of_measurement': unit, 'friendly_name': friendly_name}
            if device_class:
                attrs['device_class'] = device_class
            requests.post(
                f'{ha_url}/api/states/{entity_id}',
                json={'state': str(val), 'attributes': attrs},
                headers=headers,
                timeout=5,
            )
    except Exception:
        logger.exception('_send_to_ha error')


# ─── Database ────────────────────────────────────────────────────────────────

# Fix #1 — context manager pour connexions SQLite (toujours fermées, même en cas d'exception)
@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _col_exists(conn, table, col):
    """Vérifie si une colonne existe dans une table SQLite."""
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def init_db():
    """Initialise ou migre la base de données. Idempotente — safe à appeler à chaque démarrage."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        # WAL permet lectures et écritures simultanées sans database is locked
        # (Flask + APScheduler tournent dans des threads séparés)
        conn.execute('PRAGMA journal_mode=WAL')
        _apply_migrations(conn)
    finally:
        conn.close()


def _apply_migrations(conn):
    """Applique les migrations manquantes dans l'ordre, une transaction par migration."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return

    migrations = [
        _m1_initial_schema,       # 1 — tables + données initiales
        _m2_temperature_column,   # 2 — ADD COLUMN temperature sur measurements
        _m3_wait_hours_columns,   # 3 — ADD COLUMN wait_hours sur products + treatments
        _m4_default_wait_hours,   # 4 — data : délais d'attente produits par défaut
        _m5_ha_homepage_settings, # 5 — settings HA + homepage
        _m6_generate_tokens,      # 6 — génération des tokens HA + homepage
        _m7_photo_column,         # 7 — ADD COLUMN photo_path sur measurements
        _m8_buy_url_column,       # 8 — ADD COLUMN buy_url sur products
        _m9_add_indexes,          # 9 — index sur measured_at + added_at
        _m10_ha_push_settings,      # 10 — settings pour l'envoi vers HA
        _m11_temperature_log_index, # 11 — index sur temperature_log.recorded_at
        _m12_audit_log,             # 12 — table audit_log
        _m13_pool_type,             # 13 — setting pool_type (brome/chlore)
    ]

    for idx, fn in enumerate(migrations[current:], start=current + 1):
        fn(conn)
        conn.execute(f"PRAGMA user_version = {idx}")
        conn.commit()
        print(f"[pool-db] migration {idx}/{SCHEMA_VERSION} : {fn.__name__}", file=sys.stderr)


# ── Migrations ─────────────────────────────────────────────────────────────────
# Règles :
#   • Chaque fonction doit être idempotente (IF NOT EXISTS, INSERT OR IGNORE…).
#   • Pour ajouter une migration : créer _mN_xxx, l'ajouter à la liste ci-dessus,
#     incrémenter SCHEMA_VERSION.

def _m1_initial_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS measurements (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            measured_at  TEXT    NOT NULL,
            ph           REAL,
            bromine      REAL,
            hardness     REAL,
            alkalinity   REAL,
            temperature  REAL,
            notes        TEXT    DEFAULT '',
            created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    ''')
    for k, v in [
        ('telegram_token',   ''),
        ('telegram_chat_id', ''),
        ('reminder_enabled', 'false'),
        ('reminder_time',    '09:00'),
        ('reminder_days',    'mon,wed,fri'),
        ('pool_volume',      '50000'),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            parameter        TEXT    NOT NULL,
            direction        TEXT    NOT NULL DEFAULT 'up',
            name             TEXT    NOT NULL,
            ref_dose         REAL    NOT NULL,
            ref_dose_unit    TEXT    NOT NULL DEFAULT 'kg',
            ref_change       REAL    NOT NULL,
            ref_volume       REAL    NOT NULL,
            ref_volume_unit  TEXT    NOT NULL DEFAULT 'm3',
            wait_hours       REAL    NOT NULL DEFAULT 6
        )
    ''')
    if conn.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        conn.executemany(
            'INSERT INTO products '
            '(parameter, direction, name, ref_dose, ref_dose_unit, ref_change, ref_volume, ref_volume_unit, wait_hours) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                ('alkalinity', 'up',   'Bicarbonate de sodium (TAC+)', 1.8, 'kg', 10,  100, 'm3', 12),
                ('alkalinity', 'down', 'Acide muriatique (TAC-)',       1.0, 'L',  10,  100, 'm3',  6),
                ('ph',         'up',   'Carbonate de sodium (pH+)',     1.5, 'kg', 0.1, 100, 'm3',  4),
                ('ph',         'down', 'Bisulfate de sodium (pH-)',     1.8, 'kg', 0.1, 100, 'm3',  4),
                ('hardness',   'up',   'Chlorure de calcium (TH+)',     1.4, 'kg', 10,  100, 'm3', 24),
                ('bromine',    'up',   'Brome granulé',                 130, 'g',  1,   100, 'm3',  4),
            ]
        )
    conn.execute('''
        CREATE TABLE IF NOT EXISTS treatments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            added_at     TEXT    NOT NULL,
            parameter    TEXT    NOT NULL,
            direction    TEXT    NOT NULL DEFAULT 'up',
            product_name TEXT    NOT NULL,
            quantity     REAL,
            unit         TEXT    DEFAULT 'kg',
            notes        TEXT    DEFAULT '',
            wait_hours   REAL    NOT NULL DEFAULT 6,
            created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS temperature_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            temperature REAL    NOT NULL,
            sensor_name TEXT    DEFAULT '',
            source      TEXT    DEFAULT 'ha',
            recorded_at TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def _m2_temperature_column(conn):
    if not _col_exists(conn, 'measurements', 'temperature'):
        conn.execute('ALTER TABLE measurements ADD COLUMN temperature REAL')


def _m3_wait_hours_columns(conn):
    if not _col_exists(conn, 'products', 'wait_hours'):
        conn.execute('ALTER TABLE products ADD COLUMN wait_hours REAL NOT NULL DEFAULT 6')
    if not _col_exists(conn, 'treatments', 'wait_hours'):
        conn.execute('ALTER TABLE treatments ADD COLUMN wait_hours REAL NOT NULL DEFAULT 6')


def _m4_default_wait_hours(conn):
    for name, hours in [
        ('Bicarbonate de sodium (TAC+)', 12),
        ('Acide muriatique (TAC-)',        6),
        ('Carbonate de sodium (pH+)',      4),
        ('Bisulfate de sodium (pH-)',      4),
        ('Chlorure de calcium (TH+)',     24),
        ('Brome granulé',                  4),
    ]:
        conn.execute(
            'UPDATE products SET wait_hours = ? WHERE name = ? AND wait_hours = 6',
            (hours, name)
        )


def _m5_ha_homepage_settings(conn):
    for k, v in [
        ('ha_sensor_name',  ''),
        ('ha_token',        ''),
        ('reminder_weekly', 'false'),
        ('homepage_token',  ''),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))


def _m6_generate_tokens(conn):
    for key in ('ha_token', 'homepage_token'):
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row or not row[0]:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, secrets.token_urlsafe(32))
            )


def _m7_photo_column(conn):
    if not _col_exists(conn, 'measurements', 'photo_path'):
        conn.execute('ALTER TABLE measurements ADD COLUMN photo_path TEXT')


def _m8_buy_url_column(conn):
    if not _col_exists(conn, 'products', 'buy_url'):
        conn.execute('ALTER TABLE products ADD COLUMN buy_url TEXT')


def _m9_add_indexes(conn):
    conn.execute('CREATE INDEX IF NOT EXISTS idx_measurements_at ON measurements(measured_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_treatments_at   ON treatments(added_at)')


def _m10_ha_push_settings(conn):
    for k, v in [
        ('ha_push_url',        ''),
        ('ha_push_token',      ''),
        ('ha_push_enabled',    'true'),
        ('ha_rate_limit_s',    '60'),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))


def _m11_temperature_log_index(conn):
    conn.execute('CREATE INDEX IF NOT EXISTS idx_temperature_log_at ON temperature_log(recorded_at)')


def _m12_audit_log(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            action      TEXT    NOT NULL,
            entity      TEXT    NOT NULL DEFAULT 'measurement',
            entity_id   INTEGER,
            detail      TEXT,
            happened_at TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_at ON audit_log(happened_at)')


def _m13_pool_type(conn):
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('pool_type', 'brome')")


def _fmt_hours(h):
    """Formate une durée en heures en chaîne lisible."""
    if h < 1 / 60:
        return "< 1 min"
    if h < 1:
        return f"{int(h * 60)} min"
    hi = int(h)
    mi = int((h - hi) * 60)
    return f"{hi}h{mi:02d}" if mi else f"{hi}h"


def get_latest_temperature():
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM temperature_log ORDER BY recorded_at DESC LIMIT 1'
        ).fetchone()
    return dict(row) if row else None


def _delete_photo_file(filename):
    """Supprime un fichier photo du disque (silencieux si absent)."""
    try:
        path = os.path.join(PHOTOS_DIR, os.path.basename(filename))
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


_MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB par photo

def _handle_photo_upload(photo_file, old_path=None, remove=False):
    """Gère l'upload photo : suppression, remplacement ou conservation."""
    if remove:
        if old_path:
            _delete_photo_file(old_path)
        return None
    if photo_file and photo_file.filename:
        ext = os.path.splitext(photo_file.filename)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            photo_file.stream.seek(0, 2)
            size = photo_file.stream.tell()
            photo_file.stream.seek(0)
            if size > _MAX_PHOTO_BYTES:
                raise ValueError(f"Photo trop grande ({size // 1024 // 1024} MB, max 5 MB)")
            if old_path:
                _delete_photo_file(old_path)
            os.makedirs(PHOTOS_DIR, exist_ok=True)
            fname = uuid.uuid4().hex + ext
            photo_file.save(os.path.join(PHOTOS_DIR, fname))
            return fname
    return old_path


def _load_treatments_with_measurements(limit):
    """Charge les traitements + les mesures depuis la même fenêtre temporelle."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM treatments ORDER BY added_at DESC LIMIT ?', (limit,)
        ).fetchall()
        treatments = [dict(r) for r in rows]
        if treatments:
            oldest = min(t['added_at'] for t in treatments)
            try:
                oldest_dt  = datetime.fromisoformat(oldest.replace(' ', 'T'))
                meas_since = (oldest_dt - timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%S')
            except ValueError:
                logger.warning('_load_treatments_with_measurements: date invalide %r, fallback = aujourd\'hui', oldest)
                meas_since = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%S')
            measurements = [dict(r) for r in conn.execute(
                'SELECT measured_at, ph, bromine, hardness, alkalinity '
                'FROM measurements WHERE measured_at >= ? ORDER BY measured_at ASC',
                (meas_since,)
            ).fetchall()]
        else:
            measurements = []
    return treatments, measurements


def _find_value_before(measurements, param, ref_str):
    """Dernière valeur non-nulle du paramètre strictement avant ref_str."""
    for m in reversed(measurements):
        if m['measured_at'] <= ref_str and m.get(param) is not None:
            return m[param], m['measured_at'][:16].replace('T', ' ')
    return None, None


def get_active_treatments():
    """Traitements actifs enrichis : délais temporels + valeur mesurée avant traitement."""
    treatments, measurements = _load_treatments_with_measurements(limit=100)
    now    = datetime.now()
    result = []
    for t in treatments:
        try:
            added = datetime.fromisoformat(t['added_at'].replace(' ', 'T'))
        except ValueError:
            continue

        param = t['parameter']
        t['before_value'], t['before_date'] = _find_value_before(measurements, param, t['added_at'])

        elapsed_h   = (now - added).total_seconds() / 3600
        wait_h      = t.get('wait_hours') or WAIT_HOURS
        remaining_h = max(0.0, wait_h - elapsed_h)

        t['elapsed_hours']   = elapsed_h
        t['remaining_hours'] = remaining_h
        t['can_retest']      = remaining_h <= 0
        t['elapsed_str']     = _fmt_hours(elapsed_h)
        t['remaining_str']   = _fmt_hours(remaining_h)
        result.append(t)
    return result


def get_treatment_correlations(pool_type='brome'):
    """Corrélation traitement → amélioration : valeurs avant et après pour chaque traitement."""
    treatments, measurements = _load_treatments_with_measurements(limit=100)
    now     = datetime.now()
    results = []

    for t in treatments:
        param     = t['parameter']
        added_str = t['added_at']
        wait_h    = t.get('wait_hours') or WAIT_HOURS

        try:
            added_dt = datetime.fromisoformat(added_str.replace(' ', 'T'))
        except ValueError:
            continue

        retest_str = (added_dt + timedelta(hours=wait_h)).strftime('%Y-%m-%dT%H:%M:%S')

        t['before_value'], t['before_date'] = _find_value_before(measurements, param, added_str)

        # Après : première mesure du paramètre après la fin du délai d'attente
        after_val = after_date = None
        for m in measurements:
            if m['measured_at'] >= retest_str and m.get(param) is not None:
                after_val  = m[param]
                after_date = m['measured_at'][:16].replace('T', ' ')
                break

        t['after_value'] = after_val
        t['after_date']  = after_date
        t['delta']       = None

        before_val = t['before_value']
        if before_val is not None and after_val is not None:
            delta = round(after_val - before_val, 2)
            t['delta'] = delta
            expected_up = (t['direction'] == 'up')
            if abs(delta) < 0.01:
                t['outcome'] = 'unchanged'
            elif (delta > 0) == expected_up:
                t['outcome'] = 'corrected' if get_status(param, after_val, pool_type) == 'ok' else 'improved'
            else:
                t['outcome'] = 'overcorrected'
        elif after_val is None and (added_dt + timedelta(hours=wait_h)) > now:
            t['outcome'] = 'pending'
        elif after_val is None:
            t['outcome'] = 'no_retest'
        else:
            t['outcome'] = 'no_baseline'

        results.append(t)

    return results


def get_settings():
    global _settings_cache
    with _settings_lock:
        if _settings_cache is None:
            with get_db() as conn:
                rows = conn.execute('SELECT key, value FROM settings').fetchall()
            _settings_cache = {r['key']: r['value'] for r in rows}
        return dict(_settings_cache)  # copie défensive


def _invalidate_settings_cache():
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


def save_setting(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    _invalidate_settings_cache()


def get_last_measurement():
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM measurements ORDER BY measured_at DESC LIMIT 1'
        ).fetchone()
    return dict(row) if row else None


def _time_ago(dt_str: str) -> tuple[str, bool]:
    """Retourne (texte, is_old) depuis une chaîne ISO datetime. is_old = True si > 7 jours."""
    try:
        dt = datetime.fromisoformat(dt_str.replace(' ', 'T'))
        seconds = int((datetime.now() - dt).total_seconds())
        if seconds < 3600:
            mins = max(1, seconds // 60)
            return f'il y a {mins} min', False
        if seconds < 86400:
            return f'il y a {seconds // 3600}h', False
        days = seconds // 86400
        return f'il y a {days} jour{"s" if days > 1 else ""}', days >= 7
    except Exception:
        return '', False


def _count_pending_treatments():
    """Traitements dont le délai d'attente (par produit) n'est pas encore écoulé."""
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT added_at, wait_hours FROM treatments').fetchall()
        now   = datetime.now()
        count = 0
        for row in rows:
            try:
                added  = datetime.fromisoformat(row['added_at'].replace(' ', 'T'))
                wait_h = row['wait_hours'] or WAIT_HOURS
                if (now - added).total_seconds() / 3600 < wait_h:
                    count += 1
            except ValueError:
                pass
        return count
    except Exception:
        return 0


@app.context_processor
def inject_globals():
    return {
        'current_year': datetime.now().year,
        'pending_treatments_count': _count_pending_treatments(),
    }


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(token, chat_id, text):
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        r = requests.post(url, data={
            'chat_id':    chat_id,
            'text':       text,
            'parse_mode': 'HTML',
        }, timeout=10)
        return r.json()
    except Exception as exc:
        return {'ok': False, 'description': str(exc)}


def send_reminder():
    s = get_settings()
    if s.get('reminder_enabled') != 'true':
        return
    token    = s.get('telegram_token', '')
    chat_id  = s.get('telegram_chat_id', '')
    if not token or not chat_id:
        return

    last = get_last_measurement()
    if last:
        last_date = last['measured_at'][:16].replace('T', ' ')
        pool_volume = max(float(s.get('pool_volume') or 50000), 100)
        with get_db() as conn:
            products = [dict(r) for r in conn.execute(
                'SELECT * FROM products ORDER BY parameter, direction, name'
            ).fetchall()]
        pool_type = s.get('pool_type', 'brome')
        recs = get_recommendations(last, pool_volume, products=products, pool_type=pool_type)
        ideal_ranges = get_ideal_ranges(pool_type)
        if recs:
            alerts = '\n'.join(
                f"• {r['icon']} {r['param']}: {r['value']} {ideal_ranges[r['key']]['unit']} "
                f"→ {r['product']} ({r['dose']})"
                for r in recs
            )
            msg = (
                f"🏊 <b>Rappel – Test Piscine</b>\n\n"
                f"Dernier test : <b>{last_date}</b>\n\n"
                f"⚠️ Corrections en attente :\n{alerts}\n\n"
                f"Pensez à retester votre eau !"
            )
        else:
            msg = (
                f"🏊 <b>Rappel – Test Piscine</b>\n\n"
                f"Dernier test : <b>{last_date}</b>\n"
                f"✅ Tous les paramètres étaient dans les normes.\n\n"
                f"Effectuez un nouveau test pour confirmer !"
            )
    else:
        san_label = 'Chlore' if s.get('pool_type', 'brome') == 'chlore' else 'Brome'
        msg = (
            "🏊 <b>Rappel – Test Piscine</b>\n\n"
            "Aucun test enregistré. Pensez à mesurer :\n"
            f"• pH\n• {san_label}\n• Dureté (TH)\n• Alcalinité (TAC)"
        )

    send_telegram(token, chat_id, msg)


def send_weekly_summary():
    """Feature 18 — bilan hebdomadaire Telegram chaque lundi."""
    s = get_settings()
    if s.get('reminder_weekly') != 'true':
        return
    token   = s.get('telegram_token', '')
    chat_id = s.get('telegram_chat_id', '')
    if not token or not chat_id:
        return

    cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S')

    with get_db() as conn:
        mrows = conn.execute(
            'SELECT * FROM measurements WHERE measured_at >= ? ORDER BY measured_at ASC', (cutoff,)
        ).fetchall()
        trows = conn.execute(
            'SELECT * FROM treatments WHERE added_at >= ? ORDER BY added_at ASC', (cutoff,)
        ).fetchall()

    if not mrows:
        msg = (
            "🏊 <b>Bilan hebdomadaire – Piscine Manager</b>\n\n"
            "Aucun test enregistré cette semaine.\n"
            "⚠️ Pensez à tester votre eau régulièrement !"
        )
        send_telegram(token, chat_id, msg)
        return

    measurements = [dict(r) for r in mrows]

    def _stat(key, label, unit):
        vals = [m[key] for m in measurements if m.get(key) is not None]
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        return f"• {label} : moy <b>{avg:.1f}</b> | {min(vals)} – {max(vals)} {unit}"

    san_label  = 'Chlore' if s.get('pool_type', 'brome') == 'chlore' else 'Brome'
    stat_lines = list(filter(None, [
        _stat('ph',        '💧 pH',              ''),
        _stat('bromine',   f'🧪 {san_label}',    'ppm'),
        _stat('hardness',  '🪨 TH',              'ppm'),
        _stat('alkalinity','⚗️ TAC',             'ppm'),
    ]))

    pool_volume = float(s.get('pool_volume', 50000))
    with get_db() as conn:
        products = [dict(r) for r in conn.execute(
            'SELECT * FROM products ORDER BY parameter, direction, name'
        ).fetchall()]
    pool_type = s.get('pool_type', 'brome')
    recs  = get_recommendations(measurements[-1], pool_volume, products=products, pool_type=pool_type)
    treats = [dict(r) for r in trows]

    msg = (
        f"🏊 <b>Bilan hebdomadaire – Piscine Manager</b>\n\n"
        f"📊 <b>{len(measurements)} test(s)</b> cette semaine\n\n"
        f"<b>Valeurs (moy | min–max) :</b>\n" +
        '\n'.join(stat_lines)
    )

    if treats:
        tlines = '\n'.join(
            f"• {t['product_name']} ({t['added_at'][:10]})"
            for t in treats
        )
        msg += f"\n\n🧴 <b>Traitements appliqués :</b>\n{tlines}"

    if recs:
        rlines = '\n'.join(
            f"• {r['icon']} {r['param']}: {r['value']} → {r['product']} ({r['dose']})"
            for r in recs
        )
        msg += f"\n\n⚠️ <b>Corrections encore nécessaires :</b>\n{rlines}"
    else:
        msg += "\n\n✅ Tous les paramètres dans les normes au dernier test."

    send_telegram(token, chat_id, msg)


def setup_scheduler(s):
    time_str = s.get('reminder_time', '09:00')
    try:
        hour, minute = map(int, time_str.split(':'))
    except ValueError:
        hour, minute = 9, 0

    if s.get('reminder_enabled') == 'true':
        days        = s.get('reminder_days', 'mon,wed,fri')
        day_of_week = 'mon-sun' if days == 'daily' else days
        scheduler.add_job(
            send_reminder,
            CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute),
            id='pool_reminder',
            replace_existing=True,
        )
    else:
        try:
            scheduler.remove_job('pool_reminder')
        except Exception:
            pass

    if s.get('reminder_weekly') == 'true':
        scheduler.add_job(
            send_weekly_summary,
            CronTrigger(day_of_week='mon', hour=hour, minute=minute),
            id='pool_weekly_summary',
            replace_existing=True,
        )
    else:
        try:
            scheduler.remove_job('pool_weekly_summary')
        except Exception:
            pass


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    last = get_last_measurement()
    s = get_settings()
    pool_volume = float(s.get('pool_volume', 50000))

    with get_db() as conn:
        products = [dict(r) for r in conn.execute(
            'SELECT * FROM products ORDER BY parameter, direction, name'
        ).fetchall()]
        prev_row = conn.execute(
            'SELECT * FROM measurements ORDER BY measured_at DESC LIMIT 1 OFFSET 1'
        ).fetchone()
        _VALID_SPARK = {7, 14, 30}
        try:
            spark_days = int(request.args.get('spark', 14))
            if spark_days not in _VALID_SPARK:
                spark_days = 14
        except (ValueError, TypeError):
            spark_days = 14
        spark_cutoff = (datetime.now() - timedelta(days=spark_days)).strftime('%Y-%m-%dT%H:%M:%S')
        spark_rows = conn.execute(
            'SELECT measured_at, ph, bromine, hardness, alkalinity '
            'FROM measurements WHERE measured_at >= ? ORDER BY measured_at ASC',
            (spark_cutoff,)
        ).fetchall()

    prev = dict(prev_row) if prev_row else None
    spark = [dict(r) for r in spark_rows]

    # Tendances : delta entre last et prev pour chaque paramètre
    deltas = {}
    if last and prev:
        for param in ('ph', 'bromine', 'hardness', 'alkalinity'):
            lv = last.get(param)
            pv = prev.get(param)
            if lv is not None and pv is not None:
                deltas[param] = round(lv - pv, 2)

    last_ago, last_is_old = _time_ago(last['measured_at']) if last else ('', False)

    pool_type = s.get('pool_type', 'brome')
    ideal     = get_ideal_ranges(pool_type)
    statuses  = {}
    recs = []
    if last:
        for param in ('ph', 'bromine', 'hardness', 'alkalinity'):
            if last.get(param) is not None:
                statuses[param] = get_status(param, last[param], pool_type)
        recs = get_recommendations(last, pool_volume, products=products, pool_type=pool_type)

    return render_template(
        'index.html',
        last=last,
        prev=prev,
        deltas=deltas,
        last_ago=last_ago,
        last_is_old=last_is_old,
        spark=spark,
        spark_days=spark_days,
        statuses=statuses,
        recommendations=recs,
        ideal=ideal,
        now=datetime.now().strftime('%Y-%m-%dT%H:%M'),
        active_treatments=get_active_treatments(),
        products=products,
        wait_hours=WAIT_HOURS,
        latest_temp=get_latest_temperature(),
    )


@app.route('/add', methods=['POST'])
def add_measurement():
    try:
        measured_at = request.form.get('measured_at') or datetime.now().strftime('%Y-%m-%dT%H:%M')
        ph          = _parse_param('ph',          request.form.get('ph'))
        bromine     = _parse_param('bromine',     request.form.get('bromine'))
        hardness    = _parse_param('hardness',    request.form.get('hardness'))
        alkalinity  = _parse_param('alkalinity',  request.form.get('alkalinity'))
        temperature = _parse_param('temperature', request.form.get('temperature'))
        notes       = request.form.get('notes', '')
        photo_path  = _handle_photo_upload(request.files.get('photo'))

        with get_db() as conn:
            conn.execute(
                'INSERT INTO measurements '
                '(measured_at, ph, bromine, hardness, alkalinity, temperature, notes, photo_path) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (measured_at, ph, bromine, hardness, alkalinity, temperature, notes, photo_path),
            )
        _stats_cache.clear()
        flash('Mesure enregistrée avec succès !', 'success')
        _payload = {
            'measured_at': measured_at,
            'ph': ph, 'bromine': bromine,
            'hardness': hardness, 'alkalinity': alkalinity,
        }
        _bg_submit(_send_critical_alert, _payload)
        _bg_submit(_send_to_ha,          _payload)
    except Exception as exc:
        _flash_error('Erreur lors de l\'enregistrement. Vérifiez les valeurs saisies.', 'Erreur enregistrement mesure')
    return redirect(url_for('index'))


_ALLOWED_FILTER_COLUMNS = {'measured_at', 'recorded_at'}

def _history_filter_clause(period, from_date, to_date, col='measured_at'):
    """Retourne (where_sql, params) pour filtrer par période ou plage de dates."""
    if col not in _ALLOWED_FILTER_COLUMNS:
        raise ValueError(f"Colonne non autorisée : {col!r}")
    if from_date and to_date:
        return f'WHERE {col} >= ? AND {col} <= ?', [from_date, to_date + 'T23:59:59']
    if from_date:
        return f'WHERE {col} >= ?', [from_date]
    if to_date:
        return f'WHERE {col} <= ?', [to_date + 'T23:59:59']
    if period and str(period).isdigit() and int(period) > 0:
        cutoff = (datetime.now() - timedelta(days=int(period))).strftime('%Y-%m-%dT%H:%M:%S')
        return f'WHERE {col} >= ?', [cutoff]
    return '', []


_stats_cache: dict = {}   # { last_measured_at: (monthly_stats, yoy_rows, curr_yr) }

@app.route('/history')
def history():
    s         = get_settings()
    pool_type = s.get('pool_type', 'brome')
    ideal     = get_ideal_ranges(pool_type)
    period    = request.args.get('period', '')
    from_date = request.args.get('from_date', '')
    to_date   = request.args.get('to_date', '')

    where, params = _history_filter_clause(period, from_date, to_date, 'measured_at')
    temp_where, temp_params = _history_filter_clause(period, from_date, to_date, 'recorded_at')

    _VALID_LIMITS = {100, 200, 500, 2000}
    try:
        TABLE_LIMIT = int(request.args.get('limit', 200))
        if TABLE_LIMIT not in _VALID_LIMITS:
            TABLE_LIMIT = 200
    except (ValueError, TypeError):
        TABLE_LIMIT = 200

    with get_db() as conn:
        # Tableau : les TABLE_LIMIT mesures les plus récentes + total pour avertissement
        total_count = conn.execute(
            f'SELECT COUNT(*) FROM measurements {where}', params
        ).fetchone()[0]
        rows = conn.execute(
            f'SELECT * FROM measurements {where} ORDER BY measured_at DESC LIMIT ?',
            params + [TABLE_LIMIT]
        ).fetchall()
        # Graphiques : au plus 3000 points (colonnes utiles seulement), ordre chronologique
        CHART_LIMIT = 3000
        chart_rows = conn.execute(
            f'SELECT measured_at, ph, bromine, hardness, alkalinity '
            f'FROM measurements {where} ORDER BY measured_at ASC LIMIT ?',
            params + [CHART_LIMIT]
        ).fetchall()
        temp_rows = conn.execute(
            f'SELECT temperature, recorded_at FROM temperature_log {temp_where} ORDER BY recorded_at ASC',
            temp_params
        ).fetchall()
        # Feature 16 — statistiques mensuelles (toujours sur la totalité des données)
        # Utilise un cache invalidé dès qu'une nouvelle mesure est enregistrée
        last_ts_row = conn.execute('SELECT MAX(measured_at) FROM measurements').fetchone()
        last_ts     = last_ts_row[0] if last_ts_row else None
        curr_yr     = datetime.now().year
        cache_key   = (last_ts, curr_yr)
        if cache_key not in _stats_cache:
            monthly_rows = conn.execute('''
                SELECT
                    strftime('%Y-%m', measured_at) AS month,
                    COUNT(*) AS cnt,
                    ROUND(AVG(ph),2) AS avg_ph,   ROUND(MIN(ph),2) AS min_ph,   ROUND(MAX(ph),2) AS max_ph,
                    ROUND(AVG(bromine),2) AS avg_br,  MIN(bromine) AS min_br,  MAX(bromine) AS max_br,
                    ROUND(AVG(hardness),1) AS avg_th,  MIN(hardness) AS min_th,  MAX(hardness) AS max_th,
                    ROUND(AVG(alkalinity),1) AS avg_tac, MIN(alkalinity) AS min_tac, MAX(alkalinity) AS max_tac
                FROM measurements
                WHERE ph IS NOT NULL OR bromine IS NOT NULL OR hardness IS NOT NULL OR alkalinity IS NOT NULL
                GROUP BY month
                ORDER BY month ASC
            ''').fetchall()
            yoy_rows = conn.execute('''
                SELECT strftime('%m', measured_at)     AS month_num,
                       strftime('%Y', measured_at)     AS year,
                       ROUND(AVG(ph),         2)       AS avg_ph,
                       ROUND(AVG(bromine),    2)       AS avg_br,
                       ROUND(AVG(hardness),   1)       AS avg_th,
                       ROUND(AVG(alkalinity), 1)       AS avg_tac
                FROM measurements
                WHERE strftime('%Y', measured_at) IN (?, ?)
                  AND (ph IS NOT NULL OR bromine IS NOT NULL
                       OR hardness IS NOT NULL OR alkalinity IS NOT NULL)
                GROUP BY year, month_num
                ORDER BY year, month_num
            ''', (str(curr_yr), str(curr_yr - 1))).fetchall()
            _stats_cache.clear()
            _stats_cache[cache_key] = ([dict(r) for r in monthly_rows],
                                       [dict(r) for r in yoy_rows])
        monthly_rows_dicts, yoy_rows_dicts = _stats_cache[cache_key]

    measurements = [dict(r) for r in rows]
    for m in measurements:
        m['statuses'] = {
            param: get_status(param, m[param], pool_type)
            for param in ('ph', 'bromine', 'hardness', 'alkalinity')
            if m.get(param) is not None
        }
    chart_data      = [dict(r) for r in chart_rows]
    monthly_stats   = monthly_rows_dicts
    table_truncated = total_count > TABLE_LIMIT

    yoy = {}
    for row in yoy_rows_dicts:
        y = row['year']
        yoy.setdefault(y, {})[row['month_num']] = row

    def col(key):
        return [m.get(key) for m in chart_data]

    temp_data = [dict(r) for r in temp_rows]

    return render_template(
        'history.html',
        measurements=measurements,
        total_count=total_count,
        table_truncated=table_truncated,
        table_limit=TABLE_LIMIT,
        ideal=ideal,
        period=period,
        from_date=from_date,
        to_date=to_date,
        correlations        = get_treatment_correlations(pool_type=pool_type),
        monthly_stats       = json.dumps(monthly_stats),
        chart_labels        = json.dumps([m['measured_at'][:16].replace('T', ' ') for m in chart_data]),
        chart_ph            = json.dumps(col('ph')),
        chart_bromine       = json.dumps(col('bromine')),
        chart_hardness      = json.dumps(col('hardness')),
        chart_alkalinity    = json.dumps(col('alkalinity')),
        chart_temp_labels   = json.dumps([t['recorded_at'][:16].replace('T', ' ') for t in temp_data]),
        chart_temp_values   = json.dumps([t['temperature'] for t in temp_data]),
        temp_count          = len(temp_data),
        yoy_data      = json.dumps(yoy),
        yoy_curr_year = curr_yr,
        yoy_prev_year = curr_yr - 1,
    )


@app.route('/history/export.csv')
def export_csv():
    period    = request.args.get('period', '')
    from_date = request.args.get('from_date', '')
    to_date   = request.args.get('to_date', '')

    where, params = _history_filter_clause(period, from_date, to_date, 'measured_at')

    with get_db() as conn:
        rows = conn.execute(
            f'SELECT * FROM measurements {where} ORDER BY measured_at DESC',
            params
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    s         = get_settings()
    san_label = 'Chlore (ppm)' if s.get('pool_type', 'brome') == 'chlore' else 'Brome (ppm)'
    writer.writerow(['Date/Heure', 'pH', san_label, 'Dureté TH (ppm)', 'Alcalinité TAC (ppm)', 'Température (°C)', 'Notes'])
    for r in rows:
        writer.writerow([
            r['measured_at'][:16].replace('T', ' '),
            r['ph']         if r['ph']         is not None else '',
            r['bromine']    if r['bromine']     is not None else '',
            r['hardness']   if r['hardness']    is not None else '',
            r['alkalinity'] if r['alkalinity']  is not None else '',
            r['temperature'] if r['temperature'] is not None else '',
            r['notes'] or '',
        ])

    filename = f"piscine_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        '\ufeff' + output.getvalue(),   # BOM UTF-8 pour Excel
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


BACKUP_VERSION = 1
# Clés de paramètres à exclure du backup (tokens régénérés automatiquement)
_BACKUP_EXCLUDE_SETTINGS = {'ha_token', 'homepage_token', 'ha_push_token'}


@app.route('/backup/export')
def backup_export():
    """Exporte la totalité de la base en JSON."""
    with get_db() as conn:
        measurements  = [dict(r) for r in conn.execute('SELECT * FROM measurements  ORDER BY measured_at ASC').fetchall()]
        products      = [dict(r) for r in conn.execute('SELECT * FROM products      ORDER BY id ASC').fetchall()]
        treatments    = [dict(r) for r in conn.execute('SELECT * FROM treatments    ORDER BY added_at ASC').fetchall()]
        temp_log      = [dict(r) for r in conn.execute('SELECT * FROM temperature_log ORDER BY recorded_at ASC').fetchall()]
        settings_rows = conn.execute('SELECT key, value FROM settings').fetchall()

    settings_dict = {
        r['key']: r['value']
        for r in settings_rows
        if r['key'] not in _BACKUP_EXCLUDE_SETTINGS
    }

    payload = json.dumps({
        'version':      BACKUP_VERSION,
        'exported_at':  datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'measurements': measurements,
        'products':     products,
        'treatments':   treatments,
        'temperature_log': temp_log,
        'settings':     settings_dict,
    }, ensure_ascii=False, indent=2)

    filename = f"piscine_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return Response(
        payload,
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@app.route('/backup/import', methods=['POST'])
def backup_import():
    """Restaure la base depuis un fichier JSON exporté précédemment."""
    f = request.files.get('backup_file')
    if not f or not f.filename.endswith('.json'):
        flash('Fichier invalide. Sélectionnez un fichier .json exporté par Piscine Manager.', 'danger')
        return redirect(url_for('settings_page') + '#backup')

    try:
        data = json.loads(f.read().decode('utf-8'))
    except Exception:
        flash('Fichier JSON invalide ou corrompu.', 'danger')
        return redirect(url_for('settings_page') + '#backup')

    if data.get('version') != BACKUP_VERSION:
        flash(f"Version de backup incompatible (attendu : {BACKUP_VERSION}, reçu : {data.get('version')}).", 'danger')
        return redirect(url_for('settings_page') + '#backup')

    # Valider la structure avant d'ouvrir la transaction — échoue vite sans toucher la DB
    if not isinstance(data.get('measurements'), list) \
            or not isinstance(data.get('products'), list) \
            or not isinstance(data.get('treatments'), list):
        flash('Structure de backup invalide (clés manquantes ou mauvais types).', 'danger')
        return redirect(url_for('settings_page') + '#backup')

    try:
        # get_db() est atomique : commit() en fin de bloc, rollback() sur exception.
        # Si une INSERT échoue à mi-parcours, les DELETE précédents sont également annulés.
        with get_db() as conn:
            # ── Mesures ──────────────────────────────────────────────────────
            conn.execute('DELETE FROM measurements')
            for m in data.get('measurements', []):
                conn.execute(
                    'INSERT INTO measurements (id, measured_at, ph, bromine, hardness, alkalinity, temperature, notes, photo_path, created_at) '
                    'VALUES (:id, :measured_at, :ph, :bromine, :hardness, :alkalinity, :temperature, :notes, :photo_path, :created_at)',
                    {**m, 'photo_path': m.get('photo_path')})

            # ── Produits ─────────────────────────────────────────────────────
            conn.execute('DELETE FROM products')
            for p in data.get('products', []):
                conn.execute(
                    'INSERT INTO products (id, parameter, direction, name, ref_dose, ref_dose_unit, ref_change, ref_volume, ref_volume_unit, wait_hours, buy_url) '
                    'VALUES (:id, :parameter, :direction, :name, :ref_dose, :ref_dose_unit, :ref_change, :ref_volume, :ref_volume_unit, :wait_hours, :buy_url)',
                    {**p, 'wait_hours': p.get('wait_hours', 6), 'buy_url': p.get('buy_url')})

            # ── Traitements ──────────────────────────────────────────────────
            conn.execute('DELETE FROM treatments')
            for t in data.get('treatments', []):
                conn.execute(
                    'INSERT INTO treatments (id, added_at, parameter, direction, product_name, quantity, unit, notes, wait_hours, created_at) '
                    'VALUES (:id, :added_at, :parameter, :direction, :product_name, :quantity, :unit, :notes, :wait_hours, :created_at)',
                    {**t, 'wait_hours': t.get('wait_hours', 6)})

            # ── Journal température ──────────────────────────────────────────
            conn.execute('DELETE FROM temperature_log')
            for t in data.get('temperature_log', []):
                conn.execute(
                    'INSERT INTO temperature_log (id, temperature, sensor_name, source, recorded_at) '
                    'VALUES (:id, :temperature, :sensor_name, :source, :recorded_at)', t)

            # ── Paramètres (hors tokens) ─────────────────────────────────────
            for key, value in data.get('settings', {}).items():
                if key not in _BACKUP_EXCLUDE_SETTINGS:
                    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))

        _invalidate_settings_cache()

        n_m = len(data.get('measurements', []))
        n_p = len(data.get('products', []))
        n_t = len(data.get('treatments', []))
        flash(
            f'Restauration réussie — {n_m} mesure(s), {n_p} produit(s), {n_t} traitement(s) importés.',
            'success',
        )
        s = get_settings()
        setup_scheduler(s)
    except Exception as exc:
        _flash_error('Erreur lors de la restauration. Le fichier est peut-être corrompu.', 'Erreur restauration backup')

    return redirect(url_for('settings_page') + '#backup')


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        section = request.form.get('section', '')

        if section == 'piscine':
            try:
                vol = float(request.form.get('pool_volume') or 50000)
                if vol < 100:
                    vol = 50000.0
            except (ValueError, TypeError):
                vol = 50000.0
            save_setting('pool_volume', str(vol))
            pool_type_val = request.form.get('pool_type', 'brome')
            if pool_type_val not in ('brome', 'chlore'):
                pool_type_val = 'brome'
            save_setting('pool_type', pool_type_val)
            flash('Paramètres de la piscine sauvegardés.', 'success')

        elif section == 'serveur':
            try:
                new_port = int(request.form.get('app_port') or _DEFAULT_PORT)
                if not (1024 <= new_port <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                new_port = _DEFAULT_PORT
            _save_env_port(new_port)
            flash('Port sauvegardé. Redémarrez le service pour l\'appliquer.', 'success'
                  if new_port == _active_port else 'warning')

        elif section == 'telegram':
            save_setting('telegram_token',   request.form.get('telegram_token', ''))
            save_setting('telegram_chat_id', request.form.get('telegram_chat_id', ''))
            flash('Paramètres Telegram sauvegardés.', 'success')

        elif section == 'rappels':
            save_setting('reminder_enabled', 'true' if request.form.get('reminder_enabled') else 'false')
            save_setting('reminder_time',    request.form.get('reminder_time', '09:00'))
            save_setting('reminder_days',    request.form.get('reminder_days', 'mon,wed,fri'))
            save_setting('reminder_weekly',  'true' if request.form.get('reminder_weekly') else 'false')
            setup_scheduler(get_settings())
            flash('Rappels sauvegardés.', 'success')

        elif section == 'ha':
            save_setting('ha_sensor_name',  request.form.get('ha_sensor_name', ''))
            save_setting('ha_push_url',     request.form.get('ha_push_url', '').rstrip('/'))
            save_setting('ha_push_token',   request.form.get('ha_push_token', ''))
            save_setting('ha_push_enabled', 'true' if request.form.get('ha_push_enabled') else 'false')
            try:
                rate_s = max(10, int(request.form.get('ha_rate_limit_s') or 60))
            except (ValueError, TypeError):
                rate_s = 60
            save_setting('ha_rate_limit_s', str(rate_s))
            flash('Paramètres Home Assistant sauvegardés.', 'success')

        else:
            flash('Section inconnue.', 'danger')
            return redirect(url_for('settings_page'))

        return redirect(url_for('settings_page') + f'#{section}')

    s_dict    = get_settings()
    pool_type = s_dict.get('pool_type', 'brome')
    return render_template('settings.html',
        settings=s_dict,
        active_port=_active_port,
        configured_port=_get_env_port(),
        ideal=get_ideal_ranges(pool_type),
    )


@app.route('/test-telegram', methods=['POST'])
def test_telegram():
    s       = get_settings()
    token   = s.get('telegram_token', '')
    chat_id = s.get('telegram_chat_id', '')
    if not token or not chat_id:
        return jsonify({'ok': False, 'description': 'Token ou Chat ID manquant dans les paramètres.'})
    result = send_telegram(
        token, chat_id,
        '🏊 <b>Test de connexion</b> – Gestionnaire de piscine opérationnel !'
    )
    return jsonify(result)


@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_measurement(id):
    if request.method == 'GET':
        with get_db() as conn:
            row = conn.execute('SELECT * FROM measurements WHERE id = ?', (id,)).fetchone()
        if row is None:
            return jsonify({'error': 'Mesure introuvable'}), 404
        return jsonify(dict(row))

    try:
        measured_at = request.form.get('measured_at') or datetime.now().strftime('%Y-%m-%dT%H:%M')
        ph          = _parse_param('ph',          request.form.get('ph'))
        bromine     = _parse_param('bromine',     request.form.get('bromine'))
        hardness    = _parse_param('hardness',    request.form.get('hardness'))
        alkalinity  = _parse_param('alkalinity',  request.form.get('alkalinity'))
        temperature = _parse_param('temperature', request.form.get('temperature'))
        notes = request.form.get('notes', '')

        with get_db() as conn:
            old_row = conn.execute('SELECT * FROM measurements WHERE id=?', (id,)).fetchone()
            old = dict(old_row) if old_row else {}
        photo_path = _handle_photo_upload(
            request.files.get('photo'),
            old_path=old.get('photo_path'),
            remove=(request.form.get('remove_photo') == '1'),
        )

        with get_db() as conn:
            conn.execute(
                'UPDATE measurements SET measured_at=?, ph=?, bromine=?, hardness=?, '
                'alkalinity=?, temperature=?, notes=?, photo_path=? WHERE id=?',
                (measured_at, ph, bromine, hardness, alkalinity, temperature, notes, photo_path, id),
            )
            conn.execute(
                'INSERT INTO audit_log (action, entity, entity_id, detail) VALUES (?, ?, ?, ?)',
                ('edit', 'measurement', id, json.dumps({
                    'before': {k: old.get(k) for k in ('measured_at','ph','bromine','hardness','alkalinity')},
                    'after':  {'measured_at': measured_at, 'ph': ph, 'bromine': bromine,
                               'hardness': hardness, 'alkalinity': alkalinity},
                })),
            )
        flash('Mesure mise à jour avec succès !', 'success')
        _edit_payload = {
            'measured_at': measured_at,
            'ph': ph, 'bromine': bromine,
            'hardness': hardness, 'alkalinity': alkalinity,
        }
        _bg_submit(_send_to_ha, _edit_payload)
    except Exception as exc:
        _flash_error('Erreur lors de la mise à jour. Vérifiez les valeurs saisies.', f'Erreur mise à jour mesure id={id}')
    return redirect(url_for('history'))


@app.route('/api/ha/temperature', methods=['POST'])
def ha_temperature():
    """Endpoint appelé par Home Assistant pour envoyer la température."""
    # Token accepté via Authorization: Bearer <token> OU corps JSON/form {"token": ...}
    data_peek = request.get_json(silent=True) or {}
    fallback  = str(data_peek.get('token', '') or request.form.get('token', ''))
    err = _check_bearer_token('ha_token', fallback)
    if err:
        return err

    body = request.get_json(silent=True) or request.form
    try:
        temperature = float(body.get('temperature', 0))
        sensor_name = str(body.get('sensor', s.get('ha_sensor_name', '')))
        now_str     = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

        with get_db() as conn:
            # Rate limit configurable (défaut 60 s)
            rate_limit_s = max(10, int(s.get('ha_rate_limit_s') or 60))
            last_row = conn.execute(
                "SELECT recorded_at FROM temperature_log ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            if last_row:
                last_time = datetime.fromisoformat(last_row['recorded_at'].replace(' ', 'T'))
                if (datetime.now() - last_time).total_seconds() < rate_limit_s:
                    return jsonify({
                        'ok': False,
                        'error': f'Rate limit : 1 insertion par {rate_limit_s} s maximum',
                    }), 429

            conn.execute(
                'INSERT INTO temperature_log (temperature, sensor_name, source, recorded_at) VALUES (?, ?, ?, ?)',
                (temperature, sensor_name, 'ha', now_str)
            )
            # Feature 15 — purge automatique des données > 90 jours
            cutoff_purge = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%S')
            conn.execute("DELETE FROM temperature_log WHERE recorded_at < ?", (cutoff_purge,))

        return jsonify({'ok': True, 'temperature': temperature, 'sensor': sensor_name})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/api/calculate', methods=['POST'])
def api_calculate():
    """Feature 13 — calcul de dose côté serveur (formule unique, plus de duplication JS)."""
    data = request.get_json(silent=True) or request.form
    try:
        product_id  = int(data.get('product_id', 0))
        current_val = float(data.get('current_value'))
        target_val  = float(data.get('target_value'))
        pool_vol_l  = float(data.get('pool_volume_l', 50000))
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    if pool_vol_l < 100:
        return jsonify({'ok': False, 'error': 'Volume de piscine invalide (min 100 L)'}), 400

    with get_db() as conn:
        row = conn.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'Produit introuvable'}), 404

    p      = dict(row)
    change = target_val - current_val

    if abs(change) < 1e-9:
        return jsonify({'ok': True, 'no_change': True})

    # Formule : dose = (|Δ| / ref_change) × ref_dose × (pool_L / ref_vol_L)
    ref_vol_l = p['ref_volume'] * 1000 if p['ref_volume_unit'] == 'm3' else p['ref_volume']
    if ref_vol_l <= 0 or p['ref_change'] <= 0:
        return jsonify({'ok': False, 'error': 'Données produit invalides (volume ou variation nuls)'}), 400
    raw_dose  = (abs(change) / p['ref_change']) * p['ref_dose'] * (pool_vol_l / ref_vol_l)

    # Auto-conversion d'unité pour lisibilité
    u = p['ref_dose_unit']
    if   u == 'kg' and raw_dose < 0.1:   display_dose, display_unit = f"{raw_dose * 1000:.0f}", 'g'
    elif u == 'g'  and raw_dose >= 1000: display_dose, display_unit = f"{raw_dose / 1000:.2f}", 'kg'
    elif u == 'L'  and raw_dose < 0.1:   display_dose, display_unit = f"{raw_dose * 1000:.0f}", 'ml'
    elif u == 'ml' and raw_dose >= 1000: display_dose, display_unit = f"{raw_dose / 1000:.2f}", 'L'
    else:
        display_dose = f"{raw_dose:.2f}" if raw_dose < 10 else f"{raw_dose:.1f}"
        display_unit = u

    param       = p['parameter']
    change_unit = 'pH' if param == 'ph' else 'ppm'
    change_abs  = abs(change)
    change_disp = f"{change_abs:.1f}" if param == 'ph' else f"{change_abs:.0f}"
    direction   = 'augmenter' if change > 0 else 'baisser'

    return jsonify({
        'ok':           True,
        'no_change':    False,
        'display_dose': display_dose,
        'display_unit': display_unit,
        'detail':       f"Pour {direction} de {change_disp} {change_unit} ({current_val} → {target_val}) dans {pool_vol_l / 1000:.1f} m³",
        'icon':         '⬆️' if change > 0 else '⬇️',
        'direction':    'up' if change > 0 else 'down',
    })


@app.route('/test-ha-push', methods=['POST'])
def test_ha_push():
    s        = get_settings()
    ha_url   = s.get('ha_push_url',   '').rstrip('/')
    ha_token = s.get('ha_push_token', '')
    if not ha_url or not ha_token:
        return jsonify({'ok': False, 'error': 'URL ou token HA manquant dans les paramètres.'})
    try:
        r = requests.get(
            f'{ha_url}/api/',
            headers={'Authorization': f'Bearer {ha_token}'},
            timeout=5,
        )
        if r.status_code == 200:
            return jsonify({'ok': True, 'message': 'Connexion Home Assistant réussie !'})
        return jsonify({'ok': False, 'error': f'HTTP {r.status_code} — vérifiez l\'URL et le token.'})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)})


@app.route('/test-homepage', methods=['POST'])
def test_homepage():
    s     = get_settings()
    token = s.get('homepage_token', '')
    if not token:
        return jsonify({'ok': False, 'error': 'Token Homepage manquant dans les paramètres.'})
    last        = get_last_measurement()
    latest_temp = get_latest_temperature()
    preview = {
        'ph':          last.get('ph')         if last else None,
        'bromine':     last.get('bromine')    if last else None,
        'hardness':    last.get('hardness')   if last else None,
        'alkalinity':  last.get('alkalinity') if last else None,
        'temperature': latest_temp['temperature'] if latest_temp else None,
        'last_test':   last['measured_at'][:16].replace('T', ' ') if last else 'Aucun test',
    }
    return jsonify({'ok': True, 'message': 'Endpoint Homepage opérationnel !', 'preview': preview})


@app.route('/api/pool-volume', methods=['POST'])
def api_pool_volume():
    data = request.get_json(silent=True) or request.form
    try:
        vol = float(data.get('volume', 0))
        if vol < 100:
            return jsonify({'ok': False, 'error': 'Volume trop petit (min 100 L)'}), 400
        save_setting('pool_volume', str(vol))
        return jsonify({'ok': True, 'volume': vol})
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/regenerate-ha-token', methods=['POST'])
def regenerate_ha_token():
    new_token = secrets.token_urlsafe(32)
    save_setting('ha_token', new_token)
    flash('Nouveau token généré. Mettez à jour votre configuration Home Assistant.', 'warning')
    return redirect(url_for('settings_page') + '#ha')


@app.route('/regenerate-homepage-token', methods=['POST'])
def regenerate_homepage_token():
    new_token = secrets.token_urlsafe(32)
    save_setting('homepage_token', new_token)
    flash('Nouveau token Homepage généré. Mettez à jour votre services.yaml.', 'warning')
    return redirect(url_for('settings_page') + '#homepage')


@app.route('/api/homepage')
def api_homepage():
    """Widget Homepage (https://gethomepage.dev) — Custom API."""
    err = _check_bearer_token('homepage_token', request.args.get('token', ''))
    if err:
        return err

    s           = get_settings()
    last        = get_last_measurement()
    latest_temp = get_latest_temperature()
    pool_volume = float(s.get('pool_volume', 50000))

    with get_db() as conn:
        api_products = [dict(r) for r in conn.execute(
            'SELECT * FROM products ORDER BY parameter, direction, name'
        ).fetchall()]

    pool_type = s.get('pool_type', 'brome')
    statuses  = {}
    recs      = []
    if last:
        for param in ('ph', 'bromine', 'hardness', 'alkalinity'):
            if last.get(param) is not None:
                statuses[param] = get_status(param, last[param], pool_type)
        recs = get_recommendations(last, pool_volume, products=api_products, pool_type=pool_type)

    STATUS_ICON = {'ok': '✅', 'low': '⬇️', 'high': '⬆️', 'none': '—'}

    def _fmt(key):
        if not last or last.get(key) is None:
            return None
        v   = last[key]
        st  = statuses.get(key, 'none')
        ico = STATUS_ICON[st]
        return f"{ico} {v}"

    return jsonify({
        # Champs mappables individuellement
        'ph':            last.get('ph')         if last else None,
        'bromine':       last.get('bromine')     if last else None,
        'hardness':      last.get('hardness')    if last else None,
        'alkalinity':    last.get('alkalinity')  if last else None,
        'temperature':   latest_temp['temperature'] if latest_temp else None,
        'last_test':     last['measured_at'][:16].replace('T', ' ') if last else 'Aucun test',
        'corrections':   len(recs),
        'pending_treatments': _count_pending_treatments(),
        # Champs formatés avec icônes (pratiques pour un affichage rapide)
        'ph_display':         _fmt('ph'),
        'bromine_display':    _fmt('bromine'),
        'hardness_display':   _fmt('hardness'),
        'alkalinity_display': _fmt('alkalinity'),
        # Statuts bruts (ok / low / high / none)
        'ph_status':         statuses.get('ph',        'none'),
        'bromine_status':    statuses.get('bromine',   'none'),
        'hardness_status':   statuses.get('hardness',  'none'),
        'alkalinity_status': statuses.get('alkalinity','none'),
    })


@app.route('/api/sw-skip-list')
def api_sw_skip_list():
    """Liste des préfixes que le service worker ne doit pas mettre en cache."""
    return jsonify({'skip': SW_SKIP_PREFIXES})


@app.route('/treatments/add', methods=['POST'])
def add_treatment():
    try:
        added_at     = request.form.get('added_at') or datetime.now().strftime('%Y-%m-%dT%H:%M')
        parameter    = request.form.get('parameter', '').strip()
        direction    = request.form.get('direction', 'up')
        product_name = request.form.get('product_name', '').strip()
        quantity_raw = request.form.get('quantity', '')
        unit         = request.form.get('unit', 'kg')
        notes        = request.form.get('notes', '')
        if not product_name:
            flash('Le nom du produit est requis.', 'danger')
            return redirect(url_for('index'))
        if not parameter:
            flash('Le paramètre est requis.', 'danger')
            return redirect(url_for('index'))
        with get_db() as conn:
            # Snapshot du délai d'attente depuis la fiche produit
            prod_row = conn.execute(
                'SELECT wait_hours FROM products WHERE name = ?', (product_name,)
            ).fetchone()
            wait_h = float(prod_row['wait_hours']) if prod_row and prod_row['wait_hours'] else WAIT_HOURS
            conn.execute(
                'INSERT INTO treatments (added_at, parameter, direction, product_name, quantity, unit, notes, wait_hours) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    added_at, parameter, direction, product_name,
                    float(quantity_raw) if quantity_raw else None,
                    unit, notes, wait_h,
                )
            )
        flash(f'Traitement enregistré — retester dans {_fmt_hours(wait_h)} !', 'success')
    except Exception as exc:
        _flash_error('Erreur lors de l\'enregistrement du traitement.', 'Erreur ajout traitement')
    return redirect(url_for('index'))


@app.route('/treatments/delete/<int:id>', methods=['POST'])
def delete_treatment(id):
    with get_db() as conn:
        row = conn.execute('SELECT product_name, parameter FROM treatments WHERE id = ?', (id,)).fetchone()
        conn.execute('DELETE FROM treatments WHERE id = ?', (id,))
        if row:
            conn.execute(
                'INSERT INTO audit_log (action, entity, entity_id, detail) VALUES (?, ?, ?, ?)',
                ('delete', 'treatment', id,
                 json.dumps({'product_name': row['product_name'], 'parameter': row['parameter']}))
            )
    return redirect(url_for('index'))


@app.route('/calculator')
def calculator():
    with get_db() as conn:
        products = [dict(r) for r in conn.execute(
            'SELECT * FROM products ORDER BY parameter, direction, name'
        ).fetchall()]
    s         = get_settings()
    pool_type = s.get('pool_type', 'brome')
    return render_template('calculator.html',
        products=products,
        pool_volume=float(s.get('pool_volume', 50000)),
        ideal=get_ideal_ranges(pool_type),
        last=get_last_measurement(),
    )


@app.route('/products/add', methods=['POST'])
def add_product():
    try:
        with get_db() as conn:
            conn.execute(
                'INSERT INTO products (parameter, direction, name, ref_dose, ref_dose_unit, ref_change, ref_volume, ref_volume_unit, wait_hours, buy_url) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    request.form['parameter'],
                    request.form['direction'],
                    request.form['name'].strip(),
                    float(request.form['ref_dose']),
                    request.form['ref_dose_unit'],
                    float(request.form['ref_change']),
                    float(request.form['ref_volume']),
                    request.form['ref_volume_unit'],
                    float(request.form.get('wait_hours') or WAIT_HOURS),
                    request.form.get('buy_url', '').strip() or None,
                )
            )
        flash('Produit ajouté !', 'success')
    except Exception as exc:
        _flash_error('Erreur lors de l\'ajout du produit. Vérifiez les valeurs saisies.', 'Erreur ajout produit')
    return redirect(url_for('calculator') + '#produits')


@app.route('/products/edit/<int:id>', methods=['GET', 'POST'])
def edit_product(id):
    if request.method == 'GET':
        with get_db() as conn:
            row = conn.execute('SELECT * FROM products WHERE id = ?', (id,)).fetchone()
        if row is None:
            return jsonify({'error': 'Produit introuvable'}), 404
        return jsonify(dict(row))

    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE products SET parameter=?, direction=?, name=?, ref_dose=?, ref_dose_unit=?, '
                'ref_change=?, ref_volume=?, ref_volume_unit=?, wait_hours=?, buy_url=? WHERE id=?',
                (
                    request.form['parameter'],
                    request.form['direction'],
                    request.form['name'].strip(),
                    float(request.form['ref_dose']),
                    request.form['ref_dose_unit'],
                    float(request.form['ref_change']),
                    float(request.form['ref_volume']),
                    request.form['ref_volume_unit'],
                    float(request.form.get('wait_hours') or WAIT_HOURS),
                    request.form.get('buy_url', '').strip() or None,
                    id,
                )
            )
        flash('Produit mis à jour !', 'success')
    except Exception as exc:
        _flash_error('Erreur lors de la mise à jour du produit. Vérifiez les valeurs saisies.', f'Erreur mise à jour produit id={id}')
    return redirect(url_for('calculator') + '#produits')


@app.route('/products/delete/<int:id>', methods=['POST'])
def delete_product(id):
    with get_db() as conn:
        conn.execute('DELETE FROM products WHERE id = ?', (id,))
    flash('Produit supprimé.', 'info')
    return redirect(url_for('calculator') + '#produits')


@app.route('/delete/<int:id>', methods=['POST'])
def delete_measurement(id):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM measurements WHERE id=?', (id,)).fetchone()
        conn.execute('DELETE FROM measurements WHERE id = ?', (id,))
        conn.execute(
            'INSERT INTO audit_log (action, entity, entity_id, detail) VALUES (?, ?, ?, ?)',
            ('delete', 'measurement', id, json.dumps(
                {k: row[k] for k in ('measured_at','ph','bromine','hardness','alkalinity')} if row else {}
            )),
        )
    if row and row['photo_path']:
        _delete_photo_file(row['photo_path'])
    flash('Mesure supprimée.', 'info')
    return redirect(url_for('history'))


@app.route('/measurements/purge', methods=['POST'])
def purge_measurements():
    before_date = (request.form.get('before_date') or '').strip()
    if not before_date:
        flash('Date requise pour la purge.', 'danger')
        return redirect(url_for('history'))
    try:
        cutoff = before_date + 'T00:00:00'
        with get_db() as conn:
            photo_rows = conn.execute(
                'SELECT photo_path FROM measurements WHERE measured_at < ? AND photo_path IS NOT NULL',
                (cutoff,)
            ).fetchall()
            count = conn.execute(
                'SELECT COUNT(*) FROM measurements WHERE measured_at < ?', (cutoff,)
            ).fetchone()[0]
        if count == 0:
            flash('Aucune mesure à supprimer pour cette période.', 'info')
        else:
            with get_db() as conn:
                conn.execute('DELETE FROM measurements WHERE measured_at < ?', (cutoff,))
                treat_count = conn.execute(
                    'SELECT COUNT(*) FROM treatments WHERE added_at < ?', (cutoff,)
                ).fetchone()[0]
                conn.execute('DELETE FROM treatments WHERE added_at < ?', (cutoff,))
            for row in photo_rows:
                _delete_photo_file(row['photo_path'])
            msg = f'{count} mesure(s) supprimée(s) avant le {before_date}.'
            if treat_count:
                msg += f' {treat_count} traitement(s) orphelin(s) supprimé(s).'
            flash(msg, 'success')
    except Exception as exc:
        _flash_error('Erreur lors de la purge. Vérifiez la date saisie.', 'Erreur purge mesures')
    return redirect(url_for('history'))


@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')


# ─── Gestionnaires d'erreurs ─────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    flash('Fichier trop volumineux (max 8 MB).', 'danger')
    return redirect(request.referrer or url_for('index'))


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    s = get_settings()
    setup_scheduler(s)
    if not scheduler.running:
        scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    atexit.register(lambda: _bg_pool.shutdown(wait=False))
    app.run(debug=False, host='0.0.0.0', port=_active_port)
