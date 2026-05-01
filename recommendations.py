# Valeurs idéales pour une piscine au brome
IDEAL_RANGES = {
    'ph':         {'min': 7.2, 'max': 7.6, 'ideal': 7.4,  'unit': '',    'label': 'pH'},
    'bromine':    {'min': 3.0, 'max': 5.0, 'ideal': 4.0,  'unit': 'ppm', 'label': 'Brome'},
    'hardness':   {'min': 200, 'max': 400, 'ideal': 300,   'unit': 'ppm', 'label': 'Dureté (TH)'},
    'alkalinity': {'min': 80,  'max': 120, 'ideal': 100,   'unit': 'ppm', 'label': 'Alcalinité (TAC)'},
}


def get_status(param, value):
    """Retourne 'ok', 'low' ou 'high'."""
    r = IDEAL_RANGES[param]
    if value < r['min']:
        return 'low'
    if value > r['max']:
        return 'high'
    return 'ok'


def _v(pool_volume_liters):
    """Facteur de volume pour les dosages (base 10 000 L)."""
    return pool_volume_liters / 10_000


def _find_product(products, parameter, direction):
    """Trouve le premier produit correspondant au paramètre et à la direction ('up'/'down')."""
    if not products:
        return None
    for p in products:
        if p.get('parameter') == parameter and p.get('direction') == direction:
            return p
    return None


def _calc_dose_str(product, diff, pool_volume_liters):
    """
    Calcule la dose en utilisant la même formule que /api/calculate.
    Retourne une chaîne formatée avec l'unité, ex: '250 g' ou '1.5 kg'.
    """
    ref_vol_l = product['ref_volume'] * 1000 if product['ref_volume_unit'] == 'm3' else product['ref_volume']
    raw_dose  = (abs(diff) / product['ref_change']) * product['ref_dose'] * (pool_volume_liters / ref_vol_l)

    u = product['ref_dose_unit']
    if   u == 'kg' and raw_dose < 0.1:   disp, unit = f"{raw_dose * 1000:.0f}", 'g'
    elif u == 'g'  and raw_dose >= 1000: disp, unit = f"{raw_dose / 1000:.2f}", 'kg'
    elif u == 'L'  and raw_dose < 0.1:   disp, unit = f"{raw_dose * 1000:.0f}", 'ml'
    elif u == 'ml' and raw_dose >= 1000: disp, unit = f"{raw_dose / 1000:.2f}", 'L'
    else:
        disp = f"{raw_dose:.2f}" if raw_dose < 10 else f"{raw_dose:.1f}"
        unit = u

    return f"{disp} {unit}"


def get_recommendations(measurement, pool_volume_liters, products=None):
    """
    Retourne une liste de recommandations avec produit et dosage.
    Si products est fourni (liste de dicts de la table products), utilise les produits
    en base pour les noms et les dosages. Sinon, utilise les valeurs par défaut.
    L'alcalinité doit être corrigée AVANT le pH.
    """
    recs = []
    v = _v(pool_volume_liters)

    ph         = measurement.get('ph')
    bromine    = measurement.get('bromine')
    hardness   = measurement.get('hardness')
    alkalinity = measurement.get('alkalinity')

    # ── Alcalinité (TAC) – à corriger en premier ─────────────────────────────
    if alkalinity is not None:
        if alkalinity < 80:
            diff = 100 - alkalinity
            p = _find_product(products, 'alkalinity', 'up')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = (diff / 10) * 170 * v
                product_name = 'Bicarbonate de sodium (TAC+)'
                dose_str     = f'{dose:.0f} g'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'Alcalinité (TAC)',
                'key':     'alkalinity',
                'status':  'low',
                'value':   alkalinity,
                'ideal':   '80 – 120 ppm',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬆️',
                'detail':  (
                    f"L'alcalinité ({alkalinity} ppm) est trop basse. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name} "
                    f"en le diluant dans un seau d'eau, versez en plusieurs points."
                    f"{wait_note} <em>Corriger l'alcalinité avant d'ajuster le pH.</em>"
                ),
            })
        elif alkalinity > 120:
            diff = alkalinity - 100
            p = _find_product(products, 'alkalinity', 'down')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = (diff / 10) * 50 * v
                product_name = 'Acide muriatique dilué (pH-)'
                dose_str     = f'{dose:.0f} ml'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'Alcalinité (TAC)',
                'key':     'alkalinity',
                'status':  'high',
                'value':   alkalinity,
                'ideal':   '80 – 120 ppm',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬇️',
                'detail':  (
                    f"L'alcalinité ({alkalinity} ppm) est trop élevée. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name}, "
                    f"en plusieurs fois espacées de 4 h. "
                    f"Aérez la piscine entre les ajouts."
                    f"{wait_note} <em>Corriger l'alcalinité avant le pH.</em>"
                ),
            })

    # ── pH ───────────────────────────────────────────────────────────────────
    if ph is not None:
        if ph < 7.2:
            diff = 7.4 - ph
            p = _find_product(products, 'ph', 'up')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = (diff / 0.1) * 15 * v
                product_name = 'Carbonate de sodium (pH+)'
                dose_str     = f'{dose:.0f} g'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'pH',
                'key':     'ph',
                'status':  'low',
                'value':   ph,
                'ideal':   '7,2 – 7,6',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬆️',
                'detail':  (
                    f"Le pH ({ph}) est trop bas – l'eau est corrosive et irritante. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name}. "
                    f"Dissoudre dans un seau avant de verser."
                    f"{wait_note}"
                ),
            })
        elif ph > 7.6:
            diff = ph - 7.4
            p = _find_product(products, 'ph', 'down')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = (diff / 0.1) * 18 * v
                product_name = 'Bisulfate de sodium (pH-)'
                dose_str     = f'{dose:.0f} g'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'pH',
                'key':     'ph',
                'status':  'high',
                'value':   ph,
                'ideal':   '7,2 – 7,6',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬇️',
                'detail':  (
                    f"Le pH ({ph}) est trop élevé – réduit l'efficacité du brome. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name}. "
                    f"Verser en plusieurs fois si l'écart est important."
                    f"{wait_note}"
                ),
            })

    # ── Dureté (TH) ──────────────────────────────────────────────────────────
    if hardness is not None:
        if hardness < 200:
            diff = 300 - hardness
            p = _find_product(products, 'hardness', 'up')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = (diff / 10) * 140 * v
                product_name = 'Chlorure de calcium'
                dose_str     = f'{dose:.0f} g'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'Dureté (TH)',
                'key':     'hardness',
                'status':  'low',
                'value':   hardness,
                'ideal':   '200 – 400 ppm',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬆️',
                'detail':  (
                    f"La dureté ({hardness} ppm) est trop basse – risque de corrosion. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name}. "
                    f"Dissoudre dans un seau, verser avec la pompe en marche."
                    f"{wait_note}"
                ),
            })
        elif hardness > 400:
            pct = min(30, int((hardness - 400) / hardness * 100) + 10)
            vol_renouveler = int(pool_volume_liters * pct / 100)
            recs.append({
                'param':   'Dureté (TH)',
                'key':     'hardness',
                'status':  'high',
                'value':   hardness,
                'ideal':   '200 – 400 ppm',
                'product': 'Renouvellement partiel d\'eau',
                'dose':    f'~{vol_renouveler:,} L ({pct}%)',
                'icon':    '💧',
                'detail':  (
                    f"La dureté ({hardness} ppm) est trop élevée – eau calcaire, risque d'entartrage. "
                    f"Il n'existe pas de produit chimique pour baisser la dureté. "
                    f"Vider environ <strong>{vol_renouveler:,} L</strong> ({pct}% du volume) "
                    f"et remplacer par de l'eau fraîche."
                ),
            })

    # ── Brome ────────────────────────────────────────────────────────────────
    if bromine is not None:
        if bromine < 3:
            diff = 4.0 - bromine
            p = _find_product(products, 'bromine', 'up')
            if p:
                product_name = p['name']
                dose_str     = _calc_dose_str(p, diff, pool_volume_liters)
                wait_note    = f" Attendre {p['wait_hours']} h avant de retester." if p.get('wait_hours') else " Retester après 4 h."
            else:
                dose = diff * 13 * v
                product_name = 'Brome (granulés ou pastilles)'
                dose_str     = f'{dose:.0f} g'
                wait_note    = ' Retester après 4 h.'
            recs.append({
                'param':   'Brome',
                'key':     'bromine',
                'status':  'low',
                'value':   bromine,
                'ideal':   '3 – 5 ppm',
                'product': product_name,
                'dose':    dose_str,
                'icon':    '⬆️',
                'detail':  (
                    f"Le brome ({bromine} ppm) est insuffisant – risque bactérien. "
                    f"Ajouter environ <strong>{dose_str}</strong> de {product_name} "
                    f"(ou ajuster le débit du diffuseur). "
                    f"<strong>Ne pas se baigner</strong> tant que le taux n'est pas entre 3 et 5 ppm."
                    f"{wait_note}"
                ),
            })
        elif bromine > 5:
            if bromine > 8:
                vol_dilution = int(pool_volume_liters * 0.10)
                recs.append({
                    'param':   'Brome',
                    'key':     'bromine',
                    'status':  'high',
                    'value':   bromine,
                    'ideal':   '3 – 5 ppm',
                    'product': 'Arrêt traitement + dilution',
                    'dose':    f'Vider ~{vol_dilution:,} L (10%)',
                    'icon':    '🚫',
                    'detail':  (
                        f"Le brome ({bromine} ppm) est très élevé – irritant, dangereux. "
                        f"<strong>Arrêter immédiatement tout apport de brome.</strong> "
                        f"Ne pas se baigner. Vider ~{vol_dilution:,} L et remplir avec de l'eau fraîche. "
                        f"Laisser le soleil décomposer le brome résiduel (sans couvercle). Retester dans 24 h."
                    ),
                })
            else:
                recs.append({
                    'param':   'Brome',
                    'key':     'bromine',
                    'status':  'high',
                    'value':   bromine,
                    'ideal':   '3 – 5 ppm',
                    'product': 'Arrêt du traitement',
                    'dose':    'Attendre la décomposition naturelle',
                    'icon':    '⏳',
                    'detail':  (
                        f"Le brome ({bromine} ppm) est légèrement élevé. "
                        f"Arrêter l'apport de brome. "
                        f"La lumière UV du soleil décompose naturellement le brome. "
                        f"Retester dans 24 h. La baignade est possible mais déconseillée."
                    ),
                })

    return recs
